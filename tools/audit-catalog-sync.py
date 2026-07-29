#!/usr/bin/env python3

import argparse
import csv
import hashlib
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from catalog_policy import (
    creation_quality_issues,
    decide_catalog_status,
    is_retired_product,
    normalize_handle,
    normalized_visibility,
)


def read_csv(path: Path) -> List[Dict[str, str]]:
    last_error = None
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            with path.open(newline="", encoding=encoding) as csv_file:
                return list(csv.DictReader(csv_file, delimiter=";"))
        except UnicodeDecodeError as error:
            last_error = error
    raise RuntimeError(f"Could not read {path}: {last_error}")


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_age_hours(path: Path) -> float:
    modified_at = datetime.fromtimestamp(
        os.path.getmtime(path),
        tz=timezone.utc,
    )
    return (
        datetime.now(timezone.utc) - modified_at
    ).total_seconds() / 3600


def clean(value: Any) -> str:
    return str(value or "").strip()


def first_nonempty(*values: str) -> str:
    for value in values:
        if clean(value):
            return clean(value)
    return ""


def split_values(value: str) -> List[str]:
    return [part.strip() for part in clean(value).split(",") if part.strip()]


def parse_decimal(value: Any) -> Optional[Decimal]:
    cleaned = clean(value).replace(",", ".")
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def decimal_json(value: Optional[Decimal]):
    if value is None:
        return None
    return int(value) if value == value.to_integral() else float(value)


def source_handle(row: Dict[str, str]) -> str:
    return normalize_handle(
        first_nonempty(
            row.get("US_URL"),
            row.get("NL_URL"),
            row.get("US_Title_Short"),
            row.get("NL_Title_Short"),
            row.get("US_Title_Long"),
            row.get("NL_Title_Long"),
        )
    )


def source_title(row: Dict[str, str]) -> str:
    return first_nonempty(
        row.get("US_Title_Short"),
        row.get("NL_Title_Short"),
        row.get("US_Title_Long"),
        row.get("NL_Title_Long"),
    )


def source_description(row: Dict[str, str]) -> str:
    return first_nonempty(
        row.get("US_Description_Long"),
        row.get("US_Description_Short"),
        row.get("NL_Description_Long"),
        row.get("NL_Description_Short"),
    )


def group_source_products(
    rows: Iterable[Dict[str, str]],
) -> Tuple[Dict[str, List[Dict[str, str]]], List[Dict[str, Any]]]:
    grouped = defaultdict(list)
    excluded = []
    for row in rows:
        internal_id = clean(row.get("Internal_ID"))
        handle = source_handle(row)
        vendor = clean(row.get("Brand"))
        if is_retired_product(handle, vendor):
            excluded.append(
                {
                    "internal_id": internal_id,
                    "handle": handle,
                    "vendor": vendor,
                }
            )
            continue
        if internal_id:
            grouped[internal_id].append(row)
    return dict(grouped), excluded


def inventory_by_variant(
    rows: Iterable[Dict[str, str]],
) -> Dict[str, Dict[str, str]]:
    return {
        clean(row.get("Internal_Variant_ID")): row
        for row in rows
        if clean(row.get("Internal_Variant_ID"))
    }


def duplicate_source_identifiers(
    grouped: Dict[str, List[Dict[str, str]]],
    field: str,
) -> List[Dict[str, Any]]:
    occurrences = defaultdict(list)
    for internal_id, rows in grouped.items():
        for row in rows:
            value = clean(row.get(field))
            if not value:
                continue
            occurrences[value].append({
                "internal_id": internal_id,
                "variant_id": clean(row.get("Internal_Variant_ID")),
                "handle": source_handle(row),
            })
    return [
        {
            "value": value,
            "occurrences": entries,
        }
        for value, entries in sorted(occurrences.items())
        if len(entries) > 1
    ]


def shopify_inventory(product: Dict[str, Any]) -> Dict[str, Any]:
    variants = product.get("variants") or []
    quantities = [
        int(variant.get("inventory_quantity") or 0)
        for variant in variants
    ]
    return {
        "variant_count": len(variants),
        "tracked_variants": sum(
            1
            for variant in variants
            if variant.get("inventory_management") == "shopify"
        ),
        "quantity": sum(quantities),
        "variants_with_sku": sum(
            1 for variant in variants if clean(variant.get("sku"))
        ),
        "prices": sorted(
            clean(variant.get("price"))
            for variant in variants
            if clean(variant.get("price"))
        ),
    }


