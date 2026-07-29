#!/usr/bin/env python3

import argparse
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_CONTENT_AUDIT = (
    Path(".tmp") / "phase3-brand-seo-audit-20260725.json"
)
DEFAULT_JSON_REPORT = (
    Path(".tmp") / "phase3-brand-storefront-audit-20260725.json"
)
DEFAULT_MARKDOWN_REPORT = (
    Path(".tmp") / "phase3-brand-storefront-audit-20260725.md"
)
DEFAULT_BASELINE_PRODUCTS = (
    Path(".tmp")
    / "phase2-final-zero-drift-locked-20260725"
    / "shopify-baseline"
    / "products.json"
)


class StorefrontParser(HTMLParser):
    VOID_ELEMENTS = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.element_index = 0
        self.html_lang = ""
        self.title_parts: List[str] = []
        self.meta_description = ""
        self.meta_robots = ""
        self.canonical = ""
        self.h1_values: List[str] = []
        self.product_cards = 0
        self.product_grid_index: Optional[int] = None
        self.seo_bottom_index: Optional[int] = None
        self.seo_bottom_text: List[str] = []
        self.seo_bottom_bold_words: List[str] = []
        self.endless_controls: List[Dict[str, Any]] = []
        self.pagination_wrappers = 0
        self.collection_empty = False
        self.load_more_text = False
        self.json_ld_values: List[str] = []
        self.stack: List[Dict[str, Any]] = []
        self.capture_title = False
        self.capture_h1_depth = 0
        self.capture_seo_depth = 0
        self.capture_bold_depth = 0
        self.capture_json_ld = False

    @staticmethod
    def attr_map(
        attrs: List[Tuple[str, Optional[str]]]
    ) -> Dict[str, str]:
        return {key.lower(): value or "" for key, value in attrs}

    def handle_starttag(
        self, tag: str, attrs: List[Tuple[str, Optional[str]]]
    ) -> None:
        self.element_index += 1
        normalized = tag.lower()
        attributes = self.attr_map(attrs)
        classes = set(attributes.get("class", "").split())
        frame = {
            "tag": normalized,
            "h1": False,
            "seo": False,
            "bold": False,
            "jsonld": False,
        }

        if normalized == "html":
            self.html_lang = attributes.get("lang", "")
        elif normalized == "title":
            self.capture_title = True
        elif normalized == "meta":
            name = attributes.get("name", "").lower()
            if name == "description":
                self.meta_description = attributes.get("content", "")
            elif name == "robots":
                self.meta_robots = attributes.get("content", "")
        elif (
            normalized == "link"
            and attributes.get("rel", "").lower() == "canonical"
        ):
            self.canonical = attributes.get("href", "")

        if normalized == "h1":
            self.capture_h1_depth += 1
            self.h1_values.append("")
            frame["h1"] = True

        if attributes.get("id") == "product-grid":
            self.product_grid_index = self.element_index
        if (
            normalized == "li"
            and "grid__item" in classes
            and any(
                parent.get("product_grid", False) for parent in self.stack
            )
        ):
            self.product_cards += 1

        frame["product_grid"] = (
            attributes.get("id") == "product-grid"
            or any(
                parent.get("product_grid", False) for parent in self.stack
            )
        )

        if "collection-seo-bottom" in classes:
            self.seo_bottom_index = self.element_index
            self.capture_seo_depth += 1
            frame["seo"] = True
        elif self.capture_seo_depth:
            self.capture_seo_depth += 1
            frame["seo"] = True

        if (
            self.capture_seo_depth
            and normalized in {"strong", "b"}
        ):
            self.capture_bold_depth += 1
            frame["bold"] = True

        if "pagination-wrapper" in classes:
            self.pagination_wrappers += 1
        if "collection--empty" in classes:
            self.collection_empty = True
        if "data-endless-scroll" in attributes:
            self.endless_controls.append(
                {
                    "nextUrl": attributes.get("data-next-url", ""),
                    "hidden": "hidden" in attributes,
                }
            )

        if (
            normalized == "script"
            and attributes.get("type", "").lower()
            == "application/ld+json"
        ):
            self.capture_json_ld = True
            self.json_ld_values.append("")
            frame["jsonld"] = True

        if normalized not in self.VOID_ELEMENTS:
            self.stack.append(frame)

    def handle_startendtag(
        self, tag: str, attrs: List[Tuple[str, Optional[str]]]
    ) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in self.VOID_ELEMENTS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if not self.stack:
            return
        frame = self.stack.pop()
        if normalized == "title":
            self.capture_title = False
        if frame.get("h1"):
            self.capture_h1_depth = max(0, self.capture_h1_depth - 1)
        if frame.get("bold"):
            self.capture_bold_depth = max(
                0, self.capture_bold_depth - 1
            )
        if frame.get("seo"):
            self.capture_seo_depth = max(
                0, self.capture_seo_depth - 1
            )
        if frame.get("jsonld"):
            self.capture_json_ld = False

    def handle_data(self, data: str) -> None:
        if self.capture_title:
            self.title_parts.append(data)
        if self.capture_h1_depth and self.h1_values:
            self.h1_values[-1] += data
        if self.capture_seo_depth:
            self.seo_bottom_text.append(data)
            if self.capture_bold_depth:
                self.seo_bottom_bold_words.append(data)
        if self.capture_json_ld and self.json_ld_values:
            self.json_ld_values[-1] += data
        if normalize_text(data).lower() == "load more":
            self.load_more_text = True


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def word_count(value: str) -> int:
    return len(re.findall(r"\b[\w][\w'-]*\b", value))


