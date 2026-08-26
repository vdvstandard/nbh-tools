#!/usr/bin/env python3
"""Prepare Lightspeed import CSVs with Custom SKU copied from System ID."""

import argparse
import csv
import json
import re
from pathlib import Path


REQUIRED_COLUMNS = ("System ID", "Custom SKU", "Item")
OUTPUT_COLUMNS = ("System ID", "Custom SKU", "Item")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="Lightspeed item export CSV")
    parser.add_argument(
        "--output-dir",
        default=".tmp/lightspeed-custom-sku-from-system-id",
        help="Directory for generated import files",
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=10000,
        help="Maximum data rows per output CSV",
    )
    parser.add_argument(
        "--stock-families-only",
        action="store_true",
        help=(
            "Only include rows with stock, plus sibling size variants from "
            "the same inferred product family"
        ),
    )
    return parser.parse_args()


def read_rows(path):
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        missing = [column for column in REQUIRED_COLUMNS if column not in fieldnames]
        if missing:
            raise SystemExit(
                "Source CSV is missing required columns: " + ", ".join(missing)
            )
        rows = list(reader)
    return rows


def write_chunk(path, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def parse_quantity(value):
    text = (value or "").strip()
    text = re.sub(r"^'", "", text)
    text = re.sub(r"[^0-9,.-]", "", text)
    if not text or text == "-":
        return 0.0
    if "," in text and "." in text:
        if text.rfind(".") > text.rfind(","):
            text = text.replace(",", "")
        else:
            text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return 0.0


def normalize_key(value):
    return re.sub(r"\s+", " ", (value or "").strip()).casefold()


SIZE_WORD = (
    r"(?i:xxxxl|xxxl|xxl|xl|xs|xxs|s|m|l|"
    r"x-small|small|medium|large|x-large|xx-large|xxx-large|"
    r"one\s*size|onesize|o/s|os|osfm)"
)
NUMERIC_SIZE = (
    r"\d{1,3}(?:[.,]\d{1,2})?"
    r"(?:\s*(?:/|-|x|X)\s*\d{1,3}(?:[.,]\d{1,2})?)?"
    r"(?:\s*\([^)]+\))?"
)
TRAILING_SIZE_PATTERNS = [
    re.compile(rf"\s+\d+\s*/\s*{SIZE_WORD}$"),
    re.compile(rf"\s+{SIZE_WORD}$"),
    re.compile(rf"\s+{NUMERIC_SIZE}$"),
]


def infer_product_family(row):
    item = re.sub(r"\s+", " ", row.get("Item") or "").strip()
    for pattern in TRAILING_SIZE_PATTERNS:
        match = pattern.search(item)
        if match:
            base = item[: match.start()].strip(" -/")
            if base:
                return (
                    "variant",
                    normalize_key(row.get("Brand")),
                    normalize_key(row.get("Category")),
                    normalize_key(row.get("Subcategory 1")),
                    normalize_key(base),
                )
    return (
        "single",
        normalize_key(row.get("Brand")),
        normalize_key(row.get("Category")),
        normalize_key(row.get("Subcategory 1")),
        normalize_key(item),
    )


def select_stock_family_rows(rows):
    family_has_stock = {}
    row_families = []
    for row in rows:
        family = infer_product_family(row)
        row_families.append(family)
        if parse_quantity(row.get("Qty.")) > 0:
            family_has_stock[family] = True

    selected_rows = []
    included_positive_stock = 0
    included_zero_stock_family_variants = 0
    for row, family in zip(rows, row_families):
        if not family_has_stock.get(family):
            continue
        selected_rows.append(row)
        if parse_quantity(row.get("Qty.")) > 0:
            included_positive_stock += 1
        else:
            included_zero_stock_family_variants += 1

    return (
        selected_rows,
        {
            "inferred_stock_families": len(family_has_stock),
            "included_rows_with_positive_stock": included_positive_stock,
            "included_zero_stock_family_variants": included_zero_stock_family_variants,
            "excluded_rows_without_stock_family": len(rows) - len(selected_rows),
        },
    )


def main():
    args = parse_args()
    if args.max_rows < 1:
        raise SystemExit("--max-rows must be at least 1")

    source = Path(args.source)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    source_rows = read_rows(source)
    selection_summary = None
    if args.stock_families_only:
        selected_source_rows, selection_summary = select_stock_family_rows(source_rows)
    else:
        selected_source_rows = source_rows

    prepared_rows = []
    nonblank_custom_sku = 0
    changed_custom_sku = 0
    blank_system_id = 0

    for row in selected_source_rows:
        system_id = (row.get("System ID") or "").strip()
        custom_sku = (row.get("Custom SKU") or "").strip()
        if custom_sku:
            nonblank_custom_sku += 1
        if not system_id:
            blank_system_id += 1
        if custom_sku != system_id:
            changed_custom_sku += 1
        prepared_rows.append(
            {
                "System ID": system_id,
                "Custom SKU": system_id,
                "Item": row.get("Item") or "",
            }
        )

    output_files = []
    for index, start in enumerate(range(0, len(prepared_rows), args.max_rows), start=1):
        chunk = prepared_rows[start : start + args.max_rows]
        path = output_dir / (
            f"{source.stem}_custom_sku_system_id_part{index}.csv"
        )
        write_chunk(path, chunk)
        output_files.append({"path": str(path), "rows": len(chunk)})

    summary = {
        "source": str(source),
        "source_total_rows": len(source_rows),
        "output_total_rows": len(prepared_rows),
        "output_columns": list(OUTPUT_COLUMNS),
        "max_rows_per_file": args.max_rows,
        "stock_families_only": args.stock_families_only,
        "output_files": output_files,
        "source_rows_with_existing_custom_sku": nonblank_custom_sku,
        "rows_where_custom_sku_was_changed": changed_custom_sku,
        "rows_with_blank_system_id": blank_system_id,
    }
    if selection_summary:
        summary.update(selection_summary)

    summary_path = output_dir / f"{source.stem}_custom_sku_system_id_summary.json"
    summary_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(json.dumps({**summary, "summary_path": str(summary_path)}, indent=2))


if __name__ == "__main__":
    main()
