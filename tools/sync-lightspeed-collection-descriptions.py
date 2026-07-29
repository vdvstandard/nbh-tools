#!/usr/bin/env python3

import argparse
import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_API_VERSION = "2026-01"
DEFAULT_OLD_SITE_BASE = "https://www.nbharnhem.com"
DEFAULT_REPORT_FILE = Path(".tmp") / "lightspeed-collection-descriptions-report.json"


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
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
    cleaned = (value or "").strip().replace("http://", "").replace("https://", "").split("/")[0]
    if not cleaned:
        return ""
    return cleaned if "." in cleaned else f"{cleaned}.myshopify.com"


def request_json(url: str, token: str, method: str = "GET", payload: Optional[Dict[str, Any]] = None, timeout: int = 45) -> Tuple[Dict[str, Any], Dict[str, str]]:
    data = None
    headers = {
        "Accept": "application/json",
        "X-Shopify-Access-Token": token,
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8") or "{}"), dict(response.headers.items())


def fetch_all_collections(shop: str, token: str, api_version: str) -> List[Dict[str, Any]]:
    collections: List[Dict[str, Any]] = []
    for endpoint_key, kind in (("smart_collections", "smart"), ("custom_collections", "custom")):
        url: Optional[str] = f"https://{shop}/admin/api/{api_version}/{endpoint_key}.json?limit=250"
        while url:
            payload, headers = request_json(url, token)
            for row in payload.get(endpoint_key) or []:
                row["collection_kind"] = kind
                row["endpoint_key"] = endpoint_key
                collections.append(row)
            url = next_link(headers.get("Link", ""))
    return collections


def next_link(link_header: str) -> Optional[str]:
    for part in link_header.split(","):
        if 'rel="next"' in part:
            return part.split(";")[0].strip()[1:-1]
    return None


def publish_collection(shop: str, token: str, api_version: str, collection: Dict[str, Any]) -> Dict[str, Any]:
    endpoint_key = collection["endpoint_key"]
    wrapper_key = endpoint_key[:-1]
    url = f"https://{shop}/admin/api/{api_version}/{endpoint_key}/{collection['id']}.json"
    body = {
        wrapper_key: {
            "id": collection["id"],
            "published": True,
            "published_at": datetime.now(timezone.utc).isoformat(),
        }
    }
    payload, _ = request_json(url, token, method="PUT", payload=body)
    return payload.get(wrapper_key) or {}


def update_collection_description(shop: str, token: str, api_version: str, collection: Dict[str, Any], body_html: str) -> Dict[str, Any]:
    endpoint_key = collection["endpoint_key"]
    wrapper_key = endpoint_key[:-1]
    url = f"https://{shop}/admin/api/{api_version}/{endpoint_key}/{collection['id']}.json"
    body = {
        wrapper_key: {
            "id": collection["id"],
            "body_html": body_html,
        }
    }
    payload, _ = request_json(url, token, method="PUT", payload=body)
    return payload.get(wrapper_key) or {}


def fetch_text(url: str, timeout: int = 45) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; NeighbourhoodSync/1.0)",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def scrape_brand_links(old_site_base: str) -> Dict[str, Dict[str, str]]:
    index_url = old_site_base.rstrip("/") + "/brands/"
    page_html = fetch_text(index_url)
    links: Dict[str, Dict[str, str]] = {}
    pattern = re.compile(r'<a\s+href="([^"]*/brands/([^"/]+)/)"\s+title="([^"]+)"', re.IGNORECASE)
    for match in pattern.finditer(page_html):
        url = html.unescape(match.group(1))
        url_handle = html.unescape(match.group(2)).strip("/")
        title = html.unescape(match.group(3)).strip()
        if not title:
            continue
        link = {"title": title, "url": url, "url_handle": url_handle}
        links.setdefault(url_handle, link)
        links.setdefault(handleize(title), link)
    return links


def handleize(value: str) -> str:
    normalized = html.unescape(value or "").lower()
    normalized = normalized.replace("&", " and ")
    normalized = normalized.replace("'", "")
    normalized = re.sub(r"[^a-z0-9]+", "-", normalized)
    return normalized.strip("-")


def extract_lightspeed_description(page_html: str) -> str:
    patterns = [
        re.compile(
            r'<div\s+class="col-md-12\s+margin-top-double">\s*<div\s+class="content">\s*(.*?)\s*</div>\s*</div>',
            re.IGNORECASE | re.DOTALL,
        ),
    ]

    for pattern in patterns:
        match = pattern.search(page_html)
        if not match:
            continue
        raw = match.group(1)
        cleaned = normalize_description_html(raw)
        if meaningful_text(cleaned):
            return split_for_shopify(cleaned)
    return ""


