#!/usr/bin/env python3
"""Prepare a local redirect review plan without writing to Shopify."""

import argparse
import csv
import difflib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path


DEFAULT_BASELINE = ".tmp/phase8-shopify-baseline-20260805"
DEFAULT_PREVIOUS_BASELINE = ".tmp/shopify-baseline-phase0-complete-20260725"
DEFAULT_SITEMAP = "https://www.nbharnhem.com/sitemap.xml"
DEFAULT_OLD_HOME = "https://www.nbharnhem.com/"
DEFAULT_SHOPIFY_PREVIEW = "http://127.0.0.1:9292/"
DEFAULT_CRAWL_REPORT = (
    ".tmp/phase8-existing-site-crawl-20260805/existing-site-crawl.json"
)
DEFAULT_OUTPUT_DIR = ".tmp/phase8-redirect-preparation-20260805"
RETIRED_VENDOR_HINTS = {
    "messyweekend": "Messyweekend",
    "pop trading company": "POP Trading Company",
    "encens d'auroville": "encens d'auroville",
}


def detect_retired_vendor(*values):
    text = " ".join(str(value or "") for value in values).lower()
    return next(
        (label for needle, label in RETIRED_VENDOR_HINTS.items() if needle in text),
        None,
    )


def parse_args():
    parser = argparse.ArgumentParser(
        description="Prepare a read-only Shopify redirect mapping review."
    )
    parser.add_argument("--baseline-dir", default=DEFAULT_BASELINE)
    parser.add_argument("--previous-baseline-dir", default=DEFAULT_PREVIOUS_BASELINE)
    parser.add_argument("--sitemap-url", default=DEFAULT_SITEMAP)
    parser.add_argument("--old-home-url", default=DEFAULT_OLD_HOME)
    parser.add_argument("--shopify-preview-url", default=DEFAULT_SHOPIFY_PREVIEW)
    parser.add_argument("--crawl-report", default=DEFAULT_CRAWL_REPORT)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def fetch_sitemap(url):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Neighbourhood redirect preparation audit/1.0"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read()
        status = response.status
        content_type = response.headers.get("Content-Type", "")
    root = ET.fromstring(body)
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    raw_url_count = 0
    urls_by_path = {}
    duplicate_urls = []
    for node in root.findall("sm:url", namespace):
        location = node.findtext("sm:loc", default="", namespaces=namespace).strip()
        if not location:
            continue
        raw_url_count += 1
        parsed = urllib.parse.urlparse(location)
        item = {
            "url": location,
            "path": normalize_path(parsed.path),
            "lastmod": node.findtext("sm:lastmod", default="", namespaces=namespace)
            or None,
        }
        if item["path"] in urls_by_path:
            duplicate_urls.append(item)
            continue
        urls_by_path[item["path"]] = item
    return {
        "status": status,
        "contentType": content_type,
        "bytes": len(body),
        "rawUrlCount": raw_url_count,
        "duplicateUrls": duplicate_urls,
        "urls": list(urls_by_path.values()),
    }


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.active = None

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return
        attributes = dict(attrs)
        href = (attributes.get("href") or "").strip()
        if href:
            self.active = {"href": href, "text": []}

    def handle_data(self, data):
        if self.active is not None:
            self.active["text"].append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self.active is not None:
            self.links.append(
                {
                    "href": self.active["href"],
                    "text": re.sub(r"\s+", " ", " ".join(self.active["text"])).strip(),
                }
            )
            self.active = None


def fetch_homepage_links(url):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Neighbourhood redirect preparation audit/1.0"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read()
        content_type = response.headers.get("Content-Type", "")
        status = response.status
    encoding = "utf-8"
    content_type_match = re.search(r"charset=([^;\s]+)", content_type, re.I)
    if content_type_match:
        encoding = content_type_match.group(1).strip('"\'')
    parser = LinkParser()
    parser.feed(body.decode(encoding, errors="replace"))
    base = urllib.parse.urlparse(url)
    deduplicated = {}
    for item in parser.links:
        absolute = urllib.parse.urljoin(url, item["href"])
        parsed = urllib.parse.urlparse(absolute)
        if parsed.scheme not in {"http", "https"} or parsed.netloc != base.netloc:
            continue
        path = normalize_path(parsed.path)
        key = (path, parsed.query)
        if key not in deduplicated or (
            not deduplicated[key]["text"] and item["text"]
        ):
            deduplicated[key] = {
                "path": path,
                "query": parsed.query or None,
                "text": item["text"],
            }
    return {
        "url": url,
        "status": status,
        "contentType": content_type,
        "links": sorted(
            deduplicated.values(), key=lambda item: (item["path"], item["query"] or "")
        ),
    }


class ProductMetadataParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta = {}
        self.canonical = None

    def handle_starttag(self, tag, attrs):
        attributes = {key.lower(): value for key, value in attrs}
        if tag.lower() == "meta":
            key = (attributes.get("property") or attributes.get("name") or "").lower()
            content = (attributes.get("content") or "").strip()
            if key and content:
                self.meta[key] = content
        elif tag.lower() == "link" and "canonical" in (
            attributes.get("rel") or ""
        ).lower():
            self.canonical = attributes.get("href")


def fetch_old_product_metadata(url):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Neighbourhood redirect preparation audit/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
            status = response.status
            content_type = response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as error:
        body = error.read()
        status = error.code
        content_type = error.headers.get("Content-Type", "")
    encoding = "utf-8"
    content_type_match = re.search(r"charset=([^;\s]+)", content_type, re.I)
    if content_type_match:
        encoding = content_type_match.group(1).strip('"\'')
    html_text = body.decode(encoding, errors="replace")
    parser = ProductMetadataParser()
    parser.feed(html_text)
    description = parser.meta.get("og:description") or parser.meta.get("description") or ""
    retired_vendor = detect_retired_vendor(description)
    inventory_match = re.search(r'"inventoryLevel"\s*:\s*"?([0-9.-]+)', html_text)
    availability_match = re.search(
        r'"availability"\s*:\s*"([^"]+)"', html_text, re.I
    )
    return {
        "url": url,
        "status": status,
        "canonical": parser.canonical,
        "title": parser.meta.get("og:title"),
        "description": description,
        "availability": availability_match.group(1) if availability_match else None,
        "inventoryLevel": inventory_match.group(1) if inventory_match else None,
        "retiredVendorHint": retired_vendor,
    }


def normalize_path(value):
    path = urllib.parse.urlparse(value or "/").path or "/"
    path = re.sub(r"/{2,}", "/", path)
    return path if path.startswith("/") else f"/{path}"


def strip_path(value):
    return normalize_path(value).strip("/")


def public_product(product):
    return product.get("status") == "active" and bool(product.get("published_at"))


def public_resource(resource):
    return bool(resource.get("published_at"))


def classify_old_path(path):
    clean = strip_path(path)
    if not clean:
        return "root"
    if re.fullmatch(r"[^/]+\.html", clean):
        return "product"
    if clean == "brands" or clean.startswith("brands/"):
        return "brand"
    if clean.startswith("blogs/"):
        return "blog"
    if clean.startswith("tags/"):
        return "tag"
    if clean == "shop" or clean.startswith("shop/"):
        return "shop"
    if clean.startswith("service/"):
        return "service"
    return "listing"


def closest_handles(handle, handles, limit=3):
    matches = difflib.get_close_matches(handle, handles, n=limit, cutoff=0.58)
    return [
        {
            "handle": match,
            "score": round(difflib.SequenceMatcher(None, handle, match).ratio(), 4),
        }
        for match in matches
    ]


def load_baseline_inventory(directory):
    return {
        "products": read_json(directory / "products.json")["products"],
        "collections": (
            read_json(directory / "custom_collections.json")["custom_collections"]
            + read_json(directory / "smart_collections.json")["smart_collections"]
        ),
        "pages": read_json(directory / "pages.json")["pages"],
        "redirects": read_json(directory / "redirects.json").get("redirects") or [],
        "manifest": read_json(directory / "manifest.json"),
    }


def comparable_brand_key(value):
    normalized = (value or "").lower().replace("&", " and ")
    normalized = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")
    tokens = [token for token in normalized.split("-") if token and token != "and"]
    return "".join(tokens)


def product_has_inventory(product):
    return any(
        (variant.get("inventory_quantity") or 0) > 0
        for variant in product.get("variants") or []
    )


def build_brand_decision_evidence(rows, products, collections_by_handle):
    vendor_products = {}
    for product in products:
        vendor = (product.get("vendor") or "").strip()
        if vendor:
            vendor_products.setdefault(vendor, []).append(product)
    evidence = []
    for item in rows:
        if item["category"] != "brand" or item["status"] != "needs_decision":
            continue
        old_handle = strip_path(item["sourcePath"]).split("/", 1)[1]
        key = comparable_brand_key(old_handle)
        matching_vendors = []
        for vendor, vendor_items in vendor_products.items():
            if comparable_brand_key(vendor) != key:
                continue
            matching_vendors.append(
                {
                    "vendor": vendor,
                    "products": len(vendor_items),
                    "activeProducts": sum(
                        product.get("status") == "active" for product in vendor_items
                    ),
                    "publicProducts": sum(public_product(product) for product in vendor_items),
                    "productsWithInventory": sum(
                        product_has_inventory(product) for product in vendor_items
                    ),
                }
            )
        collection = collections_by_handle.get(old_handle)
        evidence.append(
            {
                "sourcePath": item["sourcePath"],
                "oldHandle": old_handle,
                "mappingStatus": item["status"],
                "matchingCollection": {
                    "handle": collection.get("handle"),
                    "title": collection.get("title"),
                    "public": public_resource(collection),
                }
                if collection
                else None,
                "suggestedCollections": item.get("candidates") or [],
                "matchingVendors": matching_vendors,
                "trafficEvidenceAvailable": False,
            }
        )
    return evidence


