#!/usr/bin/env python3

import argparse
import json
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_BASE_URL = "http://127.0.0.1:9292"
DEFAULT_CONTENT_AUDIT = (
    Path(".tmp") / "phase3b-brand-seo-post-apply-20260729.json"
)
DEFAULT_JSON_REPORT = Path(".tmp") / "phase4-product-card-audit-20260729.json"
DEFAULT_MARKDOWN_REPORT = Path(".tmp") / "phase4-product-card-audit-20260729.md"
LONG_TITLE_MINIMUM = 70


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def class_set(value: str) -> set[str]:
    return set((value or "").split())


def product_handle_from_href(href: str) -> str:
    path = urllib.parse.urlparse(href or "").path
    match = re.search(r"/products/([^/?#]+)", path)
    return urllib.parse.unquote(match.group(1)) if match else ""


class CollectionCardParser(HTMLParser):
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

    def __init__(
        self,
        collection_handle: str,
        page: int,
        url: str,
    ) -> None:
        super().__init__(convert_charrefs=True)
        self.collection_handle = collection_handle
        self.page = page
        self.url = url
        self.cards: List[Dict[str, Any]] = []
        self.next_url = ""
        self.stack: List[Dict[str, bool]] = []
        self.product_grid_depth = 0
        self.link_depth = 0
        self.badge_depth = 0
        self.price_depth = 0
        self.current_card: Optional[Dict[str, Any]] = None

    @staticmethod
    def attr_map(
        attrs: List[Tuple[str, Optional[str]]]
    ) -> Dict[str, str]:
        return {key.lower(): value or "" for key, value in attrs}

    def handle_starttag(
        self,
        tag: str,
        attrs: List[Tuple[str, Optional[str]]],
    ) -> None:
        normalized = tag.lower()
        attributes = self.attr_map(attrs)
        classes = class_set(attributes.get("class", ""))
        frame = {
            "product_grid": False,
            "card_root": False,
            "link": False,
            "badge": False,
            "price": False,
        }

        if attributes.get("id") == "product-grid":
            self.product_grid_depth += 1
            frame["product_grid"] = True
        if "data-endless-scroll" in attributes:
            self.next_url = attributes.get("data-next-url", "")

        if (
            self.current_card is None
            and self.product_grid_depth
            and normalized == "li"
            and "grid__item" in classes
        ):
            self.current_card = {
                "collection": self.collection_handle,
                "page": self.page,
                "url": self.url,
                "handle": "",
                "titleParts": [],
                "textParts": [],
                "badgeParts": [],
                "priceParts": [],
                "classes": [],
                "imageAlts": [],
                "imageCount": 0,
                "hasQuickAdd": False,
                "hasSalePriceClass": False,
            }
            frame["card_root"] = True

        if self.current_card is not None:
            class_value = attributes.get("class", "")
            if class_value:
                self.current_card["classes"].append(class_value)
            if normalized == "img":
                self.current_card["imageCount"] += 1
                self.current_card["imageAlts"].append(attributes.get("alt", ""))
            href = attributes.get("href", "")
            handle = product_handle_from_href(href)
            if handle:
                if not self.current_card["handle"]:
                    self.current_card["handle"] = handle
                self.link_depth += 1
                frame["link"] = True
            if (
                "badge" in classes
                or "card__in-store-exclusive-badge" in classes
            ):
                self.badge_depth += 1
                frame["badge"] = True
            if "price" in classes:
                self.price_depth += 1
                frame["price"] = True
            if "price--on-sale" in classes:
                self.current_card["hasSalePriceClass"] = True
            if (
                "quick-add" in classes
                or "quick-add__submit" in classes
                or normalized in {"product-form", "quick-add-bulk"}
            ):
                self.current_card["hasQuickAdd"] = True

        if normalized not in self.VOID_ELEMENTS:
            self.stack.append(frame)

    def handle_startendtag(
        self,
        tag: str,
        attrs: List[Tuple[str, Optional[str]]],
    ) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in self.VOID_ELEMENTS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack:
            return
        frame = self.stack.pop()
        if frame.get("link"):
            self.link_depth = max(0, self.link_depth - 1)
        if frame.get("badge"):
            self.badge_depth = max(0, self.badge_depth - 1)
        if frame.get("price"):
            self.price_depth = max(0, self.price_depth - 1)
        if frame.get("product_grid"):
            self.product_grid_depth = max(0, self.product_grid_depth - 1)
        if frame.get("card_root") and self.current_card is not None:
            self.finish_card()

    def handle_data(self, data: str) -> None:
        if self.current_card is None:
            return
        self.current_card["textParts"].append(data)
        if self.link_depth:
            self.current_card["titleParts"].append(data)
        if self.badge_depth:
            self.current_card["badgeParts"].append(data)
        if self.price_depth:
            self.current_card["priceParts"].append(data)

    def finish_card(self) -> None:
        card = self.current_card or {}
        badges = [
            normalize_text(value)
            for value in card.get("badgeParts", [])
            if normalize_text(value)
        ]
        normalized = {
            "collection": card.get("collection", ""),
            "page": card.get("page", 0),
            "url": card.get("url", ""),
            "handle": card.get("handle", ""),
            "title": normalize_text(" ".join(card.get("titleParts", []))),
            "text": normalize_text(" ".join(card.get("textParts", []))),
            "badges": sorted(set(badges)),
            "priceText": normalize_text(" ".join(card.get("priceParts", []))),
            "imageCount": card.get("imageCount", 0),
            "hasImage": card.get("imageCount", 0) > 0,
            "imageAlts": [
                normalize_text(value)
                for value in card.get("imageAlts", [])
            ],
            "hasQuickAdd": bool(card.get("hasQuickAdd", False)),
            "hasSalePriceClass": bool(card.get("hasSalePriceClass", False)),
            "classes": " ".join(card.get("classes", [])),
        }
        normalized["hasSaleBadge"] = any(
            badge.casefold() == "sale" for badge in normalized["badges"]
        )
        normalized["hasSoldOutBadge"] = any(
            "sold out" in badge.casefold()
            for badge in normalized["badges"]
        )
        normalized["hasInStoreExclusiveBadge"] = any(
            "in-store exclusive" in badge.casefold()
            for badge in normalized["badges"]
        ) or "card__in-store-exclusive-badge" in normalized["classes"]
        self.cards.append(normalized)
        self.current_card = None


