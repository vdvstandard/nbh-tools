#!/usr/bin/env python3
"""Write a generated description to one product and mark it as handled.

Sets descriptionHtml via productUpdate and, in the same --apply run, sets
the custom.ai_description_generated_at metafield so
find-new-lightspeed-draft-products.py never re-offers this product.

Dry run by default. Add --apply to write changes.
"""

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from shopify_api import get_client_credentials_token, graphql_request


DEFAULT_API_VERSION = "2026-04"
METAFIELD_NAMESPACE = "custom"
METAFIELD_KEY = "ai_description_generated_at"


def parse_env_value(value: str) -> str:
    value = value.strip()
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    return value


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$", line)
        if match and match.group(1) not in os.environ:
            os.environ[match.group(1)] = parse_env_value(match.group(2))


def normalize_shop(value: str) -> str:
    cleaned = (
        str(value or "")
        .strip()
        .replace("https://", "")
        .replace("http://", "")
        .split("/")[0]
    )
    return cleaned if "." in cleaned else f"{cleaned}.myshopify.com"


def acquire_token(shop: str) -> str:
    client_id = os.environ.get("SHOPIFY_API_KEY") or os.environ.get("SHOPIFY_CLIENT_ID")
    client_secret = os.environ.get("SHOPIFY_API_SECRET") or os.environ.get(
        "SHOPIFY_CLIENT_SECRET"
    )
    if client_id and client_secret:
        token_data = get_client_credentials_token(shop, client_id, client_secret)
        return token_data["access_token"]
    token = os.environ.get("SHOPIFY_ADMIN_ACCESS_TOKEN") or os.environ.get(
        "SHOPIFY_ACCESS_TOKEN"
    )
    if token:
        return token
    raise SystemExit("Missing Shopify Admin API credentials.")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Write a generated description to one product and mark it handled."
    )
    parser.add_argument("--shop")
    parser.add_argument("--api-version", default=DEFAULT_API_VERSION)
    parser.add_argument("--env-file")
    parser.add_argument("--product-id", required=True, help="gid://shopify/Product/...")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--description-html")
    group.add_argument("--description-file", type=Path)
    parser.add_argument("--apply", action="store_true", help="Write changes. Default is dry run.")
    return parser.parse_args()


def get_product(endpoint: str, token: str, product_id: str) -> Dict[str, Any]:
    data = graphql_request(
        endpoint,
        token,
        """
        query GetProduct($id: ID!) {
          product(id: $id) {
            id
            title
            status
            descriptionHtml
          }
        }
        """,
        {"id": product_id},
    )
    product = data.get("product")
    if not product:
        raise SystemExit(f"No product found for id {product_id}.")
    return product


def update_description(endpoint: str, token: str, product_id: str, description_html: str) -> None:
    data = graphql_request(
        endpoint,
        token,
        """
        mutation UpdateDescription($product: ProductUpdateInput!) {
          productUpdate(product: $product) {
            product { id }
            userErrors { field message }
          }
        }
        """,
        {"product": {"id": product_id, "descriptionHtml": description_html}},
    )
    errors = data["productUpdate"]["userErrors"]
    if errors:
        raise SystemExit(f"productUpdate errors: {errors}")


def set_generated_marker(endpoint: str, token: str, product_id: str) -> None:
    data = graphql_request(
        endpoint,
        token,
        """
        mutation SetMarker($metafields: [MetafieldsSetInput!]!) {
          metafieldsSet(metafields: $metafields) {
            metafields { id }
            userErrors { field message }
          }
        }
        """,
        {
            "metafields": [
                {
                    "ownerId": product_id,
                    "namespace": METAFIELD_NAMESPACE,
                    "key": METAFIELD_KEY,
                    "type": "date_time",
                    "value": datetime.now(timezone.utc).isoformat(),
                }
            ]
        },
    )
    errors = data["metafieldsSet"]["userErrors"]
    if errors:
        raise SystemExit(f"metafieldsSet errors: {errors}")


def main() -> None:
    args = parse_args()
    project_root = Path(__file__).resolve().parent.parent
    env_path = Path(args.env_file) if args.env_file else project_root / ".env"
    load_dotenv(env_path)

    shop = normalize_shop(
        args.shop
        or os.environ.get("SHOPIFY_SHOP")
        or os.environ.get("SHOPIFY_STORE_DOMAIN")
    )
    if not shop:
        raise SystemExit("Missing --shop or SHOPIFY_SHOP.")

    description_html = (
        args.description_html
        if args.description_html is not None
        else args.description_file.read_text(encoding="utf-8")
    )

    token = acquire_token(shop)
    endpoint = f"https://{shop}/admin/api/{args.api_version}/graphql.json"

    product = get_product(endpoint, token, args.product_id)
    print(f"Product: {product['title']} ({product['id']}, status={product['status']})")
    current = re.sub(r"<[^>]+>", " ", product.get("descriptionHtml") or "")
    current = re.sub(r"\s+", " ", current).strip()
    print(f"Current description length: {len(current)} characters")
    print(f"New description length: {len(re.sub('<[^>]+>', ' ', description_html))} characters")

    if not args.apply:
        print("Dry run only. Re-run with --apply to write the description and mark it handled.")
        return

    update_description(endpoint, token, args.product_id, description_html)
    set_generated_marker(endpoint, token, args.product_id)
    print("Description written and product marked as handled.")


if __name__ == "__main__":
    main()
