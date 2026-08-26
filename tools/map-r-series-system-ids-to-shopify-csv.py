#!/usr/bin/env python3
"""Map Lightspeed R-Series System IDs to Shopify variant SKUs offline."""

import argparse
import csv
import json
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shopify-csv", required=True)
    parser.add_argument("--r-series-csv", required=True)
    parser.add_argument(
        "--overrides-csv",
        help="Optional reviewed variant-to-System-ID overrides CSV",
    )
    parser.add_argument(
        "--exclusions-csv",
        help="Optional reviewed Shopify product exclusions CSV",
    )
    parser.add_argument(
        "--output-dir", default=".tmp/mortar-r-series-system-id-mapping"
    )
    parser.add_argument(
        "--zero-unresolved-inventory",
        action="store_true",
        help="Set unresolved variant inventory to zero in the Shopify import output",
    )
    return parser.parse_args()


def read_csv(path):
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        return reader.fieldnames or [], rows


def write_csv(path, fieldnames, rows):
    with Path(path).open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def normalize(value):
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(character for character in text if not unicodedata.combining(character))
    text = text.casefold().replace("&", " and ")
    tokens = re.findall(r"[a-z0-9]+", text)
    replacements = {"raglen": "raglan"}
    normalized = []
    for token in tokens:
        token = replacements.get(token, token)
        if not normalized or normalized[-1] != token:
            normalized.append(token)
    return "".join(normalized)


def parse_money(value):
    text = re.sub(r"[^0-9,.-]", "", value or "")
    if not text:
        return None
    if "," in text and "." in text:
        if text.rfind(".") > text.rfind(","):
            text = text.replace(",", "")
        else:
            text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def require_columns(fieldnames, required, label):
    missing = sorted(set(required) - set(fieldnames))
    if missing:
        raise SystemExit(f"{label} is missing required columns: {', '.join(missing)}")


def product_metadata(shopify_rows):
    metadata = defaultdict(dict)
    for row in shopify_rows:
        handle = row.get("Handle", "").strip()
        if not handle:
            continue
        for key in ("Title", "Vendor", "Status", "Published", "Gift Card"):
            if row.get(key, "").strip() and not metadata[handle].get(key):
                metadata[handle][key] = row[key].strip()
    return metadata


def option_values(row):
    values = []
    for key in ("Option1 Value", "Option2 Value", "Option3 Value"):
        value = row.get(key, "").strip()
        if value and value.casefold() not in {"default", "default title"}:
            values.append(value)
    return values


def build_shopify_variants(rows):
    metadata = product_metadata(rows)
    compare_at_prices = defaultdict(set)
    for row in rows:
        handle = row.get("Handle", "").strip()
        compare_at = parse_money(row.get("Variant Compare At Price"))
        if handle and compare_at is not None:
            compare_at_prices[handle].add(compare_at)
    variants = []
    for index, row in enumerate(rows, start=2):
        if not row.get("Variant Price", "").strip():
            continue
        handle = row.get("Handle", "").strip()
        product = metadata.get(handle, {})
        title = product.get("Title", row.get("Title", "").strip())
        vendor = product.get("Vendor", row.get("Vendor", "").strip())
        status = product.get("Status", row.get("Status", "").strip()).casefold()
        options = option_values(row)
        label_options = list(options)
        if len(options) == 1 and normalize(title).endswith(normalize(options[0])):
            label_options = []
        label = " ".join([title, *label_options]).strip()
        variants.append(
            {
                "csv_row": index,
                "source_row": row,
                "handle": handle,
                "title": title,
                "vendor": vendor,
                "status": status,
                "options": options,
                "label": label,
                "label_norm": normalize(label),
                "vendor_norm": normalize(vendor),
                "price": parse_money(row.get("Variant Price")),
                "compare_at_price": parse_money(
                    row.get("Variant Compare At Price")
                ),
                "product_compare_at_prices": compare_at_prices[handle],
                "old_sku": row.get("Variant SKU", ""),
                "barcode": row.get("Variant Barcode", ""),
                "gift_card": product.get("Gift Card", "").casefold() == "true",
            }
        )
    return variants


