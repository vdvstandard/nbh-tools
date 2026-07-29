#!/usr/bin/env python3

import argparse
import copy
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from catalog_policy import is_retired_product
from lightspeed_product_import import (
    LIGHTSPEED_VARIANT_ID_KEY,
    SourceProduct,
    build_source_products,
    read_lightspeed_rows,
)
from lightspeed_variant_sync import (
    DEFAULT_API_VERSION,
    DEFAULT_MAX_SOURCE_AGE_HOURS,
    clean,
    get_client_credentials_token,
    load_dotenv,
    normalize_option_value,
    normalize_shop,
    positive_int,
    shopify_graphql,
    source_metadata,
)


DEFAULT_PLAN_FILE = Path(".tmp") / "lightspeed-variant-sync-plan.json"
DEFAULT_RESULTS_FILE = Path(".tmp") / "lightspeed-variant-sync-results.json"


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
            query ProductsForVariantSync($first: Int!, $after: String) {
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
        products.extend(connection.get("nodes") or [])
        page_info = connection.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        after = page_info.get("endCursor")
    return products


def source_variant_id(variant: Dict[str, Any]) -> str:
    for metafield in variant.get("metafields") or []:
        if metafield.get("key") == LIGHTSPEED_VARIANT_ID_KEY:
            return clean(metafield.get("value"))
    return ""


def source_variant_combo(variant: Dict[str, Any]) -> Tuple[str, ...]:
    return tuple(
        normalize_option_value(value.get("name"))
        for value in variant.get("optionValues") or []
    )


def live_variant_combo(variant: Dict[str, Any]) -> Tuple[str, ...]:
    return tuple(
        normalize_option_value(option.get("value"))
        for option in variant.get("selectedOptions") or []
    )


def essential_quality_issues(product: SourceProduct) -> List[str]:
    return [
        issue
        for issue in product.quality_issues
        if issue != "missing_image"
    ]


def merge_product_options(
    product: SourceProduct,
    orphan_variants: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    option_values = {
        option["name"]: [
            value["name"]
            for value in option.get("values") or []
        ]
        for option in product.options
    }
    option_order = [option["name"] for option in product.options]
    for variant in orphan_variants:
        for option in variant.get("selectedOptions") or []:
            name = clean(option.get("name"))
            value = clean(option.get("value"))
            if name not in option_values:
                option_values[name] = []
                option_order.append(name)
            if value and value not in option_values[name]:
                option_values[name].append(value)
    return [
        {
            "name": name,
            "position": index,
            "values": [
                {"name": value}
                for value in option_values[name]
            ],
        }
        for index, name in enumerate(option_order, start=1)
    ]


def orphan_variant_input(variant: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": variant["id"],
        "optionValues": [
            {
                "optionName": option["name"],
                "name": option["value"],
            }
            for option in variant.get("selectedOptions") or []
        ],
    }


def build_variant_plan(
    source_products: List[SourceProduct],
    shopify_products: List[Dict[str, Any]],
    source_file: Dict[str, Any],
    max_source_age_hours: float,
) -> Dict[str, Any]:
    live_by_handle = {
        product["handle"]: product
        for product in shopify_products
        if product.get("handle")
    }
    global_live_identities = defaultdict(list)
    for product in shopify_products:
        for variant in (product.get("variants") or {}).get("nodes") or []:
            identity = clean(
                (variant.get("lightspeedVariantId") or {}).get("value")
            )
            if identity:
                global_live_identities[identity].append({
                    "productId": product.get("id"),
                    "handle": product.get("handle"),
                    "variantId": variant.get("id"),
                })

    changes = []
    unchanged = []
    missing_products = []
    invalid_products = []
    orphan_variants = []
    conflicts = []
    source_identity_counts = Counter(
        source_variant_id(variant)
        for product in source_products
        for variant in product.variants
        if source_variant_id(variant)
    )

    for product in source_products:
        if is_retired_product(product.handle, product.vendor):
            continue
        quality_issues = essential_quality_issues(product)
        if quality_issues:
            invalid_products.append({
                "handle": product.handle,
                "internalId": product.internal_id,
                "reasons": quality_issues,
            })
            continue
        live_product = live_by_handle.get(product.handle)
        if not live_product:
            missing_products.append({
                "handle": product.handle,
                "internalId": product.internal_id,
                "variantCount": len(product.variants),
            })
            continue

        live_variants = (
            (live_product.get("variants") or {}).get("nodes") or []
        )
        live_by_identity = defaultdict(list)
        live_by_combo = defaultdict(list)
        for variant in live_variants:
            identity = clean(
                (variant.get("lightspeedVariantId") or {}).get("value")
            )
            if identity:
                live_by_identity[identity].append(variant)
            live_by_combo[live_variant_combo(variant)].append(variant)

        assignments = []
        assigned_live_ids = set()
        product_conflicts = []
        for source_variant in product.variants:
            identity = source_variant_id(source_variant)
            combo = source_variant_combo(source_variant)
            if not identity:
                product_conflicts.append({
                    "handle": product.handle,
                    "reason": "missing_source_variant_identity",
                    "combo": combo,
                })
                continue
            if source_identity_counts[identity] > 1:
                product_conflicts.append({
                    "handle": product.handle,
                    "lightspeedVariantId": identity,
                    "reason": "duplicate_source_variant_identity",
                })
                continue
            global_matches = global_live_identities.get(identity) or []
            if global_matches and any(
                match["productId"] != live_product["id"]
                for match in global_matches
            ):
                product_conflicts.append({
                    "handle": product.handle,
                    "lightspeedVariantId": identity,
                    "reason": "identity_belongs_to_other_product",
                    "matches": global_matches,
                })
                continue

            identity_matches = live_by_identity.get(identity) or []
            if len(identity_matches) > 1:
                product_conflicts.append({
                    "handle": product.handle,
                    "lightspeedVariantId": identity,
                    "reason": "duplicate_live_variant_identity",
                    "matches": [
                        match["id"] for match in identity_matches
                    ],
                })
                continue
            live_variant = identity_matches[0] if identity_matches else None
            matched_by = "identity" if live_variant else None
            if not live_variant:
                combo_matches = [
                    match
                    for match in live_by_combo.get(combo) or []
                    if match["id"] not in assigned_live_ids
                ]
                if len(combo_matches) > 1:
                    product_conflicts.append({
                        "handle": product.handle,
                        "lightspeedVariantId": identity,
                        "reason": "duplicate_live_option_combo",
                        "matches": [
                            match["id"] for match in combo_matches
                        ],
                    })
                    continue
                if combo_matches:
                    live_variant = combo_matches[0]
                    matched_by = "options"
            if live_variant and live_variant["id"] in assigned_live_ids:
                product_conflicts.append({
                    "handle": product.handle,
                    "lightspeedVariantId": identity,
                    "reason": "live_variant_matched_twice",
                    "shopifyVariantId": live_variant["id"],
                })
                continue
            if live_variant:
                assigned_live_ids.add(live_variant["id"])
            assignments.append({
                "identity": identity,
                "source": source_variant,
                "live": live_variant,
                "matchedBy": matched_by or "new",
                "optionChanged": bool(
                    live_variant
                    and live_variant_combo(live_variant) != combo
                ),
            })

        if product_conflicts:
            conflicts.extend(product_conflicts)
            continue

        product_orphans = [
            variant
            for variant in live_variants
            if variant["id"] not in assigned_live_ids
        ]
        orphan_variants.extend([
            {
                "productId": live_product["id"],
                "handle": product.handle,
                "shopifyVariantId": variant["id"],
                "title": variant.get("title"),
                "lightspeedVariantId": clean(
                    (variant.get("lightspeedVariantId") or {}).get("value")
                ) or None,
            }
            for variant in product_orphans
        ])
        creates = [
            assignment
            for assignment in assignments
            if not assignment["live"]
        ]
        option_updates = [
            assignment
            for assignment in assignments
            if assignment["optionChanged"]
        ]
        identity_updates = [
            assignment
            for assignment in assignments
            if assignment["live"]
            and not clean(
                (
                    assignment["live"].get("lightspeedVariantId")
                    or {}
                ).get("value")
            )
        ]
        if not creates and not option_updates and not identity_updates:
            unchanged.append({
                "productId": live_product["id"],
                "handle": product.handle,
                "variantCount": len(assignments),
                "orphanVariantCount": len(product_orphans),
            })
            continue

        variant_inputs = []
        assignment_plan = []
        for assignment in assignments:
            variant_input = copy.deepcopy(assignment["source"])
            live_variant = assignment["live"]
            if live_variant:
                variant_input["id"] = live_variant["id"]
                variant_input.pop("metafields", None)
            variant_inputs.append(variant_input)
            assignment_plan.append({
                "lightspeedVariantId": assignment["identity"],
                "shopifyVariantId": (
                    live_variant.get("id")
                    if live_variant
                    else None
                ),
                "matchedBy": assignment["matchedBy"],
                "currentOptions": (
                    live_variant.get("selectedOptions")
                    if live_variant
                    else None
                ),
                "desiredOptions": assignment["source"]["optionValues"],
                "action": (
                    "create"
                    if not live_variant
                    else "update_options"
                    if assignment["optionChanged"]
                    else "set_identity"
                    if not clean(
                        (
                            live_variant.get("lightspeedVariantId")
                            or {}
                        ).get("value")
                    )
                    else "unchanged"
                ),
            })
        variant_inputs.extend(
            orphan_variant_input(variant)
            for variant in product_orphans
        )
        changes.append({
            "productId": live_product["id"],
            "handle": product.handle,
            "title": product.title,
            "summary": {
                "creates": len(creates),
                "optionUpdates": len(option_updates),
                "identityUpdates": len(identity_updates),
                "orphansPreserved": len(product_orphans),
            },
            "assignments": assignment_plan,
            "preservedOrphans": [
                {
                    "shopifyVariantId": variant["id"],
                    "title": variant.get("title"),
                }
                for variant in product_orphans
            ],
            "productSetInput": {
                "productOptions": merge_product_options(
                    product,
                    product_orphans,
                ),
                "variants": variant_inputs,
            },
        })

    sources_fresh = source_file["ageHours"] <= max_source_age_hours
    return {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "mode": "dry-run",
        "sourceFile": source_file,
        "summary": {
            "sourceProducts": len(source_products),
            "shopifyProductsScanned": len(shopify_products),
            "changedProducts": len(changes),
            "variantsToCreate": sum(
                change["summary"]["creates"] for change in changes
            ),
            "variantOptionUpdates": sum(
                change["summary"]["optionUpdates"] for change in changes
            ),
            "variantIdentityUpdates": sum(
                change["summary"]["identityUpdates"] for change in changes
            ),
            "orphanVariantsPreserved": len(orphan_variants),
            "missingProducts": len(missing_products),
            "invalidProducts": len(invalid_products),
            "conflicts": len(conflicts),
            "sourcesFresh": sources_fresh,
            "safeToApply": (
                sources_fresh
                and not conflicts
                and not missing_products
            ),
        },
        "changes": changes,
        "unchanged": unchanged,
        "missingProducts": missing_products,
        "invalidProducts": invalid_products,
        "orphanVariants": orphan_variants,
        "conflicts": conflicts,
    }


def apply_product_set(
    endpoint: str,
    token: str,
    change: Dict[str, Any],
) -> Dict[str, Any]:
    data = shopify_graphql(
        endpoint,
        token,
        """
        mutation ReconcileLightspeedVariants(
          $identifier: ProductSetIdentifiers,
          $input: ProductSetInput!,
          $synchronous: Boolean!
        ) {
          productSet(
            identifier: $identifier,
            input: $input,
            synchronous: $synchronous
          ) {
            product {
              id
              handle
              variants(first: 100) {
                nodes {
                  id
                  title
                }
              }
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
            "identifier": {"id": change["productId"]},
            "input": change["productSetInput"],
            "synchronous": True,
        },
    )
    payload = data.get("productSet") or {}
    errors = payload.get("userErrors") or []
    if errors:
        details = "; ".join(
            f"{'.'.join(str(part) for part in error.get('field') or [])}: "
            f"{error.get('message')} ({error.get('code')})"
            for error in errors
        )
        raise RuntimeError(f"Shopify rejected productSet: {details}")
    return payload.get("product") or {}


def verify_variant_changes(
    live_products: List[Dict[str, Any]],
    changes: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    live_by_handle = {
        product["handle"]: product
        for product in live_products
        if product.get("handle")
    }
    failures = []
    for change in changes:
        product = live_by_handle.get(change["handle"])
        variants = (
            (product or {}).get("variants") or {}
        ).get("nodes") or []
        by_identity = {
            clean(
                (variant.get("lightspeedVariantId") or {}).get("value")
            ): variant
            for variant in variants
            if clean(
                (variant.get("lightspeedVariantId") or {}).get("value")
            )
        }
        expected_orphan_ids = {
            item["shopifyVariantId"]
            for item in change.get("preservedOrphans") or []
        }
        live_ids = {variant["id"] for variant in variants}
        for assignment in change["assignments"]:
            live = by_identity.get(assignment["lightspeedVariantId"])
            desired_combo = tuple(
                normalize_option_value(option["name"])
                for option in assignment["desiredOptions"]
            )
            if not live:
                failures.append({
                    "handle": change["handle"],
                    "lightspeedVariantId": assignment[
                        "lightspeedVariantId"
                    ],
                    "reason": "missing_identity_after_sync",
                })
            elif live_variant_combo(live) != desired_combo:
                failures.append({
                    "handle": change["handle"],
                    "lightspeedVariantId": assignment[
                        "lightspeedVariantId"
                    ],
                    "reason": "option_mismatch_after_sync",
                })
        for orphan_id in expected_orphan_ids - live_ids:
            failures.append({
                "handle": change["handle"],
                "shopifyVariantId": orphan_id,
                "reason": "preserved_orphan_was_deleted",
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
            "Reconcile existing Shopify variant structure from Lightspeed "
            "without deleting orphan variants."
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
    parser.add_argument("--page-size", type=positive_int, default=50)
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
    rows = read_lightspeed_rows(source_path)
    source_products, source_warnings = build_source_products(
        rows,
        "us",
        "DRAFT",
        False,
        False,
        None,
        True,
        True,
    )
    if source_warnings:
        raise SystemExit("Source product mapping contains warnings.")

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
    plan = build_variant_plan(
        source_products,
        live_products,
        source_metadata(source_path),
        args.max_source_age_hours,
    )
    plan["mode"] = "apply" if args.apply else "dry-run"
    write_json(Path(args.plan_file), plan)
    print(json.dumps(plan["summary"], indent=2))
    for change in plan["changes"][:10]:
        print(
            f"- {change['handle']}: +{change['summary']['creates']} "
            f"variants, {change['summary']['optionUpdates']} option updates, "
            f"{change['summary']['orphansPreserved']} orphans preserved"
        )
    print(f"Plan written: {Path(args.plan_file)}")

    if plan["conflicts"]:
        raise SystemExit("Variant plan has identity or option conflicts.")
    if plan["missingProducts"]:
        raise SystemExit("Variant plan has source products missing in Shopify.")
    if not args.apply:
        print("No variant changes were written.")
        return
    if not plan["summary"]["safeToApply"]:
        raise SystemExit("Refusing --apply because the source is not safe.")

    results = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "updatedProducts": [],
        "verificationFailures": [],
    }
    for index, change in enumerate(plan["changes"], start=1):
        updated = apply_product_set(endpoint, token, change)
        results["updatedProducts"].append(updated)
        print(
            f"Updated {index}/{len(plan['changes'])}: "
            f"{change['handle']}"
        )
    live_after = get_shopify_products(endpoint, token, args.page_size)
    results["verificationFailures"] = verify_variant_changes(
        live_after,
        plan["changes"],
    )
    write_json(Path(args.results_file), results)
    print(f"Results written: {Path(args.results_file)}")
    print(f"Updated products: {len(results['updatedProducts'])}")
    print(f"Verification failures: {len(results['verificationFailures'])}")
    if results["verificationFailures"]:
        raise SystemExit("Post-sync variant verification failed.")


if __name__ == "__main__":
    main()
