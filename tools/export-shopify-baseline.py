#!/usr/bin/env python3

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from shopify_api import get_client_credentials_token


DEFAULT_API_VERSION = "2026-04"
DEFAULT_PAGE_SIZE = 250


def parse_env_value(value):
    value = value.strip()
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    return value


def load_dotenv(path):
    env_path = Path(path)
    if not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$", line)
        if match and match.group(1) not in os.environ:
            os.environ[match.group(1)] = parse_env_value(match.group(2))


def normalize_shop(value):
    cleaned = (
        (value or "")
        .strip()
        .replace("https://", "")
        .replace("http://", "")
        .split("/")[0]
    )
    return cleaned if "." in cleaned else f"{cleaned}.myshopify.com"


def next_link(link_header):
    for part in (link_header or "").split(","):
        if 'rel="next"' not in part:
            continue
        match = re.search(r"<([^>]+)>", part)
        return match.group(1) if match else None
    return None


def request_json(url, token, method="GET", payload=None, attempts=8):
    headers = {
        "Accept": "application/json",
        "X-Shopify-Access-Token": token,
    }
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    last_error = None
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(
            url,
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return (
                    json.loads(response.read().decode("utf-8") or "{}"),
                    {
                        key.lower(): value
                        for key, value in response.getheaders()
                    },
                )
        except urllib.error.HTTPError as error:
            last_error = error
            if error.code != 429 and error.code < 500:
                message = error.read().decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"Shopify request failed ({error.code}) for {url}: {message}"
                ) from error
            if attempt < attempts:
                retry_after = float(error.headers.get("Retry-After") or min(attempt, 4))
                time.sleep(max(retry_after, 0.75))
        except urllib.error.URLError as error:
            last_error = error
            if attempt < attempts:
                time.sleep(min(attempt, 4))

    raise RuntimeError(f"Shopify request failed after {attempts} attempts: {url}") from last_error


def paginated_rest_get(base_url, resource, token, params=None):
    query = dict(params or {})
    query.setdefault("limit", DEFAULT_PAGE_SIZE)
    url = f"{base_url}/{resource}.json?{urllib.parse.urlencode(query)}"
    items = []

    while url:
        data, headers = request_json(url, token)
        items.extend(data.get(resource, []))
        url = next_link(headers.get("link"))
        if url:
            time.sleep(0.55)

    return items


def get_product_sync_metafields(graphql_url, token):
    query = """
      query ProductBrandBaseline($first: Int!, $after: String) {
        products(first: $first, after: $after, sortKey: ID) {
          nodes {
            legacyResourceId
            customBrand: metafield(namespace: "custom", key: "brand") {
              id
              type
              value
            }
            lightspeedInternalId: metafield(
              namespace: "custom"
              key: "lightspeed_internal_id"
            ) {
              id
              type
              value
            }
            lightspeedSource: metafield(
              namespace: "custom"
              key: "lightspeed_source"
            ) {
              id
              type
              value
            }
            catalogStatusSync: metafield(
              namespace: "custom"
              key: "catalog_status_sync"
            ) {
              id
              type
              value
            }
          }
          pageInfo {
            hasNextPage
            endCursor
          }
        }
      }
    """
    after = None
    metafields = {}

    while True:
        payload = {
            "query": query,
            "variables": {"first": 100, "after": after},
        }
        response, _ = request_json(
            graphql_url,
            token,
            method="POST",
            payload=payload,
        )
        if response.get("errors"):
            raise RuntimeError(f"Shopify GraphQL errors: {response['errors']}")

        products = response.get("data", {}).get("products", {})
        for node in products.get("nodes", []):
            metafields[str(node["legacyResourceId"])] = {
                "custom_brand_metafield": node.get("customBrand"),
                "lightspeed_internal_id_metafield": node.get(
                    "lightspeedInternalId"
                ),
                "lightspeed_source_metafield": node.get("lightspeedSource"),
                "catalog_status_sync_metafield": node.get(
                    "catalogStatusSync"
                ),
            }

        page_info = products.get("pageInfo", {})
        if not page_info.get("hasNextPage"):
            return metafields
        after = page_info.get("endCursor")
        time.sleep(0.55)


