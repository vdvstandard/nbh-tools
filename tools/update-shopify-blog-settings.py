#!/usr/bin/env python3
"""Apply one explicitly guarded Shopify blog title and handle update."""

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from shopify_api import get_client_credentials_token, graphql_request


DEFAULT_API_VERSION = "2026-04"
DEFAULT_OUTPUT_DIR = ".tmp/phase8-shopify-blog-settings-20260805"


def parse_args():
    parser = argparse.ArgumentParser(description="Update guarded Shopify blog settings.")
    parser.add_argument("--confirm-blog-id", required=True)
    parser.add_argument("--expected-handle", required=True)
    parser.add_argument("--new-handle", required=True)
    parser.add_argument("--new-title", required=True)
    parser.add_argument("--api-version", default=DEFAULT_API_VERSION)
    parser.add_argument("--env-file")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--apply", action="store_true")
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


def acquire_token(shop):
    client_id = os.environ.get("SHOPIFY_API_KEY") or os.environ.get("SHOPIFY_CLIENT_ID")
    client_secret = os.environ.get("SHOPIFY_API_SECRET") or os.environ.get(
        "SHOPIFY_CLIENT_SECRET"
    )
    if client_id and client_secret:
        token_data = get_client_credentials_token(shop, client_id, client_secret)
        return token_data["access_token"]
    stored_token = os.environ.get("SHOPIFY_ADMIN_ACCESS_TOKEN") or os.environ.get(
        "SHOPIFY_ACCESS_TOKEN"
    )
    if stored_token:
        return stored_token
    raise SystemExit("Missing Shopify Admin API credentials.")


def main():
    args = parse_args()
    if not args.apply:
        raise SystemExit("Refusing to mutate Shopify without --apply.")
    if (args.expected_handle, args.new_handle, args.new_title) != (
        "blogs",
        "journal",
        "Journal",
    ):
        raise SystemExit("This guarded run only permits Blogs/blogs -> Journal/journal.")

    project_root = Path(__file__).resolve().parent.parent
    env_path = Path(args.env_file) if args.env_file else project_root / ".env"
    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = project_root / output_dir
    load_dotenv(env_path)
    shop = normalize_shop(
        os.environ.get("SHOPIFY_SHOP")
        or os.environ.get("SHOPIFY_STORE_DOMAIN")
        or os.environ.get("SHOPIFY_STORE")
    )
    token = acquire_token(shop)
    endpoint = f"https://{shop}/admin/api/{args.api_version}/graphql.json"

    query = """
      query ConfirmBlogSettings($id: ID!) {
        blog(id: $id) {
          id
          title
          handle
        }
      }
    """
    before = graphql_request(
        endpoint,
        token,
        query,
        {"id": args.confirm_blog_id},
        retries=1,
    ).get("blog")
    if not before:
        raise SystemExit("Confirmed Shopify blog no longer exists.")
    if before.get("handle") != args.expected_handle:
        raise SystemExit(
            f"Handle guard failed: expected {args.expected_handle}, got {before.get('handle')}."
        )

    mutation = """
      mutation UpdateBlogSettings($id: ID!, $blog: BlogUpdateInput!) {
        blogUpdate(id: $id, blog: $blog) {
          blog {
            id
            title
            handle
          }
          userErrors {
            field
            message
          }
        }
      }
    """
    result = graphql_request(
        endpoint,
        token,
        mutation,
        {
            "id": args.confirm_blog_id,
            "blog": {
                "title": args.new_title,
                "handle": args.new_handle,
                "redirectArticles": True,
                "redirectNewHandle": True,
            },
        },
        retries=1,
    )["blogUpdate"]
    if result.get("userErrors"):
        messages = "; ".join(
            error.get("message") or "Unknown error"
            for error in result["userErrors"]
        )
        raise RuntimeError(f"Blog settings update failed: {messages}")
    after = result["blog"]
    if after.get("handle") != args.new_handle or after.get("title") != args.new_title:
        raise RuntimeError("Post-write guard failed: Shopify returned unexpected settings.")

    report = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "mode": "explicit_shopify_blog_settings_apply",
        "shopifyWritesPerformed": True,
        "mutation": "blogUpdate",
        "redirectArticles": True,
        "redirectNewHandle": True,
        "before": before,
        "after": after,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "blog-settings-apply.json"
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps({"before": before, "after": after}, indent=2))
    print(f"Report: {report_path}")


if __name__ == "__main__":
    main()
