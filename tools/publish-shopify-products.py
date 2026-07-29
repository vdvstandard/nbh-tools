#!/usr/bin/env python3

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
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_API_VERSION = "2026-04"
MAX_RETRIES = 4


def load_dotenv(file_path: Path) -> None:
    if not file_path.exists():
        return
    for line in file_path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$", line)
        if match and match.group(1) not in os.environ:
            os.environ[match.group(1)] = parse_env_value(match.group(2))


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


def parse_json_response(value: str) -> Dict[str, Any]:
    try:
        data = json.loads(value or "{}")
    except json.JSONDecodeError:
        data = {"raw": value}
    return data if isinstance(data, dict) else {"value": data}


def http_request_json(
    url: str,
    method: str,
    payload: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 45,
) -> Tuple[Dict[str, Any], int, Dict[str, str]]:
    request_headers = dict(headers or {})
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return (
                parse_json_response(response.read().decode("utf-8")),
                response.status,
                {key.lower(): value for key, value in response.getheaders()},
            )
    except urllib.error.HTTPError as exc:
        return (
            parse_json_response(exc.read().decode("utf-8", errors="replace")),
            exc.code,
            {key.lower(): value for key, value in exc.headers.items()},
        )


def get_client_credentials_token(shop: str, client_id: str, client_secret: str) -> str:
    body = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"https://{shop}/admin/oauth/access_token",
        data=body,
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            data = parse_json_response(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        data = parse_json_response(exc.read().decode("utf-8", errors="replace"))
        message = data.get("error_description") or data.get("error") or "Unknown error"
        raise RuntimeError(f"Shopify client_credentials failed ({exc.code}): {message}") from exc
    token = data.get("access_token")
    if not token:
        raise RuntimeError("Shopify did not return an access_token.")
    print(f"Temporary token acquired. Scope: {data.get('scope', '(not returned)')}. Not stored in .env.")
    return token


def parse_next_link(link_header: str) -> Optional[str]:
    for part in (link_header or "").split(","):
        if 'rel="next"' not in part:
            continue
        match = re.search(r"<([^>]+)>", part)
        return match.group(1) if match else None
    return None


def get_products(shop: str, api_version: str, token: str) -> List[Dict[str, Any]]:
    fields = "id,title,handle,vendor,status,published_at,published_scope"
    query = urllib.parse.urlencode({"limit": 250, "fields": fields})
    url = f"https://{shop}/admin/api/{api_version}/products.json?{query}"
    headers = {"Accept": "application/json", "X-Shopify-Access-Token": token}
    products: List[Dict[str, Any]] = []

    while url:
        payload, status, response_headers = http_request_json(url, "GET", headers=headers)
        if status < 200 or status >= 300:
            raise RuntimeError(f"Shopify product scan failed with HTTP {status}: {json.dumps(payload)}")
        products.extend(payload.get("products") or [])
        url = parse_next_link(response_headers.get("link", ""))

    return products


def publish_product(shop: str, api_version: str, token: str, product: Dict[str, Any]) -> Dict[str, Any]:
    product_id = product["id"]
    url = f"https://{shop}/admin/api/{api_version}/products/{product_id}.json"
    headers = {"Accept": "application/json", "X-Shopify-Access-Token": token}
    published_at = datetime.now(timezone.utc).isoformat()
    last_error: Optional[Exception] = None

    for attempt in range(1, MAX_RETRIES + 1):
        payload, status, response_headers = http_request_json(
            url,
            "PUT",
            {"product": {"id": product_id, "published_at": published_at}},
            headers,
        )
        if status == 429 or status >= 500:
            last_error = RuntimeError(f"Shopify product publish failed with HTTP {status}: {json.dumps(payload)}")
            if attempt < MAX_RETRIES:
                time.sleep(int(response_headers.get("retry-after", "0") or "0") or attempt)
                continue
        if status < 200 or status >= 300:
            raise RuntimeError(f"Shopify product publish failed with HTTP {status}: {json.dumps(payload)}")
        updated = payload.get("product") or {}
        if not updated.get("published_at"):
            raise RuntimeError("Shopify accepted the update but returned no published_at value.")
        return updated

    raise last_error or RuntimeError("Shopify product publish failed after retries.")


def split_handles(value: str) -> List[str]:
    return [handle.strip() for handle in (value or "").split(",") if handle.strip()]


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def main() -> None:
    project_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Publish active Shopify products to the Online Store.")
    parser.add_argument("--apply", action="store_true", help="Publish matching products. Dry-run is the default.")
    parser.add_argument("--vendor", help="Only include products with this exact Shopify vendor.")
    parser.add_argument("--handles", default="", help="Comma-separated product handles to include.")
    parser.add_argument("--all-active", action="store_true", help="Allow --apply without a vendor or handle filter.")
    parser.add_argument("--limit", type=positive_int, help="Limit the number of matching products.")
    parser.add_argument("--shop", help="Shopify store domain.")
    parser.add_argument("--token", help="Shopify Admin access token.")
    parser.add_argument("--api-key", help="Shopify app client ID/API key.")
    parser.add_argument("--api-secret", help="Shopify app client secret.")
    parser.add_argument("--prefer-client-credentials", action="store_true", help="Use client_credentials even when an admin token is set.")
    parser.add_argument("--api-version", default=DEFAULT_API_VERSION, help="Shopify Admin API version.")
    parser.add_argument("--env-file", help="Path to .env. Default: repository .env.")
    parser.add_argument("--continue-on-error", action="store_true", help="Continue publishing after an individual product error.")
    args = parser.parse_args()

    handles = set(split_handles(args.handles))
    if args.apply and not args.vendor and not handles and not args.all_active:
        raise SystemExit("--apply requires --vendor, --handles, or the explicit --all-active flag.")

    load_dotenv(Path(args.env_file or project_root / ".env"))
    shop = normalize_shop(args.shop or os.environ.get("SHOPIFY_SHOP") or os.environ.get("SHOPIFY_STORE_DOMAIN") or os.environ.get("SHOPIFY_STORE"))
    stored_token = os.environ.get("SHOPIFY_ADMIN_ACCESS_TOKEN") or os.environ.get("SHOPIFY_ACCESS_TOKEN")
    token = args.token or stored_token
    client_id = args.api_key or os.environ.get("SHOPIFY_API_KEY") or os.environ.get("SHOPIFY_CLIENT_ID")
    client_secret = args.api_secret or os.environ.get("SHOPIFY_API_SECRET") or os.environ.get("SHOPIFY_CLIENT_SECRET")
    api_version = args.api_version or os.environ.get("SHOPIFY_API_VERSION") or DEFAULT_API_VERSION

    if not shop or (not token and (not client_id or not client_secret)):
        raise SystemExit("Missing Shopify credentials.")
    if args.prefer_client_credentials or not token:
        token = get_client_credentials_token(shop, client_id, client_secret)

    products = get_products(shop, api_version, token)
    matching = [
        product
        for product in products
        if product.get("status") == "active"
        and not product.get("published_at")
        and (not args.vendor or product.get("vendor") == args.vendor)
        and (not handles or product.get("handle") in handles)
    ]
    if args.limit:
        matching = matching[: args.limit]

    print(f"{'Apply' if args.apply else 'Dry run'}: publish active products to Online Store on {shop}")
    print(f"Products scanned: {len(products)}")
    print(f"Active unpublished matches: {len(matching)}")
    for product in matching[:15]:
        print(f"- {product['title']} ({product['handle']}) | vendor={product.get('vendor')}")
    if len(matching) > 15:
        print(f"...and {len(matching) - 15} more.")

    if not args.apply:
        print("\nNo changes were written. Re-run with --apply after reviewing the matches.")
        return

    published = 0
    errors = []
    for index, product in enumerate(matching, start=1):
        try:
            updated = publish_product(shop, api_version, token, product)
            published += 1
            print(f"Published {index}/{len(matching)}: {updated.get('title')} ({updated.get('handle')})")
        except Exception as exc:
            errors.append({"handle": product.get("handle"), "message": str(exc)})
            print(f"ERROR {index}/{len(matching)}: {product.get('title')} ({product.get('handle')}): {exc}")
            if not args.continue_on_error:
                raise

    print(f"\nDone. Published: {published}. Errors: {len(errors)}.")


if __name__ == "__main__":
    main()