def load_overrides(path, variants, items):
    if not path:
        return {}

    fields, rows = read_csv(path)
    require_columns(
        fields,
        {
            "shopify_handle",
            "shopify_option1",
            "shopify_option2",
            "shopify_option3",
            "r_system_id",
            "reason",
        },
        "Overrides CSV",
    )
    variants_by_key = {
        (variant["handle"], tuple(variant["options"])): variant
        for variant in variants
    }
    items_by_system_id = {item["system_id"]: item for item in items}
    overrides = {}
    for row_number, row in enumerate(rows, start=2):
        options = tuple(
            value
            for value in (
                row["shopify_option1"].strip(),
                row["shopify_option2"].strip(),
                row["shopify_option3"].strip(),
            )
            if value
        )
        key = (row["shopify_handle"].strip(), options)
        system_id = row["r_system_id"].strip().lstrip("'")
        if key in overrides:
            raise SystemExit(f"Duplicate override at row {row_number}: {key}")
        variant = variants_by_key.get(key)
        item = items_by_system_id.get(system_id)
        if not variant:
            raise SystemExit(f"Override row {row_number} has no Shopify variant: {key}")
        if not item:
            raise SystemExit(
                f"Override row {row_number} has no R-Series item: {system_id}"
            )
        option_norm = normalize(" ".join(variant["options"]))
        accepted_prices = {
            price
            for price in (
                variant["price"],
                variant["compare_at_price"],
                *variant["product_compare_at_prices"],
            )
            if price is not None
        }
        if (
            item["brand_norm"] != variant["vendor_norm"]
            or item["price"] not in accepted_prices
            or (option_norm and not item["item_norm"].endswith(option_norm))
        ):
            raise SystemExit(
                f"Override row {row_number} failed brand/price/option validation"
            )
        overrides[key] = {
            "item": item,
            "reason": row["reason"].strip() or "reviewed_manual_override",
        }
    if len({entry["item"]["system_id"] for entry in overrides.values()}) != len(
        overrides
    ):
        raise SystemExit("Overrides CSV assigns an R-Series System ID more than once")
    return overrides


def load_exclusions(path, variants):
    if not path:
        return {}

    fields, rows = read_csv(path)
    require_columns(
        fields,
        {"shopify_handle", "reason"},
        "Exclusions CSV",
    )
    known_handles = {variant["handle"] for variant in variants}
    exclusions = {}
    for row_number, row in enumerate(rows, start=2):
        handle = row["shopify_handle"].strip()
        if handle in exclusions:
            raise SystemExit(f"Duplicate exclusion at row {row_number}: {handle}")
        if handle not in known_handles:
            raise SystemExit(
                f"Exclusion row {row_number} has no Shopify product: {handle}"
            )
        exclusions[handle] = row["reason"].strip() or "reviewed_exclusion"
    return exclusions


def build_r_series_items(rows):
    items = []
    for index, row in enumerate(rows, start=2):
        system_id = row.get("System ID", "").strip().lstrip("'")
        items.append(
            {
                "csv_row": index,
                "source_row": row,
                "system_id": system_id,
                "item": row.get("Item", "").strip(),
                "item_norm": normalize(row.get("Item", "")),
                "brand": row.get("Brand", "").strip(),
                "brand_norm": normalize(row.get("Brand", "")),
                "price": parse_money(row.get("Price")),
                "quantity": row.get("Qty.", "").strip(),
                "publish_to_ecom": row.get("Publish to eCom", "").strip(),
            }
        )
    return items


def choose_match(variant, candidates):
    if not candidates:
        return None, "unmatched"

    def preferred(rows):
        if len(rows) == 1:
            return rows[0], ""
        published = [
            item
            for item in rows
            if item["publish_to_ecom"].casefold() == "yes"
        ]
        if len(published) == 1:
            return published[0], "_published_preferred"
        return None, ""

    same_brand_price = [
        item
        for item in candidates
        if item["brand_norm"] == variant["vendor_norm"]
        and item["price"] == variant["price"]
    ]
    selected, suffix = preferred(same_brand_price)
    if selected:
        return selected, f"exact_name_brand_price{suffix}"
    if len(same_brand_price) > 1:
        return None, "ambiguous_exact_name_brand_price"

    same_brand = [
        item for item in candidates if item["brand_norm"] == variant["vendor_norm"]
    ]
    selected, suffix = preferred(same_brand)
    if selected:
        return selected, f"exact_name_brand{suffix}"
    if len(same_brand) > 1:
        return None, "ambiguous_exact_name_brand"

    same_price = [item for item in candidates if item["price"] == variant["price"]]
    selected, suffix = preferred(same_price)
    if selected:
        return selected, f"exact_name_price{suffix}"
    if len(same_price) > 1:
        return None, "ambiguous_exact_name_price"

    selected, suffix = preferred(candidates)
    if selected:
        return selected, f"exact_name_only{suffix}"
    return None, "ambiguous_exact_name"