def compare_resource_handles(previous, current, resource_type, path_prefix):
    previous_by_id = {str(item["id"]): item for item in previous if item.get("id")}
    current_by_id = {str(item["id"]): item for item in current if item.get("id")}
    current_by_handle = {item.get("handle"): item for item in current if item.get("handle")}
    changed = []
    removed = []
    added = []
    replaced_at_same_handle = []
    rows = []
    for resource_id, old_item in previous_by_id.items():
        new_item = current_by_id.get(resource_id)
        if not new_item:
            replacement = current_by_handle.get(old_item.get("handle"))
            if replacement:
                replaced_at_same_handle.append(
                    {
                        "oldId": resource_id,
                        "newId": str(replacement.get("id")),
                        "title": replacement.get("title") or old_item.get("title"),
                        "handle": old_item.get("handle"),
                        "path": f"{path_prefix}{old_item.get('handle')}",
                    }
                )
                continue
            removed.append(
                {
                    "id": resource_id,
                    "title": old_item.get("title"),
                    "handle": old_item.get("handle"),
                }
            )
            rows.append(
                row(
                    f"{path_prefix}{old_item.get('handle')}",
                    f"removed_shopify_{resource_type}",
                    "needs_decision",
                    "high",
                    f"This {resource_type} existed in Phase 0 but no longer exists by the same Shopify ID.",
                )
            )
            continue
        old_handle = old_item.get("handle")
        new_handle = new_item.get("handle")
        if old_handle and new_handle and old_handle != new_handle:
            change = {
                "id": resource_id,
                "title": new_item.get("title") or old_item.get("title"),
                "oldHandle": old_handle,
                "newHandle": new_handle,
                "sourcePath": f"{path_prefix}{old_handle}",
                "targetPath": f"{path_prefix}{new_handle}",
            }
            changed.append(change)
            target_public = (
                public_product(new_item)
                if resource_type == "product"
                else public_resource(new_item)
            )
            rows.append(
                row(
                    change["sourcePath"],
                    f"shopify_{resource_type}_handle_change",
                    "ready_for_review" if target_public else "needs_decision",
                    "high",
                    f"The Shopify {resource_type} handle changed for the same resource ID."
                    if target_public
                    else f"The handle changed, but the current {resource_type} target is not public.",
                    change["targetPath"],
                )
            )
    for resource_id, item in current_by_id.items():
        if resource_id not in previous_by_id:
            added.append(
                {
                    "id": resource_id,
                    "title": item.get("title"),
                    "handle": item.get("handle"),
                }
            )
    return {
        "resourceType": resource_type,
        "previousCount": len(previous_by_id),
        "currentCount": len(current_by_id),
        "changedHandles": changed,
        "removed": removed,
        "added": added,
        "replacedAtSameHandle": replaced_at_same_handle,
        "rows": rows,
    }


def row(
    source_path,
    category,
    status,
    confidence,
    reason,
    target_path=None,
    candidates=None,
    source_url=None,
):
    return {
        "sourcePath": source_path,
        "sourceUrl": source_url,
        "category": category,
        "status": status,
        "confidence": confidence,
        "targetPath": target_path,
        "reason": reason,
        "candidates": candidates or [],
    }


def product_mapping(item, products_by_handle, product_handles):
    source_path = item["path"]
    handle = strip_path(source_path)[: -len(".html")]
    product = products_by_handle.get(handle)
    if product:
        target = f"/products/{handle}"
        if public_product(product):
            return row(
                source_path,
                "product",
                "ready_for_review",
                "high",
                "The old product slug exactly matches a public Shopify product handle.",
                target,
                source_url=item["url"],
            )
        return row(
            source_path,
            "product",
            "needs_decision",
            "high",
            f"The handle matches Shopify, but the target is {product.get('status')} or unpublished.",
            target,
            source_url=item["url"],
        )
    candidates = [
        {
            **candidate,
            "path": f"/products/{candidate['handle']}",
            "status": products_by_handle[candidate["handle"]].get("status"),
            "public": public_product(products_by_handle[candidate["handle"]]),
        }
        for candidate in closest_handles(handle, product_handles)
    ]
    return row(
        source_path,
        "product",
        "needs_decision",
        "low",
        "No exact Shopify product handle exists; candidates are suggestions only.",
        candidates=candidates,
        source_url=item["url"],
    )