def write_json(path, value):
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def file_manifest(path):
    data = path.read_bytes()
    return {
        "file": path.name,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def optional_export(label, fetch):
    try:
        return fetch(), None
    except RuntimeError as error:
        message = str(error)
        print(f"Warning: {label} could not be exported: {message}")
        return None, message


def acquire_token(args):
    stored_token = os.environ.get("SHOPIFY_ADMIN_ACCESS_TOKEN") or os.environ.get(
        "SHOPIFY_ACCESS_TOKEN"
    )
    client_id = args.api_key or os.environ.get("SHOPIFY_API_KEY") or os.environ.get(
        "SHOPIFY_CLIENT_ID"
    )
    client_secret = (
        args.api_secret
        or os.environ.get("SHOPIFY_API_SECRET")
        or os.environ.get("SHOPIFY_CLIENT_SECRET")
    )

    if not args.use_stored_token and client_id and client_secret:
        token_data = get_client_credentials_token(args.shop, client_id, client_secret)
        return token_data["access_token"], {
            "source": "client_credentials",
            "scope": token_data.get("scope", ""),
            "expires_in": token_data.get("expires_in"),
        }
    if stored_token:
        return stored_token, {
            "source": "stored_admin_token",
            "scope": os.environ.get("SHOPIFY_ADMIN_ACCESS_TOKEN_SCOPE", ""),
            "expires_in": None,
        }
    if client_id and client_secret:
        token_data = get_client_credentials_token(args.shop, client_id, client_secret)
        return token_data["access_token"], {
            "source": "client_credentials",
            "scope": token_data.get("scope", ""),
            "expires_in": token_data.get("expires_in"),
        }
    raise SystemExit(
        "Missing Shopify credentials. Set SHOPIFY_API_KEY and SHOPIFY_API_SECRET, "
        "or provide SHOPIFY_ADMIN_ACCESS_TOKEN."
    )


def main():
    parser = argparse.ArgumentParser(
        description="Export a read-only Shopify store baseline with hashes and counts."
    )
    parser.add_argument("--shop", help="Shopify store domain.")
    parser.add_argument("--api-key", help="Shopify app client ID.")
    parser.add_argument("--api-secret", help="Shopify app client secret.")
    parser.add_argument(
        "--api-version",
        default=DEFAULT_API_VERSION,
        help=f"Shopify Admin API version. Default: {DEFAULT_API_VERSION}.",
    )
    parser.add_argument(
        "--env-file",
        help="Path to .env. Default: ../.env relative to this script.",
    )
    parser.add_argument(
        "--output-dir",
        help="Output directory. Default: .tmp/shopify-baseline-<UTC timestamp>.",
    )
    parser.add_argument(
        "--use-stored-token",
        action="store_true",
        help="Prefer SHOPIFY_ADMIN_ACCESS_TOKEN instead of requesting a fresh token.",
    )
    parser.add_argument(
        "--minimum-products",
        type=int,
        default=1,
        help="Fail when fewer products are exported. Default: 1.",
    )
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent.parent
    env_path = Path(args.env_file or project_root / ".env")
    load_dotenv(env_path)

    args.shop = normalize_shop(
        args.shop
        or os.environ.get("SHOPIFY_SHOP")
        or os.environ.get("SHOPIFY_STORE_DOMAIN")
        or os.environ.get("SHOPIFY_STORE")
    )
    if not args.shop or args.shop == ".myshopify.com":
        raise SystemExit("Missing SHOPIFY_SHOP.")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = Path(
        args.output_dir or project_root / ".tmp" / f"shopify-baseline-{timestamp}"
    ).resolve()
    output_dir.mkdir(parents=True, exist_ok=False)

    token, auth = acquire_token(args)
    rest_base = f"https://{args.shop}/admin/api/{args.api_version}"
    graphql_url = f"{rest_base}/graphql.json"

    print(f"Exporting read-only Shopify baseline for {args.shop}.")
    print(f"Authentication: {auth['source']} (token is not stored).")

    shop_data, _ = request_json(f"{rest_base}/shop.json", token)
    products = paginated_rest_get(
        rest_base,
        "products",
        token,
    )
    custom_collections = paginated_rest_get(
        rest_base,
        "custom_collections",
        token,
    )
    smart_collections = paginated_rest_get(
        rest_base,
        "smart_collections",
        token,
    )
    redirects, redirects_error = optional_export(
        "redirects",
        lambda: paginated_rest_get(rest_base, "redirects", token),
    )
    pages, pages_error = optional_export(
        "pages",
        lambda: paginated_rest_get(rest_base, "pages", token),
    )
    product_metafields = get_product_sync_metafields(graphql_url, token)

    if len(products) < args.minimum_products:
        raise RuntimeError(
            f"Baseline validation failed: exported {len(products)} products, "
            f"expected at least {args.minimum_products}."
        )

    for product in products:
        product.update(
            product_metafields.get(
                str(product["id"]),
                {
                    "custom_brand_metafield": None,
                    "lightspeed_internal_id_metafield": None,
                    "lightspeed_source_metafield": None,
                    "catalog_status_sync_metafield": None,
                },
            )
        )

    resource_errors = {
        key: value
        for key, value in {
            "redirects": redirects_error,
            "pages": pages_error,
        }.items()
        if value
    }
    exports = {
        "shop.json": shop_data,
        "products.json": {"products": products},
        "custom_collections.json": {"custom_collections": custom_collections},
        "smart_collections.json": {"smart_collections": smart_collections},
        "redirects.json": {
            "redirects": redirects,
            "export_error": redirects_error,
        },
        "pages.json": {
            "pages": pages,
            "export_error": pages_error,
        },
    }
    export_paths = []
    for filename, data in exports.items():
        path = output_dir / filename
        write_json(path, data)
        export_paths.append(path)

    statuses = Counter(product.get("status", "unknown") for product in products)
    blank_brand_count = sum(
        1 for product in products if not product.get("custom_brand_metafield")
    )
    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "shop": args.shop,
        "api_version": args.api_version,
        "authentication": auth,
        "counts": {
            "products": len(products),
            "product_statuses": dict(sorted(statuses.items())),
            "products_without_custom_brand": blank_brand_count,
            "custom_collections": len(custom_collections),
            "smart_collections": len(smart_collections),
            "collections_total": len(custom_collections) + len(smart_collections),
            "redirects": len(redirects) if redirects is not None else None,
            "pages": len(pages) if pages is not None else None,
        },
        "resource_errors": resource_errors,
        "files": [file_manifest(path) for path in export_paths],
    }
    manifest_path = output_dir / "manifest.json"
    write_json(manifest_path, manifest)

    print(json.dumps(manifest["counts"], indent=2))
    print(f"Baseline written to {output_dir}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