def choose_strong_fuzzy_match(variant, items):
    option_norm = normalize(" ".join(variant["options"]))
    pool = [
        item
        for item in items
        if item["brand_norm"] == variant["vendor_norm"]
        and item["price"] == variant["price"]
        and (not option_norm or item["item_norm"].endswith(option_norm))
    ]
    scored = sorted(
        (
            (
                SequenceMatcher(None, variant["label_norm"], item["item_norm"]).ratio(),
                item,
            )
            for item in pool
        ),
        key=lambda entry: (-entry[0], entry[1]["system_id"]),
    )
    if not scored or scored[0][0] < 0.90:
        return None
    if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.05:
        return None
    return scored[0][1]


def fuzzy_suggestions(variant, items, limit=3):
    pool = [
        item
        for item in items
        if item["brand_norm"] == variant["vendor_norm"]
        and (item["price"] == variant["price"] or variant["price"] is None)
    ]
    if not pool:
        pool = [item for item in items if item["brand_norm"] == variant["vendor_norm"]]
    scored = []
    for item in pool:
        score = SequenceMatcher(None, variant["label_norm"], item["item_norm"]).ratio()
        if score >= 0.65:
            scored.append((score, item))
    scored.sort(key=lambda entry: (-entry[0], entry[1]["system_id"]))
    return [
        {
            "score": f"{score:.4f}",
            "system_id": item["system_id"],
            "item": item["item"],
            "brand": item["brand"],
            "price": str(item["price"] or ""),
        }
        for score, item in scored[:limit]
    ]


def review_row(variant, result):
    item = result.get("item") or {}
    suggestions = result.get("suggestions") or []
    return {
        "status": result["status"],
        "match_method": result.get("method", ""),
        "shopify_csv_row": variant["csv_row"],
        "shopify_handle": variant["handle"],
        "shopify_title": variant["title"],
        "shopify_options": " | ".join(variant["options"]),
        "shopify_vendor": variant["vendor"],
        "shopify_status": variant["status"],
        "shopify_price": str(variant["price"] or ""),
        "old_shopify_sku": variant["old_sku"],
        "new_r_system_id": item.get("system_id", ""),
        "r_item": item.get("item", ""),
        "r_brand": item.get("brand", ""),
        "r_price": str(item.get("price") or ""),
        "r_quantity": item.get("quantity", ""),
        "r_publish_to_ecom": item.get("publish_to_ecom", ""),
        "suggestions": " || ".join(
            f"{entry['score']}:{entry['system_id']}:{entry['item']}"
            for entry in suggestions
        ),
    }


