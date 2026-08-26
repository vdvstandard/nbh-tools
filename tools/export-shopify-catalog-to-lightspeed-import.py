#!/usr/bin/env python3
"""Convert a Shopify products.json baseline into Lightspeed import CSVs."""

import argparse
import csv
import json
import re
from pathlib import Path
from urllib.parse import urlparse


LIGHTSPEED_COLUMNS = [
    "Description",
    "System ID",
    "UPC",
    "EAN",
    "Custom SKU",
    "Manufacturer SKU",
    "Vendor",
    "Vendor ID",
    "Vendor cost",
    "Is Default Vendor",
    "Brand",
    "Default Cost",
    "Default - Price",
    "MSRP - Price",
    "Online - Price",
    "Matrix Description",
    "Matrix Attribute Set",
    "Attribute 1",
    "Attribute 2",
    "Attribute 3",
    "Discountable",
    "Taxable",
    "Tax Class",
    "Item Type",
    "Publish To eCom",
    "Serialized",
    "Category",
    "Subcategory 1",
    "Subcategory 2",
    "Subcategory 3",
    "Subcategory 4",
    "Clear Existing Tags",
    "Add Tags",
    "Note",
    "Display Note",
    "Archive",
    "Featured Image",
    "Image",
    "Image",
    "Image",
    "Image",
    "Image",
    "Image",
    "Image",
    "Image",
    "Image",
    "Image",
    "Image",
    "Shop Quantity on Hand",
    "Shop Unit Cost",
    "Shop Reorder Point",
    "Shop Reorder Level",
]

REVIEW_COLUMNS = [
    "shopify_product_id",
    "shopify_variant_id",
    "handle",
    "status",
    "vendor",
    "product_title",
    "variant_title",
    "sku",
    "barcode",
    "price",
    "inventory_quantity",
    "reason",
]

IMAGE_COLUMNS = [
    "shopify_product_id",
    "handle",
    "product_title",
    "image_position",
    "image_filename",
    "image_url",
]

MINIMAL_COLUMNS = ["System ID", "Custom SKU", "Item"]


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--products-json", required=True)
    parser.add_argument(
        "--output-dir",
        default=".tmp/shopify-to-lightspeed-import",
        help="Directory for generated CSVs",
    )
    return parser.parse_args()


def clean_cell(value, limit=255):
    text = "" if value is None else str(value)
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def money(value):
    if value in (None, ""):
        return ""
    return f"{float(value):.2f}"


def is_meaningful_option_name(value):
    return clean_cell(value).casefold() not in {"", "title"}


def is_meaningful_variant(product, variant):
    if len(product.get("variants") or []) > 1:
        return True
    title = clean_cell(variant.get("title"))
    if title and title.casefold() != "default title":
        return True
    return any(is_meaningful_option_name(option.get("name")) for option in product.get("options") or [])


def option_names(product):
    names = []
    for option in sorted(product.get("options") or [], key=lambda item: item.get("position") or 0):
        name = clean_cell(option.get("name"))
        if is_meaningful_option_name(name):
            names.append("Size" if name.casefold() == "waarden" else name)
    return names[:3]


def option_values(variant):
    return [
        clean_cell(variant.get("option1")),
        clean_cell(variant.get("option2")),
        clean_cell(variant.get("option3")),
    ]


def barcode_fields(value):
    barcode = clean_cell(value)
    if not barcode or not re.fullmatch(r"\d+", barcode):
        return "", ""
    if len(barcode) == 13:
        return "", barcode
    if 11 <= len(barcode) <= 18:
        return barcode, ""
    return "", ""


def image_filename(url):
    path = urlparse(url or "").path
    return clean_cell(Path(path).name)


def first_image_filename(product):
    images = sorted(product.get("images") or [], key=lambda item: item.get("position") or 0)
    if not images:
        return ""
    return image_filename(images[0].get("src"))


def add_image_filenames(row, product):
    images = sorted(product.get("images") or [], key=lambda item: item.get("position") or 0)
    filenames = [image_filename(image.get("src")) for image in images if image.get("src")]
    if not filenames:
        return
    row["Featured Image"] = filenames[0]
    for index, filename in enumerate(filenames[1:12], start=0):
        image_columns = [i for i, column in enumerate(LIGHTSPEED_COLUMNS) if column == "Image"]
        if index < len(image_columns):
            row[f"Image__{index}"] = filename


