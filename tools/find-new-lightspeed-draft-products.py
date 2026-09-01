#!/usr/bin/env python3
"""Find new draft products from Lightspeed that still need a written description.

A product qualifies when it is:
  - status: DRAFT
  - has a custom.lightspeed_internal_id metafield (came in via the Lightspeed sync)
  - does not yet have a custom.ai_description_generated_at metafield (not
    already handled by this tool)
  - has a blank or short descriptionHtml (below --short-threshold characters
    of meaningful text)

Read-only. Prints a JSON array of candidates to stdout (and optionally a file).
"""

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List

from shopify_api import graphql_request


DEFAULT_API_VERSION = "2026-04"
DEFAULT_SHORT_THRESHOLD = 80


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
    from shopify_api import get_client_credentials_token

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


def meaningful_text(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html or "")
    return re.sub(r"\s+", " ", text).strip()


def parse_args():
    parser = argparse.ArgumentParser(
        description="List new Lightspeed draft products that still need a written description."
    )
    parser.add_argument("--shop")
    parser.add_argument("--api-version", default=DEFAULT_API_VERSION)
    parser.add_argument("--short-threshold", type=int, default=DEFAULT_SHORT_THRESHOLD)
    parser.add_argument("--env-file")
    parser.add_argument("--output", help="Optional path to also write the JSON result to.")
    parser.add_argument(
        "--limit", type=int, default=0, help="Stop after this many candidates (0 = no limit)."
    )
    return parser.parse_args()


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

    token = acquire_token(shop)
    endpoint = f"https://{shop}/admin/api/{args.api_version}/graphql.json"

    query = """
    query Drafts($after: String) {
      products(first: 250, after: $after, query: "status:draft") {
        nodes {
          id
          title
          vendor
          handle
          productType
          tags
          descriptionHtml
          options { name values }
          images(first: 6) { nodes { url altText } }
          lightspeedId: metafield(namespace: "custom", key: "lightspeed_internal_id") { value }
          generatedMarker: metafield(namespace: "custom", key: "ai_description_generated_at") { value }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
    """

    candidates: List[Dict[str, Any]] = []
    after = None
    total_draft = 0
    while True:
        data = graphql_request(endpoint, token, query, {"after": after})
        page = data["products"]
        for node in page["nodes"]:
            total_draft += 1
            lightspeed_id = (node.get("lightspeedId") or {}).get("value")
            generated_marker = (node.get("generatedMarker") or {}).get("value")
            if not lightspeed_id or generated_marker:
                continue
            text = meaningful_text(node.get("descriptionHtml") or "")
            if len(text) >= args.short_threshold:
                continue
            candidates.append(
                {
                    "id": node["id"],
                    "title": node["title"],
                    "vendor": node["vendor"],
                    "handle": node["handle"],
                    "productType": node.get("productType") or "",
                    "tags": node.get("tags") or [],
                    "options": node.get("options") or [],
                    "images": [img["url"] for img in (node.get("images") or {}).get("nodes", [])],
                    "currentDescriptionHtml": node.get("descriptionHtml") or "",
                }
            )
            if args.limit and len(candidates) >= args.limit:
                break
        if args.limit and len(candidates) >= args.limit:
            break
        page_info = page["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        after = page_info["endCursor"]

    result = {
        "shop": shop,
        "totalDraftProductsScanned": total_draft,
        "candidateCount": len(candidates),
        "candidates": candidates,
    }
    output_text = json.dumps(result, indent=2, ensure_ascii=False)
    print(output_text)
    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output_text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
