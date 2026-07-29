#!/usr/bin/env python3

import argparse
import html
import json
import os
import re
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from lightspeed_variant_sync import (
    get_client_credentials_token,
    load_dotenv,
    normalize_shop,
    shopify_graphql,
)


DEFAULT_API_VERSION = "2026-04"
DEFAULT_JSON_REPORT = Path(".tmp") / "shopify-brand-seo-audit.json"
DEFAULT_MARKDOWN_REPORT = Path(".tmp") / "shopify-brand-seo-audit.md"
SPECIAL_COLLECTION_HANDLES = {"in-store-exclusive"}
PLACEHOLDER_TEXTS = {
    "coming soon",
    "lorem ipsum",
    "onze eigen producten",
}
GENERIC_PHRASES = (
    "welcome to neighbourhood",
    "your destination",
    "premier destination",
    "we proudly",
    "we are delighted",
    "we are thrilled",
    "we are excited",
    "high-end",
    "shop now",
    "ultimate destination",
    "the epitome of",
    "discerning gentleman",
    "discerning fashion",
    "discerning customers",
    "elevate your wardrobe",
    "elevate your lifestyle",
    "immerse yourself",
    "indulge in",
    "embrace the spirit",
    "unparalleled",
    "unwavering",
    "uncompromising",
    "highest standards",
    "meticulously crafted",
    "perfect fusion",
    "pure luxury",
    "timeless creations",
)
MOJIBAKE_MARKERS = ("\ufffd", "\u00c3", "\u00c2", "\u00e2\u20ac")


class DescriptionParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: List[str] = []
        self.bold_text: List[str] = []
        self.tags: Counter[str] = Counter()
        self.stack: List[str] = []
        self.bold_depth = 0
        self.mismatched_tags: List[str] = []

    def handle_starttag(
        self, tag: str, attrs: List[Tuple[str, Optional[str]]]
    ) -> None:
        normalized = tag.lower()
        self.tags[normalized] += 1
        if normalized not in {"br", "img", "hr", "meta", "link", "input"}:
            self.stack.append(normalized)
        if normalized in {"strong", "b"}:
            self.bold_depth += 1

    def handle_startendtag(
        self, tag: str, attrs: List[Tuple[str, Optional[str]]]
    ) -> None:
        self.tags[tag.lower()] += 1

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if normalized in {"strong", "b"}:
            self.bold_depth = max(0, self.bold_depth - 1)
        if normalized in {"br", "img", "hr", "meta", "link", "input"}:
            return
        if not self.stack:
            self.mismatched_tags.append(normalized)
            return
        if self.stack[-1] == normalized:
            self.stack.pop()
            return
        self.mismatched_tags.append(normalized)
        if normalized in self.stack:
            while self.stack and self.stack[-1] != normalized:
                self.stack.pop()
            if self.stack:
                self.stack.pop()

    def handle_data(self, data: str) -> None:
        if not data.strip():
            return
        self.text.append(data)
        if self.bold_depth:
            self.bold_text.append(data)


def request_json(url: str, token: str) -> Tuple[Dict[str, Any], Dict[str, str]]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "X-Shopify-Access-Token": token,
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return (
                json.loads(response.read().decode("utf-8") or "{}"),
                dict(response.headers.items()),
            )
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Shopify request failed ({exc.code}) for {url}: {body}"
        ) from exc


def next_link(link_header: str) -> Optional[str]:
    for part in link_header.split(","):
        if 'rel="next"' in part:
            return part.split(";")[0].strip()[1:-1]
    return None


def fetch_smart_collections(
    shop: str, token: str, api_version: str
) -> List[Dict[str, Any]]:
    collections: List[Dict[str, Any]] = []
    url: Optional[str] = (
        f"https://{shop}/admin/api/{api_version}/smart_collections.json"
        "?limit=250"
    )
    while url:
        payload, headers = request_json(url, token)
        collections.extend(payload.get("smart_collections") or [])
        url = next_link(headers.get("Link", ""))
    return collections


