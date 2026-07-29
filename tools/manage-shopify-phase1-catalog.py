#!/usr/bin/env python3

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from shopify_api import get_client_credentials_token, graphql_request


DEFAULT_API_VERSION = "2026-04"
TEST_PRODUCT_HANDLES = (
    "vest",
    "button-up",
    "lumber-short-herringbone-cotton-washed-navy",
    "schoen",
    "varsity-jacket",
)
RETIRED_BRAND_PRODUCTS = (
    {
        "handle": "sunglass-green-champagne",
        "vendor": "Messyweekend",
    },
    {
        "handle": "sunglass-dean-brown-coffee",
        "vendor": "Messyweekend",
    },
    {
        "handle": "sunglass-dean-grey-black",
        "vendor": "Messyweekend",
    },
    {
        "handle": "sunglass-dean-black-grey",
        "vendor": "Messyweekend",
    },
    {
        "handle": "sunglass-new-dylan-green-bottle",
        "vendor": "Messyweekend",
    },
    {
        "handle": "sunglass-rate-black-grey",
        "vendor": "Messyweekend",
    },
    {
        "handle": "pop-trading-company-tee-shirt",
        "vendor": "POP Trading Company",
    },
    {
        "handle": "e-da-amber-10",
        "vendor": "encens d'auroville",
    },
    {
        "handle": "e-da-amber-cones",
        "vendor": "encens d'auroville",
    },
)
OLD_BRAND_COLLECTION_HANDLES = (
    "blundstone",
    "danner",
    "paradise-found",
    "stelff",
    "sunray",
    "ukiyo",
    "wild-animals",
)
DESIRED_BRAND_COLLECTIONS = (
    {
        "title": "Padmore and Barnes",
        "handle": "padmore-and-barnes",
        "vendor": "Padmore and Barnes",
        "published": True,
        "body_html": (
            "<p>Shop Padmore and Barnes at Neighbourhood Arnhem. Discover "
            "handcrafted footwear rooted in the brand's Irish shoemaking "
            "heritage, selected for comfort, character and everyday wear.</p>"
        ),
    },
    {
        "title": "Ralph Lauren",
        "handle": "ralph-lauren",
        "vendor": "Ralph Lauren",
        "published": True,
        "body_html": (
            "<p>Shop Ralph Lauren at Neighbourhood Arnhem. Explore timeless "
            "American menswear combining classic sportswear, refined fabrics "
            "and versatile wardrobe staples.</p>"
        ),
    },
    {
        "title": "Steiff",
        "handle": "steiff",
        "vendor": "Steiff",
        "published": True,
        "body_html": (
            "<p>Shop Steiff at Neighbourhood Arnhem. Discover carefully made "
            "teddy bears and soft toys from the historic German maker, known "
            "for lasting quality and its signature Button in Ear.</p>"
        ),
    },
    {
        "title": "Plot",
        "handle": "plot",
        "vendor": "Plot",
        "published": False,
        "body_html": (
            "<p>Discover Plot at Neighbourhood Arnhem. Explore wool knitwear "
            "selected for its natural materials, considered construction and "
            "quiet everyday character.</p>"
        ),
    },
    {
        "title": "Atelier Neighbourhood",
        "handle": "atelier-neighbourhood",
        "vendor": "Atelier Neighbourhood",
        "published": True,
        "body_html": "<p>Onze eigen producten</p>",
    },
)


def parse_env_value(value):
    value = value.strip()
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    return value