def row_for_variant(product, variant):
    sku = clean_cell(variant.get("sku"))
    upc, ean = barcode_fields(variant.get("barcode"))
    matrix = is_meaningful_variant(product, variant)
    names = option_names(product)
    values = option_values(variant)
    status = clean_cell(product.get("status")).casefold()
    published = bool(product.get("published_at"))
    fulfillment_service = clean_cell(variant.get("fulfillment_service")).casefold()
    is_gift_card = fulfillment_service == "gift_card"

    row = {column: "" for column in LIGHTSPEED_COLUMNS}
    row["System ID"] = sku if re.fullmatch(r"\d{12}", sku or "") else ""
    row["UPC"] = upc
    row["EAN"] = ean
    row["Custom SKU"] = sku
    row["Vendor"] = clean_cell(product.get("vendor"))
    row["Is Default Vendor"] = "Yes"
    row["Brand"] = clean_cell(
        ((product.get("custom_brand_metafield") or {}).get("value"))
        or product.get("vendor")
    )
    row["Default - Price"] = money(variant.get("price"))
    row["MSRP - Price"] = money(variant.get("compare_at_price"))
    row["Online - Price"] = money(variant.get("price"))
    row["Discountable"] = "Yes"
    row["Taxable"] = "Yes" if variant.get("taxable") else "No"
    row["Tax Class"] = "Gift Card" if is_gift_card else "Item"
    row["Item Type"] = "Non-Inventory" if is_gift_card else "Single"
    row["Publish To eCom"] = "Yes" if status == "active" and published else "No"
    row["Serialized"] = "No"
    row["Category"] = clean_cell(product.get("product_type"))
    row["Clear Existing Tags"] = "No"
    row["Add Tags"] = clean_cell(product.get("tags"))
    row["Archive"] = "Yes" if status == "archived" else "No"
    row["Shop Quantity on Hand"] = str(int(variant.get("inventory_quantity") or 0))

    if matrix:
        row["Description"] = ""
        row["Matrix Description"] = clean_cell(product.get("title"))
        row["Matrix Attribute Set"] = "/".join(names) if names else "Size"
        for index, value in enumerate(values[:3], start=1):
            row[f"Attribute {index}"] = value
    else:
        row["Description"] = clean_cell(product.get("title"))

    return row


def lightspeed_row_values(row):
    values = []
    image_index = 0
    for column in LIGHTSPEED_COLUMNS:
        if column == "Image":
            values.append(row.get(f"Image__{image_index}", ""))
            image_index += 1
        else:
            values.append(row.get(column, ""))
    return values


def load_products(path):
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    return data.get("products") or []


def write_lightspeed_csv(path, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(LIGHTSPEED_COLUMNS)
        for row in rows:
            writer.writerow(lightspeed_row_values(row))


def write_csv(path, fieldnames, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main():
    args = parse_args()
    products_json = Path(args.products_json)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    products = load_products(products_json)
    all_rows = []
    update_rows = []
    review_rows = []
    image_rows = []

    for product in products:
        for image in sorted(product.get("images") or [], key=lambda item: item.get("position") or 0):
            image_rows.append(
                {
                    "shopify_product_id": product.get("id"),
                    "handle": clean_cell(product.get("handle")),
                    "product_title": clean_cell(product.get("title")),
                    "image_position": image.get("position"),
                    "image_filename": first_image_filename({"images": [image]}),
                    "image_url": image.get("src"),
                }
            )

        for variant in product.get("variants") or []:
            row = row_for_variant(product, variant)
            all_rows.append(row)
            sku = clean_cell(variant.get("sku"))
            if re.fullmatch(r"\d{12}", sku or ""):
                update_rows.append(row)
            else:
                reason = "missing_sku" if not sku else "sku_is_not_12_digit_system_id"
                review_rows.append(
                    {
                        "shopify_product_id": product.get("id"),
                        "shopify_variant_id": variant.get("id"),
                        "handle": clean_cell(product.get("handle")),
                        "status": clean_cell(product.get("status")),
                        "vendor": clean_cell(product.get("vendor")),
                        "product_title": clean_cell(product.get("title")),
                        "variant_title": clean_cell(variant.get("title")),
                        "sku": sku,
                        "barcode": clean_cell(variant.get("barcode")),
                        "price": money(variant.get("price")),
                        "inventory_quantity": int(variant.get("inventory_quantity") or 0),
                        "reason": reason,
                    }
                )

    all_path = output_dir / "shopify_catalog_lightspeed_import_all_rows.csv"
    update_path = output_dir / "shopify_catalog_lightspeed_update_by_system_id.csv"
    minimal_path = output_dir / "shopify_catalog_lightspeed_custom_sku_minimal.csv"
    review_path = output_dir / "shopify_catalog_lightspeed_missing_or_invalid_sku_review.csv"
    images_path = output_dir / "shopify_catalog_image_reference.csv"
    summary_path = output_dir / "shopify_catalog_lightspeed_import_summary.json"

    write_lightspeed_csv(all_path, all_rows)
    write_lightspeed_csv(update_path, update_rows)
    write_csv(
        minimal_path,
        MINIMAL_COLUMNS,
        [
            {
                "System ID": row["System ID"],
                "Custom SKU": row["Custom SKU"],
                "Item": "",
            }
            for row in update_rows
        ],
    )
    write_csv(review_path, REVIEW_COLUMNS, review_rows)
    write_csv(images_path, IMAGE_COLUMNS, image_rows)

    summary = {
        "products_json": str(products_json),
        "products": len(products),
        "variants_total": len(all_rows),
        "variants_with_12_digit_sku": len(update_rows),
        "variants_missing_or_invalid_sku": len(review_rows),
        "output_files": {
            "all_rows": str(all_path),
            "update_by_system_id": str(update_path),
            "minimal_custom_sku": str(minimal_path),
            "missing_or_invalid_sku_review": str(review_path),
            "image_reference": str(images_path),
        },
        "notes": [
            "Use update_by_system_id for updating existing Lightspeed items.",
            "Use minimal_custom_sku when you only want the same simple System ID to Custom SKU update file as before.",
            "Review missing_or_invalid_sku before creating items from all_rows.",
            "Import CSV image columns are left blank; Shopify image filenames and URLs are exported separately for review.",
        ],
    }
    summary_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({**summary, "summary": str(summary_path)}, indent=2))


if __name__ == "__main__":
    main()
