#!/usr/bin/env python3
"""Crawl the existing storefront read-only and inventory internal URLs."""

import argparse
import csv
import json
import re
import threading
import time
import urllib.parse
import urllib.robotparser
import xml.etree.ElementTree as ET
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

import requests


DEFAULT_BASE_URL = "https://www.nbharnhem.com/"
DEFAULT_SITEMAP_URL = None
DEFAULT_OUTPUT_DIR = ".tmp/phase8-existing-site-crawl-20260805"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36 "
    "Neighbourhood-Phase9-Audit/1.0"
)
REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}
HTTP_STATE = threading.local()
ACTION_PREFIXES = (
    "/account/",
    "/cart/add/",
    "/cart/change/",
    "/cart/delete/",
    "/checkout/",
    "/cookielaw/",
)
ASSET_EXTENSIONS = {
    ".7z",
    ".avi",
    ".css",
    ".doc",
    ".docx",
    ".eot",
    ".gif",
    ".ico",
    ".jpeg",
    ".jpg",
    ".js",
    ".json",
    ".map",
    ".mov",
    ".mp3",
    ".mp4",
    ".pdf",
    ".png",
    ".rar",
    ".rss",
    ".svg",
    ".tar",
    ".tif",
    ".tiff",
    ".ttf",
    ".webmanifest",
    ".webp",
    ".woff",
    ".woff2",
    ".xls",
    ".xlsx",
    ".xml",
    ".zip",
}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Read-only crawl of the existing Neighbourhood storefront."
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument(
        "--sitemap-url",
        default=DEFAULT_SITEMAP_URL,
        help="Defaults to /sitemap.xml on --base-url.",
    )
    parser.add_argument(
        "--root-locale-only",
        action="store_true",
        help="For sitemap indexes, crawl only child sitemaps at the root locale.",
    )
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--delay", type=float, default=0.1)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--max-pages", type=int, default=1200)
    return parser.parse_args()


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.canonical = None
        self.meta_robots = ""
        self.description = ""
        self.title_parts = []
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        attributes = {key.lower(): value for key, value in attrs}
        tag = tag.lower()
        if tag in {"a", "area"}:
            href = (attributes.get("href") or "").strip()
            if href:
                self.links.append(href)
        elif tag == "link" and "canonical" in (
            attributes.get("rel") or ""
        ).lower():
            self.canonical = attributes.get("href")
        elif tag == "meta":
            name = (attributes.get("name") or "").lower()
            if name == "robots":
                self.meta_robots = (attributes.get("content") or "").strip()
            elif name == "description":
                self.description = (attributes.get("content") or "").strip()
        elif tag == "title":
            self.in_title = True

    def handle_endtag(self, tag):
        if tag.lower() == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.title_parts.append(data)

    @property
    def title(self):
        return re.sub(r"\s+", " ", " ".join(self.title_parts)).strip()


def normalize_path(path):
    value = urllib.parse.unquote(path or "/")
    value = re.sub(r"/{2,}", "/", value)
    if not value.startswith("/"):
        value = f"/{value}"
    return urllib.parse.quote(value, safe="/~._-()'")


def normalize_internal_url(value, base_url, allowed_hosts):
    if not value or value.startswith(("#", "mailto:", "tel:", "javascript:")):
        return None
    absolute = urllib.parse.urljoin(base_url, value)
    parsed = urllib.parse.urlparse(absolute)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in allowed_hosts:
        return None
    base = urllib.parse.urlparse(base_url)
    path = normalize_path(parsed.path)
    url = urllib.parse.urlunparse((base.scheme, base.netloc, path, "", "", ""))
    return {
        "url": url,
        "path": path,
        "query": parsed.query or None,
        "fragment": parsed.fragment or None,
    }


def classify_path(path):
    clean = urllib.parse.unquote(path).strip("/")
    if not clean:
        return "root"
    if re.fullmatch(r"[^/]+\.html", clean):
        return "product"
    first = clean.split("/", 1)[0]
    if first == "brands":
        return "brand"
    if first == "blogs":
        return "blog"
    if first == "products":
        return "product"
    if first == "collections":
        return "collection"
    if first == "pages":
        return "page"
    if first == "policies":
        return "policy"
    if first == "tags":
        return "tag"
    if first == "shop":
        return "shop"
    if first == "service":
        return "service"
    if first == "account":
        return "account"
    if first in {"cart", "checkout", "cookielaw"}:
        return "action"
    return "other"