def brand_mapping(item, collections_by_handle, collection_handles, pages_by_handle):
    source_path = item["path"]
    clean = strip_path(source_path)
    if clean == "brands":
        page = pages_by_handle.get("brands")
        return row(
            source_path,
            "brand_index",
            "ready_for_review" if page and public_resource(page) else "needs_decision",
            "high",
            "The old brand index maps to the Shopify Brands page.",
            "/pages/brands",
            source_url=item["url"],
        )
    handle = clean.split("/", 1)[1]
    collection = collections_by_handle.get(handle)
    if collection:
        target = f"/collections/{handle}"
        return row(
            source_path,
            "brand",
            "ready_for_review" if public_resource(collection) else "needs_decision",
            "high",
            "The old brand slug exactly matches a Shopify collection handle."
            if public_resource(collection)
            else "The collection handle matches, but the Shopify collection is unpublished.",
            target,
            source_url=item["url"],
        )
    candidates = [
        {
            **candidate,
            "path": f"/collections/{candidate['handle']}",
            "public": public_resource(collections_by_handle[candidate["handle"]]),
        }
        for candidate in closest_handles(handle, collection_handles)
    ]
    return row(
        source_path,
        "brand",
        "needs_decision",
        "low",
        "No exact Shopify collection handle exists; do not redirect this brand to the homepage.",
        candidates=candidates,
        source_url=item["url"],
    )


def service_mapping(item):
    mappings = {
        "/service/about/": (
            "/pages/brickstore",
            "medium",
            "The Brick Store page contains the current Neighbourhood story, but requires content review.",
        ),
        "/service/general-terms-conditions/": (
            "/policies/terms-of-service",
            "high",
            "The old terms page maps to the Shopify terms policy.",
        ),
        "/service/privacy-policy/": (
            "/policies/privacy-policy",
            "high",
            "The old privacy page maps to the Shopify privacy policy.",
        ),
    }
    target = mappings.get(item["path"])
    if target:
        target_path, confidence, reason = target
        return row(
            item["path"],
            "service",
            "ready_for_review" if confidence == "high" else "needs_decision",
            confidence,
            reason,
            target_path,
            source_url=item["url"],
        )
    candidates = []
    reason = "This old service page needs an explicit destination decision."
    if item["path"] == "/service/opening-hours/":
        candidates = [
            {"path": "/pages/brickstore", "note": "Current page intentionally omits hours."},
            {"path": "/policies/contact-information", "note": "Contains contact data but no hours."},
        ]
        reason = "No current Shopify page presents opening hours without changing the approved design."
    elif item["path"] == "/service/shipping-returns/":
        candidates = [
            {"path": "/policies/shipping-policy", "note": "Shipping content."},
            {"path": "/policies/refund-policy", "note": "Returns content."},
        ]
        reason = "The old combined page now has two canonical Shopify policies."
    return row(
        item["path"],
        "service",
        "needs_decision",
        "medium",
        reason,
        candidates=candidates,
        source_url=item["url"],
    )


def listing_mapping(item, collections_by_handle):
    path = item["path"]
    direct = {
        "/catalog/": "/collections/all",
        "/collection/": "/collections/all",
        "/in-store-exclusive/": "/collections/in-store-exclusive",
    }
    if path in direct:
        target = direct[path]
        handle = target.removeprefix("/collections/")
        target_public = handle == "all" or (
            handle in collections_by_handle
            and public_resource(collections_by_handle[handle])
        )
        return row(
            path,
            "listing",
            "ready_for_review" if target_public else "needs_decision",
            "high",
            "The old listing has a direct public Shopify collection equivalent."
            if target_public
            else "The proposed Shopify collection target is not public.",
            target,
            source_url=item["url"],
        )
    return row(
        path,
        "listing",
        "needs_decision",
        "low",
        "No direct canonical Shopify destination has been approved for this listing.",
        source_url=item["url"],
    )


def homepage_only_mapping(item, old_home_url):
    path = item["path"]
    source_url = urllib.parse.urljoin(old_home_url, path)
    if path == "/cart/":
        return row(
            path,
            "homepage_link",
            "ready_for_review",
            "high",
            "The old cart route maps directly to the Shopify cart route.",
            "/cart",
            source_url=source_url,
        )
    if path in {"/cookielaw/optIn/", "/cookielaw/optOut/"}:
        return row(
            path,
            "retired_action",
            "no_redirect_needed",
            "high",
            "This is an old consent action endpoint, not a canonical landing page.",
            source_url=source_url,
        )
    if path == "/service/":
        return row(
            path,
            "homepage_link",
            "needs_decision",
            "medium",
            "The old customer-service index has no single equivalent Shopify page.",
            candidates=[
                {"path": "/policies/contact-information"},
                {"path": "/pages/brickstore"},
            ],
            source_url=source_url,
        )
    if path == "/checkout/":
        return row(
            path,
            "homepage_link",
            "needs_decision",
            "medium",
            "An old checkout session cannot migrate; decide whether this should fall back to the Shopify cart.",
            candidates=[{"path": "/cart"}],
            source_url=source_url,
        )
    if path.startswith("/account/"):
        return row(
            path,
            "legacy_account",
            "needs_decision",
            "medium",
            "Old Lightspeed customer-account state does not migrate through a URL redirect.",
            candidates=[{"path": "/account"}],
            source_url=source_url,
        )
    return row(
        path,
        "homepage_link",
        "needs_decision",
        "low",
        "This old homepage link has no current Shopify feature equivalent.",
        source_url=source_url,
    )


