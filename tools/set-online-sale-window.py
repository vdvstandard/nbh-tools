#!/usr/bin/env python3

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


DEFAULT_API_VERSION = "2026-04"
DEFAULT_NAMESPACE = "custom"
DEFAULT_KEY = "online_sale_until"
DEFAULT_TYPE = "date_time"


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


def normalize_shop(value: Optional[str]) -> str:
    cleaned = (value or "").strip().replace("http://", "").replace("https://", "").split("/")[0]
    if not cleaned:
        return ""
    return cleaned if "." in cleaned else f"{cleaned}.myshopify.com"


def handleize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")


def normalize_product_gid(value: str) -> str:
    cleaned = (value or "").strip()
    if cleaned.startswith("gid://shopify/Product/"):
        return cleaned
    if cleaned.isdigit():
        return f"gid://shopify/Product/{cleaned}"
    raise SystemExit("--product-id must be a numeric Shopify product ID or gid://shopify/Product/... value.")


def http_post_json(url: str, payload: Dict[str, Any], headers: Dict[str, str], timeout: int = 45) -> Tuple[str, int, Dict[str, str]]:
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8"), response.status, dict(response.getheaders())
    except urllib.error.HTTPError as exc:
        return exc.read().decode("utf-8", errors="replace"), exc.code, dict(exc.headers.items())


def http_post_form(url: str, data: Dict[str, Any], headers: Dict[str, str], timeout: int = 45) -> Tuple[str, int, Dict[str, str]]:
    request = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8"), response.status, dict(response.getheaders())
    except urllib.error.HTTPError as exc:
        return exc.read().decode("utf-8", errors="replace"), exc.code, dict(exc.headers.items())


