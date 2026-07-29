#!/usr/bin/env python3

import argparse
import hashlib
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from lightspeed_variant_sync import (
    DEFAULT_API_VERSION,
    get_client_credentials_token,
    load_dotenv,
    normalize_shop,
    shopify_graphql,
)


DEFAULT_SPEC_FILE = (
    Path("tools") / "data" / "shopify-brand-content.json"
)
DEFAULT_PLAN_FILE = (
    Path(".tmp") / "phase3-brand-content-plan-20260725.json"
)
DEFAULT_MARKDOWN_FILE = (
    Path(".tmp") / "phase3-brand-content-plan-20260725.md"
)
DEFAULT_RESULTS_FILE = (
    Path(".tmp") / "phase3-brand-content-apply-20260725.json"
)
SPECIAL_COLLECTION_HANDLES = {"in-store-exclusive"}
ALLOWED_DESCRIPTION_ACTIONS = {
    "preserve",
    "remove_trailing_quote",
    "replace",
}
ALLOWED_HTML_TAGS = {"p", "br", "em", "a"}
DUTCH_METADATA_WORDS = re.compile(
    r"\b(?:collectie|ontdek|producten|winkel)\b",
    re.IGNORECASE,
)
DISALLOWED_PUNCTUATION = ("\u2013", "\u2014")


class TagCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: List[str] = []
        self.stack: List[str] = []
        self.errors: List[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: List[Tuple[str, Optional[str]]],
    ) -> None:
        normalized = tag.lower()
        self.tags.append(normalized)
        if normalized != "br":
            self.stack.append(normalized)

    def handle_startendtag(
        self,
        tag: str,
        attrs: List[Tuple[str, Optional[str]]],
    ) -> None:
        self.tags.append(tag.lower())

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if not self.stack or self.stack[-1] != normalized:
            self.errors.append(f"mismatched closing tag: {normalized}")
            return
        self.stack.pop()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_value(value: Any) -> str:
    return hashlib.sha256(
        canonical_json(value).encode("utf-8")
    ).hexdigest()


def meaningful_text(value: str) -> str:
    return re.sub(
        r"\s+",
        " ",
        html.unescape(re.sub(r"<[^>]+>", " ", value or "")),
    ).strip()


def word_count(value: str) -> int:
    return len(re.findall(r"[A-Za-z0-9]+(?:['+&][A-Za-z0-9]+)*", value))


def request_json(
    url: str,
    token: str,
    method: str = "GET",
    payload: Optional[Dict[str, Any]] = None,
    timeout: int = 60,
) -> Tuple[Dict[str, Any], Dict[str, str]]:
    body = (
        json.dumps(payload).encode("utf-8")
        if payload is not None
        else None
    )
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": token,
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return (
                json.loads(response.read().decode("utf-8") or "{}"),
                dict(response.headers.items()),
            )
    except urllib.error.HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Shopify request failed ({exc.code}) for {url}: "
            f"{response_body}"
        ) from exc


def next_link(link_header: str) -> Optional[str]:
    for part in link_header.split(","):
        if 'rel="next"' in part:
            return part.split(";")[0].strip()[1:-1]
    return None


def fetch_smart_collections(
    shop: str,
    token: str,
    api_version: str,
) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    url: Optional[str] = (
        f"https://{shop}/admin/api/{api_version}/smart_collections.json"
        "?limit=250"
    )
    while url:
        payload, headers = request_json(url, token)
        items.extend(payload.get("smart_collections") or [])
        url = next_link(headers.get("Link", ""))
    return items