def map_item(
    item,
    products_by_handle,
    product_handles,
    collections_by_handle,
    collection_handles,
    pages_by_handle,
):
    category = classify_old_path(item["path"])
    if category == "product":
        return product_mapping(item, products_by_handle, product_handles)
    if category == "brand":
        return brand_mapping(
            item, collections_by_handle, collection_handles, pages_by_handle
        )
    if category == "service":
        return service_mapping(item)
    if category == "shop":
        return row(
            item["path"],
            "shop",
            "ready_for_review",
            "high",
            "Old shop and pagination paths map to the complete Shopify catalog.",
            "/collections/all",
            source_url=item["url"],
        )
    if category == "tag":
        handle = strip_path(item["path"]).split("/", 1)[1]
        collection = collections_by_handle.get(handle)
        candidates = []
        if collection and public_resource(collection):
            candidates.append(
                {
                    "path": f"/collections/{handle}",
                    "note": "Exact public collection handle; confirm equivalent tag meaning.",
                }
            )
        return row(
            item["path"],
            "tag",
            "needs_decision",
            "medium" if candidates else "low",
            "Old tag semantics must be confirmed before mapping to a collection.",
            candidates=candidates,
            source_url=item["url"],
        )
    if category == "blog":
        return row(
            item["path"],
            "blog",
            "needs_decision",
            "low",
            "Shopify blog articles are not present in the baseline export; preserve, migrate or retire explicitly.",
            source_url=item["url"],
        )
    if category == "root":
        return row(
            item["path"],
            "root",
            "no_redirect_needed",
            "high",
            "The root path remains the storefront root after the domain migration.",
            "/",
            source_url=item["url"],
        )
    return listing_mapping(item, collections_by_handle)


def map_crawl_item(
    item,
    products_by_handle,
    product_handles,
    collections_by_handle,
    collection_handles,
    pages_by_handle,
):
    path = normalize_path(item["path"])
    source_url = item.get("url")
    brand_pagination = re.fullmatch(r"/brands/([^/]+)/page\d+\.html", path)

    if path == "/cdn-cgi/l/email-protection":
        mapped = row(
            path,
            "retired_action",
            "no_redirect_needed",
            "high",
            "This Cloudflare email-protection endpoint is not a canonical landing page.",
            source_url=source_url,
        )
    elif re.fullmatch(r"/collection/page\d+\.html", path):
        mapped = row(
            path,
            "listing_pagination",
            "ready_for_review",
            "high",
            "Old catalog pagination maps to the complete Shopify catalog.",
            "/collections/all",
            source_url=source_url,
        )
    elif brand_pagination:
        canonical_item = {
            "path": f"/brands/{brand_pagination.group(1)}/",
            "url": source_url,
        }
        mapped = brand_mapping(
            canonical_item,
            collections_by_handle,
            collection_handles,
            pages_by_handle,
        )
        mapped["sourcePath"] = path
        mapped["sourceUrl"] = source_url
        mapped["category"] = "brand_pagination"
        mapped["reason"] = (
            "Old brand pagination uses the same destination as its canonical brand page. "
            + mapped["reason"]
        )
    else:
        mapped = map_item(
            {"path": path, "url": source_url},
            products_by_handle,
            product_handles,
            collections_by_handle,
            collection_handles,
            pages_by_handle,
        )

    mapped["sourceEvidence"] = "existing_site_crawl"
    mapped["crawlStatus"] = item.get("status")
    mapped["crawlCanonical"] = item.get("canonical")
    mapped["crawlFirstSeenFrom"] = item.get("firstSeenFrom")
    if item.get("status") == 404 and mapped["status"] == "needs_decision":
        mapped["reason"] = (
            "The existing site currently returns 404 for this internally linked URL. "
            + mapped["reason"]
        )
    return mapped


