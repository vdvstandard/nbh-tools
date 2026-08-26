#!/usr/bin/env python3

import argparse
import csv
import hashlib
import html
import json
import re
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


DEFAULT_BASE_URL = "https://neighbourhood-arnhem.myshopify.com"
DEFAULT_JSON_OUTPUT = Path(".tmp/product-description-review-context.json")
DEFAULT_MARKDOWN_OUTPUT = Path(".tmp/product-description-review-context.md")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean(value: Any) -> str:
    return str(value or "").strip()


def normalize_handle(value: str) -> str:
    value = clean(value).lower().replace("_", "-")
    value = re.sub(r"[^a-z0-9-]+", "-", value)
    return re.sub(r"-{2,}", "-", value).strip("-")


def meaningful_text(value: str) -> str:
    text = re.sub(r"(?is)<(?:script|style).*?</(?:script|style)>", " ", value or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def request_json(url: str) -> Dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Neighbourhood product content review/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8") or "{}")


def fetch_public_products(base_url: str) -> List[Dict[str, Any]]:
    products: List[Dict[str, Any]] = []
    page = 1
    while True:
        query = urllib.parse.urlencode({"limit": 250, "page": page})
        payload = request_json(f"{base_url.rstrip('/')}/products.json?{query}")
        batch = payload.get("products") or []
        products.extend(batch)
        if len(batch) < 250:
            break
        page += 1
    return products


def read_csv(path: Path) -> List[Dict[str, str]]:
    last_error: Optional[Exception] = None
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            with path.open(newline="", encoding=encoding) as source:
                return list(csv.DictReader(source, delimiter=";"))
        except UnicodeDecodeError as error:
            last_error = error
    raise RuntimeError(f"Could not read {path}: {last_error}")


def first_nonempty(values: Iterable[Any]) -> str:
    for value in values:
        if clean(value):
            return clean(value)
    return ""


def source_handle(row: Dict[str, str]) -> str:
    return normalize_handle(
        first_nonempty(
            [
                row.get("US_URL"),
                row.get("NL_URL"),
                row.get("US_Title_Short"),
                row.get("NL_Title_Short"),
            ]
        )
    )


def index_source_rows(rows: List[Dict[str, str]]) -> Dict[str, List[Dict[str, str]]]:
    indexed: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for row in rows:
        handle = source_handle(row)
        if handle:
            indexed[handle].append(row)
    return indexed


def summarize_source(rows: List[Dict[str, str]]) -> Dict[str, Any]:
    if not rows:
        return {"matched": False, "rows": 0}
    first = rows[0]
    return {
        "matched": True,
        "rows": len(rows),
        "internalId": clean(first.get("Internal_ID")),
        "brand": clean(first.get("Brand")),
        "title": first_nonempty(
            [
                first.get("US_Title_Short"),
                first.get("US_Title_Long"),
                first.get("NL_Title_Short"),
                first.get("NL_Title_Long"),
            ]
        ),
        "descriptionHtml": first_nonempty(
            [
                first.get("US_Description_Long"),
                first.get("US_Description_Short"),
                first.get("NL_Description_Long"),
                first.get("NL_Description_Short"),
            ]
        ),
        "categories": [
            value
            for value in dict.fromkeys(
                clean(first.get(key))
                for key in (
                    "US_Category_1",
                    "US_Category_2",
                    "US_Category_3",
                    "NL_Category_1",
                    "NL_Category_2",
                    "NL_Category_3",
                )
            )
            if value
        ],
        "tags": clean(first.get("Tags")),
        "variants": [
            {
                "sourceVariantId": clean(row.get("Internal_Variant_ID")),
                "variant": first_nonempty([row.get("US_Variant"), row.get("NL_Variant")]),
                "sku": clean(row.get("SKU")),
                "articleCode": clean(row.get("Article_Code")),
                "ean": clean(row.get("EAN")),
            }
            for row in rows
        ],
        "imageUrls": list(
            dict.fromkeys(
                part.strip()
                for row in rows
                for part in clean(row.get("Images")).split(",")
                if part.strip()
            )
        ),
    }


def summarize_product(
    product: Dict[str, Any],
    issues: List[str],
    source_rows: List[Dict[str, str]],
    base_url: str,
) -> Dict[str, Any]:
    description_html = clean(product.get("body_html"))
    return {
        "handle": clean(product.get("handle")),
        "title": clean(product.get("title")),
        "vendor": clean(product.get("vendor")),
        "productType": clean(product.get("product_type")),
        "publishedAt": product.get("published_at"),
        "storefrontUrl": f"{base_url.rstrip('/')}/products/{clean(product.get('handle'))}",
        "issues": issues,
        "currentDescriptionHtml": description_html,
        "currentDescriptionText": meaningful_text(description_html),
        "tags": product.get("tags") or [],
        "options": product.get("options") or [],
        "variants": [
            {
                "id": item.get("id"),
                "title": item.get("title"),
                "sku": clean(item.get("sku")),
                "price": item.get("price"),
                "available": item.get("available"),
                "weight": item.get("weight"),
                "weightUnit": item.get("weight_unit"),
            }
            for item in product.get("variants") or []
        ],
        "images": [
            {
                "id": item.get("id"),
                "position": item.get("position"),
                "src": item.get("src"),
                "width": item.get("width"),
                "height": item.get("height"),
            }
            for item in product.get("images") or []
        ],
        "lightspeed": summarize_source(source_rows),
    }


def markdown_report(report: Dict[str, Any]) -> str:
    lines = [
        "# Product description review context",
        "",
        f"Generated: `{report['generatedAt']}`",
        f"Public products inspected: **{report['summary']['publicProducts']}**",
        f"Products requiring review: **{report['summary']['reviewProducts']}**",
        "",
        "No Shopify content was changed.",
        "",
    ]
    for item in report["products"]:
        source = item["lightspeed"]
        lines.extend(
            [
                f"## {item['title']}",
                "",
                f"- Handle: `{item['handle']}`",
                f"- Vendor: `{item['vendor'] or '(blank)'}`",
                f"- Type: `{item['productType'] or '(blank)'}`",
                f"- Issues: `{', '.join(item['issues'])}`",
                f"- Published: `{item['publishedAt']}`",
                f"- Shopify images: `{len(item['images'])}`",
                f"- Lightspeed match: `{source['matched']}`",
                f"- Lightspeed description present: `{bool(source.get('descriptionHtml'))}`",
                f"- Variants: `{', '.join(variant['title'] or '' for variant in item['variants'])}`",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare a read-only review context for weak public product descriptions."
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--short-threshold", type=int, default=80)
    parser.add_argument("--extra-handle", action="append", default=[])
    parser.add_argument("--json-output", type=Path, default=DEFAULT_JSON_OUTPUT)
    parser.add_argument("--markdown-output", type=Path, default=DEFAULT_MARKDOWN_OUTPUT)
    args = parser.parse_args()

    public_products = fetch_public_products(args.base_url)
    source_path = args.source.expanduser().resolve()
    source_index = index_source_rows(read_csv(source_path))
    extra_handles = {normalize_handle(value) for value in args.extra_handle}

    review_products = []
    for product in public_products:
        handle = normalize_handle(product.get("handle") or "")
        description_text = meaningful_text(product.get("body_html") or "")
        issues = []
        if not description_text:
            issues.append("description_blank")
        elif len(description_text) < args.short_threshold:
            issues.append("description_short")
        if handle in extra_handles:
            issues.append("editorial_correction_requested")
        if not issues:
            continue
        review_products.append(
            summarize_product(
                product,
                issues,
                source_index.get(handle, []),
                args.base_url,
            )
        )

    report = {
        "schemaVersion": 1,
        "generatedAt": utc_now(),
        "mode": "read_only_product_description_review",
        "baseUrl": args.base_url.rstrip("/"),
        "source": {
            "name": source_path.name,
            "sha256": sha256_file(source_path),
        },
        "criteria": {
            "shortDescriptionCharacters": args.short_threshold,
            "extraHandles": sorted(extra_handles),
        },
        "summary": {
            "publicProducts": len(public_products),
            "reviewProducts": len(review_products),
            "blankDescriptions": sum(
                "description_blank" in item["issues"] for item in review_products
            ),
            "shortDescriptions": sum(
                "description_short" in item["issues"] for item in review_products
            ),
            "editorialCorrections": sum(
                "editorial_correction_requested" in item["issues"]
                for item in review_products
            ),
            "lightspeedMatches": sum(
                item["lightspeed"]["matched"] for item in review_products
            ),
        },
        "products": review_products,
    }

    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    args.markdown_output.write_text(markdown_report(report), encoding="utf-8")
    print(json.dumps(report["summary"], indent=2))
    print(f"JSON: {args.json_output.resolve()}")
    print(f"Markdown: {args.markdown_output.resolve()}")


if __name__ == "__main__":
    main()