def get_client_credentials_token(shop: str, client_id: str, client_secret: str) -> str:
    response_text, status, _ = http_post_form(
        f"https://{shop}/admin/oauth/access_token",
        {"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret},
        {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
    )
    data = json.loads(response_text or "{}")
    if status < 200 or status >= 300:
        raise RuntimeError(f"Shopify client_credentials failed ({status}): {response_text}")
    token = data.get("access_token")
    if not token:
        raise RuntimeError("Shopify did not return an access_token.")
    print(f"Temporary token acquired. Scope: {data.get('scope', '(not returned)')}. Not stored in .env.")
    return token


def shopify_graphql(endpoint: str, token: str, query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
    for attempt in range(1, 5):
        response_text, status, headers = http_post_json(
            endpoint,
            {"query": query, "variables": variables},
            {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-Shopify-Access-Token": token,
            },
        )
        payload = json.loads(response_text or "{}")
        if status == 429 or status >= 500:
            if attempt < 4:
                time.sleep(int(headers.get("Retry-After", "0") or "0") or attempt)
                continue
        if status < 200 or status >= 300:
            raise RuntimeError(f"Shopify API request failed with HTTP {status}: {json.dumps(payload)}")
        if payload.get("errors"):
            raise RuntimeError("Shopify GraphQL errors: " + "; ".join(error.get("message", "") for error in payload["errors"]))
        wait_for_throttle_budget(payload.get("extensions", {}).get("cost", {}).get("throttleStatus"))
        return payload.get("data") or {}
    raise RuntimeError("Shopify API request failed after retries.")


def wait_for_throttle_budget(throttle_status: Optional[Dict[str, Any]]) -> None:
    if not throttle_status:
        return
    currently_available = float(throttle_status.get("currentlyAvailable", 0))
    restore_rate = float(throttle_status.get("restoreRate", 0))
    if currently_available < 100 and restore_rate > 0:
        time.sleep(max(((100 - currently_available) / restore_rate), 0.25))


def get_product_by_handle(endpoint: str, token: str, handle: str, namespace: str, key: str) -> Optional[Dict[str, Any]]:
    data = shopify_graphql(
        endpoint,
        token,
        """
        query OnlineSaleWindowProductByHandle($handle: String!, $namespace: String!, $key: String!) {
          productByHandle(handle: $handle) {
            id
            title
            handle
            vendor
            onlineSaleUntil: metafield(namespace: $namespace, key: $key) {
              id
              namespace
              key
              type
              value
              updatedAt
            }
          }
        }
        """,
        {"handle": handle, "namespace": namespace, "key": key},
    )
    return data.get("productByHandle")


def get_product_by_id(endpoint: str, token: str, product_id: str, namespace: str, key: str) -> Optional[Dict[str, Any]]:
    data = shopify_graphql(
        endpoint,
        token,
        """
        query OnlineSaleWindowProductById($id: ID!, $namespace: String!, $key: String!) {
          node(id: $id) {
            ... on Product {
              id
              title
              handle
              vendor
              onlineSaleUntil: metafield(namespace: $namespace, key: $key) {
                id
                namespace
                key
                type
                value
                updatedAt
              }
            }
          }
        }
        """,
        {"id": product_id, "namespace": namespace, "key": key},
    )
    node = data.get("node")
    return node if node and node.get("id") else None


def set_online_sale_until(endpoint: str, token: str, product_id: str, namespace: str, key: str, value: str) -> Dict[str, Any]:
    data = shopify_graphql(
        endpoint,
        token,
        """
        mutation SetOnlineSaleUntil($metafields: [MetafieldsSetInput!]!) {
          metafieldsSet(metafields: $metafields) {
            metafields {
              id
              namespace
              key
              type
              value
              updatedAt
            }
            userErrors {
              field
              message
              code
            }
          }
        }
        """,
        {
            "metafields": [
                {
                    "ownerId": product_id,
                    "namespace": namespace,
                    "key": key,
                    "type": DEFAULT_TYPE,
                    "value": value,
                }
            ]
        },
    )
    payload = data.get("metafieldsSet") or {}
    errors = payload.get("userErrors") or []
    if errors:
        details = "; ".join(
            f"{error.get('code', 'ERROR')} {'.'.join(error.get('field') or [])}: {error.get('message')}" for error in errors
        )
        raise RuntimeError(f"Shopify rejected metafieldsSet: {details}")
    metafields = payload.get("metafields") or []
    return metafields[0] if metafields else {}


def parse_until(value: str) -> datetime:
    cleaned = value.strip()
    if cleaned.endswith("Z"):
        cleaned = f"{cleaned[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError as exc:
        raise SystemExit("--until must be an ISO 8601 date/time, for example 2026-07-16T12:30:00+02:00.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_shopify_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%S")


def describe_active_window(value: Optional[str], now: datetime) -> str:
    if not value:
        return "(not set)"
    try:
        parsed = parse_until(value)
    except SystemExit:
        return f"{value} (invalid date_time)"
    state = "active" if parsed > now else "expired"
    return f"{format_shopify_datetime(parsed)} UTC ({state})"


def main() -> None:
    project_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description="Temporarily allow an in-store exclusive product to be sold online by setting custom.online_sale_until."
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--handle", help="Shopify product handle, usually the last part of the product URL.")
    target.add_argument("--product-id", help="Shopify product numeric ID or gid://shopify/Product/... value.")
    window = parser.add_mutually_exclusive_group()
    window.add_argument("--hours", type=float, help="Hours from now that online sale should stay active. Defaults to 24.")
    window.add_argument("--until", help="Exact ISO 8601 end time. Timezone is supported; no timezone means UTC.")
    window.add_argument("--end-now", action="store_true", help="End the online sale window immediately by writing an expired timestamp.")
    parser.add_argument("--apply", action="store_true", help="Write the metafield to Shopify. Dry-run is default.")
    parser.add_argument("--shop", help="Shopify store domain.")
    parser.add_argument("--token", help="Shopify Admin access token.")
    parser.add_argument("--api-key", help="Shopify app client ID/API key.")
    parser.add_argument("--api-secret", help="Shopify app client secret.")
    parser.add_argument("--prefer-client-credentials", action="store_true", help="Use client_credentials even when an admin token is set.")
    parser.add_argument("--api-version", default=DEFAULT_API_VERSION, help="Shopify Admin API version.")
    parser.add_argument("--namespace", default=DEFAULT_NAMESPACE, help="Metafield namespace. Default: custom.")
    parser.add_argument("--key", default=DEFAULT_KEY, help="Metafield key. Default: online_sale_until.")
    parser.add_argument("--env-file", help="Path to .env. Default: repository .env.")
    args = parser.parse_args()

    now = datetime.now(timezone.utc).replace(microsecond=0)
    if args.end_now:
        desired_until = now - timedelta(seconds=60)
        action = "End online sale window now"
    elif args.until:
        desired_until = parse_until(args.until)
        action = "Set online sale window"
    else:
        hours = 24 if args.hours is None else args.hours
        if hours <= 0:
            raise SystemExit("--hours must be greater than 0. Use --end-now to expire the override.")
        desired_until = now + timedelta(hours=hours)
        action = f"Set online sale window for {hours:g} hour(s)"

    load_dotenv(Path(args.env_file or project_root / ".env"))
    shop = normalize_shop(args.shop or os.environ.get("SHOPIFY_SHOP") or os.environ.get("SHOPIFY_STORE_DOMAIN") or os.environ.get("SHOPIFY_STORE"))
    token = args.token or os.environ.get("SHOPIFY_ADMIN_ACCESS_TOKEN") or os.environ.get("SHOPIFY_ACCESS_TOKEN")
    client_id = args.api_key or os.environ.get("SHOPIFY_API_KEY") or os.environ.get("SHOPIFY_CLIENT_ID")
    client_secret = args.api_secret or os.environ.get("SHOPIFY_API_SECRET") or os.environ.get("SHOPIFY_CLIENT_SECRET")
    api_version = args.api_version or os.environ.get("SHOPIFY_API_VERSION") or DEFAULT_API_VERSION
    if not shop or (not token and (not client_id or not client_secret)):
        raise SystemExit("Missing Shopify credentials. Set SHOPIFY_SHOP and either SHOPIFY_ADMIN_ACCESS_TOKEN or SHOPIFY_API_KEY/SHOPIFY_API_SECRET.")

    endpoint = f"https://{shop}/admin/api/{api_version}/graphql.json"
    if args.prefer_client_credentials or not token:
        print("Getting temporary Shopify Admin token with client_credentials.")
        token = get_client_credentials_token(shop, client_id, client_secret)

    product = (
        get_product_by_handle(endpoint, token, args.handle, args.namespace, args.key)
        if args.handle
        else get_product_by_id(endpoint, token, normalize_product_gid(args.product_id), args.namespace, args.key)
    )
    if not product:
        raise SystemExit("Product not found.")

    current = product.get("onlineSaleUntil") or {}
    desired_value = format_shopify_datetime(desired_until)
    print(f"{'Apply' if args.apply else 'Dry run'}: {action} on {shop}")
    print(f"Product: {product.get('title')} ({product.get('handle')})")
    print(f"Vendor: {product.get('vendor') or '(blank)'}")
    print(f"Current {args.namespace}.{args.key}: {describe_active_window(current.get('value'), now)}")
    print(f"Desired {args.namespace}.{args.key}: {describe_active_window(desired_value, now)}")

    if handleize(product.get("vendor") or "") != "a-kind-of-guise":
        print("Warning: the current theme only treats vendor 'A Kind of Guise' as in-store exclusive.")

    if not args.apply:
        print("\nDry run only. Add --apply to write this change.")
        return

    updated = set_online_sale_until(endpoint, token, product["id"], args.namespace, args.key, desired_value)
    print(f"\nUpdated {updated.get('namespace')}.{updated.get('key')} to {updated.get('value')} [{updated.get('type')}].")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nCancelled.")