def normalize_description_html(value: str) -> str:
    cleaned = html.unescape(value or "")
    cleaned = cleaned.replace("\u2014", ", ")
    cleaned = cleaned.replace("\u2013", ", ")
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = re.sub(r"<script\b.*?</script>", "", cleaned, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"<style\b.*?</style>", "", cleaned, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"<a\b[^>]*>(.*?)</a>", r"\1", cleaned, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"<p\b[^>]*>", "<p>", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<br\s*/?>", "<br>", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<(?!/?(?:p|br|strong|em)\b)[^>]+>", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"(<p>)\s*-?\s*IN\s+STORE\s+ONLY\s*-?\s*", r"\1", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<p>\s*</p>\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^\s*<p>\s*-//-\s*</p>\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+\n", "\n", cleaned)
    cleaned = re.sub(r"\n\s+", "\n", cleaned)
    cleaned = re.sub(r">\s+<", ">\n<", cleaned)
    cleaned = re.sub(r"\s+([,.;:!?])", r"\1", cleaned)
    cleaned = re.sub(r",\s*,+", ",", cleaned)
    cleaned = re.sub(r",\s{2,}", ", ", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"(?:<p>\s*</p>\s*)+", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def remove_bold_html(value: str) -> str:
    cleaned = re.sub(r"</?(?:strong|b)\b[^>]*>", "", value or "", flags=re.IGNORECASE)
    cleaned = re.sub(
        r"\s*font-weight\s*:\s*(?:bold|[5-9]00)\s*;?",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    return cleaned


def meaningful_text(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def comparable_description_html(value: str) -> str:
    return html.unescape(value or "").strip()


def split_for_shopify(value: str) -> str:
    if "-//-" in value:
        return value
    paragraphs = re.findall(r"<p\b[^>]*>.*?</p>", value, flags=re.IGNORECASE | re.DOTALL)
    if len(paragraphs) < 2:
        return value
    first = paragraphs[0].strip()
    rest = "\n".join(paragraph.strip() for paragraph in paragraphs[1:] if meaningful_text(paragraph))
    if not first or not rest:
        return value
    return f"{first}\n<p>-//-</p>\n{rest}"


def is_blank_description(value: Optional[str]) -> bool:
    return not meaningful_text(value or "")


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish Shopify collections and sync old Lightspeed brand descriptions into blank Shopify collection descriptions.")
    parser.add_argument("--apply", action="store_true", help="Write changes. Without this, only prints a dry-run plan.")
    parser.add_argument("--publish-unpublished", action="store_true", help="Publish all unpublished Shopify custom/smart collections.")
    parser.add_argument("--overwrite-existing", action="store_true", help="Overwrite existing Shopify collection descriptions when Lightspeed has text.")
    parser.add_argument("--handles", default="", help="Comma-separated Shopify collection handles to include. Empty means all collections.")
    parser.add_argument("--clean-existing", action="store_true", help="Normalize current Shopify collection descriptions instead of scraping Lightspeed.")
    parser.add_argument("--remove-bold", action="store_true", help="Remove bold HTML styling from current collection descriptions without changing their text.")
    parser.add_argument("--old-site-base", default=DEFAULT_OLD_SITE_BASE)
    parser.add_argument("--report-file", default=str(DEFAULT_REPORT_FILE))
    parser.add_argument("--shop", default="")
    parser.add_argument("--token", default="")
    parser.add_argument("--api-version", default="")
    args = parser.parse_args()
    if args.clean_existing and args.remove_bold:
        raise SystemExit("--clean-existing and --remove-bold cannot be combined.")

    load_dotenv(Path(".env"))
    shop = normalize_shop(args.shop or os.environ.get("SHOPIFY_SHOP", ""))
    token = args.token or os.environ.get("SHOPIFY_ADMIN_ACCESS_TOKEN", "") or os.environ.get("SHOPIFY_ACCESS_TOKEN", "")
    api_version = args.api_version or os.environ.get("SHOPIFY_API_VERSION", DEFAULT_API_VERSION)

    if not shop or not token:
        raise SystemExit("Missing Shopify credentials. Set SHOPIFY_SHOP and SHOPIFY_ADMIN_ACCESS_TOKEN in .env.")

    selected_handles = {handle.strip() for handle in args.handles.split(",") if handle.strip()}
    print(f"{'Apply' if args.apply else 'Dry run'}: Shopify collections + Lightspeed brand descriptions")
    collections = fetch_all_collections(shop, token, api_version)
    if selected_handles:
        collections = [collection for collection in collections if (collection.get("handle") or "") in selected_handles]
    brand_links = {} if args.clean_existing or args.remove_bold else scrape_brand_links(args.old_site_base)

    published_plan = [collection for collection in collections if not collection.get("published_at")]
    scraped: Dict[str, Dict[str, str]] = {}
    description_updates: List[Dict[str, Any]] = []
    scrape_misses: List[Dict[str, str]] = []
    skipped_existing: List[Dict[str, str]] = []

    for collection in sorted(collections, key=lambda item: (item.get("title") or "").lower()):
        handle = collection.get("handle") or ""
        if args.remove_bold:
            current_description = collection.get("body_html") or ""
            cleaned_description = remove_bold_html(current_description)
            if cleaned_description != current_description:
                description_updates.append(
                    {
                        "collection": collection,
                        "lightspeed_title": collection.get("title") or "",
                        "lightspeed_url": "shopify-admin-remove-bold",
                        "description_html": cleaned_description,
                        "description_text_preview": meaningful_text(cleaned_description)[:220],
                    }
                )
            continue
        if args.clean_existing:
            current_description = collection.get("body_html") or ""
            cleaned_description = normalize_description_html(current_description)
            if comparable_description_html(cleaned_description) != comparable_description_html(current_description):
                description_updates.append(
                    {
                        "collection": collection,
                        "lightspeed_title": collection.get("title") or "",
                        "lightspeed_url": "shopify-admin-current-description",
                        "description_html": cleaned_description,
                        "description_text_preview": meaningful_text(cleaned_description)[:220],
                    }
                )
            continue

        link = brand_links.get(handle) or brand_links.get(handleize(collection.get("title") or ""))
        if not link:
            continue
        cache_key = link["url"]
        if cache_key not in scraped:
            time.sleep(0.1)
            try:
                page_html = fetch_text(link["url"])
                description_html = extract_lightspeed_description(page_html)
            except Exception as exc:
                description_html = ""
                scrape_misses.append({"title": collection.get("title") or "", "handle": handle, "reason": str(exc)})
            scraped[cache_key] = {**link, "description_html": description_html}
        description_html = scraped[cache_key]["description_html"]
        if not description_html:
            scrape_misses.append({"title": collection.get("title") or "", "handle": handle, "reason": "no Lightspeed description found"})
            continue
        if not args.overwrite_existing and not is_blank_description(collection.get("body_html")):
            skipped_existing.append({"title": collection.get("title") or "", "handle": handle})
            continue
        description_updates.append(
            {
                "collection": collection,
                "lightspeed_title": link["title"],
                "lightspeed_url": link["url"],
                "description_html": description_html,
                "description_text_preview": meaningful_text(description_html)[:220],
            }
        )

    published_results: List[Dict[str, Any]] = []
    description_results: List[Dict[str, Any]] = []

    if args.apply:
        if args.publish_unpublished:
            for collection in published_plan:
                updated = publish_collection(shop, token, api_version, collection)
                published_results.append(
                    {
                        "title": updated.get("title") or collection.get("title"),
                        "handle": updated.get("handle") or collection.get("handle"),
                        "published_at": updated.get("published_at"),
                    }
                )
        for update in description_updates:
            collection = update["collection"]
            updated = update_collection_description(shop, token, api_version, collection, update["description_html"])
            description_results.append(
                {
                    "title": updated.get("title") or collection.get("title"),
                    "handle": updated.get("handle") or collection.get("handle"),
                    "source": update["lightspeed_url"],
                }
            )

    report = {
        "mode": "apply" if args.apply else "dry_run",
        "collections_total": len(collections),
        "unpublished_planned": len(published_plan),
        "unpublished_titles": [{"title": c.get("title"), "handle": c.get("handle"), "kind": c.get("collection_kind")} for c in published_plan],
        "published_written": published_results,
        "lightspeed_brand_links_found": len({item["url"] for item in brand_links.values()}),
        "description_updates_planned": [
            {
                "title": update["collection"].get("title"),
                "handle": update["collection"].get("handle"),
                "source": update["lightspeed_url"],
                "preview": update["description_text_preview"],
            }
            for update in description_updates
        ],
        "description_updates_written": description_results,
        "skipped_existing_descriptions": skipped_existing,
        "scrape_misses": scrape_misses,
    }

    report_path = Path(args.report_file)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"Collections total: {len(collections)}")
    print(f"Unpublished collections: {len(published_plan)}")
    print(f"Lightspeed brand links found: {report['lightspeed_brand_links_found']}")
    print(f"Description updates planned: {len(description_updates)}")
    print(f"Skipped existing descriptions: {len(skipped_existing)}")
    print(f"Scrape misses/no text: {len(scrape_misses)}")
    if args.apply:
        print(f"Published written: {len(published_results) if args.publish_unpublished else 0}")
        print(f"Descriptions written: {len(description_results)}")
    print(f"Report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
