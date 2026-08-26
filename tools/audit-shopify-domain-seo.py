#!/usr/bin/env python3
"""Read-only domain, TLS, robots and sitemap preflight for Shopify launch."""

import argparse
import json
import re
import socket
import ssl
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import requests


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36 "
    "Neighbourhood-Phase9-Audit/1.0"
)


class HeadParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title_parts = []
        self.in_title = False
        self.description = ""
        self.canonical = ""
        self.robots = ""

    def handle_starttag(self, tag, attrs):
        values = {key.lower(): value or "" for key, value in attrs}
        tag = tag.lower()
        if tag == "title":
            self.in_title = True
        elif tag == "meta":
            name = values.get("name", "").lower()
            if name == "description":
                self.description = values.get("content", "").strip()
            elif name == "robots":
                self.robots = values.get("content", "").strip()
        elif tag == "link" and "canonical" in values.get("rel", "").lower():
            self.canonical = values.get("href", "").strip()

    def handle_endtag(self, tag):
        if tag.lower() == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.title_parts.append(data)

    @property
    def title(self):
        return re.sub(r"\s+", " ", " ".join(self.title_parts)).strip()


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--shopify-url", default="https://neighbourhood-arnhem.myshopify.com"
    )
    parser.add_argument("--legacy-url", default="https://www.nbharnhem.com")
    parser.add_argument(
        "--shop-baseline",
        default=".tmp/phase9-shopify-baseline-20260811/shop.json",
    )
    parser.add_argument(
        "--output", default=".tmp/phase9-domain-seo-20260811.json"
    )
    return parser.parse_args()


def session():
    value = requests.Session()
    value.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-GB,en;q=0.9",
        }
    )
    return value


def http_read(client, url):
    try:
        response = client.get(url, timeout=30, allow_redirects=True)
        response.raise_for_status()
        return {
            "requestedUrl": url,
            "status": response.status_code,
            "finalUrl": response.url,
            "contentType": response.headers.get("Content-Type", ""),
            "bytes": len(response.content),
            "text": response.text,
            "error": None,
        }
    except requests.RequestException as error:
        response = getattr(error, "response", None)
        return {
            "requestedUrl": url,
            "status": response.status_code if response is not None else None,
            "finalUrl": response.url if response is not None else None,
            "contentType": response.headers.get("Content-Type", "")
            if response is not None
            else "",
            "bytes": len(response.content) if response is not None else 0,
            "text": response.text if response is not None else "",
            "error": str(error),
        }


def tls_read(host):
    context = ssl.create_default_context()
    try:
        with socket.create_connection((host, 443), timeout=15) as raw_socket:
            with context.wrap_socket(raw_socket, server_hostname=host) as secure_socket:
                certificate = secure_socket.getpeercert()
        expires = datetime.strptime(
            certificate["notAfter"], "%b %d %H:%M:%S %Y %Z"
        ).replace(tzinfo=timezone.utc)
        subject = dict(item[0] for item in certificate.get("subject", []))
        issuer = dict(item[0] for item in certificate.get("issuer", []))
        return {
            "host": host,
            "valid": True,
            "commonName": subject.get("commonName"),
            "issuer": issuer.get("commonName"),
            "notAfter": expires.isoformat(),
            "daysRemaining": (expires - datetime.now(timezone.utc)).days,
            "subjectAltNames": [
                value
                for name_type, value in certificate.get("subjectAltName", [])
                if name_type == "DNS"
            ],
            "error": None,
        }
    except (OSError, ssl.SSLError, KeyError, ValueError) as error:
        return {
            "host": host,
            "valid": False,
            "error": str(error),
        }


def dns_read(host):
    try:
        addresses = sorted(
            {
                item[4][0]
                for item in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
            }
        )
        return {"host": host, "addresses": addresses, "error": None}
    except OSError as error:
        return {"host": host, "addresses": [], "error": str(error)}


def sitemap_summary(xml_text):
    root = ET.fromstring(xml_text)
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locations = [
        node.text.strip()
        for node in root.findall("sm:sitemap/sm:loc", namespace)
        if node.text and node.text.strip()
    ]
    root_children = []
    locale_prefixes = set()
    for location in locations:
        parts = [part for part in urlparse(location).path.split("/") if part]
        if len(parts) == 1:
            root_children.append(location)
        elif parts:
            locale_prefixes.add(parts[0])
    return {
        "childSitemaps": len(locations),
        "rootChildSitemaps": len(root_children),
        "localePrefixes": sorted(locale_prefixes),
        "locations": locations,
    }