def fetch_collection_seo(
    shop: str, token: str, api_version: str
) -> Dict[str, Dict[str, Any]]:
    endpoint = f"https://{shop}/admin/api/{api_version}/graphql.json"
    query = """
      query CollectionSeo($first: Int!, $after: String) {
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
            image {
              url
              altText
              width
              height
            }
          }
          pageInfo {
            hasNextPage
            endCursor
          }
        }
      }
    """
    by_id: Dict[str, Dict[str, Any]] = {}
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
            by_id[str(node["legacyResourceId"])] = node
        page_info = connection["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        after = page_info["endCursor"]
    return by_id


def fetch_vendor_product_counts(
    shop: str, token: str, api_version: str
) -> Dict[str, Dict[str, int]]:
    endpoint = f"https://{shop}/admin/api/{api_version}/graphql.json"
    query = """
      query ProductVendors($first: Int!, $after: String) {
        products(first: $first, after: $after, sortKey: ID) {
          nodes {
            vendor
            status
          }
          pageInfo {
            hasNextPage
            endCursor
          }
        }
      }
    """
    counts: Dict[str, Counter[str]] = defaultdict(Counter)
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
            if vendor:
                counts[vendor][(node.get("status") or "").upper()] += 1
        page_info = connection["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        after = page_info["endCursor"]
    return {
        vendor: {
            "total": sum(statuses.values()),
            "active": statuses["ACTIVE"],
            "draft": statuses["DRAFT"],
            "archived": statuses["ARCHIVED"],
        }
        for vendor, statuses in counts.items()
    }


def vendor_rule(collection: Dict[str, Any]) -> Optional[str]:
    rules = collection.get("rules") or []
    if len(rules) != 1:
        return None
    rule = rules[0]
    if (
        (rule.get("column") or "").lower() == "vendor"
        and (rule.get("relation") or "").lower() == "equals"
    ):
        return (rule.get("condition") or "").strip() or None
    return None


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def normalize_for_match(value: str) -> str:
    normalized = (
        html.unescape(value or "")
        .lower()
        .replace("&", " and ")
        .replace("+", " and ")
    )
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def word_count(value: str) -> int:
    return len(re.findall(r"\b[a-zA-Z0-9][a-zA-Z0-9'-]*\b", value))


def sentence_keys(value: str) -> Iterable[str]:
    for sentence in re.split(r"(?<=[.!?])\s+", value):
        key = normalize_for_match(sentence)
        if word_count(key) >= 9:
            yield key


def add_issue(
    issues: List[Dict[str, str]], code: str, severity: str, detail: str
) -> None:
    issues.append({"code": code, "severity": severity, "detail": detail})


def description_metrics(body_html: str) -> Dict[str, Any]:
    parser = DescriptionParser()
    parser.feed(body_html or "")
    parser.close()
    text = normalize_text(" ".join(parser.text))
    bold_text = normalize_text(" ".join(parser.bold_text))
    paragraphs = parser.tags["p"]
    delimiter_present = bool(
        re.search(r"(?:<p[^>]*>\s*)?-\s*//\s*-(?:\s*</p>)?", body_html or "")
    )
    return {
        "text": text,
        "wordCount": word_count(text),
        "paragraphCount": paragraphs,
        "boldWordCount": word_count(bold_text),
        "delimiterPresent": delimiter_present,
        "unclosedTags": list(parser.stack),
        "mismatchedTags": parser.mismatched_tags,
        "tags": dict(parser.tags),
    }


def analyze_collection(
    collection: Dict[str, Any],
    graphql_collection: Dict[str, Any],
    product_counts: Dict[str, int],
    duplicate_sentences: Dict[str, int],
) -> Dict[str, Any]:
    title = collection.get("title") or ""
    vendor = vendor_rule(collection) or title
    body_html = graphql_collection.get("descriptionHtml") or ""
    metrics = description_metrics(body_html)
    text = metrics["text"]
    text_match = normalize_for_match(text)
    vendor_match = normalize_for_match(vendor)
    issues: List[Dict[str, str]] = []

    if not text:
        add_issue(issues, "blank_description", "error", "No collection copy.")
    elif text_match in PLACEHOLDER_TEXTS:
        add_issue(
            issues,
            "placeholder_description",
            "error",
            "Placeholder copy must be replaced.",
        )

    if text and vendor_match not in text_match:
        add_issue(
            issues,
            "brand_not_named",
            "warning",
            "The collection copy does not name its own brand.",
        )

    generic_matches = [
        phrase for phrase in GENERIC_PHRASES if phrase in text.lower()
    ]
    if len(generic_matches) >= 4 or (
        metrics["wordCount"] >= 220 and len(generic_matches) >= 2
    ):
        add_issue(
            issues,
            "generic_boilerplate",
            "warning",
            "Repeated generic marketing language: "
            + ", ".join(generic_matches[:5]),
        )

    repeated = sorted(
        {
            sentence
            for sentence in sentence_keys(text)
            if duplicate_sentences.get(sentence, 0) > 1
        }
    )
    if repeated:
        add_issue(
            issues,
            "duplicate_copy",
            "warning",
            f"{len(repeated)} sentence(s) also occur in another brand collection.",
        )

    if any(marker in (body_html or "") for marker in MOJIBAKE_MARKERS):
        add_issue(
            issues,
            "encoding_damage",
            "error",
            "The HTML contains likely mojibake or replacement characters.",
        )

    if re.search(r"<(?:script|style|iframe)\b", body_html, re.IGNORECASE):
        add_issue(
            issues,
            "unsafe_html",
            "error",
            "Unexpected script, style or iframe markup is present.",
        )

    if metrics["unclosedTags"] or metrics["mismatchedTags"]:
        add_issue(
            issues,
            "malformed_html",
            "error",
            "The description contains unbalanced HTML tags.",
        )

    if text.endswith('"') and text.count('"') % 2 == 1:
        add_issue(
            issues,
            "stray_trailing_quote",
            "error",
            "The description ends with an unmatched quotation mark.",
        )

    if metrics["wordCount"] and (
        metrics["boldWordCount"] / metrics["wordCount"] >= 0.8
        or re.search(
            r"font-weight\s*:\s*(?:bold|[5-9]00)",
            body_html,
            re.IGNORECASE,
        )
    ):
        add_issue(
            issues,
            "full_bold_description",
            "error",
            "Most or all description copy is bold.",
        )

    if 0 < metrics["wordCount"] < 20:
        add_issue(
            issues,
            "thin_description",
            "info",
            "The copy is brief; length alone does not require a rewrite.",
        )

    seo = graphql_collection.get("seo") or {}
    seo_title = (seo.get("title") or "").strip()
    seo_description = (seo.get("description") or "").strip()
    if not seo_title:
        add_issue(
            issues,
            "missing_seo_title",
            "warning",
            "No explicit search engine title is set.",
        )
    elif re.search(r"\bcollectie\b", seo_title, re.IGNORECASE):
        add_issue(
            issues,
            "non_english_seo_title",
            "warning",
            "The search engine title is Dutch on an English storefront.",
        )
    if not seo_description:
        add_issue(
            issues,
            "missing_meta_description",
            "warning",
            "No explicit search engine description is set.",
        )
    else:
        if re.search(
            r"\b(?:ontdek|producten|bij)\b",
            seo_description,
            re.IGNORECASE,
        ):
            add_issue(
                issues,
                "non_english_meta_description",
                "warning",
                "The meta description is Dutch on an English storefront.",
            )
        if re.fullmatch(
            r"Ontdek alle producten van .+ bij Neighbourhood Arnhem\.",
            seo_description,
            re.IGNORECASE,
        ):
            add_issue(
                issues,
                "generic_meta_template",
                "warning",
                "The meta description is a repeated generic template.",
            )

    image = graphql_collection.get("image")
    if image and not (image.get("altText") or "").strip():
        add_issue(
            issues,
            "missing_collection_image_alt",
            "warning",
            "The collection image has no alt text.",
        )

    content_issue_codes = {
        "blank_description",
        "placeholder_description",
        "brand_not_named",
        "generic_boilerplate",
        "duplicate_copy",
    }
    technical_issue_codes = {
        "encoding_damage",
        "unsafe_html",
        "malformed_html",
        "full_bold_description",
        "stray_trailing_quote",
    }
    codes = {issue["code"] for issue in issues}
    if codes & content_issue_codes:
        description_action = "content_proposal"
    elif codes & technical_issue_codes:
        description_action = "technical_repair"
    else:
        description_action = "preserve"

    if {"missing_seo_title", "missing_meta_description"} & codes:
        seo_action = "complete_metadata"
    elif {
        "non_english_seo_title",
        "non_english_meta_description",
        "generic_meta_template",
    } & codes:
        seo_action = "replace_metadata"
    else:
        seo_action = "preserve"
    return {
        "id": collection.get("id"),
        "handle": collection.get("handle"),
        "title": title,
        "vendor": vendor,
        "published": bool(collection.get("published_at")),
        "productCounts": product_counts,
        "descriptionAction": description_action,
        "seoAction": seo_action,
        "metrics": {
            key: value for key, value in metrics.items() if key != "text"
        },
        "seo": seo,
        "image": image,
        "issues": issues,
        "descriptionPreview": text[:280],
    }


def markdown_escape(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")


def build_markdown(report: Dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# Shopify brand SEO audit",
        "",
        f"Generated: `{report['generatedAt']}`",
        "",
        "## Summary",
        "",
        f"- Canonical brand collections: {summary['canonicalBrandCollections']}",
        f"- Preserve descriptions: {summary['descriptionActions'].get('preserve', 0)}",
        (
            "- Technical HTML repairs: "
            f"{summary['descriptionActions'].get('technical_repair', 0)}"
        ),
        (
            "- Content proposals needed: "
            f"{summary['descriptionActions'].get('content_proposal', 0)}"
        ),
        f"- Missing vendor collections: {len(report['missingVendorCollections'])}",
        f"- Duplicate canonical vendor collections: {len(report['duplicateVendorCollections'])}",
        "",
        "## Collections",
        "",
        "| Brand | Products A/D | Words | Description | SEO | Issues |",
        "| --- | ---: | ---: | --- | --- | --- |",
    ]
    for item in report["collections"]:
        counts = item["productCounts"]
        issue_codes = ", ".join(issue["code"] for issue in item["issues"]) or "-"
        lines.append(
            "| "
            + " | ".join(
                [
                    markdown_escape(item["title"]),
                    f"{counts.get('active', 0)}/{counts.get('draft', 0)}",
                    str(item["metrics"]["wordCount"]),
                    item["descriptionAction"],
                    item["seoAction"],
                    markdown_escape(issue_codes),
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- Word count is diagnostic only and never decides quality by itself.",
            "- Missing SEO metadata is separate from description quality.",
            "- No Shopify content or theme files were changed by this audit.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only audit of canonical Shopify brand collections, copy and "
            "SEO metadata."
        )
    )
    parser.add_argument("--shop", default="")
    parser.add_argument("--api-version", default=DEFAULT_API_VERSION)
    parser.add_argument("--json-report", type=Path, default=DEFAULT_JSON_REPORT)
    parser.add_argument(
        "--markdown-report", type=Path, default=DEFAULT_MARKDOWN_REPORT
    )
    args = parser.parse_args()

    load_dotenv(Path(".env"))
    shop = normalize_shop(args.shop or os.environ.get("SHOPIFY_SHOP", ""))
    client_id = os.environ.get("SHOPIFY_CLIENT_ID") or os.environ.get(
        "SHOPIFY_API_KEY", ""
    )
    client_secret = os.environ.get("SHOPIFY_CLIENT_SECRET") or os.environ.get(
        "SHOPIFY_API_SECRET", ""
    )
    if not shop or not client_id or not client_secret:
        raise SystemExit(
            "SHOPIFY_SHOP and Shopify client credentials are required."
        )

    token = get_client_credentials_token(shop, client_id, client_secret)
    smart_collections = fetch_smart_collections(
        shop, token, args.api_version
    )
    collection_seo = fetch_collection_seo(shop, token, args.api_version)
    vendor_counts = fetch_vendor_product_counts(
        shop, token, args.api_version
    )

    vendor_collections: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    special_collections: List[Dict[str, Any]] = []
    for collection in smart_collections:
        vendor = vendor_rule(collection)
        if not vendor:
            continue
        if collection.get("handle") in SPECIAL_COLLECTION_HANDLES:
            special_collections.append(collection)
            continue
        vendor_collections[vendor].append(collection)

    sentence_frequency: Counter[str] = Counter()
    for collections in vendor_collections.values():
        for collection in collections:
            live = collection_seo.get(str(collection["id"]), {})
            metrics = description_metrics(live.get("descriptionHtml") or "")
            sentence_frequency.update(set(sentence_keys(metrics["text"])))

    audited: List[Dict[str, Any]] = []
    for vendor in sorted(vendor_collections, key=str.casefold):
        for collection in vendor_collections[vendor]:
            audited.append(
                analyze_collection(
                    collection,
                    collection_seo.get(str(collection["id"]), {}),
                    vendor_counts.get(
                        vendor,
                        {"total": 0, "active": 0, "draft": 0, "archived": 0},
                    ),
                    sentence_frequency,
                )
            )

    missing_vendor_collections = sorted(
        set(vendor_counts) - set(vendor_collections),
        key=str.casefold,
    )
    duplicate_vendor_collections = [
        {
            "vendor": vendor,
            "handles": sorted(
                collection.get("handle") or ""
                for collection in collections
            ),
        }
        for vendor, collections in sorted(
            vendor_collections.items(), key=lambda item: item[0].casefold()
        )
        if len(collections) > 1
    ]
    description_actions = Counter(
        item["descriptionAction"] for item in audited
    )
    seo_actions = Counter(item["seoAction"] for item in audited)
    issue_counts = Counter(
        issue["code"] for item in audited for issue in item["issues"]
    )
    report = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "mode": "read_only",
        "shop": shop,
        "apiVersion": args.api_version,
        "rubric": {
            "descriptionActions": {
                "preserve": "No objective description defect found.",
                "technical_repair": "Keep wording; repair markup or encoding.",
                "content_proposal": "Editorial review or replacement proposed.",
            },
            "wordCountPolicy": (
                "Diagnostic only; no minimum or maximum decides quality."
            ),
        },
        "summary": {
            "shopifyProducts": sum(
                counts["total"] for counts in vendor_counts.values()
            ),
            "productVendors": len(vendor_counts),
            "smartCollections": len(smart_collections),
            "canonicalBrandCollections": len(audited),
            "descriptionActions": dict(description_actions),
            "seoActions": dict(seo_actions),
            "issueCounts": dict(issue_counts),
        },
        "missingVendorCollections": missing_vendor_collections,
        "duplicateVendorCollections": duplicate_vendor_collections,
        "specialCollectionsExcluded": [
            {
                "title": item.get("title"),
                "handle": item.get("handle"),
                "vendor": vendor_rule(item),
            }
            for item in special_collections
        ],
        "collections": audited,
    }

    args.json_report.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_report.parent.mkdir(parents=True, exist_ok=True)
    args.json_report.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    args.markdown_report.write_text(
        build_markdown(report),
        encoding="utf-8",
    )

    print(json.dumps(report["summary"], indent=2))
    print(f"JSON report: {args.json_report}")
    print(f"Markdown report: {args.markdown_report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
