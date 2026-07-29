#!/usr/bin/env python3

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from catalog_policy import (
    creation_quality_issues,
    decide_catalog_status,
    is_retired_product,
)
from lightspeed_variant_sync import (
    DEFAULT_MAX_SOURCE_AGE_HOURS,
    source_metadata,
)

DEFAULT_API_VERSION = "2026-04"
DEFAULT_PLAN_FILE = Path(".tmp") / "lightspeed-product-import-plan.json"
DEFAULT_RESULTS_FILE = Path(".tmp") / "lightspeed-product-import-results.json"
LIGHTSPEED_NAMESPACE = "custom"
LIGHTSPEED_ID_KEY = "lightspeed_internal_id"
LIGHTSPEED_SOURCE_KEY = "lightspeed_source"
LIGHTSPEED_SOURCE_VALUE = "c_series_csv"
LIGHTSPEED_VARIANT_ID_KEY = "lightspeed_c_series_variant_id"
DEFAULT_BRAND_NAMESPACE = "custom"
DEFAULT_BRAND_KEY = "brand"
DEFAULT_BRAND_TYPE = "single_line_text_field"
MAX_PRODUCT_OPTIONS = 3
MAX_PRODUCT_SET_RETRIES = 4


@dataclass
class SourceProduct:
    internal_id: str
    handle: str
    title: str
    vendor: str
    description_html: str
    product_type: str
    tags: List[str]
    images: List[str]
    options: List[Dict[str, Any]]
    variants: List[Dict[str, Any]]
    visible_values: List[str]
    total_inventory: Optional[Decimal]
    desired_status: str
    catalog_reasons: List[str]
    quality_issues: List[str]
    input: Dict[str, Any]
    warnings: List[str]


def load_dotenv(file_path: Path) -> None:
    if not file_path.exists():
        return
    for line in file_path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$", line)
        if not match:
            continue
        key, raw_value = match.group(1), match.group(2)
        if key in os.environ:
            continue
        os.environ[key] = parse_env_value(raw_value)


def parse_env_value(value: str) -> str:
    trimmed = value.strip()
    if (trimmed.startswith('"') and trimmed.endswith('"')) or (trimmed.startswith("'") and trimmed.endswith("'")):
        return trimmed[1:-1]
    return trimmed


def normalize_shop(value: str) -> str:
    if not value:
        return ""
    cleaned = value.strip().replace("http://", "").replace("https://", "").split("/")[0]
    if not cleaned:
        return ""
    return cleaned if "." in cleaned else f"{cleaned}.myshopify.com"


def http_post_json(url: str, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None, timeout: int = 45) -> Tuple[str, int, Dict[str, str]]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers or {},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8"), response.status, dict(response.getheaders())
    except urllib.error.HTTPError as exc:
        return exc.read().decode("utf-8", errors="replace"), exc.code, dict(exc.headers.items())


def http_put_json(url: str, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None, timeout: int = 45) -> Tuple[str, int, Dict[str, str]]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers or {},
        method="PUT",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8"), response.status, dict(response.getheaders())
    except urllib.error.HTTPError as exc:
        return exc.read().decode("utf-8", errors="replace"), exc.code, dict(exc.headers.items())


def http_post_form(url: str, data: Dict[str, Any], headers: Optional[Dict[str, str]] = None, timeout: int = 45) -> Tuple[str, int, Dict[str, str]]:
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(data).encode("utf-8"),
        headers=headers or {},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8"), response.status, dict(response.getheaders())
    except urllib.error.HTTPError as exc:
        return exc.read().decode("utf-8", errors="replace"), exc.code, dict(exc.headers.items())