def fetch_html(url: str, attempts: int = 3) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "User-Agent": "NeighbourhoodStorefrontAudit/1.0",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                result = {
                    "status": response.status,
                    "url": response.geturl(),
                    "html": response.read().decode(
                        "utf-8", errors="replace"
                    ),
                }
        except urllib.error.HTTPError as exc:
            result = {
                "status": exc.code,
                "url": exc.geturl(),
                "html": exc.read().decode("utf-8", errors="replace"),
            }
        final_path = urllib.parse.urlsplit(result["url"]).path.rstrip("/")
        if final_path != "/password" or attempt == attempts:
            return result
        time.sleep(0.3 * attempt)
    return result


def schema_types(raw_values: List[str]) -> List[str]:
    found: List[str] = []
    for raw in raw_values:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            found.append("INVALID_JSON")
            continue
        values = payload if isinstance(payload, list) else [payload]
        for value in values:
            if not isinstance(value, dict):
                continue
            schema_type = value.get("@type")
            if isinstance(schema_type, list):
                found.extend(str(item) for item in schema_type)
            elif schema_type:
                found.append(str(schema_type))
    return found


def canonical_expected(
    canonical: str, handle: str, requested_page: int
) -> bool:
    if not canonical:
        return False
    parsed = urllib.parse.urlsplit(canonical)
    if parsed.path.rstrip("/") != f"/collections/{handle}":
        return False
    query = urllib.parse.parse_qs(parsed.query)
    if requested_page == 1:
        return "page" not in query
    return query.get("page") == [str(requested_page)]


