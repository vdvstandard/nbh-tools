#!/usr/bin/env python3

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from catalog_policy import is_retired_product


DEFAULT_API_VERSION = "2026-04"
DEFAULT_LOCATION_NAME = "Neighbourhood Store"
DEFAULT_PLAN_FILE = Path(".tmp") / "lightspeed-inventory-sync-plan.json"
DEFAULT_RESULTS_FILE = Path(".tmp") / "lightspeed-inventory-sync-results.json"
INVENTORY_BATCH_SIZE = 50
DEFAULT_MAX_SOURCE_AGE_HOURS = 24.0


def load_dotenv(file_path: Path) -> None:
    if not file_path.exists():
        return
    for line in file_path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$", line)
        if match and match.group(1) not in os.environ:
            os.environ[match.group(1)] = parse_env_value(match.group(2))


def parse_env_value(value: str) -> str:
    trimmed = value.strip()
    if (trimmed.startswith('"') and trimmed.endswith('"')) or (trimmed.startswith("'") and trimmed.endswith("'")):
        return trimmed[1:-1]
    return trimmed


def normalize_shop(value: str) -> str:
    cleaned = (value or "").strip().replace("http://", "").replace("https://", "").split("/")[0]
    if not cleaned:
        return ""
    return cleaned if "." in cleaned else f"{cleaned}.myshopify.com"


def http_post_json(url: str, payload: Dict[str, Any], headers: Dict[str, str], timeout: int = 60) -> Tuple[str, int, Dict[str, str]]:
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8"), response.status, dict(response.getheaders())
    except urllib.error.HTTPError as exc:
        return exc.read().decode("utf-8", errors="replace"), exc.code, dict(exc.headers.items())


def http_post_form(url: str, data: Dict[str, Any], headers: Dict[str, str], timeout: int = 60) -> Tuple[str, int, Dict[str, str]]:
    request = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8"), response.status, dict(response.getheaders())
    except urllib.error.HTTPError as exc:
        return exc.read().decode("utf-8", errors="replace"), exc.code, dict(exc.headers.items())