def get_client_credentials_token(shop: str, client_id: str, client_secret: str) -> str:
    response_text, status, _ = http_post_form(
        f"https://{shop}/admin/oauth/access_token",
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    data = parse_json_response(response_text)
    if status < 200 or status >= 300:
        message = data.get("error_description") or data.get("error") or response_text or "Unknown error"
        raise RuntimeError(f"Shopify client_credentials failed ({status}): {message}")
    token = data.get("access_token")
    if not token:
        raise RuntimeError("Shopify did not return an access_token.")
    print(f"Temporary token acquired. Scope: {data.get('scope', '(not returned)')}. Not stored in .env.")
    return token


def shopify_graphql(endpoint: str, token: str, query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
    last_error: Optional[Exception] = None
    for attempt in range(1, MAX_PRODUCT_SET_RETRIES + 1):
        response_text, status, headers = http_post_json(
            endpoint,
            {"query": query, "variables": variables},
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-Shopify-Access-Token": token,
            },
        )
        payload = parse_json_response(response_text)
        retry_after = int(headers.get("Retry-After", "0") or "0")

        if status == 429 or status >= 500:
            last_error = RuntimeError(f"Shopify API request failed with HTTP {status}: {json.dumps(payload)}")
            if attempt < MAX_PRODUCT_SET_RETRIES:
                time.sleep(retry_after or attempt)
                continue

        if status < 200 or status >= 300:
            raise RuntimeError(f"Shopify API request failed with HTTP {status}: {json.dumps(payload)}")

        if payload.get("errors"):
            messages = "; ".join(error.get("message", "") for error in payload["errors"])
            raise RuntimeError(f"Shopify GraphQL errors: {messages}")

        wait_for_throttle_budget(payload.get("extensions", {}).get("cost", {}).get("throttleStatus"))
        return payload.get("data") or {}

    raise last_error or RuntimeError("Shopify API request failed after retries.")


def parse_json_response(value: str) -> Dict[str, Any]:
    try:
        data = json.loads(value or "{}")
    except json.JSONDecodeError:
        data = {"raw": value}
    return data if isinstance(data, dict) else {"value": data}


def wait_for_throttle_budget(throttle_status: Optional[Dict[str, Any]]) -> None:
    if not throttle_status:
        return
    currently_available = float(throttle_status.get("currentlyAvailable", 0))
    restore_rate = float(throttle_status.get("restoreRate", 0))
    if currently_available < 100 and restore_rate > 0:
        wait_ms = int(((100 - currently_available) / restore_rate) * 1000)
        time.sleep(max(wait_ms / 1000.0, 0.25))


def read_lightspeed_rows(path: Path) -> List[Dict[str, str]]:
    last_error: Optional[Exception] = None
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            with path.open(newline="", encoding=encoding) as csv_file:
                return list(csv.DictReader(csv_file, delimiter=";"))
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
    raise RuntimeError(f"Could not read {path}: {last_error}")


def build_source_products(
    rows: List[Dict[str, str]],
    language: str,
    status: str,
    status_from_visible: bool,
    include_images: bool,
    brand_metafield: Optional[Dict[str, str]],
    enforce_catalog_policy: bool,
    stage_new_products: bool,
) -> Tuple[List[SourceProduct], List[str]]:
    grouped: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    warnings: List[str] = []
    for index, row in enumerate(rows, start=2):
        internal_id = clean(row.get("Internal_ID"))
        if not internal_id:
            warnings.append(f"CSV row {index}: missing Internal_ID; row skipped.")
            continue
        grouped[internal_id].append(row)

    products: List[SourceProduct] = []
    for internal_id, product_rows in grouped.items():
        products.append(
            build_source_product(
                internal_id,
                product_rows,
                language,
                status,
                status_from_visible,
                include_images,
                brand_metafield,
                enforce_catalog_policy,
                stage_new_products,
            )
        )

    handle_counts = Counter(product.handle for product in products if product.handle)
    duplicate_handles = [handle for handle, count in handle_counts.items() if count > 1]
    if duplicate_handles:
        warnings.append(f"Duplicate source handles found: {', '.join(duplicate_handles[:20])}")

    return products, warnings


def build_source_product(
    internal_id: str,
    rows: List[Dict[str, str]],
    language: str,
    status: str,
    status_from_visible: bool,
    include_images: bool,
    brand_metafield: Optional[Dict[str, str]],
    enforce_catalog_policy: bool,
    stage_new_products: bool,
) -> SourceProduct:
    first = rows[0]
    warnings: List[str] = []
    title = first_nonempty(
        localized(first, language, "Title_Short"),
        localized(first, language, "Title_Long"),
        localized(first, fallback_language(language), "Title_Short"),
        localized(first, fallback_language(language), "Title_Long"),
        f"Lightspeed product {internal_id}",
    )
    title = truncate(collapse_space(title), 255)
    handle = normalize_handle(first_nonempty(
        localized(first, language, "URL"),
        localized(first, fallback_language(language), "URL"),
        title,
    ))
    vendor = truncate(collapse_space(clean(first.get("Brand"))), 255)
    description_html = first_nonempty(
        localized(first, language, "Description_Long"),
        localized(first, language, "Description_Short"),
        localized(first, fallback_language(language), "Description_Long"),
        localized(first, fallback_language(language), "Description_Short"),
    )
    product_type = truncate(collapse_space(last_nonempty(
        localized(first, language, "Category_3"),
        localized(first, language, "Category_2"),
        localized(first, language, "Category_1"),
        localized(first, fallback_language(language), "Category_3"),
        localized(first, fallback_language(language), "Category_2"),
        localized(first, fallback_language(language), "Category_1"),
    )), 255)
    tags = split_comma_values(first_nonempty(*(clean(row.get("Tags")) for row in rows)))
    images = unique(item for row in rows for item in split_comma_values(clean(row.get("Images"))))
    visible_values = unique(clean(row.get("Visible")).upper() for row in rows if clean(row.get("Visible")))
    options, variants, variant_warnings = build_product_options_and_variants(rows, language)
    warnings.extend(variant_warnings)
    stock_values = [parse_money(clean(row.get("Stock_Level"))) for row in rows]
    total_inventory = (
        sum((value for value in stock_values if value is not None), Decimal("0"))
        if all(value is not None for value in stock_values)
        else None
    )
    quality_issues = list(
        creation_quality_issues(
            title=title,
            vendor=vendor,
            variant_ids=[
                clean(row.get("Internal_Variant_ID")) for row in rows
            ],
            variant_prices=[
                parse_money(clean(row.get("Price"))) for row in rows
            ],
            image_urls=images,
        )
    )
    if enforce_catalog_policy:
        decision = decide_catalog_status(
            visible_values,
            total_inventory,
            quality_issues,
        )
        desired_status = decision.status
        catalog_reasons = list(decision.reasons)
    else:
        desired_status = (
            status_for_visible(visible_values, status)
            if status_from_visible
            else status
        )
        catalog_reasons = ["legacy_status_mode"]
    create_status = "DRAFT" if stage_new_products else desired_status

    product_input: Dict[str, Any] = {
        "title": title,
        "handle": handle,
        "status": create_status,
        "productOptions": options,
        "variants": variants,
        "metafields": [
            {
                "namespace": LIGHTSPEED_NAMESPACE,
                "key": LIGHTSPEED_ID_KEY,
                "type": "single_line_text_field",
                "value": internal_id,
            },
            {
                "namespace": LIGHTSPEED_NAMESPACE,
                "key": LIGHTSPEED_SOURCE_KEY,
                "type": "single_line_text_field",
                "value": LIGHTSPEED_SOURCE_VALUE,
            },
        ],
    }
    if vendor and brand_metafield:
        product_input["metafields"].append({
            "namespace": brand_metafield["namespace"],
            "key": brand_metafield["key"],
            "type": brand_metafield["type"],
            "value": metafield_value_for_vendor(vendor, brand_metafield["type"]),
        })
    if vendor:
        product_input["vendor"] = vendor
    if description_html:
        product_input["descriptionHtml"] = description_html
    if product_type:
        product_input["productType"] = product_type
    if tags:
        product_input["tags"] = tags
    if include_images and images:
        product_input["files"] = [
            {
                "alt": title,
                "contentType": "IMAGE",
                "originalSource": image,
            }
            for image in images
        ]

    if not handle:
        warnings.append("Product has no usable handle.")
    if len(options) > MAX_PRODUCT_OPTIONS:
        warnings.append(f"Product has {len(options)} options; Shopify supports at most {MAX_PRODUCT_OPTIONS}.")

    return SourceProduct(
        internal_id=internal_id,
        handle=handle,
        title=title,
        vendor=vendor,
        description_html=description_html,
        product_type=product_type,
        tags=tags,
        images=images,
        options=options,
        variants=variants,
        visible_values=visible_values,
        total_inventory=total_inventory,
        desired_status=desired_status,
        catalog_reasons=catalog_reasons,
        quality_issues=quality_issues,
        input=product_input,
        warnings=warnings,
    )


def build_product_options_and_variants(rows: List[Dict[str, str]], language: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
    option_names: List[str] = []
    parsed_rows: List[Tuple[Dict[str, str], List[Tuple[str, str]]]] = []
    warnings: List[str] = []

    for row in rows:
        pairs = parse_variant_pairs(first_nonempty(
            localized(row, language, "Variant"),
            localized(row, fallback_language(language), "Variant"),
        ))
        parsed_rows.append((row, pairs))
        for name, _ in pairs:
            if name not in option_names:
                option_names.append(name)

    if not option_names:
        option_names = ["Title"]

    option_values: Dict[str, List[str]] = {name: [] for name in option_names}
    variants: List[Dict[str, Any]] = []
    seen_combinations: set = set()
    for position, (row, pairs) in enumerate(parsed_rows, start=1):
        pair_map = {name: value for name, value in pairs}
        selected_values: List[str] = []
        option_value_inputs: List[Dict[str, str]] = []
        for option_name in option_names:
            value = truncate(collapse_space(pair_map.get(option_name) or "Default Title"), 255)
            selected_values.append(value)
            if value not in option_values[option_name]:
                option_values[option_name].append(value)
            option_value_inputs.append({"optionName": option_name, "name": value})

        combination = tuple(selected_values)
        if combination in seen_combinations:
            warnings.append(f"Duplicate variant combination skipped: {combination}")
            continue
        seen_combinations.add(combination)

        variant: Dict[str, Any] = {
            "position": position,
            "optionValues": option_value_inputs,
            "inventoryPolicy": "DENY",
        }
        variant_id = clean(row.get("Internal_Variant_ID"))
        if variant_id:
            variant["metafields"] = [
                {
                    "namespace": LIGHTSPEED_NAMESPACE,
                    "key": LIGHTSPEED_VARIANT_ID_KEY,
                    "type": "single_line_text_field",
                    "value": variant_id,
                }
            ]
        sku = clean(row.get("SKU"))
        if sku:
            variant["sku"] = sku
        barcode = clean(row.get("EAN"))
        if barcode:
            variant["barcode"] = barcode
        price = parse_money(clean(row.get("Price")))
        if price is not None:
            variant["price"] = money_to_shopify(price)
        compare_at = parse_money(clean(row.get("Price_Old")))
        if compare_at is not None and price is not None and compare_at > price:
            variant["compareAtPrice"] = money_to_shopify(compare_at)
        variants.append(variant)

    options = [
        {
            "name": option_name,
            "position": index,
            "values": [{"name": value} for value in values],
        }
        for index, (option_name, values) in enumerate(option_values.items(), start=1)
    ]
    return options, variants, warnings


def parse_variant_pairs(value: str) -> List[Tuple[str, str]]:
    cleaned = clean(value)
    if not cleaned:
        return [("Title", "Default Title")]
    try:
        pieces = [piece.strip().strip('"') for piece in next(csv.reader([cleaned], skipinitialspace=True))]
    except csv.Error:
        pieces = [cleaned]

    pairs: List[Tuple[str, str]] = []
    for piece in pieces:
        piece = piece.strip()
        if not piece:
            continue
        if ":" in piece:
            raw_name, raw_value = piece.split(":", 1)
            name = collapse_space(raw_name) or "Option"
            value = collapse_space(raw_value) or "Default Title"
        else:
            name = "Title"
            value = collapse_space(piece) or "Default Title"
        pairs.append((name, value))
    return pairs or [("Title", "Default Title")]


def fetch_existing_products(endpoint: str, token: str, page_size: int, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    products: List[Dict[str, Any]] = []
    after: Optional[str] = None
    while True:
        first = page_size if limit is None else min(page_size, max(limit - len(products), 0))
        if first <= 0:
            break
        data = shopify_graphql(
            endpoint,
            token,
            """
            query ExistingProducts($first: Int!, $after: String, $namespace: String!, $key: String!) {
              products(first: $first, after: $after, sortKey: ID) {
                nodes {
                  id
                  title
                  handle
                  vendor
                  metafield(namespace: $namespace, key: $key) {
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
            {
                "first": first,
                "after": after,
                "namespace": LIGHTSPEED_NAMESPACE,
                "key": LIGHTSPEED_ID_KEY,
            },
        )
        connection = data.get("products") or {}
        products.extend(connection.get("nodes") or [])
        page_info = connection.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        after = page_info.get("endCursor")
        if limit is not None and len(products) >= limit:
            break
    return products


def build_plan(
    source_products: List[SourceProduct],
    existing_products: List[Dict[str, Any]],
    source_file: Dict[str, Any],
    shop: str,
    api_version: str,
    args: argparse.Namespace,
    source_warnings: List[str],
) -> Dict[str, Any]:
    existing_by_handle = {clean(product.get("handle")).lower(): product for product in existing_products if clean(product.get("handle"))}
    existing_by_lightspeed_id = {
        clean((product.get("metafield") or {}).get("value")): product
        for product in existing_products
        if clean((product.get("metafield") or {}).get("value"))
    }
    source_handle_counts = Counter(product.handle for product in source_products if product.handle)

    creates: List[Dict[str, Any]] = []
    skips: List[Dict[str, Any]] = []
    invalid: List[Dict[str, Any]] = []

    for product in source_products:
        product_errors = validation_errors(product, source_handle_counts)
        if product_errors:
            invalid.append(plan_item(product, reason="invalid_source", messages=product_errors))
            continue

        existing_by_id = existing_by_lightspeed_id.get(product.internal_id)
        if existing_by_id:
            skips.append(plan_item(product, reason="existing_lightspeed_id", existing=existing_by_id))
            continue

        existing_by_handle_match = existing_by_handle.get(product.handle.lower())
        if existing_by_handle_match:
            skips.append(plan_item(product, reason="existing_handle", existing=existing_by_handle_match))
            continue

        creates.append(plan_item(product, reason="create"))

    if args.max_create is not None:
        creates = creates[: args.max_create]

    all_product_warnings = [
        f"{product.handle or product.internal_id}: {warning}"
        for product in source_products
        for warning in product.warnings
    ]

    return {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "mode": "apply" if args.apply else "dry-run",
        "sourceFile": source_file,
        "shop": shop,
        "apiVersion": api_version,
        "options": {
            "language": args.language,
            "status": args.status,
            "statusFromVisible": args.status_from_visible,
            "enforceCatalogPolicy": args.enforce_catalog_policy,
            "stageNewProducts": not args.publish_eligible_on_create,
            "includeImages": args.include_images,
            "includeBrandMetafield": not args.no_brand_metafield,
            "brandMetafieldNamespace": getattr(args, "brand_metafield_namespace", None),
            "brandMetafieldKey": getattr(args, "brand_metafield_key", None),
            "brandMetafieldType": getattr(args, "brand_metafield_type", None),
            "offline": args.offline,
            "limit": args.limit,
            "maxCreate": args.max_create,
            "maxSourceAgeHours": args.max_source_age_hours,
        },
        "summary": {
            "sourceProducts": len(source_products),
            "existingShopifyProductsScanned": len(existing_products),
            "toCreate": len(creates),
            "skippedExisting": len(skips),
            "invalidSourceProducts": len(invalid),
            "warnings": len(source_warnings) + len(all_product_warnings),
            "sourceFresh": (
                source_file["ageHours"] <= args.max_source_age_hours
            ),
        },
        "warnings": source_warnings + all_product_warnings,
        "creates": creates,
        "skips": skips,
        "invalid": invalid,
    }


def validation_errors(product: SourceProduct, source_handle_counts: Counter) -> List[str]:
    errors: List[str] = []
    if not product.handle:
        errors.append("missing handle")
    if not product.title:
        errors.append("missing title")
    if source_handle_counts.get(product.handle, 0) > 1:
        errors.append("duplicate source handle")
    if len(product.options) > MAX_PRODUCT_OPTIONS:
        errors.append(f"too many options ({len(product.options)})")
    if not product.variants:
        errors.append("missing variants")
    errors.extend(
        f"quality gate: {issue}"
        for issue in product.quality_issues
        if issue != "missing_image"
    )
    return errors


def plan_item(
    product: SourceProduct,
    reason: str,
    existing: Optional[Dict[str, Any]] = None,
    messages: Optional[List[str]] = None,
) -> Dict[str, Any]:
    item: Dict[str, Any] = {
        "reason": reason,
        "source": {
            "internalId": product.internal_id,
            "handle": product.handle,
            "title": product.title,
            "vendor": product.vendor,
            "productType": product.product_type,
            "variantCount": len(product.variants),
            "imageCount": len(product.images),
            "visibleValues": product.visible_values,
            "totalInventory": (
                float(product.total_inventory)
                if product.total_inventory is not None
                else None
            ),
            "desiredStatus": product.desired_status,
            "plannedCreateStatus": product.input.get("status"),
            "catalogReasons": product.catalog_reasons,
            "qualityIssues": product.quality_issues,
            "warnings": product.warnings,
        },
        "productSetInput": product.input,
        "images": product.images,
    }
    if existing:
        item["existing"] = {
            "id": existing.get("id", ""),
            "handle": existing.get("handle", ""),
            "title": existing.get("title", ""),
            "vendor": existing.get("vendor", ""),
        }
    if messages:
        item["messages"] = messages
    return item


def apply_plan(
    endpoint: str,
    token: str,
    plan: Dict[str, Any],
    continue_on_error: bool,
    publish_active_to_online_store: bool,
) -> Dict[str, Any]:
    results: Dict[str, Any] = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "shop": plan.get("shop"),
        "apiVersion": plan.get("apiVersion"),
        "created": [],
        "errors": [],
        "publicationErrors": [],
    }
    creates = plan.get("creates") or []
    print(f"Applying {len(creates)} new product creates.")
    for index, item in enumerate(creates, start=1):
        source = item["source"]
        label = f"{source['title']} ({source['handle']})"
        created = None
        try:
            product = create_product(endpoint, token, item["productSetInput"])
            created = {
                "sourceInternalId": source["internalId"],
                "sourceHandle": source["handle"],
                "product": product,
                "plannedImageCount": source["imageCount"],
                "onlineStorePublished": False,
            }
            results["created"].append(created)
            if publish_active_to_online_store and item["productSetInput"].get("status") == "ACTIVE":
                published_product = publish_product_to_online_store(endpoint, token, product["id"])
                created["onlineStorePublished"] = True
                created["publishedAt"] = published_product.get("published_at")
            print(f"Created {index}/{len(creates)}: {label} -> {product.get('id')}")
        except Exception as exc:
            if created is not None:
                results["publicationErrors"].append(
                    {
                        "sourceInternalId": source.get("internalId"),
                        "sourceHandle": source.get("handle"),
                        "title": source.get("title"),
                        "message": str(exc),
                    }
                )
                print(f"PUBLICATION ERROR {index}/{len(creates)}: {label}: {exc}", file=sys.stderr)
                if not continue_on_error:
                    raise
                continue
            error = {
                "sourceInternalId": source.get("internalId"),
                "sourceHandle": source.get("handle"),
                "title": source.get("title"),
                "message": str(exc),
            }
            results["errors"].append(error)
            print(f"ERROR {index}/{len(creates)}: {label}: {exc}", file=sys.stderr)
            if not continue_on_error:
                raise
    return results


def publish_product_to_online_store(endpoint: str, token: str, product_id: str) -> Dict[str, Any]:
    numeric_id = product_id.rsplit("/", 1)[-1]
    rest_base_url = endpoint.rsplit("/", 1)[0]
    url = f"{rest_base_url}/products/{numeric_id}.json"
    published_at = datetime.now(timezone.utc).isoformat()
    last_error: Optional[Exception] = None

    for attempt in range(1, MAX_PRODUCT_SET_RETRIES + 1):
        response_text, status, headers = http_put_json(
            url,
            {"product": {"id": int(numeric_id), "published_at": published_at}},
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Shopify-Access-Token": token,
            },
        )
        payload = parse_json_response(response_text)
        if status == 429 or status >= 500:
            last_error = RuntimeError(f"Shopify product publish failed with HTTP {status}: {json.dumps(payload)}")
            if attempt < MAX_PRODUCT_SET_RETRIES:
                time.sleep(int(headers.get("Retry-After", "0") or "0") or attempt)
                continue
        if status < 200 or status >= 300:
            raise RuntimeError(f"Shopify product publish failed with HTTP {status}: {json.dumps(payload)}")
        product = payload.get("product") or {}
        if not product.get("published_at"):
            raise RuntimeError("Shopify accepted the product update but returned no published_at value.")
        return product

    raise last_error or RuntimeError("Shopify product publish failed after retries.")


def create_product(endpoint: str, token: str, product_input: Dict[str, Any]) -> Dict[str, Any]:
    data = shopify_graphql(
        endpoint,
        token,
        """
        mutation CreateProductFromLightspeed($input: ProductSetInput!, $synchronous: Boolean!) {
          productSet(input: $input, synchronous: $synchronous) {
            product {
              id
              title
              handle
              status
            }
            userErrors {
              field
              message
            }
          }
        }
        """,
        {"input": product_input, "synchronous": True},
    )
    payload = data.get("productSet") or {}
    user_errors = payload.get("userErrors") or []
    if user_errors:
        raise RuntimeError(format_user_errors("productSet", user_errors))
    product = payload.get("product")
    if not product or not product.get("id"):
        raise RuntimeError("Shopify did not return a product for productSet.")
    return product


def format_user_errors(operation: str, user_errors: List[Dict[str, Any]]) -> str:
    details = []
    for error in user_errors:
        field = ".".join(str(part) for part in (error.get("field") or []))
        message = error.get("message") or "Unknown error"
        details.append(f"{field}: {message}" if field else message)
    return f"Shopify rejected {operation}: " + "; ".join(details)


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def print_plan_summary(plan: Dict[str, Any]) -> None:
    summary = plan["summary"]
    print(f"Source products: {summary['sourceProducts']}")
    print(f"Existing Shopify products scanned: {summary['existingShopifyProductsScanned']}")
    print(f"New products to create: {summary['toCreate']}")
    print(f"Existing products skipped: {summary['skippedExisting']}")
    print(f"Invalid source products: {summary['invalidSourceProducts']}")
    print(f"Warnings: {summary['warnings']}")
    print(f"Source file fresh: {summary['sourceFresh']}")

    creates = plan.get("creates") or []
    if creates:
        print("\nSample creates:")
        for item in creates[:15]:
            source = item["source"]
            print(
                f"- {source['title']} ({source['handle']}): "
                f"{source['variantCount']} variants, {source['imageCount']} images, "
                f"create {source['plannedCreateStatus']}, desired {source['desiredStatus']}"
            )
        if len(creates) > 15:
            print(f"...and {len(creates) - 15} more.")

    invalid = plan.get("invalid") or []
    if invalid:
        print("\nInvalid samples:")
        for item in invalid[:10]:
            print(f"- {item['source']['handle'] or item['source']['internalId']}: {', '.join(item.get('messages') or [])}")


def clean(value: Any) -> str:
    return str(value or "").strip()


def collapse_space(value: str) -> str:
    return re.sub(r"\s+", " ", clean(value)).strip()


def truncate(value: str, limit: int) -> str:
    value = clean(value)
    return value if len(value) <= limit else value[:limit].rstrip()


def localized(row: Dict[str, str], language: str, suffix: str) -> str:
    return clean(row.get(f"{language.upper()}_{suffix}"))


def fallback_language(language: str) -> str:
    return "nl" if language == "us" else "us"


def first_nonempty(*values: str) -> str:
    for value in values:
        if clean(value):
            return clean(value)
    return ""


def last_nonempty(*values: str) -> str:
    for value in values:
        if clean(value):
            return clean(value)
    return ""


def split_comma_values(value: str) -> List[str]:
    return unique(part.strip() for part in clean(value).split(",") if part.strip())


def unique(values: Iterable[str]) -> List[str]:
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def normalize_handle(value: str) -> str:
    base = clean(value).lower()
    base = base.replace("_", "-")
    base = re.sub(r"[^a-z0-9-]+", "-", base)
    base = re.sub(r"-{2,}", "-", base)
    return base.strip("-")


def parse_money(value: str) -> Optional[Decimal]:
    cleaned = clean(value)
    if not cleaned:
        return None
    cleaned = re.sub(r"[^0-9,.-]", "", cleaned)
    if not cleaned:
        return None
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    else:
        cleaned = cleaned.replace(",", ".")
    try:
        amount = Decimal(cleaned)
    except InvalidOperation:
        return None
    return amount if amount >= 0 else None


def money_to_shopify(value: Decimal) -> str:
    return f"{value:.2f}"


def metafield_value_for_vendor(vendor: str, metafield_type: str) -> str:
    if metafield_type.startswith("list."):
        return json.dumps([vendor])
    if metafield_type == "json":
        return json.dumps(vendor)
    return vendor


def status_for_visible(visible_values: List[str], fallback_status: str) -> str:
    if not visible_values:
        return fallback_status
    if any(value in {"Y", "S"} for value in visible_values):
        return "ACTIVE"
    return "DRAFT"


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def verify_created_products(
    live_products: List[Dict[str, Any]],
    created: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    live_by_id = {
        product.get("id"): product
        for product in live_products
        if product.get("id")
    }
    failures = []
    for record in created:
        created_product = record.get("product") or {}
        live = live_by_id.get(created_product.get("id"))
        reasons = []
        if not live:
            reasons.append("missing_after_create")
        else:
            if live.get("handle") != record.get("sourceHandle"):
                reasons.append("handle_mismatch")
            live_internal_id = clean(
                (live.get("metafield") or {}).get("value")
            )
            if live_internal_id != str(record.get("sourceInternalId") or ""):
                reasons.append("lightspeed_internal_id_mismatch")
        if reasons:
            failures.append({
                "productId": created_product.get("id"),
                "handle": record.get("sourceHandle"),
                "reasons": reasons,
            })
    return failures


def main() -> None:
    project_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description="Plan and apply Lightspeed C-Series product CSV imports into Shopify.",
    )
    parser.add_argument("--source", required=True, help="Path to the Lightspeed C-Series products CSV export.")
    parser.add_argument("--apply", action="store_true", help="Create missing Shopify products. Dry-run is the default.")
    parser.add_argument("--offline", action="store_true", help="Do not connect to Shopify; treat all valid source products as new.")
    parser.add_argument("--shop", help="Shopify store domain.")
    parser.add_argument("--token", help="Shopify Admin access token.")
    parser.add_argument("--api-key", help="Shopify app client ID/API key.")
    parser.add_argument("--api-secret", help="Shopify app client secret.")
    parser.add_argument("--prefer-client-credentials", action="store_true", help="Use app client_credentials even when SHOPIFY_ADMIN_ACCESS_TOKEN is set.")
    parser.add_argument("--api-version", default=DEFAULT_API_VERSION, help="Shopify Admin API version.")
    parser.add_argument("--language", choices=["us", "nl"], default="us", help="Lightspeed language columns to prefer.")
    parser.add_argument("--status", choices=["DRAFT", "ACTIVE", "ARCHIVED"], default="DRAFT", help="Shopify status for new products.")
    parser.add_argument("--status-from-visible", action="store_true", help="Map Lightspeed Visible Y/S to ACTIVE and N to DRAFT.")
    parser.add_argument(
        "--legacy-status-mode",
        dest="enforce_catalog_policy",
        action="store_false",
        help=(
            "Disable the catalog policy and use --status/--status-from-visible. "
            "The catalog policy is enabled by default."
        ),
    )
    parser.set_defaults(enforce_catalog_policy=True)
    parser.add_argument(
        "--publish-eligible-on-create",
        action="store_true",
        help=(
            "Create policy-eligible products as ACTIVE immediately. By default "
            "all new products are staged as DRAFT until inventory verification."
        ),
    )
    parser.add_argument("--skip-online-store-publish", action="store_true", help="Keep newly created ACTIVE products unpublished on the Online Store.")
    parser.add_argument("--include-images", dest="include_images", action="store_true", default=True, help="Create Shopify media from Lightspeed image URLs during --apply.")
    parser.add_argument("--no-images", dest="include_images", action="store_false", help="Skip Shopify media creation during --apply.")
    parser.add_argument("--brand-metafield-namespace", help="Product brand metafield namespace. Default: METAFIELD_NAMESPACE or custom.")
    parser.add_argument("--brand-metafield-key", help="Product brand metafield key. Default: METAFIELD_KEY or brands.")
    parser.add_argument("--brand-metafield-type", help="Product brand metafield type. Default: METAFIELD_TYPE or single_line_text_field.")
    parser.add_argument("--no-brand-metafield", action="store_true", help="Do not include a brand metafield on newly created products.")
    parser.add_argument(
        "--include-retired-vendors",
        action="store_true",
        help=(
            "Include catalog items retired from Shopify. By default retired "
            "vendors and explicitly retired product handles are skipped."
        ),
    )
    parser.add_argument("--limit", type=positive_int, help="Limit source products to inspect, useful for smoke tests.")
    parser.add_argument("--max-create", type=positive_int, help="Limit planned creates after existing-product matching.")
    parser.add_argument("--page-size", type=positive_int, default=250, help="Shopify product page size.")
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
    parser.add_argument("--env-file", help="Path to .env. Default: repository .env.")
    parser.add_argument("--plan-file", default=str(project_root / DEFAULT_PLAN_FILE), help="JSON plan output path.")
    parser.add_argument("--results-file", default=str(project_root / DEFAULT_RESULTS_FILE), help="JSON apply results output path.")
    parser.add_argument("--continue-on-error", action="store_true", help="Continue applying remaining products after a Shopify error.")

    args = parser.parse_args()
    if args.page_size > 250:
        raise SystemExit("--page-size must be between 1 and 250.")
    if args.max_source_age_hours <= 0:
        raise SystemExit("--max-source-age-hours must be positive.")
    if args.apply and args.offline:
        raise SystemExit("--apply cannot be used with --offline.")

    env_path = Path(args.env_file or project_root / ".env")
    load_dotenv(env_path)

    source_path = Path(args.source)
    if not source_path.exists():
        raise SystemExit(f"Source CSV not found: {source_path}")
    source_file = source_metadata(source_path)

    shop = normalize_shop(args.shop or os.environ.get("SHOPIFY_SHOP") or os.environ.get("SHOPIFY_STORE_DOMAIN") or os.environ.get("SHOPIFY_STORE"))
    token = args.token or os.environ.get("SHOPIFY_ADMIN_ACCESS_TOKEN") or os.environ.get("SHOPIFY_ACCESS_TOKEN")
    client_id = args.api_key or os.environ.get("SHOPIFY_API_KEY") or os.environ.get("SHOPIFY_CLIENT_ID")
    client_secret = args.api_secret or os.environ.get("SHOPIFY_API_SECRET") or os.environ.get("SHOPIFY_CLIENT_SECRET")
    api_version = args.api_version or os.environ.get("SHOPIFY_API_VERSION") or DEFAULT_API_VERSION
    brand_metafield = None
    if not args.no_brand_metafield:
        brand_metafield = {
            "namespace": args.brand_metafield_namespace or os.environ.get("METAFIELD_NAMESPACE") or DEFAULT_BRAND_NAMESPACE,
            "key": args.brand_metafield_key or os.environ.get("METAFIELD_KEY") or DEFAULT_BRAND_KEY,
            "type": args.brand_metafield_type or os.environ.get("METAFIELD_TYPE") or DEFAULT_BRAND_TYPE,
        }
        args.brand_metafield_namespace = brand_metafield["namespace"]
        args.brand_metafield_key = brand_metafield["key"]
        args.brand_metafield_type = brand_metafield["type"]

    print(f"{'Apply' if args.apply else 'Dry run'}: Lightspeed CSV -> Shopify product import")
    print(f"Source: {source_path}")

    rows = read_lightspeed_rows(source_path)
    source_products, source_warnings = build_source_products(
        rows,
        args.language,
        args.status,
        args.status_from_visible,
        args.include_images,
        brand_metafield,
        args.enforce_catalog_policy,
        not args.publish_eligible_on_create,
    )
    if not args.include_retired_vendors:
        retired_products = [
            product
            for product in source_products
            if is_retired_product(product.handle, product.vendor)
        ]
        source_products = [
            product
            for product in source_products
            if not is_retired_product(product.handle, product.vendor)
        ]
        if retired_products:
            retired_counts = Counter(
                product.vendor or "(no vendor)"
                for product in retired_products
            )
            summary = ", ".join(
                f"{vendor}: {count}"
                for vendor, count in sorted(retired_counts.items())
            )
            source_warnings.append(
                "Retired catalog products skipped by policy: " + summary
            )
            print(
                f"Retired catalog products skipped: {len(retired_products)} "
                f"({summary})"
            )
    if args.limit is not None:
        source_products = source_products[: args.limit]
    print(f"Rows read: {len(rows)}")

    existing_products: List[Dict[str, Any]] = []
    endpoint = ""
    if args.offline:
        shop = shop or "(offline)"
        print("Offline mode: Shopify existing products were not scanned.")
    else:
        if not shop or (not token and (not client_id or not client_secret)):
            parser.print_help()
            raise SystemExit("Missing Shopify credentials. Set SHOPIFY_SHOP and either SHOPIFY_ADMIN_ACCESS_TOKEN or SHOPIFY_API_KEY/SHOPIFY_API_SECRET.")
        endpoint = f"https://{shop}/admin/api/{api_version}/graphql.json"
        if args.prefer_client_credentials or not token:
            print("Getting temporary Shopify Admin token with client_credentials.")
            token = get_client_credentials_token(shop, client_id, client_secret)
        print(f"Scanning existing Shopify products on {shop}.")
        existing_products = fetch_existing_products(endpoint, token, args.page_size)

    plan = build_plan(
        source_products,
        existing_products,
        source_file,
        shop,
        api_version,
        args,
        source_warnings,
    )
    write_json(Path(args.plan_file), plan)
    print_plan_summary(plan)
    print(f"\nPlan written: {Path(args.plan_file)}")

    if not args.apply:
        print("No changes were written. Re-run with --apply after reviewing the plan.")
        return

    if (
        plan["summary"]["toCreate"]
        and not plan["summary"]["sourceFresh"]
        and not args.allow_stale_source
    ):
        raise SystemExit(
            "Refusing --apply because the source file is stale. Export a fresh "
            "file or use --allow-stale-source after explicit review."
        )

    assert token
    results = apply_plan(endpoint, token, plan, args.continue_on_error, not args.skip_online_store_publish)
    live_products = fetch_existing_products(endpoint, token, args.page_size)
    results["verificationFailures"] = verify_created_products(
        live_products,
        results["created"],
    )
    write_json(Path(args.results_file), results)
    print(f"\nResults written: {Path(args.results_file)}")
    print(f"Created products: {len(results['created'])}")
    print(f"Errors: {len(results['errors'])}")
    print(f"Publication errors: {len(results['publicationErrors'])}")
    print(f"Verification failures: {len(results['verificationFailures'])}")
    if results["verificationFailures"]:
        raise SystemExit("Post-import verification failed.")


if __name__ == "__main__":
    main()
