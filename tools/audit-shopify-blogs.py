#!/usr/bin/env python3
"""Export a read-only inventory of Shopify blogs and articles."""

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from shopify_api import get_client_credentials_token


DEFAULT_API_VERSION = "2026-04"
DEFAULT_OUTPUT_DIR = ".tmp/phase8-shopify-blog-audit-20260805"


def parse_args():
    parser = argparse.ArgumentParser(
        description="Read Shopify blog and article inventory without mutations."
    )
    parser.add_argument("--shop")
    parser.add_argument("--api-version", default=DEFAULT_API_VERSION)
    parser.add_argument("--env-file")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


def parse_env_value(value):
    value = value.strip()
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    return value


def load_dotenv(path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$", line)
        if match and match.group(1) not in os.environ:
            os.environ[match.group(1)] = parse_env_value(match.group(2))


def normalize_shop(value):
    cleaned = (
        str(value or "")
        .strip()
        .replace("https://", "")
        .replace("http://", "")
        .split("/")[0]
    )
    return cleaned if "." in cleaned else f"{cleaned}.myshopify.com"


def next_link(link_header):
    for part in str(link_header or "").split(","):
        if 'rel="next"' not in part:
            continue
        match = re.search(r"<([^>]+)>", part)
        return match.group(1) if match else None
    return None


def request_json(url, token, attempts=5):
    headers = {
        "Accept": "application/json",
        "X-Shopify-Access-Token": token,
    }
    last_error = None
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8") or "{}"), {
                    key.lower(): value for key, value in response.getheaders()
                }
        except urllib.error.HTTPError as error:
            last_error = error
            if error.code != 429 and error.code < 500:
                message = error.read().decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"Shopify GET failed ({error.code}) for {url}: {message}"
                ) from error
            if attempt < attempts:
                time.sleep(float(error.headers.get("Retry-After") or attempt))
        except urllib.error.URLError as error:
            last_error = error
            if attempt < attempts:
                time.sleep(attempt)
    raise RuntimeError(f"Shopify GET failed after {attempts} attempts: {url}") from last_error


def paginated_get(url, resource, token):
    items = []
    while url:
        data, headers = request_json(url, token)
        items.extend(data.get(resource, []))
        url = next_link(headers.get("link"))
    return items


def acquire_token(shop):
    stored_token = os.environ.get("SHOPIFY_ADMIN_ACCESS_TOKEN") or os.environ.get(
        "SHOPIFY_ACCESS_TOKEN"
    )
    client_id = os.environ.get("SHOPIFY_API_KEY") or os.environ.get("SHOPIFY_CLIENT_ID")
    client_secret = os.environ.get("SHOPIFY_API_SECRET") or os.environ.get(
        "SHOPIFY_CLIENT_SECRET"
    )
    if client_id and client_secret:
        token_data = get_client_credentials_token(shop, client_id, client_secret)
        return token_data["access_token"], "client_credentials"
    if stored_token:
        return stored_token, "stored_admin_token"
    raise SystemExit("Missing Shopify Admin API credentials.")


def main():
    args = parse_args()
    project_root = Path(__file__).resolve().parent.parent
    env_path = Path(args.env_file) if args.env_file else project_root / ".env"
    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = project_root / output_dir
    load_dotenv(env_path)
    shop = normalize_shop(
        args.shop
        or os.environ.get("SHOPIFY_SHOP")
        or os.environ.get("SHOPIFY_STORE_DOMAIN")
        or os.environ.get("SHOPIFY_STORE")
    )
    if not shop or shop == ".myshopify.com":
        raise SystemExit("Missing SHOPIFY_SHOP.")

    token, auth_source = acquire_token(shop)
    rest_base = f"https://{shop}/admin/api/{args.api_version}"
    blogs = paginated_get(f"{rest_base}/blogs.json?limit=250", "blogs", token)
    redirects = paginated_get(
        f"{rest_base}/redirects.json?limit=250", "redirects", token
    )
    article_rows = []
    for blog in blogs:
        articles = paginated_get(
            f"{rest_base}/blogs/{blog['id']}/articles.json?limit=250&published_status=any",
            "articles",
            token,
        )
        article_rows.extend(
            {
                **article,
                "blog_title": blog.get("title"),
                "blog_handle": blog.get("handle"),
            }
            for article in articles
        )

    blog_handle_redirects = [
        item
        for item in redirects
        if re.match(r"^/blogs/(?:news|blogs)(?:/|$)", str(item.get("path") or ""))
        and re.match(
            r"^/blogs/(?:blogs|journal)(?:/|$)",
            str(item.get("target") or ""),
        )
    ]
    summary = {
        "blogs": len(blogs),
        "articles": len(article_rows),
        "publishedArticles": sum(bool(item.get("published_at")) for item in article_rows),
        "articlesWithImage": sum(bool(item.get("image")) for item in article_rows),
        "blogHandleRedirects": len(blog_handle_redirects),
    }
    report = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "mode": "read_only_shopify_blog_audit",
        "httpMethods": ["GET"],
        "shopifyWritesPerformed": False,
        "applySupported": False,
        "shop": shop,
        "apiVersion": args.api_version,
        "authenticationSource": auth_source,
        "summary": summary,
        "blogs": blogs,
        "articles": article_rows,
        "blogHandleRedirects": blog_handle_redirects,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "shopify-blog-audit.json"
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))
    print("Shopify writes performed: false")
    print(f"Report: {report_path}")


if __name__ == "__main__":
    main()