def analyze_page(
    base_url: str,
    collection: Dict[str, Any],
    page: int,
) -> Dict[str, Any]:
    handle = collection["handle"]
    suffix = "" if page == 1 else f"?page={page}"
    requested_url = (
        f"{base_url.rstrip('/')}/collections/{handle}{suffix}"
    )
    response = fetch_html(requested_url)
    parser = StorefrontParser()
    parser.feed(response["html"])
    parser.close()

    title = normalize_text(" ".join(parser.title_parts))
    raw_h1_values = [
        normalize_text(value) for value in parser.h1_values if normalize_text(value)
    ]
    h1_values = [
        re.sub(r"^Collection:\s*", "", value, flags=re.IGNORECASE)
        for value in raw_h1_values
    ]
    seo_text = normalize_text(" ".join(parser.seo_bottom_text))
    bold_text = normalize_text(" ".join(parser.seo_bottom_bold_words))
    types = schema_types(parser.json_ld_values)
    product_counts = collection["productCounts"]
    expected_cards = 0
    if collection["published"]:
        remaining = max(product_counts["active"] - ((page - 1) * 24), 0)
        expected_cards = min(remaining, 24)

    issues: List[Dict[str, str]] = []

    def issue(code: str, severity: str, detail: str) -> None:
        issues.append(
            {"code": code, "severity": severity, "detail": detail}
        )

    if collection["published"] and response["status"] != 200:
        issue(
            "published_page_unavailable",
            "error",
            f"Expected HTTP 200, received {response['status']}.",
        )
    if not collection["published"] and response["status"] == 200:
        issue(
            "unpublished_page_accessible",
            "warning",
            "The unpublished collection is accessible in the preview.",
        )
    if response["status"] == 200:
        if parser.html_lang.lower() != "en":
            issue(
                "non_english_html_lang",
                "warning",
                f"HTML language is {parser.html_lang or '(blank)'}.",
            )
        if len(h1_values) != 1:
            issue(
                "invalid_h1_count",
                "error",
                f"Expected one H1, found {len(h1_values)}.",
            )
        elif h1_values[0].casefold() != collection["title"].casefold():
            issue(
                "h1_title_mismatch",
                "warning",
                f"H1 is '{h1_values[0]}'.",
            )
        if not title:
            issue("missing_title", "error", "No document title.")
        if not parser.meta_description:
            issue(
                "missing_rendered_meta_description",
                "warning",
                "No rendered meta description.",
            )
        if not canonical_expected(parser.canonical, handle, page):
            issue(
                "canonical_mismatch",
                "error",
                f"Unexpected canonical: {parser.canonical or '(blank)'}.",
            )
        if parser.load_more_text:
            issue(
                "visible_load_more_copy",
                "error",
                "The rendered HTML contains a Load more label.",
            )
        if parser.product_cards != expected_cards:
            issue(
                "initial_product_count_mismatch",
                "error",
                (
                    f"Expected {expected_cards} cards on page {page}, "
                    f"found {parser.product_cards}."
                ),
            )
        if (
            parser.seo_bottom_index is not None
            and parser.product_grid_index is not None
            and parser.seo_bottom_index < parser.product_grid_index
        ):
            issue(
                "seo_before_product_grid",
                "error",
                "Bottom SEO copy appears before the product grid in the DOM.",
            )
        if seo_text and word_count(bold_text) >= word_count(seo_text) * 0.8:
            issue(
                "rendered_seo_mostly_bold",
                "error",
                "Most rendered bottom SEO copy is bold.",
            )
        if product_counts["active"] == 0 and collection["published"]:
            issue(
                "published_empty_brand",
                "warning",
                "Published brand collection has no active products.",
            )
        if "noindex" in parser.meta_robots.lower() and collection["published"]:
            issue(
                "published_collection_noindex",
                "error",
                "Published collection renders a noindex directive.",
            )

    return {
        "page": page,
        "requestedUrl": requested_url,
        "finalUrl": response["url"],
        "status": response["status"],
        "htmlLang": parser.html_lang,
        "title": title,
        "metaDescription": parser.meta_description,
        "canonical": parser.canonical,
        "robots": parser.meta_robots,
        "h1": h1_values,
        "h1Raw": raw_h1_values,
        "productCards": parser.product_cards,
        "expectedProductCards": expected_cards,
        "collectionEmpty": parser.collection_empty,
        "paginationWrappers": parser.pagination_wrappers,
        "endlessControls": parser.endless_controls,
        "loadMoreText": parser.load_more_text,
        "seoBottomWordCount": word_count(seo_text),
        "seoBottomAfterGrid": (
            parser.seo_bottom_index is not None
            and parser.product_grid_index is not None
            and parser.seo_bottom_index > parser.product_grid_index
        ),
        "structuredDataTypes": types,
        "issues": issues,
    }