def detect_plan_conflicts(rows, existing_redirects):
    conflicts = []
    targets_by_source = {}
    existing_by_source = {
        normalize_path(item.get("path")): normalize_path(item.get("target"))
        for item in existing_redirects
    }
    plan_sources = {
        item["sourcePath"]
        for item in rows
        if item.get("targetPath") and item["status"] != "no_redirect_needed"
    }
    for item in rows:
        if item["status"] == "no_redirect_needed":
            continue
        source = item["sourcePath"]
        target = item.get("targetPath")
        if not target:
            continue
        targets_by_source.setdefault(source, set()).add(target)
        if source == target and item["status"] != "no_redirect_needed":
            conflicts.append({"type": "self_redirect", "sourcePath": source})
        if target in plan_sources:
            conflicts.append(
                {"type": "potential_chain", "sourcePath": source, "targetPath": target}
            )
        existing_target = existing_by_source.get(source)
        if existing_target and existing_target != target:
            conflicts.append(
                {
                    "type": "existing_redirect_conflict",
                    "sourcePath": source,
                    "existingTarget": existing_target,
                    "proposedTarget": target,
                }
            )
    for source, targets in targets_by_source.items():
        if len(targets) > 1:
            conflicts.append(
                {"type": "duplicate_source", "sourcePath": source, "targets": sorted(targets)}
            )
    for source, count in Counter(item["sourcePath"] for item in rows).items():
        if count > 1:
            conflicts.append(
                {"type": "duplicate_source_rows", "sourcePath": source, "rows": count}
            )
    return conflicts