def get_client_credentials_token(shop: str, client_id: str, client_secret: str) -> str:
    response_text, status, _ = http_post_form(
        f"https://{shop}/admin/oauth/access_token",
        {"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret},
        {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
    )
    data = json.loads(response_text or "{}")
    if status < 200 or status >= 300:
        raise RuntimeError(f"Shopify client_credentials failed ({status}): {response_text}")
    token = data.get("access_token")
    if not token:
        raise RuntimeError("Shopify did not return an access_token.")
    print(f"Temporary token acquired. Scope: {data.get('scope', '(not returned)')}. Not stored in .env.")
    return token


def shopify_graphql(endpoint: str, token: str, query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
    for attempt in range(1, 5):
        response_text, status, headers = http_post_json(
            endpoint,
            {"query": query, "variables": variables},
            {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-Shopify-Access-Token": token,
            },
        )
        try:
            payload = json.loads(response_text or "{}")
        except json.JSONDecodeError:
            payload = {"raw": response_text}
        if status == 429 or status >= 500:
            if attempt < 4:
                time.sleep(int(headers.get("Retry-After", "0") or "0") or attempt)
                continue
        if status < 200 or status >= 300:
            raise RuntimeError(f"Shopify API request failed with HTTP {status}: {json.dumps(payload)}")
        if payload.get("errors"):
            raise RuntimeError("Shopify GraphQL errors: " + "; ".join(error.get("message", "") for error in payload["errors"]))
        wait_for_throttle_budget(payload.get("extensions", {}).get("cost", {}).get("throttleStatus"))
        return payload.get("data") or {}
    raise RuntimeError("Shopify API request failed after retries.")


def wait_for_throttle_budget(throttle_status: Optional[Dict[str, Any]]) -> None:
    if not throttle_status:
        return
    currently_available = float(throttle_status.get("currentlyAvailable", 0))
    restore_rate = float(throttle_status.get("restoreRate", 0))
    if currently_available < 150 and restore_rate > 0:
        time.sleep(max(((150 - currently_available) / restore_rate), 0.25))


def read_csv(path: Path) -> List[Dict[str, str]]:
    last_error: Optional[Exception] = None
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            with path.open(newline="", encoding=encoding) as csv_file:
                return list(csv.DictReader(csv_file, delimiter=";"))
        except UnicodeDecodeError as exc:
            last_error = exc
    raise RuntimeError(f"Could not read {path}: {last_error}")


def source_metadata(path: Path) -> Dict[str, Any]:
    modified_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    age_hours = max(
        (datetime.now(timezone.utc) - modified_at).total_seconds() / 3600,
        0,
    )
    digest = hashlib.sha256()
    with path.open("rb") as source_file:
        for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "path": str(path.resolve()),
        "modifiedAt": modified_at.isoformat(),
        "ageHours": round(age_hours, 2),
        "sha256": digest.hexdigest(),
    }


def filter_retired_catalog_rows(
    product_rows: List[Dict[str, str]],
    inventory_rows: List[Dict[str, str]],
) -> Tuple[List[Dict[str, str]], List[Dict[str, str]], Dict[str, int]]:
    retained_products = []
    retired_product_ids = set()
    retired_variant_ids = set()
    for row in product_rows:
        handle = normalize_handle(
            first_nonempty(
                clean(row.get("US_URL")),
                clean(row.get("NL_URL")),
                clean(row.get("US_Title_Short")),
                clean(row.get("NL_Title_Short")),
            )
        )
        if is_retired_product(handle, clean(row.get("Brand"))):
            internal_id = clean(row.get("Internal_ID"))
            variant_id = clean(row.get("Internal_Variant_ID"))
            if internal_id:
                retired_product_ids.add(internal_id)
            if variant_id:
                retired_variant_ids.add(variant_id)
            continue
        retained_products.append(row)

    retained_inventory = [
        row
        for row in inventory_rows
        if clean(row.get("Internal_Variant_ID")) not in retired_variant_ids
    ]
    return retained_products, retained_inventory, {
        "products": len(retired_product_ids),
        "variants": len(retired_variant_ids),
        "inventoryRows": len(inventory_rows) - len(retained_inventory),
    }


def filter_invalid_catalog_rows(
    product_rows: List[Dict[str, str]],
    inventory_rows: List[Dict[str, str]],
) -> Tuple[List[Dict[str, str]], List[Dict[str, str]], Dict[str, Any]]:
    grouped = defaultdict(list)
    for row in product_rows:
        grouped[clean(row.get("Internal_ID"))].append(row)

    excluded_products = []
    excluded_variant_ids = set()
    for internal_id, rows in grouped.items():
        reasons = []
        if not clean(rows[0].get("Brand")):
            reasons.append("missing_vendor")
        if any(
            parse_money(row.get("Price")) is None
            or parse_money(row.get("Price")) <= 0
            for row in rows
        ):
            reasons.append("missing_or_invalid_price")
        if any(
            not clean(row.get("Internal_Variant_ID"))
            for row in rows
        ):
            reasons.append("missing_external_variant_id")
        if not reasons:
            continue
        variant_ids = {
            clean(row.get("Internal_Variant_ID"))
            for row in rows
            if clean(row.get("Internal_Variant_ID"))
        }
        excluded_variant_ids.update(variant_ids)
        excluded_products.append({
            "internalId": internal_id,
            "handle": normalize_handle(
                first_nonempty(
                    clean(rows[0].get("US_URL")),
                    clean(rows[0].get("NL_URL")),
                    clean(rows[0].get("US_Title_Short")),
                    clean(rows[0].get("NL_Title_Short")),
                )
            ),
            "variantIds": sorted(variant_ids),
            "reasons": reasons,
        })

    retained_products = [
        row
        for row in product_rows
        if clean(row.get("Internal_Variant_ID")) not in excluded_variant_ids
    ]
    retained_inventory = [
        row
        for row in inventory_rows
        if clean(row.get("Internal_Variant_ID")) not in excluded_variant_ids
    ]
    return retained_products, retained_inventory, {
        "products": len(excluded_products),
        "variants": len(excluded_variant_ids),
        "inventoryRows": len(inventory_rows) - len(retained_inventory),
        "items": excluded_products,
    }


def build_product_mapping(product_rows: List[Dict[str, str]]) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, List[str]]]:
    by_product: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for row in product_rows:
        internal_id = clean(row.get("Internal_ID"))
        if internal_id:
            by_product[internal_id].append(row)

    mapping: Dict[str, Dict[str, Any]] = {}
    warnings: Dict[str, List[str]] = defaultdict(list)
    for internal_id, rows in by_product.items():
        first = rows[0]
        handle = normalize_handle(first_nonempty(clean(first.get("US_URL")), clean(first.get("NL_URL")), clean(first.get("US_Title_Short")), clean(first.get("NL_Title_Short"))))
        option_names = option_names_for_rows(rows)
        seen_combos = set()
        for row in rows:
            variant_id = clean(row.get("Internal_Variant_ID"))
            if not variant_id:
                continue
            pair_map = dict(parse_variant_pairs(first_nonempty(clean(row.get("US_Variant")), clean(row.get("NL_Variant")))))
            combo = tuple(normalize_option_value(pair_map.get(name) or "Default Title") for name in option_names)
            if combo in seen_combos:
                warnings[handle].append(f"Duplicate source option combo {combo}")
            seen_combos.add(combo)
            mapping[variant_id] = {
                "handle": handle,
                "title": first_nonempty(clean(row.get("US_Title_Short")), clean(row.get("NL_Title_Short")), clean(row.get("US_Title_Long")), clean(row.get("NL_Title_Long"))),
                "variant": first_nonempty(clean(row.get("US_Variant")), clean(row.get("NL_Variant"))),
                "optionNames": option_names,
                "combo": combo,
                "price": parse_money(row.get("Price")),
            }
    return mapping, warnings