def analyze_product_page(
    base_url: str,
    product: Dict[str, Any],
) -> Dict[str, Any]:
    handle = product["handle"]
    requested_url = f"{base_url.rstrip('/')}/products/{handle}"
    response = fetch_html(requested_url)
    parser = StorefrontParser()
    parser.feed(response["html"])
    parser.close()
    title = normalize_text(" ".join(parser.title_parts))
    h1_values = [
        normalize_text(value)
        for value in parser.h1_values
        if normalize_text(value)
    ]
    types = schema_types(parser.json_ld_values)
    issues: List[Dict[str, str]] = []

    def issue(code: str, severity: str, detail: str) -> None:
        issues.append(
            {"code": code, "severity": severity, "detail": detail}
        )

    if response["status"] != 200:
        issue(
            "active_product_unavailable",
            "error",
            f"Expected HTTP 200, received {response['status']}.",
        )
    else:
        if parser.html_lang.lower() != "en":
            issue(
                "product_non_english_html_lang",
                "warning",
                f"HTML language is {parser.html_lang or '(blank)'}.",
            )
        if len(h1_values) != 1:
            issue(
                "product_invalid_h1_count",
                "error",
                f"Expected one H1, found {len(h1_values)}.",
            )
        elif h1_values[0].casefold() != product["title"].casefold():
            issue(
                "product_h1_mismatch",
                "warning",
                f"H1 is '{h1_values[0]}'.",
            )
        if not title:
            issue("product_missing_title", "error", "No document title.")
        if not parser.meta_description:
            issue(
                "product_missing_meta_description",
                "warning",
                "No rendered meta description.",
            )
        canonical_path = urllib.parse.urlsplit(parser.canonical).path.rstrip("/")
        if canonical_path != f"/products/{handle}":
            issue(
                "product_canonical_mismatch",
                "error",
                f"Unexpected canonical: {parser.canonical or '(blank)'}.",
            )
        if "INVALID_JSON" in types:
            issue(
                "invalid_product_json_ld",
                "error",
                "At least one JSON-LD block is invalid.",
            )
        if not {"Product", "ProductGroup"} & set(types):
            issue(
                "missing_product_structured_data",
                "error",
                "No Product or ProductGroup JSON-LD block was rendered.",
            )

    return {
        "vendor": product["vendor"],
        "handle": handle,
        "productTitle": product["title"],
        "requestedUrl": requested_url,
        "finalUrl": response["url"],
        "status": response["status"],
        "htmlLang": parser.html_lang,
        "title": title,
        "metaDescription": parser.meta_description,
        "canonical": parser.canonical,
        "h1": h1_values,
        "structuredDataTypes": types,
        "issues": issues,
    }