def write_review_csv(path, rows):
    fieldnames = [
        "source_path",
        "source_url",
        "target_path",
        "category",
        "status",
        "confidence",
        "source_evidence",
        "crawl_status",
        "crawl_canonical",
        "reason",
        "candidates_json",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for item in rows:
            writer.writerow(
                {
                    "source_path": item["sourcePath"],
                    "source_url": item.get("sourceUrl") or "",
                    "target_path": item.get("targetPath") or "",
                    "category": item["category"],
                    "status": item["status"],
                    "confidence": item["confidence"],
                    "source_evidence": item.get("sourceEvidence") or "",
                    "crawl_status": item.get("crawlStatus") or "",
                    "crawl_canonical": item.get("crawlCanonical") or "",
                    "reason": item["reason"],
                    "candidates_json": json.dumps(
                        item.get("candidates") or [], ensure_ascii=False
                    ),
                }
            )


def main():
    args = parse_args()
    project_root = Path(__file__).resolve().parent.parent
    baseline_dir = Path(args.baseline_dir)
    previous_baseline_dir = Path(args.previous_baseline_dir)
    crawl_report_path = Path(args.crawl_report)
    output_dir = Path(args.output_dir)
    if not baseline_dir.is_absolute():
        baseline_dir = project_root / baseline_dir
    if not previous_baseline_dir.is_absolute():
        previous_baseline_dir = project_root / previous_baseline_dir
    if not crawl_report_path.is_absolute():
        crawl_report_path = project_root / crawl_report_path
    if not output_dir.is_absolute():
        output_dir = project_root / output_dir
    required = [
        "products.json",
        "custom_collections.json",
        "smart_collections.json",
        "pages.json",
        "redirects.json",
        "manifest.json",
    ]
    missing = [name for name in required if not (baseline_dir / name).exists()]
    if missing:
        raise SystemExit(f"Baseline is incomplete; missing: {', '.join(missing)}")
    previous_missing = [
        name for name in required if not (previous_baseline_dir / name).exists()
    ]
    if previous_missing:
        raise SystemExit(
            "Previous baseline is incomplete; missing: "
            + ", ".join(previous_missing)
        )

    current = load_baseline_inventory(baseline_dir)
    previous = load_baseline_inventory(previous_baseline_dir)
    products = current["products"]
    collections = current["collections"]
    pages = current["pages"]
    existing_redirects = current["redirects"]
    manifest = current["manifest"]

    products_by_handle = {item["handle"]: item for item in products if item.get("handle")}
    collections_by_handle = {
        item["handle"]: item for item in collections if item.get("handle")
    }
    pages_by_handle = {item["handle"]: item for item in pages if item.get("handle")}
    product_handles = sorted(products_by_handle)
    collection_handles = sorted(collections_by_handle)

    sitemap = fetch_sitemap(args.sitemap_url)
    old_homepage = fetch_homepage_links(args.old_home_url)
    shopify_homepage = fetch_homepage_links(args.shopify_preview_url)
    sitemap_paths = {item["path"] for item in sitemap["urls"]}
    old_homepage_links_not_in_sitemap = [
        item for item in old_homepage["links"] if item["path"] not in sitemap_paths
    ]
    rows = [
        map_item(
            item,
            products_by_handle,
            product_handles,
            collections_by_handle,
            collection_handles,
            pages_by_handle,
        )
        for item in sitemap["urls"]
    ]
    rows.extend(
        homepage_only_mapping(item, args.old_home_url)
        for item in old_homepage_links_not_in_sitemap
        if not item.get("query")
    )
    crawl_report = read_json(crawl_report_path) if crawl_report_path.exists() else None
    crawl_relevant_urls = []
    crawl_excluded_urls = []
    crawl_rows = []
    if crawl_report:
        for item in crawl_report.get("discoveredNotInSitemap", []):
            if item.get("skipReason") or not item.get("crawled"):
                crawl_excluded_urls.append(item)
                continue
            crawl_relevant_urls.append(item)

        planned_paths = {item["sourcePath"] for item in rows}
        for item in crawl_relevant_urls:
            path = normalize_path(item["path"])
            if path in planned_paths:
                continue
            mapped = map_crawl_item(
                item,
                products_by_handle,
                product_handles,
                collections_by_handle,
                collection_handles,
                pages_by_handle,
            )
            crawl_rows.append(mapped)
            rows.append(mapped)
            planned_paths.add(path)
    handle_comparisons = {
        "products": compare_resource_handles(
            previous["products"], products, "product", "/products/"
        ),
        "collections": compare_resource_handles(
            previous["collections"], collections, "collection", "/collections/"
        ),
        "pages": compare_resource_handles(
            previous["pages"], pages, "page", "/pages/"
        ),
    }
    for comparison in handle_comparisons.values():
        rows.extend(comparison.pop("rows"))
    previous_products_by_handle = {
        item.get("handle"): item
        for item in previous["products"]
        if item.get("handle")
    }
    unmatched_product_evidence = []
    for item in rows:
        if not (
            item["category"] == "product"
            and item["status"] == "needs_decision"
            and not item.get("targetPath")
            and item.get("sourceUrl")
        ):
            continue
        handle = strip_path(item["sourcePath"])[: -len(".html")]
        previous_product = previous_products_by_handle.get(handle)
        old_metadata = fetch_old_product_metadata(item["sourceUrl"])
        retired_vendor = detect_retired_vendor(
            old_metadata.get("description"),
            (previous_product or {}).get("vendor"),
        )
        if retired_vendor:
            old_metadata["retiredVendorHint"] = retired_vendor
        unmatched_product_evidence.append(
            {
                "sourcePath": item["sourcePath"],
                "suggestedCandidates": item.get("candidates") or [],
                "oldProduct": old_metadata,
                "previousShopifyProduct": {
                    "id": str(previous_product.get("id")),
                    "title": previous_product.get("title"),
                    "handle": previous_product.get("handle"),
                    "vendor": previous_product.get("vendor"),
                    "status": previous_product.get("status"),
                }
                if previous_product
                else None,
            }
        )
    previous_stelff = next(
        (
            item
            for item in previous["collections"]
            if item.get("handle") == "stelff"
        ),
        None,
    )
    stelff_row = next(
        (item for item in rows if item["sourcePath"] == "/collections/stelff"),
        None,
    )
    stelff_evidence = [
        {
            "evidence": "The typo collection existed in the Phase 0 Shopify baseline.",
            "id": str(previous_stelff.get("id")),
            "publishedAt": previous_stelff.get("published_at"),
        }
    ] if previous_stelff else []
    if stelff_row:
        stelff_row.update(
            {
                "category": "typo_candidate",
                "status": "needs_evidence",
                "confidence": "medium",
                "targetPath": "/collections/steiff",
                "reason": (
                    "The old typo collection was removed; only create this redirect after "
                    "confirming the URL was shared, indexed or requested."
                ),
                "candidates": stelff_evidence,
            }
        )
    else:
        rows.append(
            row(
                "/collections/stelff",
                "typo_candidate",
                "needs_evidence",
                "medium",
                "Only create this redirect after confirming the typo URL was shared, indexed or requested.",
                "/collections/steiff",
                candidates=stelff_evidence,
            )
        )

    brand_decision_evidence = build_brand_decision_evidence(
        rows, products, collections_by_handle
    )
    conflicts = detect_plan_conflicts(rows, existing_redirects)
    category_counts = Counter(item["category"] for item in rows)
    status_counts = Counter(item["status"] for item in rows)
    confidence_counts = Counter(item["confidence"] for item in rows)
    ready_rows = [item for item in rows if item["status"] == "ready_for_review"]
    report = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "mode": "read_only_redirect_preparation",
        "shopifyWritesPerformed": False,
        "applySupported": False,
        "sources": {
            "sitemapUrl": args.sitemap_url,
            "sitemapStatus": sitemap["status"],
            "sitemapContentType": sitemap["contentType"],
            "sitemapBytes": sitemap["bytes"],
            "sitemapRawUrls": sitemap["rawUrlCount"],
            "sitemapUniqueUrls": len(sitemap["urls"]),
            "sitemapDuplicateUrls": len(sitemap["duplicateUrls"]),
            "baselineDir": str(baseline_dir),
            "baselineCreatedAt": manifest.get("created_at"),
            "previousBaselineDir": str(previous_baseline_dir),
            "previousBaselineCreatedAt": previous["manifest"].get("created_at"),
            "oldHomepageUrl": args.old_home_url,
            "oldHomepageStatus": old_homepage["status"],
            "oldHomepageInternalLinks": len(old_homepage["links"]),
            "shopifyPreviewUrl": args.shopify_preview_url,
            "shopifyPreviewStatus": shopify_homepage["status"],
            "shopifyHomepageInternalLinks": len(shopify_homepage["links"]),
            "crawlReportPath": str(crawl_report_path),
            "crawlReportLoaded": bool(crawl_report),
            "crawlMode": crawl_report.get("mode") if crawl_report else None,
            "crawlDiscoveredUrls": (
                crawl_report.get("summary", {}).get("discoveredUrls")
                if crawl_report
                else None
            ),
            "crawlRelevantUrlsOutsideSitemap": len(crawl_relevant_urls),
            "crawlExcludedProtectedOrUncrawledUrls": len(crawl_excluded_urls),
            "crawlRowsAdded": len(crawl_rows),
        },
        "summary": {
            "oldSitemapUrls": len(sitemap["urls"]),
            "oldSitemapRawUrls": sitemap["rawUrlCount"],
            "oldSitemapDuplicateUrls": len(sitemap["duplicateUrls"]),
            "planRows": len(rows),
            "categories": dict(sorted(category_counts.items())),
            "statuses": dict(sorted(status_counts.items())),
            "confidences": dict(sorted(confidence_counts.items())),
            "readyForReview": len(ready_rows),
            "crawlRowsAdded": len(crawl_rows),
            "crawlBrokenInternalUrls": sum(
                1 for item in crawl_relevant_urls if item.get("status") == 404
            ),
            "existingRedirects": len(existing_redirects),
            "conflicts": len(conflicts),
            "changedShopifyHandles": sum(
                len(item["changedHandles"]) for item in handle_comparisons.values()
            ),
            "removedShopifyResources": sum(
                len(item["removed"]) for item in handle_comparisons.values()
            ),
            "addedShopifyResources": sum(
                len(item["added"]) for item in handle_comparisons.values()
            ),
            "replacedAtSameHandle": sum(
                len(item["replacedAtSameHandle"])
                for item in handle_comparisons.values()
            ),
        },
        "shopifyHandleComparisons": handle_comparisons,
        "brandDecisionEvidence": brand_decision_evidence,
        "unmatchedProductEvidence": unmatched_product_evidence,
        "homepageLinkInventories": {
            "oldStorefront": old_homepage,
            "oldStorefrontLinksNotInSitemap": old_homepage_links_not_in_sitemap,
            "shopifyPreview": shopify_homepage,
        },
        "sitemapDuplicateUrls": sitemap["duplicateUrls"],
        "existingSiteCrawl": {
            "reportPath": str(crawl_report_path),
            "loaded": bool(crawl_report),
            "writeSafety": {
                "mode": crawl_report.get("mode") if crawl_report else None,
                "httpMethods": crawl_report.get("httpMethods") if crawl_report else [],
                "writesPerformed": (
                    crawl_report.get("writesPerformed") if crawl_report else None
                ),
                "applySupported": (
                    crawl_report.get("applySupported") if crawl_report else None
                ),
            },
            "relevantUrlsOutsideSitemap": crawl_relevant_urls,
            "excludedProtectedOrUncrawledUrls": crawl_excluded_urls,
            "rowsAdded": crawl_rows,
        },
        "sourceGaps": [
            "No analytics landing-page export was available in the workspace.",
            "No search-console URL export was available in the workspace.",
            "Shopify blog articles are not included in the current baseline exporter.",
        ]
        + ([] if crawl_report else ["No existing-site crawl report was available."]),
        "existingRedirects": existing_redirects,
        "preserveChecks": {
            "contactToBrickstore": any(
                normalize_path(item.get("path")) == "/pages/contact"
                and normalize_path(item.get("target")) == "/pages/brickstore"
                for item in existing_redirects
            )
        },
        "conflicts": conflicts,
        "rows": rows,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "redirect-preparation.json"
    csv_path = output_dir / "redirect-review.csv"
    ready_csv_path = output_dir / "redirect-ready-for-review.csv"
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    write_review_csv(csv_path, rows)
    write_review_csv(ready_csv_path, ready_rows)
    print(json.dumps(report["summary"], indent=2))
    print(f"Existing contact redirect preserved: {report['preserveChecks']['contactToBrickstore']}")
    print("Shopify writes performed: false")
    print(f"Report: {report_path}")
    print(f"Review CSV: {csv_path}")
    print(f"Ready-for-review CSV (not an import file): {ready_csv_path}")


if __name__ == "__main__":
    main()