def load_dotenv(path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$", line)
        if match and match.group(1) not in os.environ:
            os.environ[match.group(1)] = parse_env_value(match.group(2))


def normalize_shop(value):
    cleaned = (
        (value or "")
        .strip()
        .replace("https://", "")
        .replace("http://", "")
        .split("/")[0]
    )
    return cleaned if "." in cleaned else f"{cleaned}.myshopify.com"


def request_json(method, url, token, payload=None, attempts=8):
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": token,
    }
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    last_error = None
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(
            url, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = response.read().decode("utf-8")
                return json.loads(body or "{}")
        except urllib.error.HTTPError as error:
            last_error = error
            body = error.read().decode("utf-8", errors="replace")
            if error.code != 429 and error.code < 500:
                raise RuntimeError(
                    f"Shopify request failed ({error.code}) for {url}: {body}"
                ) from error
            if attempt < attempts:
                time.sleep(float(error.headers.get("Retry-After") or attempt))
        except urllib.error.URLError as error:
            last_error = error
            if attempt < attempts:
                time.sleep(min(attempt, 4))
    raise RuntimeError(
        f"Shopify request failed after {attempts} attempts: {url}"
    ) from last_error


def rest_url(shop, api_version, path, params=None):
    url = f"https://{shop}/admin/api/{api_version}/{path}.json"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return url


def get_resource(shop, api_version, token, resource, handle):
    data = request_json(
        "GET",
        rest_url(
            shop,
            api_version,
            resource,
            {"handle": handle, "limit": 10},
        ),
        token,
    )
    return data.get(resource, [])


def get_all_products(shop, api_version, token):
    query = """
      query PhaseOneProducts($first: Int!, $after: String) {
        products(first: $first, after: $after, sortKey: ID) {
          nodes {
            id
            legacyResourceId
            title
            handle
            vendor
            status
            publishedAt
            lightspeedSource: metafield(
              namespace: "custom"
              key: "lightspeed_source"
            ) {
              value
            }
          }
          pageInfo {
            hasNextPage
            endCursor
          }
        }
      }
    """
    endpoint = f"https://{shop}/admin/api/{api_version}/graphql.json"
    after = None
    products = []
    while True:
        data = graphql_request(
            endpoint,
            token,
            query,
            {"first": 100, "after": after},
        )
        connection = data["products"]
        for node in connection.get("nodes", []):
            products.append(
                {
                    "id": int(node["legacyResourceId"]),
                    "title": node["title"],
                    "handle": node["handle"],
                    "vendor": node["vendor"],
                    "status": node["status"].lower(),
                    "published_at": node.get("publishedAt"),
                    "lightspeed_source": (
                        node.get("lightspeedSource") or {}
                    ).get("value"),
                }
            )
        page_info = connection.get("pageInfo", {})
        if not page_info.get("hasNextPage"):
            return products
        after = page_info.get("endCursor")
        time.sleep(0.55)


def acquire_token(shop):
    client_id = os.environ.get("SHOPIFY_API_KEY") or os.environ.get(
        "SHOPIFY_CLIENT_ID"
    )
    client_secret = os.environ.get("SHOPIFY_API_SECRET") or os.environ.get(
        "SHOPIFY_CLIENT_SECRET"
    )
    if client_id and client_secret:
        return get_client_credentials_token(shop, client_id, client_secret)[
            "access_token"
        ]
    token = os.environ.get("SHOPIFY_ADMIN_ACCESS_TOKEN") or os.environ.get(
        "SHOPIFY_ACCESS_TOKEN"
    )
    if token:
        return token
    raise SystemExit("Missing Shopify credentials in .env.")


def compact_collection(collection, kind):
    if not collection:
        return None
    return {
        "id": collection.get("id"),
        "kind": kind,
        "title": collection.get("title"),
        "handle": collection.get("handle"),
        "published_at": collection.get("published_at"),
        "sort_order": collection.get("sort_order"),
        "disjunctive": collection.get("disjunctive"),
        "body_html": collection.get("body_html"),
        "rules": collection.get("rules"),
    }


def current_collection(shop, api_version, token, handle):
    custom = get_resource(
        shop, api_version, token, "custom_collections", handle
    )
    smart = get_resource(
        shop, api_version, token, "smart_collections", handle
    )
    if len(custom) + len(smart) > 1:
        raise RuntimeError(f"Multiple collections found for handle {handle}.")
    if custom:
        return compact_collection(custom[0], "custom")
    if smart:
        return compact_collection(smart[0], "smart")
    return None


def desired_payload(config, preserved=None):
    body_html = config["body_html"]
    if preserved and preserved.get("body_html"):
        body_html = preserved["body_html"]
    return {
        "title": config["title"],
        "handle": config["handle"],
        "body_html": body_html,
        "published": config["published"],
        "sort_order": "most-relevant",
        "disjunctive": False,
        "rules": [
            {
                "column": "vendor",
                "relation": "equals",
                "condition": config["vendor"],
            }
        ],
    }


def collection_matches(collection, payload):
    if not collection or collection["kind"] != "smart":
        return False
    actual_rules = [
        {
            "column": rule.get("column"),
            "relation": rule.get("relation"),
            "condition": rule.get("condition"),
        }
        for rule in collection.get("rules") or []
    ]
    return (
        collection.get("title") == payload["title"]
        and collection.get("handle") == payload["handle"]
        and bool(collection.get("published_at")) == payload["published"]
        and collection.get("sort_order") == payload["sort_order"]
        and collection.get("disjunctive") == payload["disjunctive"]
        and (collection.get("body_html") or "") == (payload["body_html"] or "")
        and actual_rules == payload["rules"]
    )


def delete_collection(shop, api_version, token, collection):
    resource = (
        "custom_collections"
        if collection["kind"] == "custom"
        else "smart_collections"
    )
    request_json(
        "DELETE",
        rest_url(
            shop, api_version, f"{resource}/{collection['id']}"
        ),
        token,
    )


def create_smart_collection(shop, api_version, token, payload):
    response = request_json(
        "POST",
        rest_url(shop, api_version, "smart_collections"),
        token,
        {"smart_collection": payload},
    )
    return response["smart_collection"]


def update_smart_collection(shop, api_version, token, collection_id, payload):
    response = request_json(
        "PUT",
        rest_url(
            shop, api_version, f"smart_collections/{collection_id}"
        ),
        token,
        {"smart_collection": {"id": collection_id, **payload}},
    )
    return response["smart_collection"]


def build_plan(shop, api_version, token):
    products = get_all_products(shop, api_version, token)
    by_handle = {product["handle"]: product for product in products}
    vendor_statuses = Counter(
        (product["vendor"], product["status"]) for product in products
    )

    product_actions = []
    for handle in TEST_PRODUCT_HANDLES:
        product = by_handle.get(handle)
        if not product:
            product_actions.append(
                {"action": "already_absent", "handle": handle}
            )
            continue
        if product.get("vendor") != "Neighbourhood Arnhem":
            raise RuntimeError(
                f"Safety stop: {handle} has vendor {product.get('vendor')!r}, "
                "not 'Neighbourhood Arnhem'."
            )
        product_actions.append(
            {
                "action": "delete",
                "id": product["id"],
                "title": product["title"],
                "handle": handle,
                "vendor": product["vendor"],
                "status": product["status"],
            }
        )

    retired_brand_actions = []
    for target in RETIRED_BRAND_PRODUCTS:
        handle = target["handle"]
        product = by_handle.get(handle)
        if not product:
            retired_brand_actions.append(
                {
                    "action": "already_absent",
                    "handle": handle,
                    "expected_vendor": target["vendor"],
                }
            )
            continue
        if product.get("vendor") != target["vendor"]:
            raise RuntimeError(
                f"Safety stop: {handle} has vendor {product.get('vendor')!r}, "
                f"not {target['vendor']!r}."
            )
        retired_brand_actions.append(
            {
                "action": "delete",
                "id": product["id"],
                "title": product["title"],
                "handle": handle,
                "vendor": product["vendor"],
                "status": product["status"],
                "lightspeed_source": product.get("lightspeed_source"),
            }
        )

    obsolete_actions = []
    for handle in OLD_BRAND_COLLECTION_HANDLES:
        collection = current_collection(
            shop, api_version, token, handle
        )
        obsolete_actions.append(
            {
                "action": "delete" if collection else "already_absent",
                "handle": handle,
                "current": collection,
            }
        )

    desired_actions = []
    for config in DESIRED_BRAND_COLLECTIONS:
        current = current_collection(
            shop, api_version, token, config["handle"]
        )
        payload = desired_payload(config, current)
        if collection_matches(current, payload):
            action = "unchanged"
        elif current and current["kind"] == "custom":
            action = "replace_custom_with_smart"
        elif current:
            action = "update_smart"
        else:
            action = "create_smart"
        desired_actions.append(
            {
                "action": action,
                "current": current,
                "desired": payload,
                "active_products_for_vendor": vendor_statuses[
                    (config["vendor"], "active")
                ],
                "draft_products_for_vendor": vendor_statuses[
                    (config["vendor"], "draft")
                ],
            }
        )

    return {
        "products": product_actions,
        "retired_brand_products": retired_brand_actions,
        "obsolete_brand_collections": obsolete_actions,
        "desired_brand_collections": desired_actions,
    }


def apply_plan(shop, api_version, token, plan):
    results = {
        "products": [],
        "retired_brand_products": [],
        "obsolete_brand_collections": [],
        "desired_brand_collections": [],
    }
    for item in plan["products"]:
        if item["action"] == "already_absent":
            results["products"].append(item)
            continue
        request_json(
            "DELETE",
            rest_url(shop, api_version, f"products/{item['id']}"),
            token,
        )
        results["products"].append({**item, "action": "deleted"})

    for item in plan["retired_brand_products"]:
        if item["action"] == "already_absent":
            results["retired_brand_products"].append(item)
            continue
        request_json(
            "DELETE",
            rest_url(shop, api_version, f"products/{item['id']}"),
            token,
        )
        results["retired_brand_products"].append(
            {**item, "action": "deleted"}
        )

    for item in plan["obsolete_brand_collections"]:
        current = item.get("current")
        if not current:
            results["obsolete_brand_collections"].append(item)
            continue
        delete_collection(shop, api_version, token, current)
        results["obsolete_brand_collections"].append(
            {**item, "action": "deleted"}
        )

    for item in plan["desired_brand_collections"]:
        action = item["action"]
        current = item.get("current")
        desired = item["desired"]
        if action == "unchanged":
            result = current
        elif action == "replace_custom_with_smart":
            delete_collection(shop, api_version, token, current)
            result = create_smart_collection(
                shop, api_version, token, desired
            )
        elif action == "update_smart":
            result = update_smart_collection(
                shop, api_version, token, current["id"], desired
            )
        else:
            result = create_smart_collection(
                shop, api_version, token, desired
            )
        results["desired_brand_collections"].append(
            {
                "action": (
                    action if action == "unchanged" else action + "_applied"
                ),
                "id": result.get("id"),
                "title": result.get("title"),
                "handle": result.get("handle"),
                "published_at": result.get("published_at"),
                "rules": result.get("rules"),
            }
        )
        time.sleep(0.55)
    return results


def verify_desired_collections(shop, api_version, token, plan):
    desired_items = plan["desired_brand_collections"]
    ids = [
        f"gid://shopify/Collection/{item['current']['id']}"
        for item in desired_items
        if item.get("current")
    ]
    if not ids:
        return []

    query = """
      query VerifyPhaseOneCollections($ids: [ID!]!) {
        nodes(ids: $ids) {
          ... on Collection {
            id
            legacyResourceId
            title
            handle
            products(first: 100) {
              nodes {
                handle
                vendor
                status
                publishedAt
              }
              pageInfo {
                hasNextPage
              }
            }
          }
        }
      }
    """
    endpoint = f"https://{shop}/admin/api/{api_version}/graphql.json"
    data = graphql_request(endpoint, token, query, {"ids": ids})
    by_handle = {
        node["handle"]: node
        for node in data.get("nodes", [])
        if node
    }
    verification = []
    for item in desired_items:
        desired = item["desired"]
        handle = desired["handle"]
        node = by_handle.get(handle)
        if not node:
            verification.append(
                {
                    "handle": handle,
                    "ok": False,
                    "error": "collection_not_found",
                }
            )
            continue
        product_nodes = node["products"]["nodes"]
        statuses = Counter(
            product["status"].lower() for product in product_nodes
        )
        vendors = sorted(
            {product["vendor"] for product in product_nodes}
        )
        expected_active = item["active_products_for_vendor"]
        expected_draft = item["draft_products_for_vendor"]
        actual_published = bool(
            (item.get("current") or {}).get("published_at")
        )
        has_next_page = node["products"]["pageInfo"]["hasNextPage"]
        ok = (
            not has_next_page
            and statuses["active"] == expected_active
            and statuses["draft"] == expected_draft
            and vendors == [desired["rules"][0]["condition"]]
            and actual_published == desired["published"]
        )
        verification.append(
            {
                "handle": handle,
                "collection_id": node["legacyResourceId"],
                "ok": ok,
                "published": actual_published,
                "expected_published": desired["published"],
                "product_statuses": dict(sorted(statuses.items())),
                "expected_product_statuses": {
                    "active": expected_active,
                    "draft": expected_draft,
                },
                "vendors": vendors,
                "product_handles": sorted(
                    product["handle"] for product in product_nodes
                ),
            }
        )
    return verification


def main():
    parser = argparse.ArgumentParser(
        description="Plan or apply the approved Shopify Phase 1 catalog cleanup."
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--shop")
    parser.add_argument("--api-version", default=DEFAULT_API_VERSION)
    parser.add_argument("--env-file")
    parser.add_argument("--output")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    load_dotenv(Path(args.env_file or root / ".env"))
    shop = normalize_shop(
        args.shop
        or os.environ.get("SHOPIFY_SHOP")
        or os.environ.get("SHOPIFY_STORE_DOMAIN")
        or os.environ.get("SHOPIFY_STORE")
    )
    if not shop or shop == ".myshopify.com":
        raise SystemExit("Missing SHOPIFY_SHOP.")
    token = acquire_token(shop)

    plan = build_plan(shop, args.api_version, token)
    report = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": "apply" if args.apply else "dry-run",
        "shop": shop,
        "api_version": args.api_version,
        "plan": plan,
    }
    if args.apply:
        report["results"] = apply_plan(
            shop, args.api_version, token, plan
        )
        time.sleep(1)
        plan = build_plan(shop, args.api_version, token)
        report["post_apply_plan"] = plan
    report["verification"] = verify_desired_collections(
        shop, args.api_version, token, plan
    )
    report["verification_ok"] = all(
        item["ok"] for item in report["verification"]
    )

    suffix = "apply" if args.apply else "dry-run"
    output = Path(
        args.output
        or root
        / ".tmp"
        / f"shopify-phase1-catalog-{suffix}-{datetime.now():%Y%m%d}.json"
    ).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"Report written to {output}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
