#!/usr/bin/env python3

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from lightspeed_variant_sync import (
    DEFAULT_API_VERSION,
    clean,
    get_client_credentials_token,
    load_dotenv,
    normalize_shop,
    positive_int,
    shopify_graphql,
)


LOCK_NAMESPACE = "custom"
LOCK_KEY = "catalog_status_sync"
LOCK_VALUE = "manual_review"
DEFAULT_PLAN_FILE = Path(".tmp") / "legacy-draft-status-lock-plan.json"
DEFAULT_RESULTS_FILE = Path(".tmp") / "legacy-draft-status-lock-results.json"
BATCH_SIZE = 25


def get_shopify_products(
    endpoint: str,
    token: str,
    page_size: int,
) -> List[Dict[str, Any]]:
    products = []
    after = None
    while True:
        data = shopify_graphql(
            endpoint,
            token,
            """
            query ProductsForStatusLock($first: Int!, $after: String) {
              products(first: $first, after: $after, sortKey: ID) {
                nodes {
                  id
                  title
                  handle
                  status
                  catalogStatusSync: metafield(
                    namespace: "custom",
                    key: "catalog_status_sync"
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
            """,
            {"first": page_size, "after": after},
        )
        connection = data.get("products") or {}
        products.extend(connection.get("nodes") or [])
        page_info = connection.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        after = page_info.get("endCursor")
    return products


def baseline_gid(product: Dict[str, Any]) -> str:
    return f"gid://shopify/Product/{product['id']}"


def build_plan(
    baseline_products: List[Dict[str, Any]],
    live_products: List[Dict[str, Any]],
) -> Dict[str, Any]:
    original_drafts = [
        product
        for product in baseline_products
        if clean(product.get("status")).lower() == "draft"
    ]
    live_by_id = {
        product["id"]: product
        for product in live_products
        if product.get("id")
    }
    updates = []
    unchanged = []
    conflicts = []
    for baseline in original_drafts:
        product_id = baseline_gid(baseline)
        live = live_by_id.get(product_id)
        if not live:
            conflicts.append({
                "productId": product_id,
                "handle": baseline.get("handle"),
                "reason": "missing_live_product",
            })
            continue
        if live.get("handle") != baseline.get("handle"):
            conflicts.append({
                "productId": product_id,
                "handle": baseline.get("handle"),
                "liveHandle": live.get("handle"),
                "reason": "handle_changed_since_baseline",
            })
            continue
        if live.get("status") != "DRAFT":
            conflicts.append({
                "productId": product_id,
                "handle": baseline.get("handle"),
                "liveStatus": live.get("status"),
                "reason": "original_draft_is_now_active",
            })
            continue
        item = {
            "productId": product_id,
            "handle": live.get("handle"),
            "title": live.get("title"),
        }
        lock_value = clean(
            (live.get("catalogStatusSync") or {}).get("value")
        )
        if lock_value == LOCK_VALUE:
            unchanged.append(item)
        elif lock_value:
            conflicts.append({
                **item,
                "reason": "different_existing_status_lock",
                "actual": lock_value,
            })
        else:
            updates.append(item)

    return {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "mode": "dry-run",
        "summary": {
            "baselineProducts": len(baseline_products),
            "originalDrafts": len(original_drafts),
            "updates": len(updates),
            "unchanged": len(unchanged),
            "conflicts": len(conflicts),
            "safeToApply": (
                len(original_drafts) == 83
                and not conflicts
            ),
        },
        "updates": updates,
        "unchanged": unchanged,
        "conflicts": conflicts,
    }


