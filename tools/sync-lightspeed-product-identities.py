#!/usr/bin/env python3

import argparse
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from lightspeed_variant_sync import (
    DEFAULT_API_VERSION,
    DEFAULT_MAX_SOURCE_AGE_HOURS,
    build_product_mapping,
    clean,
    filter_invalid_catalog_rows,
    filter_retired_catalog_rows,
    get_client_credentials_token,
    load_dotenv,
    normalize_shop,
    positive_int,
    read_csv,
    shopify_graphql,
    source_metadata,
)


LIGHTSPEED_NAMESPACE = "custom"
LIGHTSPEED_ID_KEY = "lightspeed_internal_id"
LIGHTSPEED_SOURCE_KEY = "lightspeed_source"
LIGHTSPEED_SOURCE_VALUE = "c_series_csv"
DEFAULT_PLAN_FILE = Path(".tmp") / "lightspeed-product-identity-plan.json"
DEFAULT_RESULTS_FILE = Path(".tmp") / "lightspeed-product-identity-results.json"
METAFIELDS_BATCH_SIZE = 24


def build_source_products(rows: List[Dict[str, str]]) -> Dict[str, str]:
    variant_mapping, warnings = build_product_mapping(rows)
    if warnings:
        raise RuntimeError("Source contains duplicate product option mappings.")
    by_internal_id = defaultdict(list)
    for row in rows:
        internal_id = clean(row.get("Internal_ID"))
        variant_id = clean(row.get("Internal_Variant_ID"))
        if internal_id and variant_id and variant_id in variant_mapping:
            by_internal_id[internal_id].append(variant_mapping[variant_id])
    products = {
        variants[0]["handle"]: internal_id
        for internal_id, variants in by_internal_id.items()
        if variants and variants[0]["handle"]
    }
    handle_counts = Counter(
        variants[0]["handle"]
        for variants in by_internal_id.values()
        if variants and variants[0]["handle"]
    )
    duplicates = [
        handle
        for handle, count in handle_counts.items()
        if count > 1
    ]
    if duplicates:
        raise RuntimeError(
            "Duplicate source product handles: " + ", ".join(duplicates)
        )
    return products


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
            query ProductsForLightspeedIdentity(
              $first: Int!,
              $after: String
            ) {
              products(first: $first, after: $after, sortKey: ID) {
                nodes {
                  id
                  title
                  handle
                  lightspeedInternalId: metafield(
                    namespace: "custom",
                    key: "lightspeed_internal_id"
                  ) {
                    value
                  }
                  lightspeedSource: metafield(
                    namespace: "custom",
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


def build_plan(
    source_products: Dict[str, str],
    shopify_products: List[Dict[str, Any]],
    source_file: Dict[str, Any],
    max_source_age_hours: float,
) -> Dict[str, Any]:
    live_by_handle = {
        product["handle"]: product
        for product in shopify_products
        if product.get("handle")
    }
    updates = []
    unchanged = []
    missing = []
    conflicts = []
    for handle, internal_id in sorted(source_products.items()):
        live = live_by_handle.get(handle)
        if not live:
            missing.append({
                "handle": handle,
                "lightspeedInternalId": internal_id,
            })
            continue
        live_id = clean(
            (live.get("lightspeedInternalId") or {}).get("value")
        )
        live_source = clean(
            (live.get("lightspeedSource") or {}).get("value")
        )
        if live_id and live_id != internal_id:
            conflicts.append({
                "handle": handle,
                "productId": live["id"],
                "reason": "existing_internal_id_mismatch",
                "expected": internal_id,
                "actual": live_id,
            })
            continue
        if live_source and live_source != LIGHTSPEED_SOURCE_VALUE:
            conflicts.append({
                "handle": handle,
                "productId": live["id"],
                "reason": "existing_source_mismatch",
                "expected": LIGHTSPEED_SOURCE_VALUE,
                "actual": live_source,
            })
            continue
        item = {
            "handle": handle,
            "title": live.get("title"),
            "productId": live["id"],
            "lightspeedInternalId": internal_id,
            "setInternalId": live_id != internal_id,
            "setSource": live_source != LIGHTSPEED_SOURCE_VALUE,
        }
        if item["setInternalId"] or item["setSource"]:
            updates.append(item)
        else:
            unchanged.append(item)

    fresh = source_file["ageHours"] <= max_source_age_hours
    return {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "mode": "dry-run",
        "sourceFile": source_file,
        "summary": {
            "sourceProducts": len(source_products),
            "shopifyProductsScanned": len(shopify_products),
            "updates": len(updates),
            "unchanged": len(unchanged),
            "missing": len(missing),
            "conflicts": len(conflicts),
            "sourceFresh": fresh,
            "safeToApply": fresh and not missing and not conflicts,
        },
        "updates": updates,
        "unchanged": unchanged,
        "missing": missing,
        "conflicts": conflicts,
    }


def metafield_inputs(updates: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    inputs = []
    for update in updates:
        if update["setInternalId"]:
            inputs.append({
                "ownerId": update["productId"],
                "namespace": LIGHTSPEED_NAMESPACE,
                "key": LIGHTSPEED_ID_KEY,
                "type": "single_line_text_field",
                "value": update["lightspeedInternalId"],
            })
        if update["setSource"]:
            inputs.append({
                "ownerId": update["productId"],
                "namespace": LIGHTSPEED_NAMESPACE,
                "key": LIGHTSPEED_SOURCE_KEY,
                "type": "single_line_text_field",
                "value": LIGHTSPEED_SOURCE_VALUE,
            })
    return inputs


def set_metafields(
    endpoint: str,
    token: str,
    inputs: List[Dict[str, str]],
) -> List[Dict[str, Any]]:
    data = shopify_graphql(
        endpoint,
        token,
        """
        mutation SetLightspeedProductIdentities(
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
        {"metafields": inputs},
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


def chunked(values: List[Dict[str, str]], size: int):
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
            "Backfill persistent Lightspeed product identities by exact "
            "Shopify handle."
        )
    )
    parser.add_argument("--products-source", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--shop")
    parser.add_argument("--token")
    parser.add_argument("--api-key")
    parser.add_argument("--api-secret")
    parser.add_argument("--prefer-client-credentials", action="store_true")
    parser.add_argument("--api-version", default=DEFAULT_API_VERSION)
    parser.add_argument("--page-size", type=positive_int, default=100)
    parser.add_argument(
        "--max-source-age-hours",
        type=float,
        default=DEFAULT_MAX_SOURCE_AGE_HOURS,
    )
    parser.add_argument("--env-file")
    parser.add_argument("--plan-file", default=str(project_root / DEFAULT_PLAN_FILE))
    parser.add_argument("--results-file", default=str(project_root / DEFAULT_RESULTS_FILE))
    args = parser.parse_args()

    source_path = Path(args.products_source)
    source_rows = read_csv(source_path)
    source_rows, _, _ = filter_retired_catalog_rows(source_rows, [])
    source_rows, _, invalid_exclusions = filter_invalid_catalog_rows(
        source_rows,
        [],
    )
    source_products = build_source_products(source_rows)

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
    plan = build_plan(
        source_products,
        live_products,
        source_metadata(source_path),
        args.max_source_age_hours,
    )
    plan["invalidCatalogItems"] = invalid_exclusions["items"]
    plan["mode"] = "apply" if args.apply else "dry-run"
    write_json(Path(args.plan_file), plan)
    print(json.dumps(plan["summary"], indent=2))
    print(f"Invalid catalog products excluded: {invalid_exclusions['products']}")
    print(f"Plan written: {Path(args.plan_file)}")

    if not args.apply:
        print("No product identities were written.")
        return
    if not plan["summary"]["safeToApply"]:
        raise SystemExit("Refusing --apply because the identity plan is unsafe.")

    inputs = metafield_inputs(plan["updates"])
    batches = list(chunked(inputs, METAFIELDS_BATCH_SIZE))
    results = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "updatedMetafields": [],
        "verificationFailures": [],
    }
    for index, batch in enumerate(batches, start=1):
        results["updatedMetafields"].extend(
            set_metafields(endpoint, token, batch)
        )
        print(f"Identity batch {index}/{len(batches)}: {len(batch)} metafields")

    live_after = {
        product["handle"]: product
        for product in get_shopify_products(
            endpoint,
            token,
            args.page_size,
        )
    }
    for update in plan["updates"]:
        live = live_after.get(update["handle"]) or {}
        actual_id = clean(
            (live.get("lightspeedInternalId") or {}).get("value")
        )
        actual_source = clean(
            (live.get("lightspeedSource") or {}).get("value")
        )
        if (
            actual_id != update["lightspeedInternalId"]
            or actual_source != LIGHTSPEED_SOURCE_VALUE
        ):
            results["verificationFailures"].append({
                "handle": update["handle"],
                "expectedInternalId": update["lightspeedInternalId"],
                "actualInternalId": actual_id,
                "actualSource": actual_source,
            })
    write_json(Path(args.results_file), results)
    print(f"Results written: {Path(args.results_file)}")
    print(f"Metafields updated: {len(results['updatedMetafields'])}")
    print(f"Verification failures: {len(results['verificationFailures'])}")
    if results["verificationFailures"]:
        raise SystemExit("Product identity verification failed.")


if __name__ == "__main__":
    main()
