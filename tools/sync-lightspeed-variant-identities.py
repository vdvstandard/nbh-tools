#!/usr/bin/env python3

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from lightspeed_variant_sync import (
    DEFAULT_API_VERSION,
    DEFAULT_MAX_SOURCE_AGE_HOURS,
    build_product_mapping,
    build_shopify_variant_index,
    clean,
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
LIGHTSPEED_VARIANT_ID_KEY = "lightspeed_c_series_variant_id"
DEFAULT_PLAN_FILE = Path(".tmp") / "lightspeed-variant-identity-plan.json"
DEFAULT_RESULTS_FILE = Path(".tmp") / "lightspeed-variant-identity-results.json"
METAFIELDS_BATCH_SIZE = 25


def get_shopify_variants(
    endpoint: str,
    token: str,
    page_size: int,
) -> List[Dict[str, Any]]:
    variants = []
    after = None
    while True:
        data = shopify_graphql(
            endpoint,
            token,
            """
            query VariantsForLightspeedIdentity(
              $first: Int!,
              $after: String
            ) {
              products(first: $first, after: $after, sortKey: ID) {
                nodes {
                  id
                  title
                  handle
                  variants(first: 100) {
                    nodes {
                      id
                      title
                      selectedOptions {
                        name
                        value
                      }
                      lightspeedVariantId: metafield(
                        namespace: "custom",
                        key: "lightspeed_c_series_variant_id"
                      ) {
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


def build_identity_plan(
    mapping: Dict[str, Dict[str, Any]],
    current_variant_ids: set,
    shopify_variants: List[Dict[str, Any]],
    mapping_source: Dict[str, Any],
    current_source: Dict[str, Any],
    max_source_age_hours: float,
) -> Dict[str, Any]:
    shopify_index = build_shopify_variant_index(shopify_variants)
    updates = []
    unchanged = []
    no_longer_current = []
    conflicts = []
    for lightspeed_variant_id, source in mapping.items():
        if lightspeed_variant_id not in current_variant_ids:
            no_longer_current.append({
                "lightspeedVariantId": lightspeed_variant_id,
                "handle": source["handle"],
                "sourceVariant": source["variant"],
            })
            continue
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
        match = matches[0]
        existing_value = clean(
            (match.get("lightspeedVariantId") or {}).get("value")
        )
        item = {
            "lightspeedVariantId": lightspeed_variant_id,
            "shopifyVariantId": match["id"],
            "handle": source["handle"],
            "title": source["title"],
            "sourceVariant": source["variant"],
            "existingValue": existing_value or None,
        }
        if existing_value and existing_value != lightspeed_variant_id:
            conflicts.append({
                **item,
                "reason": "existing_identity_mismatch",
            })
        elif existing_value == lightspeed_variant_id:
            unchanged.append(item)
        else:
            updates.append(item)

    current_fresh = current_source["ageHours"] <= max_source_age_hours
    return {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "mode": "dry-run",
        "mappingSource": mapping_source,
        "currentSource": current_source,
        "summary": {
            "mappingVariants": len(mapping),
            "currentVariantIds": len(current_variant_ids),
            "shopifyVariantsScanned": len(shopify_variants),
            "updates": len(updates),
            "unchanged": len(unchanged),
            "noLongerCurrent": len(no_longer_current),
            "conflicts": len(conflicts),
            "currentSourceFresh": current_fresh,
            "safeToApply": current_fresh and not conflicts,
        },
        "updates": updates,
        "unchanged": unchanged[:200],
        "noLongerCurrent": no_longer_current,
        "conflicts": conflicts,
    }


def set_variant_identities(
    endpoint: str,
    token: str,
    updates: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    data = shopify_graphql(
        endpoint,
        token,
        """
        mutation SetLightspeedVariantIdentities(
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
                    "ownerId": update["shopifyVariantId"],
                    "namespace": LIGHTSPEED_NAMESPACE,
                    "key": LIGHTSPEED_VARIANT_ID_KEY,
                    "type": "single_line_text_field",
                    "value": update["lightspeedVariantId"],
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
            "Backfill persistent Lightspeed variant IDs by exact historical "
            "handle and option matching."
        )
    )
    parser.add_argument("--mapping-source", required=True, help="Historical product CSV that exactly matches current Shopify variants.")
    parser.add_argument("--current-source", required=True, help="Fresh product CSV used to confirm that mapped variant IDs still exist.")
    parser.add_argument("--apply", action="store_true", help="Write variant identity metafields. Dry-run is default.")
    parser.add_argument("--shop", help="Shopify store domain.")
    parser.add_argument("--token", help="Shopify Admin access token.")
    parser.add_argument("--api-key", help="Shopify app client ID/API key.")
    parser.add_argument("--api-secret", help="Shopify app client secret.")
    parser.add_argument("--prefer-client-credentials", action="store_true", help="Use app client_credentials.")
    parser.add_argument("--api-version", default=DEFAULT_API_VERSION)
    parser.add_argument("--page-size", type=positive_int, default=50)
    parser.add_argument(
        "--max-source-age-hours",
        type=float,
        default=DEFAULT_MAX_SOURCE_AGE_HOURS,
    )
    parser.add_argument("--env-file", help="Path to .env.")
    parser.add_argument("--plan-file", default=str(project_root / DEFAULT_PLAN_FILE))
    parser.add_argument("--results-file", default=str(project_root / DEFAULT_RESULTS_FILE))
    args = parser.parse_args()

    if args.page_size > 250:
        raise SystemExit("--page-size must be between 1 and 250.")
    mapping_path = Path(args.mapping_source)
    current_path = Path(args.current_source)
    mapping_rows = read_csv(mapping_path)
    current_rows = read_csv(current_path)
    mapping_rows, _, _ = filter_retired_catalog_rows(mapping_rows, [])
    current_rows, _, _ = filter_retired_catalog_rows(current_rows, [])
    mapping, mapping_warnings = build_product_mapping(mapping_rows)
    current_mapping, current_warnings = build_product_mapping(current_rows)
    if mapping_warnings or current_warnings:
        raise SystemExit("Source option mappings contain duplicate combinations.")

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
    shopify_variants = get_shopify_variants(endpoint, token, args.page_size)
    plan = build_identity_plan(
        mapping,
        set(current_mapping),
        shopify_variants,
        source_metadata(mapping_path),
        source_metadata(current_path),
        args.max_source_age_hours,
    )
    plan["mode"] = "apply" if args.apply else "dry-run"
    write_json(Path(args.plan_file), plan)
    summary = plan["summary"]
    print(json.dumps(summary, indent=2))
    print(f"Plan written: {Path(args.plan_file)}")

    if plan["conflicts"]:
        raise SystemExit("Variant identity plan has matching conflicts.")
    if not args.apply:
        print("No variant identities were written.")
        return
    if not summary["safeToApply"]:
        raise SystemExit("Refusing --apply because the current source is stale.")

    results = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "updated": [],
        "verificationFailures": [],
    }
    batches = list(chunked(plan["updates"], METAFIELDS_BATCH_SIZE))
    for index, batch in enumerate(batches, start=1):
        results["updated"].extend(
            set_variant_identities(endpoint, token, batch)
        )
        print(f"Identity batch {index}/{len(batches)}: {len(batch)} variants")

    live_after = {
        variant["id"]: clean(
            (variant.get("lightspeedVariantId") or {}).get("value")
        )
        for variant in get_shopify_variants(
            endpoint,
            token,
            args.page_size,
        )
    }
    for update in plan["updates"]:
        actual = live_after.get(update["shopifyVariantId"])
        if actual != update["lightspeedVariantId"]:
            results["verificationFailures"].append({
                "shopifyVariantId": update["shopifyVariantId"],
                "expected": update["lightspeedVariantId"],
                "actual": actual,
            })
    write_json(Path(args.results_file), results)
    print(f"Results written: {Path(args.results_file)}")
    print(f"Updated: {len(results['updated'])}")
    print(f"Verification failures: {len(results['verificationFailures'])}")
    if results["verificationFailures"]:
        raise SystemExit("Variant identity verification failed.")


if __name__ == "__main__":
    main()