def fetch_graphql_collections(
    endpoint: str,
    token: str,
) -> Dict[str, Dict[str, Any]]:
    query = """
      query BrandContentCollections($first: Int!, $after: String) {
        collections(first: $first, after: $after, sortKey: TITLE) {
          nodes {
            id
            legacyResourceId
            handle
            title
            descriptionHtml
            updatedAt
            seo {
              title
              description
            }
          }
          pageInfo {
            hasNextPage
            endCursor
          }
        }
      }
    """
    by_handle: Dict[str, Dict[str, Any]] = {}
    after: Optional[str] = None
    while True:
        data = shopify_graphql(
            endpoint,
            token,
            query,
            {"first": 100, "after": after},
        )
        connection = data["collections"]
        for node in connection.get("nodes") or []:
            by_handle[node["handle"]] = node
        page_info = connection["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        after = page_info["endCursor"]
    return by_handle


def fetch_product_context(
    endpoint: str,
    token: str,
) -> Dict[str, Dict[str, Any]]:
    query = """
      query BrandContentProducts($first: Int!, $after: String) {
        products(first: $first, after: $after, sortKey: ID) {
          nodes {
            vendor
            status
            title
            productType
          }
          pageInfo {
            hasNextPage
            endCursor
          }
        }
      }
    """
    by_vendor: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {
            "statuses": Counter(),
            "activeTitles": [],
            "activeProductTypes": set(),
        }
    )
    after: Optional[str] = None
    while True:
        data = shopify_graphql(
            endpoint,
            token,
            query,
            {"first": 250, "after": after},
        )
        connection = data["products"]
        for node in connection.get("nodes") or []:
            vendor = (node.get("vendor") or "").strip()
            if not vendor:
                continue
            status = (node.get("status") or "").upper()
            item = by_vendor[vendor]
            item["statuses"][status] += 1
            if status == "ACTIVE":
                item["activeTitles"].append(node.get("title") or "")
                product_type = (node.get("productType") or "").strip()
                if product_type:
                    item["activeProductTypes"].add(product_type)
        page_info = connection["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        after = page_info["endCursor"]
    return {
        vendor: {
            "total": sum(item["statuses"].values()),
            "active": item["statuses"]["ACTIVE"],
            "draft": item["statuses"]["DRAFT"],
            "archived": item["statuses"]["ARCHIVED"],
            "activeProductTypes": sorted(item["activeProductTypes"]),
            "activeTitleSamples": sorted(
                item["activeTitles"],
                key=str.casefold,
            )[:12],
        }
        for vendor, item in by_vendor.items()
    }


def vendor_rule(collection: Dict[str, Any]) -> Optional[str]:
    rules = collection.get("rules") or []
    if len(rules) != 1:
        return None
    rule = rules[0]
    if (
        (rule.get("column") or "").lower() != "vendor"
        or (rule.get("relation") or "").lower() != "equals"
    ):
        return None
    return (rule.get("condition") or "").strip() or None


def load_spec(path: Path) -> Dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schemaVersion") != 1:
        raise ValueError("Unsupported content spec schemaVersion.")
    if data.get("language") != "en":
        raise ValueError("Customer-facing content spec must use English.")
    if not isinstance(data.get("collections"), dict):
        raise ValueError("Content spec collections must be an object.")
    return data


def validate_description_html(
    handle: str,
    action: str,
    body_html: str,
) -> List[str]:
    errors: List[str] = []
    if not meaningful_text(body_html):
        errors.append(f"{handle}: replacement description is blank")
        return errors
    parser = TagCollector()
    parser.feed(body_html)
    disallowed_tags = sorted(set(parser.tags) - ALLOWED_HTML_TAGS)
    if disallowed_tags:
        errors.append(
            f"{handle}: disallowed HTML tags: {', '.join(disallowed_tags)}"
        )
    if parser.errors or parser.stack:
        errors.append(f"{handle}: malformed HTML")
    if "<strong" in body_html.lower() or "<b" in body_html.lower():
        errors.append(f"{handle}: bold markup is not allowed")
    if action == "replace" and body_html.count("<p>-//-</p>") != 1:
        errors.append(
            f"{handle}: replacement must have one top/bottom delimiter"
        )
    if any(mark in body_html for mark in DISALLOWED_PUNCTUATION):
        errors.append(f"{handle}: en or em dash is not allowed")
    return errors


def validate_spec(spec: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    seen_titles: Dict[str, str] = {}
    seen_descriptions: Dict[str, str] = {}
    for handle, item in sorted(spec["collections"].items()):
        action = item.get("descriptionAction")
        if action not in ALLOWED_DESCRIPTION_ACTIONS:
            errors.append(f"{handle}: invalid descriptionAction {action!r}")
        if action == "replace":
            body_html = item.get("descriptionHtml") or ""
            errors.extend(
                validate_description_html(handle, action, body_html)
            )
        seo_title = (item.get("seoTitle") or "").strip()
        meta_description = (item.get("metaDescription") or "").strip()
        if not seo_title:
            errors.append(f"{handle}: SEO title is blank")
        if not meta_description:
            errors.append(f"{handle}: meta description is blank")
        if DUTCH_METADATA_WORDS.search(seo_title):
            errors.append(f"{handle}: SEO title contains Dutch wording")
        if DUTCH_METADATA_WORDS.search(meta_description):
            errors.append(f"{handle}: meta description contains Dutch wording")
        if any(mark in seo_title for mark in DISALLOWED_PUNCTUATION):
            errors.append(f"{handle}: SEO title contains an en or em dash")
        if any(mark in meta_description for mark in DISALLOWED_PUNCTUATION):
            errors.append(
                f"{handle}: meta description contains an en or em dash"
            )
        if len(seo_title) >= 60:
            errors.append(
                f"{handle}: SEO title length is {len(seo_title)}, expected <60"
            )
        if len(meta_description) >= 120:
            errors.append(
                f"{handle}: meta description length is "
                f"{len(meta_description)}, expected <120"
            )
        normalized_title = seo_title.casefold()
        normalized_description = meta_description.casefold()
        if normalized_title in seen_titles:
            errors.append(
                f"{handle}: duplicate SEO title with "
                f"{seen_titles[normalized_title]}"
            )
        if normalized_description in seen_descriptions:
            errors.append(
                f"{handle}: duplicate meta description with "
                f"{seen_descriptions[normalized_description]}"
            )
        seen_titles[normalized_title] = handle
        seen_descriptions[normalized_description] = handle
        if "published" in item and not isinstance(item["published"], bool):
            errors.append(f"{handle}: published must be true or false")
    return errors


def remove_trailing_quote(body_html: str) -> str:
    repaired, count = re.subn(
        r'"\s*(</p>\s*)$',
        r"\1",
        body_html,
        count=1,
        flags=re.IGNORECASE,
    )
    if count == 0:
        return body_html
    return repaired


def collection_snapshot(
    graphql_item: Dict[str, Any],
    rest_item: Dict[str, Any],
) -> Dict[str, Any]:
    seo = graphql_item.get("seo") or {}
    return {
        "id": graphql_item["id"],
        "legacyResourceId": str(graphql_item["legacyResourceId"]),
        "handle": graphql_item["handle"],
        "title": graphql_item["title"],
        "descriptionHtml": graphql_item.get("descriptionHtml") or "",
        "seo": {
            "title": seo.get("title") or "",
            "description": seo.get("description") or "",
        },
        "published": bool(rest_item.get("published_at")),
        "updatedAt": graphql_item.get("updatedAt"),
    }


def desired_snapshot(
    current: Dict[str, Any],
    spec_item: Dict[str, Any],
) -> Dict[str, Any]:
    action = spec_item["descriptionAction"]
    if action == "replace":
        description_html = spec_item["descriptionHtml"]
    elif action == "remove_trailing_quote":
        description_html = remove_trailing_quote(
            current["descriptionHtml"]
        )
    else:
        description_html = current["descriptionHtml"]
    return {
        "descriptionHtml": description_html,
        "seo": {
            "title": spec_item["seoTitle"].strip(),
            "description": spec_item["metaDescription"].strip(),
        },
        "published": spec_item.get("published", current["published"]),
    }


def changed_fields(
    current: Dict[str, Any],
    desired: Dict[str, Any],
) -> List[str]:
    changes: List[str] = []
    if current["descriptionHtml"] != desired["descriptionHtml"]:
        changes.append("descriptionHtml")
    if current["seo"]["title"] != desired["seo"]["title"]:
        changes.append("seo.title")
    if current["seo"]["description"] != desired["seo"]["description"]:
        changes.append("seo.description")
    if current["published"] != desired["published"]:
        changes.append("published")
    return changes


def build_plan(
    shop: str,
    api_version: str,
    spec: Dict[str, Any],
    spec_digest: str,
    smart_collections: List[Dict[str, Any]],
    graphql_collections: Dict[str, Dict[str, Any]],
    product_context: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    rest_by_handle = {
        item["handle"]: item for item in smart_collections
    }
    canonical = {
        item["handle"]: vendor_rule(item)
        for item in smart_collections
        if item.get("handle") not in SPECIAL_COLLECTION_HANDLES
        and vendor_rule(item)
    }
    spec_handles = set(spec["collections"])
    canonical_handles = set(canonical)
    if spec_handles != canonical_handles:
        missing = sorted(canonical_handles - spec_handles)
        extra = sorted(spec_handles - canonical_handles)
        raise ValueError(
            "Content spec does not exactly match canonical brand collections. "
            f"Missing: {missing}; extra: {extra}"
        )

    actions: List[Dict[str, Any]] = []
    for handle in sorted(spec_handles):
        if handle not in graphql_collections:
            raise ValueError(f"GraphQL collection missing for {handle}.")
        current = collection_snapshot(
            graphql_collections[handle],
            rest_by_handle[handle],
        )
        desired = desired_snapshot(
            current,
            spec["collections"][handle],
        )
        vendor = canonical[handle]
        context = product_context.get(
            vendor,
            {
                "total": 0,
                "active": 0,
                "draft": 0,
                "archived": 0,
                "activeProductTypes": [],
                "activeTitleSamples": [],
            },
        )
        actions.append(
            {
                "handle": handle,
                "vendor": vendor,
                "descriptionAction": (
                    spec["collections"][handle]["descriptionAction"]
                ),
                "changes": changed_fields(current, desired),
                "expected": current,
                "desired": desired,
                "productContext": context,
                "desiredMetrics": {
                    "descriptionWords": word_count(
                        meaningful_text(desired["descriptionHtml"])
                    ),
                    "seoTitleCharacters": len(desired["seo"]["title"]),
                    "metaDescriptionCharacters": len(
                        desired["seo"]["description"]
                    ),
                },
            }
        )

    field_counts = Counter(
        field_name
        for action in actions
        for field_name in action["changes"]
    )
    plan_core = {
        "schemaVersion": 1,
        "mode": "dry_run",
        "generatedAt": utc_now(),
        "shop": shop,
        "apiVersion": api_version,
        "specDigest": spec_digest,
        "summary": {
            "canonicalCollections": len(actions),
            "collectionsWithChanges": sum(
                bool(action["changes"]) for action in actions
            ),
            "descriptionReplacements": sum(
                action["descriptionAction"] == "replace"
                for action in actions
            ),
            "descriptionRepairs": sum(
                action["descriptionAction"] == "remove_trailing_quote"
                for action in actions
            ),
            "descriptionsPreserved": sum(
                action["descriptionAction"] == "preserve"
                for action in actions
            ),
            "visibilityChanges": field_counts["published"],
            "fieldChanges": dict(sorted(field_counts.items())),
        },
        "actions": actions,
    }
    plan_core["planDigest"] = sha256_value(plan_core)
    return plan_core


def update_collection(
    endpoint: str,
    token: str,
    action: Dict[str, Any],
) -> Dict[str, Any]:
    changed = set(action["changes"])
    update: Dict[str, Any] = {"id": action["expected"]["id"]}
    if "descriptionHtml" in changed:
        update["descriptionHtml"] = action["desired"]["descriptionHtml"]
    if {"seo.title", "seo.description"} & changed:
        update["seo"] = action["desired"]["seo"]
    if len(update) == 1:
        return {"skipped": True}
    data = shopify_graphql(
        endpoint,
        token,
        """
        mutation UpdateBrandCollection(
          $input: CollectionInput!
        ) {
          collectionUpdate(input: $input) {
            collection {
              id
              handle
              descriptionHtml
              seo {
                title
                description
              }
              updatedAt
            }
            userErrors {
              field
              message
            }
          }
        }
        """,
        {"input": update},
    )
    payload = data.get("collectionUpdate") or {}
    errors = payload.get("userErrors") or []
    if errors:
        detail = "; ".join(
            f"{'.'.join(str(part) for part in error.get('field') or [])}: "
            f"{error.get('message')}"
            for error in errors
        )
        raise RuntimeError(
            f"Shopify rejected collectionUpdate for {action['handle']}: "
            f"{detail}"
        )
    return payload.get("collection") or {}


def hide_smart_collection(
    shop: str,
    token: str,
    api_version: str,
    action: Dict[str, Any],
) -> Dict[str, Any]:
    legacy_id = action["expected"]["legacyResourceId"]
    url = (
        f"https://{shop}/admin/api/{api_version}/smart_collections/"
        f"{legacy_id}.json"
    )
    payload, _ = request_json(
        url,
        token,
        method="PUT",
        payload={
            "smart_collection": {
                "id": int(legacy_id),
                "published": False,
            }
        },
    )
    updated = payload.get("smart_collection") or {}
    if updated.get("published_at") is not None:
        raise RuntimeError(
            f"Collection {action['handle']} remained published."
        )
    return {
        "id": updated.get("id"),
        "handle": updated.get("handle"),
        "publishedAt": updated.get("published_at"),
    }


def compare_expected(
    actions: List[Dict[str, Any]],
    smart_collections: List[Dict[str, Any]],
    graphql_collections: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    rest_by_handle = {
        item["handle"]: item for item in smart_collections
    }
    conflicts: List[Dict[str, Any]] = []
    for action in actions:
        handle = action["handle"]
        if handle not in rest_by_handle or handle not in graphql_collections:
            conflicts.append(
                {"handle": handle, "reason": "collection_missing"}
            )
            continue
        actual = collection_snapshot(
            graphql_collections[handle],
            rest_by_handle[handle],
        )
        if actual != action["expected"]:
            conflicts.append(
                {
                    "handle": handle,
                    "reason": "current_state_changed_since_plan",
                    "expected": action["expected"],
                    "actual": actual,
                }
            )
    return conflicts


def verify_desired(
    actions: List[Dict[str, Any]],
    smart_collections: List[Dict[str, Any]],
    graphql_collections: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    rest_by_handle = {
        item["handle"]: item for item in smart_collections
    }
    failures: List[Dict[str, Any]] = []
    for action in actions:
        handle = action["handle"]
        current = collection_snapshot(
            graphql_collections[handle],
            rest_by_handle[handle],
        )
        actual = {
            "descriptionHtml": current["descriptionHtml"],
            "seo": current["seo"],
            "published": current["published"],
        }
        if actual != action["desired"]:
            failures.append(
                {
                    "handle": handle,
                    "expected": action["desired"],
                    "actual": actual,
                }
            )
    return failures


def build_markdown(plan: Dict[str, Any]) -> str:
    summary = plan["summary"]
    lines = [
        "# Phase 3 brand content plan",
        "",
        "Status: dry run. No Shopify content was changed by this report.",
        "",
        "## Summary",
        "",
        f"- Canonical brand collections: {summary['canonicalCollections']}",
        f"- Collections with changes: {summary['collectionsWithChanges']}",
        f"- Description replacements: {summary['descriptionReplacements']}",
        f"- Technical description repairs: {summary['descriptionRepairs']}",
        f"- Existing descriptions preserved: {summary['descriptionsPreserved']}",
        f"- Visibility changes: {summary['visibilityChanges']}",
        "",
        "## Collections",
        "",
    ]
    for action in plan["actions"]:
        desired = action["desired"]
        metrics = action["desiredMetrics"]
        changes = ", ".join(action["changes"]) or "none"
        visibility = "published" if desired["published"] else "hidden"
        lines.extend(
            [
                f"### {action['expected']['title']}",
                "",
                f"- Handle: `{action['handle']}`",
                f"- Description action: `{action['descriptionAction']}`",
                f"- Changes: {changes}",
                f"- Desired visibility: {visibility}",
                f"- Active products: {action['productContext']['active']}",
                (
                    "- Lengths: "
                    f"{metrics['descriptionWords']} description words, "
                    f"{metrics['seoTitleCharacters']} title characters, "
                    f"{metrics['metaDescriptionCharacters']} meta characters"
                ),
                f"- SEO title: {desired['seo']['title']}",
                f"- Meta description: {desired['seo']['description']}",
                "",
                meaningful_text(desired["descriptionHtml"]),
                "",
            ]
        )
    return "\n".join(lines)


def acquire_context(
    shop: str,
    token: str,
    api_version: str,
) -> Tuple[
    List[Dict[str, Any]],
    Dict[str, Dict[str, Any]],
    Dict[str, Dict[str, Any]],
]:
    endpoint = (
        f"https://{shop}/admin/api/{api_version}/graphql.json"
    )
    smart_collections = fetch_smart_collections(
        shop,
        token,
        api_version,
    )
    graphql_collections = fetch_graphql_collections(endpoint, token)
    product_context = fetch_product_context(endpoint, token)
    return smart_collections, graphql_collections, product_context


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Plan and apply conflict-checked Shopify brand collection "
            "description, SEO and visibility changes."
        )
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--shop", default="")
    parser.add_argument("--api-version", default=DEFAULT_API_VERSION)
    parser.add_argument("--spec-file", type=Path, default=DEFAULT_SPEC_FILE)
    parser.add_argument("--plan-file", type=Path, default=DEFAULT_PLAN_FILE)
    parser.add_argument(
        "--markdown-file",
        type=Path,
        default=DEFAULT_MARKDOWN_FILE,
    )
    parser.add_argument(
        "--results-file",
        type=Path,
        default=DEFAULT_RESULTS_FILE,
    )
    args = parser.parse_args()

    load_dotenv(Path(".env"))
    shop = normalize_shop(args.shop or os.environ.get("SHOPIFY_SHOP", ""))
    client_id = os.environ.get("SHOPIFY_CLIENT_ID") or os.environ.get(
        "SHOPIFY_API_KEY",
        "",
    )
    client_secret = os.environ.get("SHOPIFY_CLIENT_SECRET") or os.environ.get(
        "SHOPIFY_API_SECRET",
        "",
    )
    if not shop or not client_id or not client_secret:
        raise SystemExit(
            "SHOPIFY_SHOP and Shopify client credentials are required."
        )

    spec = load_spec(args.spec_file)
    spec_errors = validate_spec(spec)
    if spec_errors:
        raise SystemExit(
            "Content spec validation failed:\n- "
            + "\n- ".join(spec_errors)
        )
    spec_digest = sha256_value(spec)
    token = get_client_credentials_token(
        shop,
        client_id,
        client_secret,
    )

    if not args.apply:
        smart, graphql, product_context = acquire_context(
            shop,
            token,
            args.api_version,
        )
        plan = build_plan(
            shop,
            args.api_version,
            spec,
            spec_digest,
            smart,
            graphql,
            product_context,
        )
        args.plan_file.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_file.parent.mkdir(parents=True, exist_ok=True)
        args.plan_file.write_text(
            json.dumps(plan, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        args.markdown_file.write_text(
            build_markdown(plan),
            encoding="utf-8",
        )
        print(json.dumps(plan["summary"], indent=2))
        print(f"Plan digest: {plan['planDigest']}")
        print(f"Plan: {args.plan_file}")
        print(f"Review: {args.markdown_file}")
        return 0

    if not args.plan_file.exists():
        raise SystemExit(
            "Apply requires an existing reviewed dry-run plan file."
        )
    plan = json.loads(args.plan_file.read_text(encoding="utf-8"))
    supplied_digest = plan.pop("planDigest", "")
    actual_digest = sha256_value(plan)
    plan["planDigest"] = supplied_digest
    if supplied_digest != actual_digest:
        raise SystemExit("Plan digest is invalid; refusing to apply.")
    if plan.get("mode") != "dry_run":
        raise SystemExit("Apply requires a dry-run plan.")
    if plan.get("shop") != shop:
        raise SystemExit("Plan shop does not match current Shopify shop.")
    if plan.get("apiVersion") != args.api_version:
        raise SystemExit("Plan API version does not match this run.")
    if plan.get("specDigest") != spec_digest:
        raise SystemExit("Content spec changed after the plan was created.")

    smart, graphql, _ = acquire_context(
        shop,
        token,
        args.api_version,
    )
    conflicts = compare_expected(plan["actions"], smart, graphql)
    if conflicts:
        args.results_file.parent.mkdir(parents=True, exist_ok=True)
        args.results_file.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "mode": "apply_blocked",
                    "generatedAt": utc_now(),
                    "shop": shop,
                    "planDigest": supplied_digest,
                    "conflicts": conflicts,
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        raise SystemExit(
            f"Current Shopify state has {len(conflicts)} conflict(s); "
            "nothing was written."
        )

    endpoint = (
        f"https://{shop}/admin/api/{args.api_version}/graphql.json"
    )
    results: Dict[str, Any] = {
        "schemaVersion": 1,
        "mode": "apply",
        "startedAt": utc_now(),
        "shop": shop,
        "planDigest": supplied_digest,
        "collectionUpdates": [],
        "visibilityUpdates": [],
        "errors": [],
        "verificationFailures": [],
    }
    try:
        for action in plan["actions"]:
            if set(action["changes"]) & {
                "descriptionHtml",
                "seo.title",
                "seo.description",
            }:
                updated = update_collection(endpoint, token, action)
                results["collectionUpdates"].append(
                    {
                        "handle": action["handle"],
                        "updatedAt": updated.get("updatedAt"),
                    }
                )
        for action in plan["actions"]:
            if (
                "published" in action["changes"]
                and action["desired"]["published"] is False
            ):
                updated = hide_smart_collection(
                    shop,
                    token,
                    args.api_version,
                    action,
                )
                results["visibilityUpdates"].append(updated)

        smart, graphql, _ = acquire_context(
            shop,
            token,
            args.api_version,
        )
        results["verificationFailures"] = verify_desired(
            plan["actions"],
            smart,
            graphql,
        )
        if results["verificationFailures"]:
            raise RuntimeError(
                f"{len(results['verificationFailures'])} post-apply "
                "verification failure(s)."
            )
        results["completedAt"] = utc_now()
        results["verified"] = True
    except Exception as exc:
        results["completedAt"] = utc_now()
        results["verified"] = False
        results["errors"].append(str(exc))
        raise
    finally:
        args.results_file.parent.mkdir(parents=True, exist_ok=True)
        args.results_file.write_text(
            json.dumps(results, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    print(
        json.dumps(
            {
                "collectionUpdates": len(results["collectionUpdates"]),
                "visibilityUpdates": len(results["visibilityUpdates"]),
                "verificationFailures": len(
                    results["verificationFailures"]
                ),
                "verified": results["verified"],
            },
            indent=2,
        )
    )
    print(f"Results: {args.results_file}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        raise SystemExit(130)