def fetch_html(url: str) -> Dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Neighbourhood product card audit"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return {
                "status": response.status,
                "url": response.geturl(),
                "html": response.read().decode("utf-8", errors="replace"),
            }
    except Exception as exc:
        return {"status": 0, "url": url, "html": "", "error": str(exc)}


def load_collection_handles(path: Path) -> List[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    handles = [
        item["handle"]
        for item in data.get("collections", [])
        if item.get("handle") and item.get("published")
    ]
    if "in-store-exclusive" not in handles:
        handles.append("in-store-exclusive")
    return handles


def crawl_collection(
    base_url: str,
    handle: str,
    max_pages: int,
) -> Dict[str, Any]:
    pages = []
    cards: List[Dict[str, Any]] = []
    url = f"{base_url.rstrip('/')}/collections/{handle}"
    seen_urls = set()
    for page in range(1, max_pages + 1):
        if url in seen_urls:
            pages.append(
                {
                    "page": page,
                    "url": url,
                    "status": 0,
                    "cards": 0,
                    "nextUrl": "",
                    "issue": "duplicate_next_url",
                }
            )
            break
        seen_urls.add(url)
        response = fetch_html(url)
        parser = CollectionCardParser(handle, page, response["url"])
        parser.feed(response["html"])
        parser.close()
        cards.extend(parser.cards)
        pages.append(
            {
                "page": page,
                "url": url,
                "finalUrl": response["url"],
                "status": response["status"],
                "cards": len(parser.cards),
                "nextUrl": parser.next_url,
                "error": response.get("error", ""),
            }
        )
        if response["status"] != 200 or not parser.next_url:
            break
        url = urllib.parse.urljoin(response["url"], parser.next_url)
    return {"handle": handle, "pages": pages, "cards": cards}


def card_location(card: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "handle": card["handle"],
        "title": card["title"],
        "collection": card["collection"],
        "page": card["page"],
        "url": card["url"],
        "priceText": card["priceText"],
        "badges": card["badges"],
        "hasImage": card["hasImage"],
        "hasQuickAdd": card["hasQuickAdd"],
    }


def scenario_candidates(cards: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    return {
        "normal_price": [
            card for card in cards
            if card["hasImage"]
            and card["priceText"]
            and not card["hasSaleBadge"]
            and not card["hasSoldOutBadge"]
            and not card["hasInStoreExclusiveBadge"]
        ],
        "sale_price": [
            card for card in cards
            if card["priceText"]
            and (card["hasSaleBadge"] or card["hasSalePriceClass"])
            and not card["hasSoldOutBadge"]
        ],
        "long_title": [
            card for card in cards
            if len(card["title"]) >= LONG_TITLE_MINIMUM
            and card["priceText"]
        ],
        "missing_image": [
            card for card in cards
            if not card["hasImage"]
            and card["title"]
            and card["priceText"]
        ],
        "in_store_exclusive": [
            card for card in cards
            if card["collection"] == "in-store-exclusive"
            and card["hasInStoreExclusiveBadge"]
            and not card["hasQuickAdd"]
        ],
    }


def build_scenarios(cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    candidates = scenario_candidates(cards)
    scenarios = [
        {
            "key": "normal_price",
            "label": "Normal price",
            "expectation": "Card has image, price and no sale/sold-out/in-store badge.",
            "status": "pass" if candidates["normal_price"] else "fail",
            "fixture": (
                card_location(candidates["normal_price"][0])
                if candidates["normal_price"]
                else None
            ),
        },
        {
            "key": "sale_price",
            "label": "Sale price",
            "expectation": "Card renders sale treatment and a price block.",
            "status": "pass" if candidates["sale_price"] else "fail",
            "fixture": (
                card_location(candidates["sale_price"][0])
                if candidates["sale_price"]
                else None
            ),
        },
        {
            "key": "long_title",
            "label": "Long title",
            "expectation": (
                f"Card with title length >= {LONG_TITLE_MINIMUM} renders title and price."
            ),
            "status": "pass" if candidates["long_title"] else "fail",
            "fixture": (
                card_location(
                    sorted(
                        candidates["long_title"],
                        key=lambda card: len(card["title"]),
                        reverse=True,
                    )[0]
                )
                if candidates["long_title"]
                else None
            ),
        },
        {
            "key": "missing_image",
            "label": "Missing image",
            "expectation": (
                "If a visible card has no image, it still renders title and price."
            ),
            "status": (
                "pass" if candidates["missing_image"] else "not_applicable"
            ),
            "fixture": (
                card_location(candidates["missing_image"][0])
                if candidates["missing_image"]
                else None
            ),
            "note": (
                "No visible product card without an image was found in the "
                "crawled collection pages."
                if not candidates["missing_image"]
                else ""
            ),
        },
        {
            "key": "in_store_exclusive",
            "label": "In-store exclusive",
            "expectation": (
                "In-store-exclusive card renders the badge and no quick-add UI."
            ),
            "status": (
                "pass" if candidates["in_store_exclusive"] else "fail"
            ),
            "fixture": (
                card_location(candidates["in_store_exclusive"][0])
                if candidates["in_store_exclusive"]
                else None
            ),
        },
    ]
    return scenarios


def summarize_issues(
    collections: List[Dict[str, Any]],
    scenarios: List[Dict[str, Any]],
) -> List[Dict[str, str]]:
    issues: List[Dict[str, str]] = []
    for collection in collections:
        for page in collection["pages"]:
            if page["status"] != 200:
                issues.append(
                    {
                        "code": "collection_page_unavailable",
                        "severity": "error",
                        "detail": (
                            f"{collection['handle']} page {page['page']} "
                            f"returned {page['status']}: {page.get('error') or ''}"
                        ),
                    }
                )
    for scenario in scenarios:
        if scenario["status"] == "fail":
            issues.append(
                {
                    "code": f"{scenario['key']}_fixture_missing",
                    "severity": "error",
                    "detail": (
                        f"No passing card was found for {scenario['label']}."
                    ),
                }
            )
    return issues


def markdown_escape(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")


def build_markdown(report: Dict[str, Any]) -> str:
    lines = [
        "# Product card audit",
        "",
        f"Generated: `{report['generatedAt']}`",
        f"Base URL: `{report['baseUrl']}`",
        "",
        "## Summary",
        "",
        f"- Collections crawled: {report['summary']['collectionsCrawled']}",
        f"- Pages crawled: {report['summary']['pagesCrawled']}",
        f"- Cards crawled: {report['summary']['cardsCrawled']}",
        f"- Errors: {report['summary']['errors']}",
        f"- Warnings: {report['summary']['warnings']}",
        "",
        "## Scenarios",
        "",
        "| Scenario | Status | Fixture | Price | Badges |",
        "| --- | --- | --- | --- | --- |",
    ]
    for scenario in report["scenarios"]:
        fixture = scenario.get("fixture") or {}
        fixture_label = "-"
        if fixture:
            fixture_label = (
                f"{fixture['title']} "
                f"({fixture['collection']} page {fixture['page']})"
            )
        lines.append(
            "| "
            + " | ".join(
                [
                    markdown_escape(scenario["label"]),
                    scenario["status"],
                    markdown_escape(fixture_label),
                    markdown_escape(fixture.get("priceText", "")),
                    markdown_escape(", ".join(fixture.get("badges", []))),
                ]
            )
            + " |"
        )
    if report["issues"]:
        lines.extend(["", "## Issues", ""])
        for issue in report["issues"]:
            lines.append(
                f"- `{issue['severity']}` `{issue['code']}`: {issue['detail']}"
            )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit rendered collection product cards in the local theme preview."
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument(
        "--content-audit",
        type=Path,
        default=DEFAULT_CONTENT_AUDIT,
        help="Brand SEO audit JSON containing canonical collection handles.",
    )
    parser.add_argument("--json-report", type=Path, default=DEFAULT_JSON_REPORT)
    parser.add_argument(
        "--markdown-report",
        type=Path,
        default=DEFAULT_MARKDOWN_REPORT,
    )
    parser.add_argument("--max-pages", type=int, default=12)
    args = parser.parse_args()

    handles = load_collection_handles(args.content_audit)
    collections = [
        crawl_collection(args.base_url, handle, args.max_pages)
        for handle in handles
    ]
    cards = [
        card
        for collection in collections
        for card in collection["cards"]
    ]
    scenarios = build_scenarios(cards)
    issues = summarize_issues(collections, scenarios)
    summary = {
        "collectionsCrawled": len(collections),
        "pagesCrawled": sum(len(item["pages"]) for item in collections),
        "cardsCrawled": len(cards),
        "errors": sum(1 for item in issues if item["severity"] == "error"),
        "warnings": sum(
            1 for item in issues if item["severity"] == "warning"
        ),
        "notApplicable": sum(
            1 for item in scenarios if item["status"] == "not_applicable"
        ),
    }
    report = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "mode": "read_only_storefront_html_audit",
        "baseUrl": args.base_url,
        "contentAudit": str(args.content_audit),
        "summary": summary,
        "scenarios": scenarios,
        "issues": issues,
        "collections": [
            {
                "handle": item["handle"],
                "pages": item["pages"],
                "cards": len(item["cards"]),
            }
            for item in collections
        ],
    }

    args.json_report.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_report.parent.mkdir(parents=True, exist_ok=True)
    args.json_report.write_text(
        json.dumps(report, indent=2) + "\n",
        encoding="utf-8",
    )
    args.markdown_report.write_text(
        build_markdown(report),
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2))
    print(f"JSON report: {args.json_report}")
    print(f"Markdown report: {args.markdown_report}")
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
