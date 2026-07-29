#!/usr/bin/env python3

import argparse
import json
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List

from lightspeed_variant_sync import (
    DEFAULT_API_VERSION,
    DEFAULT_MAX_SOURCE_AGE_HOURS,
    build_product_mapping,
    build_shopify_variant_index,
    clean,
    filter_retired_catalog_rows,
    filter_invalid_catalog_rows,
    get_client_credentials_token,
    load_dotenv,
    normalize_shop,
    parse_money,
    positive_int,
    read_csv,
    shopify_graphql,
    source_metadata,
)


DEFAULT_PLAN_FILE = Path(".tmp") / "lightspeed-price-sync-plan.json"
DEFAULT_RESULTS_FILE = Path(".tmp") / "lightspeed-price-sync-results.json"


def get_shopify_price_variants(
    endpoint: str,
    token: str,
    page_size: int,
) -> List[Dict[str, Any]]:
    variants: List[Dict[str, Any]] = []
    after = None
    while True:
        data = shopify_graphql(
            endpoint,
            token,
            """
            query ProductsForPriceSync($first: Int!, $after: String) {
              products(first: $first, after: $after, sortKey: ID) {
                nodes {
                  id
                  title
                  handle
                  variants(first: 100) {
                    nodes {
                      id
                      title
                      price
                      selectedOptions {
                        name
                        value
                      }
                    }
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
        for product in connection.get("nodes") or []:
            for variant in (product.get("variants") or {}).get("nodes") or []:
                variant["product"] = {
                    "id": product.get("id"),
                    "title": product.get("title"),
                    "handle": product.get("handle"),
                }
                variants.append(variant)
        page_info = connection.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        after = page_info.get("endCursor")
    return variants


def build_price_plan(
    source_mapping: Dict[str, Dict[str, Any]],
    shopify_variants: List[Dict[str, Any]],
    source_file: Dict[str, Any],
    retired_exclusions: Dict[str, int],
    invalid_exclusions: Dict[str, Any],
    max_source_age_hours: float,
) -> Dict[str, Any]:
    shopify_index = build_shopify_variant_index(shopify_variants)
    updates = []
    unchanged = []
    conflicts = []

    for lightspeed_variant_id, source in source_mapping.items():
        matches = shopify_index.get((source["handle"], source["combo"]), [])
        if len(matches) != 1:
            conflicts.append({
                "lightspeedVariantId": lightspeed_variant_id,
                "handle": source["handle"],
                "sourceVariant": source["variant"],
                "reason": (
                    "missing_shopify_variant_match"
                    if not matches
                    else "duplicate_shopify_variant_match"
                ),
                "matches": [match.get("id") for match in matches],
            })
            continue

        desired_price = source.get("price")
        if desired_price is None or desired_price <= 0:
            conflicts.append({
                "lightspeedVariantId": lightspeed_variant_id,
                "handle": source["handle"],
                "sourceVariant": source["variant"],
                "reason": "missing_or_invalid_source_price",
            })
            continue

        match = matches[0]
        current_price = parse_money(match.get("price"))
        item = {
            "lightspeedVariantId": lightspeed_variant_id,
            "productId": (match.get("product") or {}).get("id"),
            "shopifyVariantId": match.get("id"),
            "handle": source["handle"],
            "title": source["title"],
            "sourceVariant": source["variant"],
            "currentPrice": (
                format(current_price, ".2f")
                if current_price is not None
                else None
            ),
            "desiredPrice": format(desired_price, ".2f"),
        }
        if current_price == desired_price:
            unchanged.append(item)
        else:
            updates.append(item)

    sources_fresh = source_file["ageHours"] <= max_source_age_hours
    return {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "mode": "dry-run",
        "sourceFile": source_file,
        "summary": {
            "sourceVariants": len(source_mapping),
            "shopifyVariantsScanned": len(shopify_variants),
            "updates": len(updates),
            "unchanged": len(unchanged),
            "conflicts": len(conflicts),
            "retiredProductsExcluded": retired_exclusions["products"],
            "retiredVariantsExcluded": retired_exclusions["variants"],
            "invalidProductsExcluded": invalid_exclusions["products"],
            "invalidVariantsExcluded": invalid_exclusions["variants"],
            "sourcesFresh": sources_fresh,
            "safeToApply": sources_fresh and not conflicts,
        },
        "updates": updates,
        "unchanged": unchanged[:200],
        "conflicts": conflicts,
        "invalidCatalogItems": invalid_exclusions["items"],
    }


def update_product_variant_prices(
    endpoint: str,
    token: str,
    product_id: str,
    updates: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    data = shopify_graphql(
        endpoint,
        token,
        """
        mutation UpdateLightspeedPrices(
          $productId: ID!,
          $variants: [ProductVariantsBulkInput!]!
        ) {
          productVariantsBulkUpdate(
            productId: $productId,
            variants: $variants,
            allowPartialUpdates: false
          ) {
            productVariants {
              id
              price
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
            "productId": product_id,
            "variants": [
                {
                    "id": update["shopifyVariantId"],
                    "price": update["desiredPrice"],
                }
                for update in updates
            ],
        },
    )
    payload = data.get("productVariantsBulkUpdate") or {}
    errors = payload.get("userErrors") or []
    if errors:
        details = "; ".join(
            f"{'.'.join(str(part) for part in error.get('field') or [])}: "
            f"{error.get('message')} ({error.get('code')})"
            for error in errors
        )
        raise RuntimeError(f"Shopify rejected productVariantsBulkUpdate: {details}")
    return payload.get("productVariants") or []


def verify_prices(
    endpoint: str,
    token: str,
    page_size: int,
    expected: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    live_by_id = {
        variant["id"]: variant
        for variant in get_shopify_price_variants(endpoint, token, page_size)
    }
    failures = []
    for update in expected:
        live = live_by_id.get(update["shopifyVariantId"])
        actual = parse_money((live or {}).get("price"))
        desired = Decimal(update["desiredPrice"])
        if actual != desired:
            failures.append({
                "shopifyVariantId": update["shopifyVariantId"],
                "handle": update["handle"],
                "expectedPrice": update["desiredPrice"],
                "actualPrice": (
                    format(actual, ".2f")
                    if actual is not None
                    else None
                ),
            })
    return failures


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
            "Reconcile Shopify variant prices from a Lightspeed C-Series "
            "product export."
        )
    )
    parser.add_argument("--products-source", required=True, help="Path to the Lightspeed products CSV export.")
    parser.add_argument("--apply", action="store_true", help="Write price changes to Shopify. Dry-run is default.")
    parser.add_argument("--shop", help="Shopify store domain.")
    parser.add_argument("--token", help="Shopify Admin access token.")
    parser.add_argument("--api-key", help="Shopify app client ID/API key.")
    parser.add_argument("--api-secret", help="Shopify app client secret.")
    parser.add_argument("--prefer-client-credentials", action="store_true", help="Use client_credentials even when an admin token is set.")
    parser.add_argument("--api-version", default=DEFAULT_API_VERSION, help="Shopify Admin API version.")
    parser.add_argument("--page-size", type=positive_int, default=50, help="Shopify product page size.")
    parser.add_argument(
        "--include-retired-catalog-items",
        action="store_true",
        help="Include deliberately retired brands and test products.",
    )
    parser.add_argument(
        "--max-source-age-hours",
        type=float,
        default=DEFAULT_MAX_SOURCE_AGE_HOURS,
        help=f"Refuse --apply when the CSV is older than this. Default: {DEFAULT_MAX_SOURCE_AGE_HOURS:g}.",
    )
    parser.add_argument(
        "--allow-stale-source",
        action="store_true",
        help="Override the source-age guard after explicit review.",
    )
    parser.add_argument("--continue-on-error", action="store_true", help="Continue applying remaining products after an error.")
    parser.add_argument("--env-file", help="Path to .env. Default: repository .env.")
    parser.add_argument("--plan-file", default=str(project_root / DEFAULT_PLAN_FILE), help="JSON plan output path.")
    parser.add_argument("--results-file", default=str(project_root / DEFAULT_RESULTS_FILE), help="JSON apply results output path.")
    args = parser.parse_args()

    if args.page_size > 250:
        raise SystemExit("--page-size must be between 1 and 250.")
    if args.max_source_age_hours <= 0:
        raise SystemExit("--max-source-age-hours must be positive.")

    products_source = Path(args.products_source)
    source_file = source_metadata(products_source)
    source_rows = read_csv(products_source)
    retired_exclusions = {"products": 0, "variants": 0, "inventoryRows": 0}
    if not args.include_retired_catalog_items:
        source_rows, _, retired_exclusions = filter_retired_catalog_rows(
            source_rows,
            [],
        )
    source_rows, _, invalid_exclusions = filter_invalid_catalog_rows(
        source_rows,
        [],
    )
    source_mapping, mapping_warnings = build_product_mapping(source_rows)

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
    endpoint = f"https://{shop}/admin/api/{api_version}/graphql.json"
    if args.prefer_client_credentials or not token:
        print("Getting temporary Shopify Admin token with client_credentials.")
        token = get_client_credentials_token(shop, client_id, client_secret)

    print(f"{'Apply' if args.apply else 'Dry run'}: syncing Lightspeed prices to Shopify on {shop}")
    shopify_variants = get_shopify_price_variants(
        endpoint,
        token,
        args.page_size,
    )
    plan = build_price_plan(
        source_mapping,
        shopify_variants,
        source_file,
        retired_exclusions,
        invalid_exclusions,
        args.max_source_age_hours,
    )
    plan["mode"] = "apply" if args.apply else "dry-run"
    if mapping_warnings:
        plan["mappingWarnings"] = mapping_warnings
        plan["summary"]["mappingWarningHandles"] = len(mapping_warnings)
    write_json(Path(args.plan_file), plan)

    summary = plan["summary"]
    print(f"Source variants: {summary['sourceVariants']}")
    print(f"Shopify variants scanned: {summary['shopifyVariantsScanned']}")
    print(f"Price updates needed: {summary['updates']}")
    print(f"Already correct: {summary['unchanged']}")
    print(f"Conflicts: {summary['conflicts']}")
    print(f"Retired products excluded: {summary['retiredProductsExcluded']}")
    print(f"Invalid catalog products excluded: {summary['invalidProductsExcluded']}")
    print(f"Source file fresh: {summary['sourcesFresh']}")
    for update in plan["updates"][:15]:
        print(
            f"- {update['title']} ({update['handle']}) / "
            f"{update['sourceVariant']}: {update['currentPrice']} -> "
            f"{update['desiredPrice']}"
        )
    if len(plan["updates"]) > 15:
        print(f"...and {len(plan['updates']) - 15} more.")
    print(f"\nPlan written: {Path(args.plan_file)}")

    if plan["conflicts"]:
        raise SystemExit("Price plan has unmatched, duplicate or invalid variants.")
    if not args.apply:
        print("No price changes were written. Export fresh source data before applying.")
        return
    if not summary["sourcesFresh"] and not args.allow_stale_source:
        raise SystemExit(
            "Refusing --apply because the source file is stale. Export a fresh "
            "file or use --allow-stale-source after explicit review."
        )

    by_product: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for update in plan["updates"]:
        by_product[update["productId"]].append(update)
    results = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "updated": [],
        "errors": [],
        "verificationFailures": [],
    }
    successful_updates = []
    for index, (product_id, updates) in enumerate(by_product.items(), start=1):
        try:
            updated_variants = update_product_variant_prices(
                endpoint,
                token,
                product_id,
                updates,
            )
            results["updated"].extend(updated_variants)
            successful_updates.extend(updates)
            print(
                f"Updated product {index}/{len(by_product)}: "
                f"{updates[0]['handle']} ({len(updates)} variants)"
            )
        except Exception as exc:
            results["errors"].append({
                "productId": product_id,
                "handle": updates[0]["handle"],
                "variantCount": len(updates),
                "message": str(exc),
            })
            if not args.continue_on_error:
                write_json(Path(args.results_file), results)
                raise
        time.sleep(0.05)

    results["verificationFailures"] = verify_prices(
        endpoint,
        token,
        args.page_size,
        successful_updates,
    )
    write_json(Path(args.results_file), results)
    print(f"\nResults written: {Path(args.results_file)}")
    print(f"Variants updated: {len(results['updated'])}")
    print(f"Errors: {len(results['errors'])}")
    print(f"Verification failures: {len(results['verificationFailures'])}")
    if results["verificationFailures"]:
        raise SystemExit("Post-sync price verification failed.")


if __name__ == "__main__":
    main()