def crawl_skip_reason(path, query=None, fragment=None):
    decoded = urllib.parse.unquote(path)
    if decoded.rstrip("/") == "/collections/vendors" and query:
        return "query_dependent_route"
    if (
        decoded.rstrip("/") == "/policies"
        and fragment == "shopifyReshowConsentBanner"
    ):
        return "consent_action_guard"
    if any(decoded.startswith(prefix) for prefix in ACTION_PREFIXES):
        return "action_guard"
    extension = Path(decoded.rstrip("/")).suffix.lower()
    if extension in ASSET_EXTENSIONS:
        return "asset_guard"
    return None


def http_session():
    session = getattr(HTTP_STATE, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update(REQUEST_HEADERS)
        HTTP_STATE.session = session
    return session


def retry_delay(response, attempts, maximum=16):
    retry_after = response.headers.get("Retry-After")
    try:
        return max(float(retry_after), 1)
    except (TypeError, ValueError):
        return min(2 ** (attempts - 1), maximum)


def fetch_text(url, timeout=30, retries=5):
    attempts = 0
    while True:
        attempts += 1
        response = http_session().get(url, timeout=timeout)
        if response.status_code in {429, 502, 503, 504} and attempts <= retries:
            time.sleep(retry_delay(response, attempts))
            continue
        response.raise_for_status()
        return {
            "status": response.status_code,
            "url": response.url,
            "contentType": response.headers.get("Content-Type", ""),
            "body": response.content,
            "attempts": attempts,
        }


def xml_local_name(tag):
    return tag.rsplit("}", 1)[-1]


def parse_urlset(root, namespace):
    urls = []
    for node in root.findall("sm:url", namespace):
        location = node.findtext("sm:loc", default="", namespaces=namespace).strip()
        if location:
            urls.append(
                {
                    "url": location,
                    "lastmod": node.findtext(
                        "sm:lastmod", default="", namespaces=namespace
                    )
                    or None,
                }
            )
    return urls


def read_sitemap(url, root_locale_only=False):
    response = fetch_text(url)
    root = ET.fromstring(response["body"])
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    urls = parse_urlset(root, namespace)
    child_sitemaps = []
    declared_child_sitemaps = []

    if xml_local_name(root.tag) == "sitemapindex":
        for node in root.findall("sm:sitemap", namespace):
            location = node.findtext(
                "sm:loc", default="", namespaces=namespace
            ).strip()
            if location:
                declared_child_sitemaps.append(location)

        selected_children = declared_child_sitemaps
        if root_locale_only:
            selected_children = [
                child
                for child in declared_child_sitemaps
                if urllib.parse.urlparse(child).path.count("/") == 1
            ]

        for child_url in selected_children:
            child_response = fetch_text(child_url)
            child_root = ET.fromstring(child_response["body"])
            child_urls = parse_urlset(child_root, namespace)
            urls.extend(child_urls)
            child_sitemaps.append(
                {
                    "url": child_url,
                    "status": child_response["status"],
                    "contentType": child_response["contentType"],
                    "bytes": len(child_response["body"]),
                    "urls": len(child_urls),
                }
            )
            time.sleep(0.1)

    return {
        "status": response["status"],
        "contentType": response["contentType"],
        "bytes": len(response["body"]),
        "urls": urls,
        "declaredChildSitemaps": len(declared_child_sitemaps),
        "childSitemaps": child_sitemaps,
    }


def read_robots(base_url):
    robots_url = urllib.parse.urljoin(base_url, "/robots.txt")
    try:
        response = fetch_text(robots_url)
        text = response["body"].decode("utf-8", errors="replace")
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(robots_url)
        parser.parse(text.splitlines())
        return {
            "url": robots_url,
            "status": response["status"],
            "text": text,
            "parser": parser,
            "error": None,
        }
    except Exception as error:
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(robots_url)
        parser.parse([])
        return {
            "url": robots_url,
            "status": None,
            "text": "",
            "parser": parser,
            "error": str(error),
        }


def response_encoding(content_type):
    match = re.search(r"charset=([^;\s]+)", content_type or "", re.I)
    return match.group(1).strip('"\'') if match else "utf-8"


def fetch_page(url, delay, retries):
    started = time.perf_counter()
    attempts = 0
    while True:
        attempts += 1
        if delay:
            time.sleep(delay)
        try:
            response = http_session().get(url, timeout=30)
            if response.status_code in {429, 502, 503, 504} and attempts <= retries:
                time.sleep(max(retry_delay(response, attempts, maximum=8), delay))
                continue
            body = response.content
            status = response.status_code
            final_url = response.url
            content_type = response.headers.get("Content-Type", "")
            break
        except requests.RequestException as error:
            return {
                "url": url,
                "status": None,
                "finalUrl": None,
                "error": str(error),
                "attempts": attempts,
                "elapsedMs": round((time.perf_counter() - started) * 1000),
                "links": [],
            }

    parser = PageParser()
    if "html" in content_type.lower():
        parser.feed(body.decode(response_encoding(content_type), errors="replace"))
    return {
        "url": url,
        "status": status,
        "finalUrl": final_url,
        "redirected": final_url.rstrip("/") != url.rstrip("/"),
        "contentType": content_type,
        "bytes": len(body),
        "attempts": attempts,
        "elapsedMs": round((time.perf_counter() - started) * 1000),
        "title": parser.title,
        "description": parser.description,
        "canonical": parser.canonical,
        "metaRobots": parser.meta_robots,
        "indexable": "noindex" not in parser.meta_robots.lower(),
        "links": parser.links,
        "error": None,
    }


def add_discovery(inventory, normalized, source, in_sitemap=False, lastmod=None):
    url = normalized["url"]
    item = inventory.setdefault(
        url,
        {
            "url": url,
            "path": normalized["path"],
            "category": classify_path(normalized["path"]),
            "inSitemap": False,
            "sitemapLastmod": None,
            "firstSeenFrom": source,
            "discoveredFrom": set(),
            "queryVariants": set(),
            "skipReason": crawl_skip_reason(
                normalized["path"],
                normalized.get("query"),
                normalized.get("fragment"),
            ),
        },
    )
    item["inSitemap"] = item["inSitemap"] or in_sitemap
    if lastmod:
        item["sitemapLastmod"] = lastmod
    if source:
        item["discoveredFrom"].add(source)
    if normalized.get("query"):
        item["queryVariants"].add(normalized["query"])
    return item


def serializable_inventory_item(item, result=None, robots_allowed=True):
    output = {
        **{key: value for key, value in item.items() if key not in {"discoveredFrom", "queryVariants"}},
        "discoveredFrom": sorted(item["discoveredFrom"]),
        "queryVariants": sorted(item["queryVariants"]),
        "robotsAllowed": robots_allowed,
        "crawled": result is not None,
    }
    if result:
        output.update({key: value for key, value in result.items() if key != "links"})
    return output


def write_csv(path, rows):
    fieldnames = [
        "url",
        "path",
        "category",
        "in_sitemap",
        "crawled",
        "status",
        "final_url",
        "redirected",
        "canonical",
        "title",
        "indexable",
        "robots_allowed",
        "skip_reason",
        "first_seen_from",
        "discovered_from",
        "query_variants",
        "error",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for item in rows:
            writer.writerow(
                {
                    "url": item["url"],
                    "path": item["path"],
                    "category": item["category"],
                    "in_sitemap": item["inSitemap"],
                    "crawled": item["crawled"],
                    "status": item.get("status"),
                    "final_url": item.get("finalUrl"),
                    "redirected": item.get("redirected"),
                    "canonical": item.get("canonical"),
                    "title": item.get("title"),
                    "indexable": item.get("indexable"),
                    "robots_allowed": item["robotsAllowed"],
                    "skip_reason": item.get("skipReason"),
                    "first_seen_from": item.get("firstSeenFrom"),
                    "discovered_from": json.dumps(item.get("discoveredFrom") or []),
                    "query_variants": json.dumps(item.get("queryVariants") or []),
                    "error": item.get("error"),
                }
            )


def main():
    args = parse_args()
    if args.workers < 1 or args.workers > 8:
        raise SystemExit("--workers must be between 1 and 8")
    if args.max_pages < 1:
        raise SystemExit("--max-pages must be positive")
    if args.retries < 0:
        raise SystemExit("--retries cannot be negative")

    base_url = args.base_url.rstrip("/") + "/"
    base = urllib.parse.urlparse(base_url)
    allowed_hosts = {base.hostname.lower(), base.hostname.lower().removeprefix("www.")}
    allowed_hosts.add(f"www.{base.hostname.lower().removeprefix('www.')}")
    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = Path(__file__).resolve().parent.parent / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    sitemap_url = args.sitemap_url or urllib.parse.urljoin(base_url, "/sitemap.xml")
    sitemap = read_sitemap(sitemap_url, root_locale_only=args.root_locale_only)
    robots = read_robots(base_url)
    inventory = {}
    queue = deque()
    queued = set()
    results = {}

    home = normalize_internal_url(base_url, base_url, allowed_hosts)
    add_discovery(inventory, home, "seed:homepage")
    queue.append(home["url"])
    queued.add(home["url"])
    for entry in sitemap["urls"]:
        normalized = normalize_internal_url(entry["url"], base_url, allowed_hosts)
        if not normalized:
            continue
        item = add_discovery(
            inventory,
            normalized,
            "seed:sitemap",
            in_sitemap=True,
            lastmod=entry.get("lastmod"),
        )
        if not item["skipReason"] and normalized["url"] not in queued:
            queue.append(normalized["url"])
            queued.add(normalized["url"])

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        while queue and len(results) < args.max_pages:
            batch = []
            while queue and len(batch) < args.workers:
                url = queue.popleft()
                item = inventory[url]
                if item["skipReason"] or url in results:
                    continue
                if not robots["parser"].can_fetch(USER_AGENT, url):
                    item["skipReason"] = "robots_disallowed"
                    continue
                batch.append(url)
            if not batch:
                continue
            batch_results = list(
                executor.map(
                    lambda url: fetch_page(url, args.delay, args.retries), batch
                )
            )
            for result in batch_results:
                url = result["url"]
                results[url] = result
                for href in result.get("links") or []:
                    normalized = normalize_internal_url(href, result.get("finalUrl") or url, allowed_hosts)
                    if not normalized:
                        continue
                    discovered = add_discovery(inventory, normalized, url)
                    if (
                        not discovered["skipReason"]
                        and normalized["url"] not in queued
                        and normalized["url"] not in results
                        and len(queued) < args.max_pages * 3
                    ):
                        queue.append(normalized["url"])
                        queued.add(normalized["url"])

    rows = []
    for url, item in sorted(inventory.items(), key=lambda entry: entry[1]["path"]):
        robots_allowed = robots["parser"].can_fetch(USER_AGENT, url)
        rows.append(serializable_inventory_item(item, results.get(url), robots_allowed))

    status_counts = Counter(
        str(item.get("status")) for item in rows if item.get("status") is not None
    )
    category_counts = Counter(item["category"] for item in rows)
    skip_counts = Counter(item.get("skipReason") for item in rows if item.get("skipReason"))
    discovered_not_in_sitemap = [item for item in rows if not item["inSitemap"]]
    errors = [item for item in rows if item.get("error")]
    redirects = [item for item in rows if item.get("redirected")]
    canonical_mismatches = [
        item
        for item in rows
        if item.get("canonical")
        and normalize_internal_url(item["canonical"], item["url"], allowed_hosts)
        and normalize_internal_url(item["canonical"], item["url"], allowed_hosts)["path"]
        != item["path"]
    ]
    report = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "mode": "read_only_existing_storefront_crawl",
        "httpMethods": ["GET"],
        "writesPerformed": False,
        "applySupported": False,
        "baseUrl": base_url,
        "sitemap": {
            "url": sitemap_url,
            "status": sitemap["status"],
            "contentType": sitemap["contentType"],
            "bytes": sitemap["bytes"],
            "urls": len(sitemap["urls"]),
            "declaredChildSitemaps": sitemap["declaredChildSitemaps"],
            "fetchedChildSitemaps": len(sitemap["childSitemaps"]),
            "rootLocaleOnly": args.root_locale_only,
        },
        "robots": {
            "url": robots["url"],
            "status": robots["status"],
            "error": robots["error"],
            "respected": True,
        },
        "guards": {
            "actionPrefixes": list(ACTION_PREFIXES),
            "assetExtensions": sorted(ASSET_EXTENSIONS),
            "maxPages": args.max_pages,
            "workers": args.workers,
            "delaySecondsPerWorker": args.delay,
            "retries": args.retries,
        },
        "summary": {
            "discoveredUrls": len(rows),
            "crawledUrls": len(results),
            "sitemapUrls": len(sitemap["urls"]),
            "discoveredNotInSitemap": len(discovered_not_in_sitemap),
            "statuses": dict(sorted(status_counts.items())),
            "categories": dict(sorted(category_counts.items())),
            "skipped": dict(sorted(skip_counts.items())),
            "errors": len(errors),
            "redirectedResponses": len(redirects),
            "canonicalMismatches": len(canonical_mismatches),
            "crawlLimitReached": bool(queue),
        },
        "urls": rows,
        "discoveredNotInSitemap": discovered_not_in_sitemap,
        "errors": errors,
        "redirectedResponses": redirects,
        "canonicalMismatches": canonical_mismatches,
    }
    report_path = output_dir / "existing-site-crawl.json"
    inventory_csv = output_dir / "existing-site-urls.csv"
    discovered_csv = output_dir / "discovered-not-in-sitemap.csv"
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    write_csv(inventory_csv, rows)
    write_csv(discovered_csv, discovered_not_in_sitemap)
    print(json.dumps(report["summary"], indent=2))
    print("Writes performed: false")
    print(f"Report: {report_path}")
    print(f"URL inventory: {inventory_csv}")
    print(f"Discovered outside sitemap: {discovered_csv}")


if __name__ == "__main__":
    main()