def option_names_for_rows(rows: List[Dict[str, str]]) -> List[str]:
    option_names: List[str] = []
    for row in rows:
        pairs = parse_variant_pairs(first_nonempty(clean(row.get("US_Variant")), clean(row.get("NL_Variant"))))
        for name, _ in pairs:
            if name not in option_names:
                option_names.append(name)
    return option_names or ["Title"]


def parse_variant_pairs(value: str) -> List[Tuple[str, str]]:
    if not clean(value):
        return [("Title", "Default Title")]
    try:
        pieces = [piece.strip().strip('"') for piece in next(csv.reader([value], skipinitialspace=True))]
    except csv.Error:
        pieces = [value]
    pairs: List[Tuple[str, str]] = []
    for piece in pieces:
        piece = piece.strip()
        if not piece:
            continue
        if ":" in piece:
            raw_name, raw_value = piece.split(":", 1)
            pairs.append((collapse_space(raw_name) or "Option", collapse_space(raw_value) or "Default Title"))
        else:
            pairs.append(("Title", collapse_space(piece) or "Default Title"))
    return pairs or [("Title", "Default Title")]


def build_inventory_rows(inventory_rows: List[Dict[str, str]], product_mapping: Dict[str, Dict[str, Any]], clamp_negative: bool) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    mapped = []
    unmapped = []
    for index, row in enumerate(inventory_rows, start=2):
        variant_id = clean(row.get("Internal_Variant_ID"))
        stock = parse_stock(row.get("Stock_Level"))
        if stock is None:
            unmapped.append({"row": index, "variantId": variant_id, "reason": "invalid_stock", "stockValue": row.get("Stock_Level")})
            continue
        source_was_negative = stock < 0
        if clamp_negative and source_was_negative:
            stock = 0
        source = product_mapping.get(variant_id)
        if not source:
            unmapped.append({"row": index, "variantId": variant_id, "reason": "missing_product_export_mapping"})
            continue
        mapped.append({
            "row": index,
            "lightspeedVariantId": variant_id,
            "handle": source["handle"],
            "title": source["title"],
            "variant": source["variant"],
            "combo": source["combo"],
            "desiredQuantity": stock,
            "sourceWasNegative": source_was_negative,
            "stockTrack": clean(row.get("Stock_Track")).upper(),
            "visible": clean(row.get("Visible")).upper(),
        })
    return mapped, unmapped


def get_locations(endpoint: str, token: str) -> List[Dict[str, str]]:
    data = shopify_graphql(
        endpoint,
        token,
        """
        query InventoryLocations {
          locations(first: 100) {
            nodes {
              id
              name
            }
          }
        }
        """,
        {},
    )
    return data.get("locations", {}).get("nodes", [])