def markdown_escape(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")


def build_markdown(report: Dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# Brand collection storefront audit",
        "",
        f"Generated: `{report['generatedAt']}`",
        f"Base URL: `{report['baseUrl']}`",
        "",
        "## Summary",
        "",
        f"- Collections checked: {summary['collectionsChecked']}",
        f"- Pages checked: {summary['pagesChecked']}",
        f"- Product samples checked: {summary['productSamplesChecked']}",
        f"- Errors: {summary['errors']}",
        f"- Warnings: {summary['warnings']}",
        "",
        "## Collections",
        "",
        "| Brand | HTTP | Cards | H1 | Canonical | SEO after grid | Issues |",
        "| --- | ---: | ---: | --- | --- | --- | --- |",
    ]
    for item in report["collections"]:
        page = item["pages"][0]
        codes = ", ".join(
            issue["code"] for issue in page["issues"]
        ) or "-"
        lines.append(
            "| "
            + " | ".join(
                [
                    markdown_escape(item["title"]),
                    str(page["status"]),
                    (
                        f"{page['productCards']}/"
                        f"{page['expectedProductCards']}"
                    ),
                    "ok" if len(page["h1"]) == 1 else "check",
                    (
                        "ok"
                        if canonical_expected(
                            page["canonical"], item["handle"], 1
                        )
                        else "check"
                    ),
                    "yes" if page["seoBottomAfterGrid"] else "n/a",
                    markdown_escape(codes),
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "- No Shopify content or theme files were changed.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only HTML audit of Shopify brand collection storefront "
            "pages."
        )
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:9292")
    parser.add_argument(
        "--content-audit", type=Path, default=DEFAULT_CONTENT_AUDIT
    )
    parser.add_argument(
        "--baseline-products",
        type=Path,
        default=DEFAULT_BASELINE_PRODUCTS,
    )
    parser.add_argument("--json-report", type=Path, default=DEFAULT_JSON_REPORT)
    parser.add_argument(
        "--markdown-report", type=Path, default=DEFAULT_MARKDOWN_REPORT
    )
    args = parser.parse_args()

    content_audit = json.loads(
        args.content_audit.read_text(encoding="utf-8")
    )
    audited_collections: List[Dict[str, Any]] = []
    for collection in content_audit["collections"]:
        pages = [analyze_page(args.base_url, collection, 1)]
        active_count = collection["productCounts"]["active"]
        if collection["published"] and active_count > 24:
            pages.append(analyze_page(args.base_url, collection, 2))
        audited_collections.append(
            {
                "handle": collection["handle"],
                "title": collection["title"],
                "published": collection["published"],
                "productCounts": collection["productCounts"],
                "pages": pages,
            }
        )

    baseline_products = json.loads(
        args.baseline_products.read_text(encoding="utf-8")
    ).get("products") or []
    product_by_vendor: Dict[str, Dict[str, Any]] = {}
    for product in sorted(
        baseline_products,
        key=lambda item: (
            (item.get("vendor") or "").casefold(),
            (item.get("handle") or "").casefold(),
        ),
    ):
        vendor = (product.get("vendor") or "").strip()
        if (
            vendor
            and (product.get("status") or "").lower() == "active"
            and vendor not in product_by_vendor
        ):
            product_by_vendor[vendor] = {
                "vendor": vendor,
                "handle": product.get("handle") or "",
                "title": product.get("title") or "",
            }
    product_samples = [
        analyze_product_page(args.base_url, product_by_vendor[vendor])
        for vendor in sorted(product_by_vendor, key=str.casefold)
    ]

    all_issues = [
        issue
        for collection in audited_collections
        for page in collection["pages"]
        for issue in page["issues"]
    ]
    all_issues.extend(
        issue
        for product in product_samples
        for issue in product["issues"]
    )
    issue_counts = Counter(issue["code"] for issue in all_issues)
    report = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "mode": "read_only",
        "baseUrl": args.base_url,
        "summary": {
            "collectionsChecked": len(audited_collections),
            "pagesChecked": sum(
                len(collection["pages"])
                for collection in audited_collections
            ),
            "productSamplesChecked": len(product_samples),
            "errors": sum(
                issue["severity"] == "error" for issue in all_issues
            ),
            "warnings": sum(
                issue["severity"] == "warning" for issue in all_issues
            ),
            "issueCounts": dict(issue_counts),
        },
        "collections": audited_collections,
        "productSamples": product_samples,
    }
    args.json_report.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_report.parent.mkdir(parents=True, exist_ok=True)
    args.json_report.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    args.markdown_report.write_text(
        build_markdown(report),
        encoding="utf-8",
    )
    print(json.dumps(report["summary"], indent=2))
    print(f"JSON report: {args.json_report}")
    print(f"Markdown report: {args.markdown_report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