def load_shop(path):
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    return data.get("shop", data) if isinstance(data, dict) else data


def public_http(item):
    return {key: value for key, value in item.items() if key != "text"}


def main():
    args = parse_args()
    shopify_url = args.shopify_url.rstrip("/")
    legacy_url = args.legacy_url.rstrip("/")
    shopify_host = urlparse(shopify_url).hostname
    legacy_host = urlparse(legacy_url).hostname
    apex_legacy_host = legacy_host.removeprefix("www.")
    client = session()

    shopify_home = http_read(client, f"{shopify_url}/")
    legacy_home = http_read(client, f"{legacy_url}/")
    robots = http_read(client, f"{shopify_url}/robots.txt")
    sitemap = http_read(client, f"{shopify_url}/sitemap.xml")

    home_parser = HeadParser()
    home_parser.feed(shopify_home["text"])
    legacy_parser = HeadParser()
    legacy_parser.feed(legacy_home["text"])
    robots_text = robots["text"]
    sitemap_data = sitemap_summary(sitemap["text"])
    shop = load_shop(args.shop_baseline)

    report = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "mode": "read_only_domain_seo_preflight",
        "httpMethods": ["GET"],
        "writesPerformed": False,
        "shopify": {
            "http": public_http(shopify_home),
            "homepage": {
                "title": home_parser.title,
                "description": home_parser.description,
                "canonical": home_parser.canonical,
                "metaRobots": home_parser.robots,
                "indexable": "noindex" not in home_parser.robots.lower(),
            },
            "shopBaseline": {
                "domain": shop.get("domain"),
                "myshopifyDomain": shop.get("myshopify_domain"),
                "passwordEnabled": shop.get("password_enabled"),
                "primaryLocale": shop.get("primary_locale"),
            },
        },
        "legacy": {
            "http": public_http(legacy_home),
            "homepage": {
                "title": legacy_parser.title,
                "canonical": legacy_parser.canonical,
                "lightspeedDetected": bool(
                    re.search(r"lightspeed|seoshop", legacy_home["text"], re.I)
                ),
                "shopifyDetected": bool(
                    re.search(
                        r"cdn\.shopify\.com|Shopify\.theme",
                        legacy_home["text"],
                        re.I,
                    )
                ),
            },
        },
        "dns": [
            dns_read(apex_legacy_host),
            dns_read(legacy_host),
            dns_read(shopify_host),
        ],
        "tls": [
            tls_read(apex_legacy_host),
            tls_read(legacy_host),
            tls_read(shopify_host),
        ],
        "robots": {
            **public_http(robots),
            "sitemapLines": [
                line.strip()
                for line in robots_text.splitlines()
                if line.lower().startswith("sitemap:")
            ],
            "globalDisallow": bool(
                re.search(
                    r"(?ms)^User-agent:\s*\*.*?^Disallow:\s*/\s*$",
                    robots_text,
                )
            ),
            "checkoutBlocked": bool(
                re.search(r"(?m)^Disallow:\s*/checkouts?", robots_text)
            ),
        },
        "sitemap": {**public_http(sitemap), **sitemap_data},
    }

    issues = []
    if shop.get("password_enabled"):
        issues.append("shopify_store_password_enabled")
    if shop.get("domain") == shop.get("myshopify_domain"):
        issues.append("custom_primary_domain_not_connected")
    if report["robots"]["globalDisallow"]:
        issues.append("robots_blocks_entire_storefront")
    if not home_parser.canonical:
        issues.append("homepage_canonical_missing")
    if any(not item["valid"] for item in report["tls"]):
        issues.append("tls_validation_failed")
    report["issues"] = issues
    report["summary"] = {
        "shopifyStatus": shopify_home["status"],
        "legacyStatus": legacy_home["status"],
        "passwordEnabled": bool(shop.get("password_enabled")),
        "customPrimaryDomainConnected": shop.get("domain")
        != shop.get("myshopify_domain"),
        "robotsGlobalDisallow": report["robots"]["globalDisallow"],
        "sitemapChildCount": sitemap_data["childSitemaps"],
        "localePrefixes": sitemap_data["localePrefixes"],
        "tlsValid": all(item["valid"] for item in report["tls"]),
        "issues": len(issues),
    }

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report["summary"], indent=2))
    print("Writes performed: false")
    print(f"Report: {output.resolve()}")
    raise SystemExit(1 if issues else 0)


if __name__ == "__main__":
    main()