def get_shopify_variants(
    endpoint: str,
    token: str,
    page_size: int,
    location_id: str,
) -> List[Dict[str, Any]]:
    variants: List[Dict[str, Any]] = []
    after = None
    while True:
        data = shopify_graphql(
            endpoint,
            token,
            """
            query ProductsForInventory($first: Int!, $after: String, $locationId: ID!) {
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
                      inventoryItem {
                        id
                        tracked
                        inventoryLevel(locationId: $locationId) {
                          location {
                            id
                            name
                          }
                          quantities(names: ["available"]) {
                            name
                            quantity
                          }
                        }
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
            {
                "first": page_size,
                "after": after,
                "locationId": location_id,
            },
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


def build_shopify_variant_index(variants: List[Dict[str, Any]]) -> Dict[Tuple[str, Tuple[str, ...]], List[Dict[str, Any]]]:
    index: Dict[Tuple[str, Tuple[str, ...]], List[Dict[str, Any]]] = defaultdict(list)
    for variant in variants:
        handle = clean((variant.get("product") or {}).get("handle"))
        combo = tuple(normalize_option_value(option.get("value")) for option in variant.get("selectedOptions") or [])
        index[(handle, combo)].append(variant)
    return index


def available_at_location(variant: Dict[str, Any], location_id: str) -> Optional[int]:
    level = (variant.get("inventoryItem") or {}).get("inventoryLevel")
    if not level or (level.get("location") or {}).get("id") != location_id:
        return None
    for quantity in level.get("quantities") or []:
        if quantity.get("name") == "available":
            return quantity.get("quantity")
    return None


def has_location_level(variant: Dict[str, Any], location_id: str) -> bool:
    level = (variant.get("inventoryItem") or {}).get("inventoryLevel")
    return bool(
        level
        and (level.get("location") or {}).get("id") == location_id
    )


def build_plan(
    mapped_inventory: List[Dict[str, Any]],
    unmapped_inventory: List[Dict[str, Any]],
    shopify_variants: List[Dict[str, Any]],
    location: Dict[str, str],
    args: argparse.Namespace,
    source_files: Dict[str, Any],
    retired_exclusions: Dict[str, int],
    invalid_exclusions: Dict[str, Any],
) -> Dict[str, Any]:
    index = build_shopify_variant_index(shopify_variants)
    updates = []
    no_changes = []
    unmatched = list(unmapped_inventory)
    duplicate_matches = []
    negative_rows = []

    for item in mapped_inventory:
        matches = index.get((item["handle"], item["combo"]), [])
        if not matches:
            unmatched.append({
                "row": item["row"],
                "variantId": item["lightspeedVariantId"],
                "handle": item["handle"],
                "variant": item["variant"],
                "combo": item["combo"],
                "reason": "missing_shopify_variant_match",
            })
            continue
        if len(matches) > 1:
            duplicate_matches.append({
                "row": item["row"],
                "variantId": item["lightspeedVariantId"],
                "handle": item["handle"],
                "variant": item["variant"],
                "combo": item["combo"],
                "matches": [match.get("id") for match in matches],
            })
            continue
        match = matches[0]
        inventory_item = match.get("inventoryItem") or {}
        current_quantity = available_at_location(match, location["id"])
        tracked = bool(inventory_item.get("tracked"))
        has_level = has_location_level(match, location["id"])
        desired = item["desiredQuantity"]
        if item.get("sourceWasNegative"):
            negative_rows.append(item)
        update = {
            "lightspeedVariantId": item["lightspeedVariantId"],
            "handle": item["handle"],
            "title": item["title"],
            "sourceVariant": item["variant"],
            "productId": (match.get("product") or {}).get("id"),
            "shopifyVariantId": match.get("id"),
            "shopifyVariantTitle": match.get("title"),
            "inventoryItemId": inventory_item.get("id"),
            "tracked": tracked,
            "hasLocationLevel": has_level,
            "currentQuantity": current_quantity,
            "desiredQuantity": desired,
            "stockTrack": item["stockTrack"],
        }
        needs_tracking = item["stockTrack"] == "Y" and not tracked
        needs_quantity = current_quantity != desired
        needs_activation = not has_level
        if needs_tracking or needs_quantity or needs_activation:
            update["needsTracking"] = needs_tracking
            update["needsQuantity"] = needs_quantity
            update["needsActivation"] = needs_activation
            updates.append(update)
        else:
            no_changes.append(update)

    if args.max_updates is not None:
        updates = updates[: args.max_updates]

    return {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "mode": "apply" if args.apply else "dry-run",
        "location": location,
        "sourceFiles": source_files,
        "options": {
            "clampNegativeToZero": args.clamp_negative_to_zero,
            "includeRetiredCatalogItems": args.include_retired_catalog_items,
            "maxSourceAgeHours": args.max_source_age_hours,
            "maxUpdates": args.max_updates,
        },
        "summary": {
            "inventoryRows": len(mapped_inventory) + len(unmapped_inventory),
            "mappedInventoryRows": len(mapped_inventory),
            "shopifyVariantsScanned": len(shopify_variants),
            "updates": len(updates),
            "noChanges": len(no_changes),
            "unmatched": len(unmatched),
            "duplicateMatches": len(duplicate_matches),
            "negativeSourceQuantities": len(negative_rows),
            "needsTracking": sum(1 for update in updates if update.get("needsTracking")),
            "needsActivation": sum(1 for update in updates if update.get("needsActivation")),
            "needsQuantity": sum(1 for update in updates if update.get("needsQuantity")),
            "retiredProductsExcluded": retired_exclusions["products"],
            "retiredVariantsExcluded": retired_exclusions["variants"],
            "retiredInventoryRowsExcluded": retired_exclusions["inventoryRows"],
            "invalidProductsExcluded": invalid_exclusions["products"],
            "invalidVariantsExcluded": invalid_exclusions["variants"],
            "invalidInventoryRowsExcluded": invalid_exclusions["inventoryRows"],
            "sourcesFresh": all(
                metadata["ageHours"] <= args.max_source_age_hours
                for metadata in source_files.values()
            ),
        },
        "updates": updates,
        "noChanges": no_changes[:200],
        "unmatched": unmatched,
        "duplicateMatches": duplicate_matches,
        "negativeRows": negative_rows,
        "invalidCatalogItems": invalid_exclusions["items"],
    }


def apply_plan(endpoint: str, token: str, plan: Dict[str, Any], continue_on_error: bool) -> Dict[str, Any]:
    results = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "location": plan["location"],
        "tracked": [],
        "activated": [],
        "quantityBatches": [],
        "errors": [],
    }
    location_id = plan["location"]["id"]
    updates = plan.get("updates") or []

    tracking_by_product = defaultdict(list)
    for update in updates:
        if update.get("needsTracking"):
            tracking_by_product[update["productId"]].append(update)
    for index, (product_id, product_updates) in enumerate(
        tracking_by_product.items(),
        start=1,
    ):
        try:
            result = update_product_variant_tracking(
                endpoint,
                token,
                product_id,
                product_updates,
            )
            results["tracked"].append({
                "productId": product_id,
                "variantCount": len(product_updates),
                "result": result,
            })
            print(
                f"Tracking product {index}/{len(tracking_by_product)}: "
                f"{product_updates[0]['handle']} "
                f"({len(product_updates)} variants)"
            )
        except Exception as exc:
            for update in product_updates:
                record_error(results, update, exc)
            if not continue_on_error:
                raise

    set_quantity_updates = []
    for update in updates:
        if update.get("needsActivation"):
            try:
                result = activate_inventory(endpoint, token, update["inventoryItemId"], location_id, update["desiredQuantity"])
                results["activated"].append({"inventoryItemId": update["inventoryItemId"], "quantity": update["desiredQuantity"], "result": result})
                print(f"Activated: {update['handle']} / {update['sourceVariant']} -> {update['desiredQuantity']}")
            except Exception as exc:
                record_error(results, update, exc)
                if not continue_on_error:
                    raise
        elif update.get("needsQuantity"):
            set_quantity_updates.append(update)

    for batch_index, batch in enumerate(chunked(set_quantity_updates, INVENTORY_BATCH_SIZE), start=1):
        try:
            result = set_inventory_quantities(endpoint, token, location_id, batch)
            results["quantityBatches"].append({"count": len(batch), "result": result})
            print(f"Quantity batch {batch_index}: {len(batch)} variants")
        except Exception as exc:
            for update in batch:
                record_error(results, update, exc)
            if not continue_on_error:
                raise

    return results


def update_inventory_tracking(endpoint: str, token: str, inventory_item_id: str, tracked: bool) -> Dict[str, Any]:
    data = shopify_graphql(
        endpoint,
        token,
        """
        mutation TrackInventoryItem($id: ID!, $input: InventoryItemInput!) {
          inventoryItemUpdate(id: $id, input: $input) {
            inventoryItem {
              id
              tracked
            }
            userErrors {
              field
              message
              code
            }
          }
        }
        """,
        {"id": inventory_item_id, "input": {"tracked": tracked}},
    )
    payload = data.get("inventoryItemUpdate") or {}
    errors = payload.get("userErrors") or []
    if errors:
        raise RuntimeError(format_user_errors("inventoryItemUpdate", errors))
    return payload.get("inventoryItem") or {}


def update_product_variant_tracking(
    endpoint: str,
    token: str,
    product_id: str,
    updates: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    data = shopify_graphql(
        endpoint,
        token,
        """
        mutation TrackProductVariants(
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
              inventoryItem {
                id
                tracked
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
            "productId": product_id,
            "variants": [
                {
                    "id": update["shopifyVariantId"],
                    "inventoryItem": {"tracked": True},
                }
                for update in updates
            ],
        },
    )
    payload = data.get("productVariantsBulkUpdate") or {}
    errors = payload.get("userErrors") or []
    if errors:
        raise RuntimeError(
            format_user_errors("productVariantsBulkUpdate", errors)
        )
    return payload.get("productVariants") or []


def activate_inventory(endpoint: str, token: str, inventory_item_id: str, location_id: str, quantity: int) -> Dict[str, Any]:
    identity = json.dumps(
        {
            "inventoryItemId": inventory_item_id,
            "locationId": location_id,
            "quantity": quantity,
        },
        separators=(",", ":"),
    )
    idempotency_key = str(uuid.uuid5(uuid.NAMESPACE_URL, identity))
    data = shopify_graphql(
        endpoint,
        token,
        """
        mutation ActivateInventory(
          $inventoryItemId: ID!,
          $locationId: ID!,
          $available: Int,
          $idempotencyKey: String!
        ) {
          inventoryActivate(
            inventoryItemId: $inventoryItemId,
            locationId: $locationId,
            available: $available
          ) @idempotent(key: $idempotencyKey) {
            inventoryLevel {
              id
              quantities(names: ["available"]) {
                name
                quantity
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
            "inventoryItemId": inventory_item_id,
            "locationId": location_id,
            "available": quantity,
            "idempotencyKey": idempotency_key,
        },
    )
    payload = data.get("inventoryActivate") or {}
    errors = payload.get("userErrors") or []
    if errors:
        raise RuntimeError(format_user_errors("inventoryActivate", errors))
    return {
        "idempotencyKey": idempotency_key,
        "inventoryLevel": payload.get("inventoryLevel") or {},
    }


def set_inventory_quantities(endpoint: str, token: str, location_id: str, updates: List[Dict[str, Any]]) -> Dict[str, Any]:
    identity = json.dumps(
        {
            "locationId": location_id,
            "updates": sorted(
                (
                    update["inventoryItemId"],
                    update["currentQuantity"],
                    update["desiredQuantity"],
                )
                for update in updates
            ),
        },
        separators=(",", ":"),
    )
    idempotency_key = str(uuid.uuid5(uuid.NAMESPACE_URL, identity))
    quantities = [
        {
            "inventoryItemId": update["inventoryItemId"],
            "locationId": location_id,
            "quantity": update["desiredQuantity"],
            "changeFromQuantity": update["currentQuantity"],
        }
        for update in updates
    ]
    data = shopify_graphql(
        endpoint,
        token,
        """
        mutation SetInventoryQuantities($input: InventorySetQuantitiesInput!, $idempotencyKey: String!) {
          inventorySetQuantities(input: $input) @idempotent(key: $idempotencyKey) {
            inventoryAdjustmentGroup {
              createdAt
              reason
              changes {
                name
                delta
                quantityAfterChange
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
            "input": {
                "name": "available",
                "reason": "correction",
                "referenceDocumentUri": (
                    "gid://neighbourhood-lightspeed-sync/"
                    f"InventorySync/{idempotency_key}"
                ),
                "quantities": quantities,
            },
            "idempotencyKey": idempotency_key,
        },
    )
    payload = data.get("inventorySetQuantities") or {}
    errors = payload.get("userErrors") or []
    if errors:
        raise RuntimeError(format_user_errors("inventorySetQuantities", errors))
    return {
        "idempotencyKey": idempotency_key,
        "inventoryAdjustmentGroup": payload.get("inventoryAdjustmentGroup") or {},
    }


def verify_inventory_updates(
    endpoint: str,
    token: str,
    page_size: int,
    location_id: str,
    updates: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    live_by_id = {
        variant.get("id"): variant
        for variant in get_shopify_variants(
            endpoint,
            token,
            page_size,
            location_id,
        )
        if variant.get("id")
    }
    failures = []
    for update in updates:
        live = live_by_id.get(update["shopifyVariantId"])
        reasons = []
        if not live:
            reasons.append("missing_shopify_variant")
            actual_tracked = None
            actual_quantity = None
        else:
            inventory_item = live.get("inventoryItem") or {}
            actual_tracked = bool(inventory_item.get("tracked"))
            actual_quantity = available_at_location(live, location_id)
            if update["stockTrack"] == "Y" and not actual_tracked:
                reasons.append("inventory_tracking_not_enabled")
            if actual_quantity != update["desiredQuantity"]:
                reasons.append("quantity_mismatch")
        if reasons:
            failures.append({
                "shopifyVariantId": update["shopifyVariantId"],
                "handle": update["handle"],
                "sourceVariant": update["sourceVariant"],
                "expectedTracked": update["stockTrack"] == "Y",
                "actualTracked": actual_tracked,
                "expectedQuantity": update["desiredQuantity"],
                "actualQuantity": actual_quantity,
                "reasons": reasons,
            })
    return failures


def record_error(results: Dict[str, Any], update: Dict[str, Any], exc: Exception) -> None:
    results["errors"].append({
        "inventoryItemId": update.get("inventoryItemId"),
        "handle": update.get("handle"),
        "variant": update.get("sourceVariant"),
        "message": str(exc),
    })
    print(f"ERROR {update.get('handle')} / {update.get('sourceVariant')}: {exc}", file=sys.stderr)


def format_user_errors(operation: str, errors: List[Dict[str, Any]]) -> str:
    details = "; ".join(f"{'.'.join(str(part) for part in error.get('field') or [])}: {error.get('message')} ({error.get('code')})" for error in errors)
    return f"Shopify rejected {operation}: {details}"


def print_plan(plan: Dict[str, Any]) -> None:
    summary = plan["summary"]
    print(f"Location: {plan['location']['name']}")
    print(f"Inventory rows: {summary['inventoryRows']}")
    print(f"Mapped inventory rows: {summary['mappedInventoryRows']}")
    print(f"Shopify variants scanned: {summary['shopifyVariantsScanned']}")
    print(f"Needs update: {summary['updates']}")
    print(f"No changes: {summary['noChanges']}")
    print(f"Unmatched: {summary['unmatched']}")
    print(f"Duplicate matches: {summary['duplicateMatches']}")
    print(f"Needs tracking enabled: {summary['needsTracking']}")
    print(f"Needs location activation: {summary['needsActivation']}")
    print(f"Needs quantity set: {summary['needsQuantity']}")
    print(f"Negative source quantities: {summary['negativeSourceQuantities']}")
    print(f"Retired products excluded: {summary['retiredProductsExcluded']}")
    print(f"Invalid catalog products excluded: {summary['invalidProductsExcluded']}")
    print(f"Source files fresh: {summary['sourcesFresh']}")
    updates = plan.get("updates") or []
    if updates:
        print("\nSample updates:")
        for update in updates[:15]:
            print(
                f"- {update['title']} ({update['handle']}) / {update['sourceVariant']}: "
                f"{update['currentQuantity']} -> {update['desiredQuantity']}, tracked={update['tracked']}"
            )
        if len(updates) > 15:
            print(f"...and {len(updates) - 15} more.")
    unmatched = plan.get("unmatched") or []
    if unmatched:
        print("\nSample unmatched:")
        for item in unmatched[:15]:
            print(f"- row {item.get('row')} {item.get('variantId')} {item.get('handle', '')}: {item.get('reason')}")


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def select_location(locations: List[Dict[str, str]], location_name: str) -> Dict[str, str]:
    if not locations:
        raise RuntimeError("No Shopify locations found.")
    exact = [location for location in locations if clean(location.get("name")).lower() == location_name.lower()]
    if exact:
        return exact[0]
    names = ", ".join(location.get("name", "") for location in locations)
    raise RuntimeError(f'Location "{location_name}" not found. Available locations: {names}')


def clean(value: Any) -> str:
    return str(value or "").strip()


def collapse_space(value: str) -> str:
    return re.sub(r"\s+", " ", clean(value)).strip()


def first_nonempty(*values: str) -> str:
    for value in values:
        if clean(value):
            return clean(value)
    return ""


def normalize_handle(value: str) -> str:
    base = clean(value).lower().replace("_", "-")
    base = re.sub(r"[^a-z0-9-]+", "-", base)
    base = re.sub(r"-{2,}", "-", base)
    return base.strip("-")


def normalize_option_value(value: Any) -> str:
    return re.sub(r"\s+", " ", clean(value).lower()).strip()


def parse_stock(value: Any) -> Optional[int]:
    raw = clean(value).replace(",", ".")
    if not raw:
        return None
    try:
        return int(float(raw))
    except ValueError:
        return None


def parse_money(value: Any) -> Optional[Decimal]:
    raw = clean(value).replace(",", ".")
    if not raw:
        return None
    try:
        amount = Decimal(raw)
    except InvalidOperation:
        return None
    return amount.quantize(Decimal("0.01"))


def chunked(values: List[Dict[str, Any]], size: int) -> Iterable[List[Dict[str, Any]]]:
    for index in range(0, len(values), size):
        yield values[index:index + size]


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def main() -> None:
    project_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Sync Lightspeed C-Series inventory export quantities to Shopify.")
    parser.add_argument("--inventory-source", required=True, help="Path to Lightspeed inventory CSV export.")
    parser.add_argument("--products-source", required=True, help="Path to matching Lightspeed products CSV export.")
    parser.add_argument("--apply", action="store_true", help="Write inventory changes to Shopify. Dry-run is default.")
    parser.add_argument("--shop", help="Shopify store domain.")
    parser.add_argument("--token", help="Shopify Admin access token.")
    parser.add_argument("--api-key", help="Shopify app client ID/API key.")
    parser.add_argument("--api-secret", help="Shopify app client secret.")
    parser.add_argument("--prefer-client-credentials", action="store_true", help="Use client_credentials even when an admin token is set.")
    parser.add_argument("--api-version", default=DEFAULT_API_VERSION, help="Shopify Admin API version.")
    parser.add_argument("--location-name", help=f'Shopify location name. Default: SHOPIFY_LOCATION_NAME or "{DEFAULT_LOCATION_NAME}".')
    parser.add_argument("--page-size", type=positive_int, default=40, help="Shopify product page size.")
    parser.add_argument("--max-updates", type=positive_int, help="Limit updates after matching, useful for smoke tests.")
    parser.add_argument(
        "--clamp-negative-to-zero",
        dest="clamp_negative_to_zero",
        action="store_true",
        default=True,
        help="Write negative Lightspeed stock as zero. This is the default.",
    )
    parser.add_argument(
        "--preserve-negative-stock",
        dest="clamp_negative_to_zero",
        action="store_false",
        help="Preserve negative source quantities after explicit review.",
    )
    parser.add_argument(
        "--include-retired-catalog-items",
        action="store_true",
        help="Include deliberately retired brands and test products. Excluded by default.",
    )
    parser.add_argument(
        "--max-source-age-hours",
        type=float,
        default=DEFAULT_MAX_SOURCE_AGE_HOURS,
        help=f"Refuse --apply when either CSV is older than this. Default: {DEFAULT_MAX_SOURCE_AGE_HOURS:g}.",
    )
    parser.add_argument(
        "--allow-stale-source",
        action="store_true",
        help="Override the source-age apply guard. Use only after explicit review.",
    )
    parser.add_argument("--continue-on-error", action="store_true", help="Continue applying remaining updates after a Shopify error.")
    parser.add_argument("--env-file", help="Path to .env. Default: repository .env.")
    parser.add_argument("--plan-file", default=str(project_root / DEFAULT_PLAN_FILE), help="JSON plan output path.")
    parser.add_argument("--results-file", default=str(project_root / DEFAULT_RESULTS_FILE), help="JSON apply results output path.")
    args = parser.parse_args()

    if args.page_size > 250:
        raise SystemExit("--page-size must be between 1 and 250.")

    load_dotenv(Path(args.env_file or project_root / ".env"))
    shop = normalize_shop(args.shop or os.environ.get("SHOPIFY_SHOP") or os.environ.get("SHOPIFY_STORE_DOMAIN") or os.environ.get("SHOPIFY_STORE"))
    token = args.token or os.environ.get("SHOPIFY_ADMIN_ACCESS_TOKEN") or os.environ.get("SHOPIFY_ACCESS_TOKEN")
    client_id = args.api_key or os.environ.get("SHOPIFY_API_KEY") or os.environ.get("SHOPIFY_CLIENT_ID")
    client_secret = args.api_secret or os.environ.get("SHOPIFY_API_SECRET") or os.environ.get("SHOPIFY_CLIENT_SECRET")
    api_version = args.api_version or os.environ.get("SHOPIFY_API_VERSION") or DEFAULT_API_VERSION
    location_name = args.location_name or os.environ.get("SHOPIFY_LOCATION_NAME") or DEFAULT_LOCATION_NAME
    if not shop or (not token and (not client_id or not client_secret)):
        raise SystemExit("Missing Shopify credentials.")
    endpoint = f"https://{shop}/admin/api/{api_version}/graphql.json"
    if args.prefer_client_credentials or not token:
        print("Getting temporary Shopify Admin token with client_credentials.")
        token = get_client_credentials_token(shop, client_id, client_secret)

    print(f"{'Apply' if args.apply else 'Dry run'}: syncing Lightspeed inventory to Shopify on {shop}")
    inventory_source = Path(args.inventory_source)
    products_source = Path(args.products_source)
    source_files = {
        "inventory": source_metadata(inventory_source),
        "products": source_metadata(products_source),
    }
    inventory_rows = read_csv(inventory_source)
    product_rows = read_csv(products_source)
    retired_exclusions = {"products": 0, "variants": 0, "inventoryRows": 0}
    if not args.include_retired_catalog_items:
        product_rows, inventory_rows, retired_exclusions = filter_retired_catalog_rows(
            product_rows,
            inventory_rows,
        )
    product_rows, inventory_rows, invalid_exclusions = filter_invalid_catalog_rows(
        product_rows,
        inventory_rows,
    )
    product_mapping, mapping_warnings = build_product_mapping(product_rows)
    mapped_inventory, unmapped_inventory = build_inventory_rows(inventory_rows, product_mapping, args.clamp_negative_to_zero)
    locations = get_locations(endpoint, token)
    location = select_location(locations, location_name)
    shopify_variants = get_shopify_variants(
        endpoint,
        token,
        args.page_size,
        location["id"],
    )
    plan = build_plan(
        mapped_inventory,
        unmapped_inventory,
        shopify_variants,
        location,
        args,
        source_files,
        retired_exclusions,
        invalid_exclusions,
    )
    if mapping_warnings:
        plan["mappingWarnings"] = mapping_warnings
        plan["summary"]["mappingWarningHandles"] = len(mapping_warnings)
    write_json(Path(args.plan_file), plan)
    print_plan(plan)
    print(f"\nPlan written: {Path(args.plan_file)}")

    if plan["summary"]["unmatched"] or plan["summary"]["duplicateMatches"]:
        raise SystemExit("Inventory plan has unmatched or duplicate variant matches. Fix matching before applying.")

    if not args.apply:
        print("No inventory changes were written. Re-run with --apply after reviewing the plan.")
        return

    if not plan["summary"]["sourcesFresh"] and not args.allow_stale_source:
        raise SystemExit(
            "Refusing --apply because one or more source files are older than "
            f"{args.max_source_age_hours:g} hours. Export fresh files or pass "
            "--allow-stale-source after explicit review."
        )

    results = apply_plan(endpoint, token, plan, args.continue_on_error)
    results["verificationFailures"] = verify_inventory_updates(
        endpoint,
        token,
        args.page_size,
        location["id"],
        plan["updates"],
    )
    write_json(Path(args.results_file), results)
    print(f"\nResults written: {Path(args.results_file)}")
    print(f"Tracking product batches: {len(results['tracked'])}")
    print(
        "Tracking variants: "
        f"{sum(item['variantCount'] for item in results['tracked'])}"
    )
    print(f"Location activations: {len(results['activated'])}")
    print(f"Quantity batches: {len(results['quantityBatches'])}")
    print(f"Errors: {len(results['errors'])}")
    print(f"Verification failures: {len(results['verificationFailures'])}")
    if results["verificationFailures"]:
        raise SystemExit("Post-sync inventory verification failed.")


if __name__ == "__main__":
    main()