def main():
    args = parse_args()
    shopify_fields, shopify_rows = read_csv(args.shopify_csv)
    r_fields, r_rows = read_csv(args.r_series_csv)
    require_columns(
        shopify_fields,
        {
            "Handle",
            "Title",
            "Vendor",
            "Option1 Value",
            "Option2 Value",
            "Option3 Value",
            "Variant SKU",
            "Variant Price",
        },
        "Shopify CSV",
    )
    require_columns(
        r_fields,
        {"System ID", "Item", "Brand", "Price", "Qty.", "Publish to eCom"},
        "R-Series CSV",
    )

    variants = build_shopify_variants(shopify_rows)
    items = build_r_series_items(r_rows)
    invalid_system_ids = sorted(
        item["system_id"]
        for item in items
        if not re.fullmatch(r"\d{12}", item["system_id"])
    )
    duplicate_system_ids = sorted(
        system_id
        for system_id, count in Counter(item["system_id"] for item in items).items()
        if count > 1
    )
    if invalid_system_ids or duplicate_system_ids:
        raise SystemExit(
            "R-Series System IDs failed validation: "
            f"invalid={len(invalid_system_ids)}, duplicates={len(duplicate_system_ids)}"
        )

    overrides = load_overrides(args.overrides_csv, variants, items)
    exclusions = load_exclusions(args.exclusions_csv, variants)

    by_name = defaultdict(list)
    for item in items:
        by_name[item["item_norm"]].append(item)

    results = []
    for variant in variants:
        override = overrides.get((variant["handle"], tuple(variant["options"])))
        exclusion_reason = exclusions.get(variant["handle"])
        if exclusion_reason:
            results.append(
                {
                    "variant": variant,
                    "item": None,
                    "method": f"excluded_{normalize(exclusion_reason)}",
                    "status": "excluded",
                }
            )
            continue
        if variant["gift_card"]:
            results.append(
                {
                    "variant": variant,
                    "item": None,
                    "method": "excluded_non_inventory_gift_card",
                    "status": "excluded",
                }
            )
            continue
        if override:
            results.append(
                {
                    "variant": variant,
                    "item": override["item"],
                    "method": f"manual_override_{normalize(override['reason'])}",
                    "status": "matched",
                }
            )
            continue

        candidate, method = choose_match(variant, by_name.get(variant["label_norm"], []))
        if not candidate and method == "unmatched":
            candidate = choose_strong_fuzzy_match(variant, items)
            if candidate:
                method = "strong_fuzzy_name_brand_price_option"
        if candidate:
            results.append(
                {
                    "variant": variant,
                    "item": candidate,
                    "method": method,
                    "status": "matched",
                }
            )
        else:
            results.append(
                {
                    "variant": variant,
                    "item": None,
                    "method": method,
                    "status": "ambiguous" if method.startswith("ambiguous") else "unmatched",
                    "suggestions": fuzzy_suggestions(variant, items),
                }
            )

    assigned = defaultdict(list)
    for result in results:
        if result["status"] == "matched":
            assigned[result["item"]["system_id"]].append(result)
    collisions = {
        system_id: rows for system_id, rows in assigned.items() if len(rows) > 1
    }
    for system_id, rows in collisions.items():
        active_rows = [
            result for result in rows if result["variant"]["status"] == "active"
        ]
        keep = active_rows[0] if len(active_rows) == 1 else None
        for result in rows:
            if result is keep:
                result["method"] = f"{result['method']}_active_preferred"
                continue
            result["status"] = "collision"
            result["method"] = "duplicate_shopify_assignment"
            result["suggestions"] = [
                {
                    "score": "1.0000",
                    "system_id": system_id,
                    "item": result["item"]["item"],
                    "brand": result["item"]["brand"],
                    "price": str(result["item"]["price"] or ""),
                }
            ]
            result["item"] = None

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    mapped_rows = [dict(row) for row in shopify_rows]
    matched = [result for result in results if result["status"] == "matched"]
    parked = [
        result
        for result in results
        if result["status"] not in {"matched", "excluded"}
    ]
    for result in matched:
        mapped_rows[result["variant"]["csv_row"] - 2]["Variant SKU"] = result["item"][
            "system_id"
        ]
        if args.zero_unresolved_inventory:
            mapped_rows[result["variant"]["csv_row"] - 2][
                "Variant Inventory Tracker"
            ] = "shopify"
    if args.zero_unresolved_inventory:
        for result in parked:
            mapped_rows[result["variant"]["csv_row"] - 2][
                "Variant Inventory Qty"
            ] = "0"

    review_fields = [
        "status",
        "match_method",
        "shopify_csv_row",
        "shopify_handle",
        "shopify_title",
        "shopify_options",
        "shopify_vendor",
        "shopify_status",
        "shopify_price",
        "old_shopify_sku",
        "new_r_system_id",
        "r_item",
        "r_brand",
        "r_price",
        "r_quantity",
        "r_publish_to_ecom",
        "suggestions",
    ]
    review = [review_row(result["variant"], result) for result in results]
    unresolved = [
        row for row in review if row["status"] not in {"matched", "excluded"}
    ]
    rollback = [
        {
            "shopify_csv_row": result["variant"]["csv_row"],
            "shopify_handle": result["variant"]["handle"],
            "shopify_options": " | ".join(result["variant"]["options"]),
            "old_shopify_sku": result["variant"]["old_sku"],
            "new_r_system_id": result["item"]["system_id"],
        }
        for result in matched
    ]

    active_results = [
        result for result in results if result["variant"]["status"] == "active"
    ]
    active_unresolved = [
        result
        for result in active_results
        if result["status"] not in {"matched", "excluded"}
    ]
    active_by_handle = defaultdict(list)
    for result in active_results:
        active_by_handle[result["variant"]["handle"]].append(result)
    active_handles = {
        handle
        for handle, handle_results in active_by_handle.items()
        if any(result["status"] == "matched" for result in handle_results)
        and all(
            result["status"] in {"matched", "excluded"}
            for result in handle_results
        )
    }
    active_mapped_rows = [
        row for row in mapped_rows if row.get("Handle", "").strip() in active_handles
    ]

    r_system_ids = {item["system_id"] for item in items}
    matched_ids = [result["item"]["system_id"] for result in matched]
    active_variant_rows = [
        row for row in active_mapped_rows if row.get("Variant Price", "").strip()
    ]
    active_skus = [
        row.get("Variant SKU", "").strip().lstrip("'")
        for row in active_variant_rows
    ]
    allowed_changes = {
        (result["variant"]["csv_row"] - 2, "Variant SKU")
        for result in matched
    }
    if args.zero_unresolved_inventory:
        allowed_changes.update(
            (result["variant"]["csv_row"] - 2, "Variant Inventory Tracker")
            for result in matched
        )
        allowed_changes.update(
            (result["variant"]["csv_row"] - 2, "Variant Inventory Qty")
            for result in parked
        )
    validation = {
        "fullOutputRowCountMatchesSource": len(mapped_rows) == len(shopify_rows),
        "onlyExpectedFieldsChanged": all(
            all(
                source.get(field, "") == mapped.get(field, "")
                or (row_index, field) in allowed_changes
                for field in shopify_fields
            )
            for row_index, (source, mapped) in enumerate(
                zip(shopify_rows, mapped_rows)
            )
        ),
        "matchedSystemIdsUnique": len(matched_ids) == len(set(matched_ids)),
        "activeVariantCountMatches": len(active_variant_rows)
        == sum(1 for result in active_results if result["status"] == "matched"),
        "activeSkusAreTwelveDigits": all(
            re.fullmatch(r"\d{12}", sku) for sku in active_skus
        ),
        "activeSkusExistInRSeries": all(sku in r_system_ids for sku in active_skus),
        "activeSkusUnique": len(active_skus) == len(set(active_skus)),
        "giftCardExcludedFromActiveOutput": all(
            row.get("Handle", "").strip() != "neighbourhood-gift-card"
            for row in active_mapped_rows
        ),
        "parkedUnresolvedInventoryIsZero": not args.zero_unresolved_inventory
        or all(
            mapped_rows[result["variant"]["csv_row"] - 2].get(
                "Variant Inventory Qty", ""
            )
            == "0"
            for result in parked
        ),
        "mappedInventoryTrackingEnabled": not args.zero_unresolved_inventory
        or all(
            mapped_rows[result["variant"]["csv_row"] - 2].get(
                "Variant Inventory Tracker", ""
            )
            == "shopify"
            for result in matched
        ),
    }
    failed_checks = [name for name, passed in validation.items() if not passed]
    if failed_checks:
        raise SystemExit(f"Output validation failed: {', '.join(failed_checks)}")

    full_output = output_dir / "shopify-products-with-r-system-ids-REVIEW-ONLY.csv"
    active_output = output_dir / "shopify-active-products-with-r-system-ids.csv"
    import_output = output_dir / "shopify-mortar-import.csv"
    write_csv(full_output, shopify_fields, mapped_rows)
    write_csv(active_output, shopify_fields, active_mapped_rows)
    if args.zero_unresolved_inventory:
        write_csv(import_output, shopify_fields, mapped_rows)
    write_csv(output_dir / "mapping-review.csv", review_fields, review)
    write_csv(output_dir / "unresolved-mappings.csv", review_fields, unresolved)
    write_csv(
        output_dir / "sku-rollback.csv",
        [
            "shopify_csv_row",
            "shopify_handle",
            "shopify_options",
            "old_shopify_sku",
            "new_r_system_id",
        ],
        rollback,
    )

    status_counts = Counter(result["status"] for result in results)
    method_counts = Counter(
        result["method"] for result in results if result["status"] == "matched"
    )
    summary = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "mode": "offline_dry_run_csv_mapping",
        "shopifyWritesPerformed": False,
        "mortarWritesPerformed": False,
        "inputs": {
            "shopifyCsv": str(Path(args.shopify_csv).resolve()),
            "rSeriesCsv": str(Path(args.r_series_csv).resolve()),
        },
        "counts": {
            "shopifyCsvRows": len(shopify_rows),
            "shopifyVariants": len(variants),
            "activeShopifyVariants": len(active_results),
            "rSeriesRows": len(r_rows),
            "rSeriesItems": len(items),
            "rSeriesPublishToEcomItems": sum(
                1 for item in items if item["publish_to_ecom"].casefold() == "yes"
            ),
            "matched": status_counts["matched"],
            "excluded": status_counts["excluded"],
            "ambiguous": status_counts["ambiguous"],
            "unmatched": status_counts["unmatched"],
            "collisions": status_counts["collision"],
            "activeUnresolved": len(active_unresolved),
            "activeExcluded": sum(
                1 for result in active_results if result["status"] == "excluded"
            ),
            "parkedUnresolvedAtZero": len(parked)
            if args.zero_unresolved_inventory
            else 0,
            "existingSkusReplacedInOutput": sum(
                1
                for result in matched
                if result["variant"]["old_sku"].strip()
                and result["variant"]["old_sku"].strip().lstrip("'")
                != result["item"]["system_id"]
            ),
            "blankSkusFilledInOutput": sum(
                1 for result in matched if not result["variant"]["old_sku"].strip()
            ),
        },
        "matchMethods": dict(sorted(method_counts.items())),
        "validation": validation,
        "readyForImport": len(unresolved) == 0,
        "readyForShopifyMortarImport": bool(args.zero_unresolved_inventory)
        and len(active_unresolved) == 0
        and all(validation.values()),
        "activeCatalogReadyForImport": len(active_unresolved) == 0,
        "outputs": {
            "mappedShopifyCsvReviewOnly": str(full_output.resolve()),
            "activeMappedShopifyCsv": str(active_output.resolve()),
            "mappingReview": str((output_dir / "mapping-review.csv").resolve()),
            "unresolvedMappings": str(
                (output_dir / "unresolved-mappings.csv").resolve()
            ),
            "rollback": str((output_dir / "sku-rollback.csv").resolve()),
            "readme": str((output_dir / "README.txt").resolve()),
        },
    }
    if args.zero_unresolved_inventory:
        summary["outputs"]["shopifyMortarImportCsv"] = str(
            import_output.resolve()
        )
    if args.overrides_csv:
        summary["inputs"]["overridesCsv"] = str(Path(args.overrides_csv).resolve())
    if args.exclusions_csv:
        summary["inputs"]["exclusionsCsv"] = str(
            Path(args.exclusions_csv).resolve()
        )
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / "README.txt").write_text(
        "Mortar R-Series to Shopify SKU mapping\n"
        "\n"
        "No Shopify or Mortar writes were performed.\n"
        "shopify-active-products-with-r-system-ids.csv contains only fully "
        "mapped active physical products.\n"
        "The Shopify gift card is intentionally excluded because it has no "
        "physical POS inventory.\n"
        "shopify-products-with-r-system-ids-REVIEW-ONLY.csv contains unresolved "
        "draft products and must not be imported.\n"
        "shopify-mortar-import.csv is the reviewed Shopify product import: "
        "mapped System IDs are in Variant SKU and unresolved drafts are parked "
        "at zero inventory.\n"
        "sku-rollback.csv preserves every replaced Shopify SKU.\n",
        encoding="utf-8",
    )

    print(json.dumps(summary, indent=2))
    if not summary["readyForImport"]:
        print("Full mapped CSV is for review only; unresolved variants remain.")


if __name__ == "__main__":
    main()