def source_product_record(
    internal_id: str,
    rows: List[Dict[str, str]],
    inventory_index: Dict[str, Dict[str, str]],
    shopify: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    first = rows[0]
    handle = source_handle(first)
    title = source_title(first)
    vendor = clean(first.get("Brand"))
    variant_ids = [
        clean(row.get("Internal_Variant_ID"))
        for row in rows
    ]
    inventory_rows = [
        inventory_index.get(variant_id)
        for variant_id in variant_ids
        if variant_id
    ]
    inventory_rows = [row for row in inventory_rows if row]
    stock_values = [
        parse_decimal(row.get("Stock_Level"))
        for row in inventory_rows
    ]
    valid_stock_values = [
        value for value in stock_values if value is not None
    ]
    total_inventory = (
        sum(valid_stock_values, Decimal("0"))
        if len(valid_stock_values) == len(variant_ids)
        else None
    )
    visible_values = normalized_visibility(
        [
            *(row.get("Visible") for row in rows),
            *(row.get("Visible") for row in inventory_rows),
        ]
    )
    images = sorted(
        {
            image
            for row in rows
            for image in split_values(row.get("Images"))
        }
    )
    current_images = (shopify or {}).get("images") or []
    quality_issues = creation_quality_issues(
        title=title,
        vendor=vendor,
        variant_ids=variant_ids,
        variant_prices=[parse_decimal(row.get("Price")) for row in rows],
        image_urls=images or [
            image.get("src")
            for image in current_images
            if image.get("src")
        ],
    )
    decision = decide_catalog_status(
        visible_values,
        total_inventory,
        quality_issues,
    )
    shopify_state = shopify_inventory(shopify or {})
    source_prices = sorted(
        f"{value:.2f}"
        for value in (
            parse_decimal(row.get("Price")) for row in rows
        )
        if value is not None
    )
    source_skus = [
        clean(row.get("SKU"))
        for row in rows
        if clean(row.get("SKU"))
    ]
    source_eans = [
        clean(row.get("EAN"))
        for row in rows
        if clean(row.get("EAN"))
    ]
    warnings = []
    if len(inventory_rows) != len(variant_ids):
        warnings.append("missing_inventory_export_rows")
    if any(value is not None and value < 0 for value in stock_values):
        warnings.append("negative_variant_inventory")
    if not source_skus:
        warnings.append("source_skus_blank")
    if not source_eans:
        warnings.append("source_eans_blank")
    if not source_description(first) and not clean((shopify or {}).get("body_html")):
        warnings.append("description_blank")

    drifts = []
    if not shopify:
        drifts.append("missing_in_shopify")
    else:
        if clean(shopify.get("vendor")) != vendor:
            drifts.append("vendor_mismatch")
        brand = clean(
            (shopify.get("custom_brand_metafield") or {}).get("value")
        )
        if brand != vendor:
            drifts.append("brand_metafield_mismatch")
        if len(rows) != shopify_state["variant_count"]:
            drifts.append("variant_count_mismatch")
        if source_prices != shopify_state["prices"]:
            drifts.append("price_mismatch")
        if shopify_state["tracked_variants"] != len(rows):
            drifts.append("inventory_tracking_disabled")
        if source_skus and shopify_state["variants_with_sku"] != len(source_skus):
            drifts.append("sku_count_mismatch")

    current_status = clean((shopify or {}).get("status")).upper()
    lightspeed_source = clean(
        (
            (shopify or {}).get("lightspeed_source_metafield")
            or {}
        ).get("value")
    )
    catalog_status_sync = clean(
        (
            (shopify or {}).get("catalog_status_sync_metafield")
            or {}
        ).get("value")
    )
    managed_by_sync = (
        lightspeed_source == "c_series_csv"
        and catalog_status_sync != "manual_review"
    )
    status_action = (
        "create"
        if not shopify
        else (
            f"{current_status}_to_{decision.status}"
            if current_status != decision.status
            else "unchanged"
        )
    )
    return {
        "internal_id": internal_id,
        "handle": handle,
        "title": title,
        "vendor": vendor,
        "source": {
            "visibility": list(visible_values),
            "variant_count": len(rows),
            "inventory_rows": len(inventory_rows),
            "total_inventory": decimal_json(total_inventory),
            "negative_inventory_variants": sum(
                1
                for value in stock_values
                if value is not None and value < 0
            ),
            "image_count": len(images),
            "description_present": bool(source_description(first)),
            "sku_count": len(source_skus),
            "ean_count": len(source_eans),
            "prices": source_prices,
        },
        "shopify": {
            "id": (shopify or {}).get("id"),
            "status": current_status or None,
            "published": bool((shopify or {}).get("published_at")),
            "vendor": (shopify or {}).get("vendor"),
            "brand": (
                (shopify or {}).get("custom_brand_metafield") or {}
            ).get("value"),
            "lightspeed_source": lightspeed_source or None,
            "catalog_status_sync": catalog_status_sync or None,
            "lightspeed_internal_id": (
                (
                    (shopify or {}).get(
                        "lightspeed_internal_id_metafield"
                    )
                    or {}
                ).get("value")
            ),
            "managed_by_sync": managed_by_sync,
            "image_count": len(current_images),
            **shopify_state,
        },
        "decision": {
            "desired_status": decision.status,
            "reasons": list(decision.reasons),
            "blocking_quality_issues": list(quality_issues),
            "status_action": status_action,
            "sync_status_action": (
                status_action if managed_by_sync else "manual_review_only"
            ),
        },
        "warnings": warnings,
        "drifts": drifts,
    }


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Read-only catalog eligibility and drift audit across C-Series "
            "exports and a Shopify baseline."
        )
    )
    parser.add_argument("--products-source", required=True)
    parser.add_argument("--inventory-source", required=True)
    parser.add_argument(
        "--shopify-products",
        required=True,
        help="Path to products.json from a Shopify baseline.",
    )
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--max-source-age-hours",
        type=float,
        default=24,
        help="Maximum safe source age for status application. Default: 24.",
    )
    args = parser.parse_args()

    product_path = Path(args.products_source).resolve()
    inventory_path = Path(args.inventory_source).resolve()
    shopify_path = Path(args.shopify_products).resolve()
    product_rows = read_csv(product_path)
    inventory_rows = read_csv(inventory_path)
    grouped, excluded_rows = group_source_products(product_rows)
    duplicate_skus = duplicate_source_identifiers(grouped, "SKU")
    duplicate_eans = duplicate_source_identifiers(grouped, "EAN")
    inventory_index = inventory_by_variant(inventory_rows)
    shopify_products = read_json(shopify_path).get("products") or []
    shopify_by_handle = {
        clean(product.get("handle")): product
        for product in shopify_products
    }

    records = [
        source_product_record(
            internal_id,
            rows,
            inventory_index,
            shopify_by_handle.get(source_handle(rows[0])),
        )
        for internal_id, rows in grouped.items()
    ]
    records.sort(key=lambda item: item["handle"])
    source_handles = {record["handle"] for record in records}
    shopify_only = sorted(
        {
            clean(product.get("handle"))
            for product in shopify_products
            if clean(product.get("handle")) not in source_handles
        }
    )
    desired_statuses = Counter(
        record["decision"]["desired_status"] for record in records
    )
    current_statuses = Counter(
        record["shopify"]["status"] or "MISSING" for record in records
    )
    status_actions = Counter(
        record["decision"]["status_action"] for record in records
    )
    sync_status_actions = Counter(
        record["decision"]["sync_status_action"] for record in records
    )
    reason_counts = Counter(
        reason
        for record in records
        for reason in record["decision"]["reasons"]
    )
    warning_counts = Counter(
        warning
        for record in records
        for warning in record["warnings"]
    )
    drift_counts = Counter(
        drift
        for record in records
        for drift in record["drifts"]
    )
    excluded_products = {
        (item["internal_id"], item["handle"], item["vendor"])
        for item in excluded_rows
    }
    product_age_hours = source_age_hours(product_path)
    inventory_age_hours = source_age_hours(inventory_path)
    sources_fresh = (
        product_age_hours <= args.max_source_age_hours
        and inventory_age_hours <= args.max_source_age_hours
    )
    report = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": "read-only",
        "sources": {
            "products": {
                "path": str(product_path),
                "sha256": sha256(product_path),
                "rows": len(product_rows),
                "age_hours": round(product_age_hours, 2),
            },
            "inventory": {
                "path": str(inventory_path),
                "sha256": sha256(inventory_path),
                "rows": len(inventory_rows),
                "age_hours": round(inventory_age_hours, 2),
            },
            "shopify_products": {
                "path": str(shopify_path),
                "sha256": sha256(shopify_path),
                "products": len(shopify_products),
            },
        },
        "summary": {
            "source_products_after_policy": len(records),
            "retired_products_excluded": len(excluded_products),
            "shopify_products": len(shopify_products),
            "shopify_only_products": len(shopify_only),
            "current_statuses": dict(sorted(current_statuses.items())),
            "desired_statuses": dict(sorted(desired_statuses.items())),
            "status_actions": dict(sorted(status_actions.items())),
            "sync_status_actions": dict(
                sorted(sync_status_actions.items())
            ),
            "sync_managed_products": sum(
                1
                for record in records
                if record["shopify"]["managed_by_sync"]
            ),
            "manual_products": sum(
                1
                for record in records
                if not record["shopify"]["managed_by_sync"]
            ),
            "decision_reasons": dict(sorted(reason_counts.items())),
            "warnings": dict(sorted(warning_counts.items())),
            "drifts": dict(sorted(drift_counts.items())),
            "duplicate_source_sku_values": len(duplicate_skus),
            "duplicate_source_ean_values": len(duplicate_eans),
            "sources_fresh": sources_fresh,
            "max_source_age_hours": args.max_source_age_hours,
            "safe_to_apply_statuses": (
                sources_fresh
                and not duplicate_skus
                and not duplicate_eans
            ),
        },
        "shopify_only_handles": shopify_only,
        "duplicate_source_skus": duplicate_skus,
        "duplicate_source_eans": duplicate_eans,
        "records": records,
    }
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report["summary"], indent=2, ensure_ascii=False))
    print(f"Audit written to {output}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