def set_locks(
    endpoint: str,
    token: str,
    updates: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    data = shopify_graphql(
        endpoint,
        token,
        """
        mutation LockLegacyDraftStatuses(
          $metafields: [MetafieldsSetInput!]!
        ) {
          metafieldsSet(metafields: $metafields) {
            metafields {
              id
              ownerType
              value
            }
            userErrors {
              field
              message
              code
            }
          }
        }
        """,
        {
            "metafields": [
                {
                    "ownerId": update["productId"],
                    "namespace": LOCK_NAMESPACE,
                    "key": LOCK_KEY,
                    "type": "single_line_text_field",
                    "value": LOCK_VALUE,
                }
                for update in updates
            ]
        },
    )
    payload = data.get("metafieldsSet") or {}
    errors = payload.get("userErrors") or []
    if errors:
        details = "; ".join(
            f"{'.'.join(str(part) for part in error.get('field') or [])}: "
            f"{error.get('message')} ({error.get('code')})"
            for error in errors
        )
        raise RuntimeError(f"Shopify rejected metafieldsSet: {details}")
    return payload.get("metafields") or []


def chunked(values: List[Dict[str, Any]], size: int):
    for index in range(0, len(values), size):
        yield values[index:index + size]


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    project_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description=(
            "Lock the exact 83 original legacy drafts to manual status review."
        )
    )
    parser.add_argument("--baseline-products", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--shop")
    parser.add_argument("--token")
    parser.add_argument("--api-key")
    parser.add_argument("--api-secret")
    parser.add_argument("--prefer-client-credentials", action="store_true")
    parser.add_argument("--api-version", default=DEFAULT_API_VERSION)
    parser.add_argument("--page-size", type=positive_int, default=100)
    parser.add_argument("--env-file")
    parser.add_argument("--plan-file", default=str(project_root / DEFAULT_PLAN_FILE))
    parser.add_argument("--results-file", default=str(project_root / DEFAULT_RESULTS_FILE))
    args = parser.parse_args()

    baseline_data = json.loads(
        Path(args.baseline_products).read_text(encoding="utf-8")
    )
    baseline_products = baseline_data.get("products") or []
    load_dotenv(Path(args.env_file or project_root / ".env"))
    shop = normalize_shop(
        args.shop
        or os.environ.get("SHOPIFY_SHOP")
        or os.environ.get("SHOPIFY_STORE_DOMAIN")
        or os.environ.get("SHOPIFY_STORE")
    )
    token = (
        args.token
        or os.environ.get("SHOPIFY_ADMIN_ACCESS_TOKEN")
        or os.environ.get("SHOPIFY_ACCESS_TOKEN")
    )
    client_id = (
        args.api_key
        or os.environ.get("SHOPIFY_API_KEY")
        or os.environ.get("SHOPIFY_CLIENT_ID")
    )
    client_secret = (
        args.api_secret
        or os.environ.get("SHOPIFY_API_SECRET")
        or os.environ.get("SHOPIFY_CLIENT_SECRET")
    )
    api_version = (
        args.api_version
        or os.environ.get("SHOPIFY_API_VERSION")
        or DEFAULT_API_VERSION
    )
    if not shop or (not token and (not client_id or not client_secret)):
        raise SystemExit("Missing Shopify credentials.")
    if args.prefer_client_credentials or not token:
        token = get_client_credentials_token(
            shop,
            client_id,
            client_secret,
        )
    endpoint = f"https://{shop}/admin/api/{api_version}/graphql.json"
    live_products = get_shopify_products(endpoint, token, args.page_size)
    plan = build_plan(baseline_products, live_products)
    plan["mode"] = "apply" if args.apply else "dry-run"
    write_json(Path(args.plan_file), plan)
    print(json.dumps(plan["summary"], indent=2))
    print(f"Plan written: {Path(args.plan_file)}")

    if not args.apply:
        print("No status locks were written.")
        return
    if not plan["summary"]["safeToApply"]:
        raise SystemExit("Refusing --apply because the status-lock plan is unsafe.")

    results = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "updated": [],
        "verificationFailures": [],
    }
    batches = list(chunked(plan["updates"], BATCH_SIZE))
    for index, batch in enumerate(batches, start=1):
        results["updated"].extend(set_locks(endpoint, token, batch))
        print(f"Lock batch {index}/{len(batches)}: {len(batch)} products")

    live_after = {
        product["id"]: clean(
            (product.get("catalogStatusSync") or {}).get("value")
        )
        for product in get_shopify_products(
            endpoint,
            token,
            args.page_size,
        )
    }
    for update in plan["updates"]:
        if live_after.get(update["productId"]) != LOCK_VALUE:
            results["verificationFailures"].append({
                "productId": update["productId"],
                "handle": update["handle"],
                "actual": live_after.get(update["productId"]),
            })
    write_json(Path(args.results_file), results)
    print(f"Results written: {Path(args.results_file)}")
    print(f"Updated: {len(results['updated'])}")
    print(f"Verification failures: {len(results['verificationFailures'])}")
    if results["verificationFailures"]:
        raise SystemExit("Legacy draft status-lock verification failed.")


if __name__ == "__main__":
    main()
