#!/usr/bin/env python3
"""Move newly arrived products to the top of the manual 'all' collection.

The 'all' collection keeps its automated rule (price > 0) for membership,
but its sort order is MANUAL so it can be drag-and-drop reordered in
Shopify admin. New rule-matched products join the collection on their
own, but Shopify does not place them at the top. This tool detects
products added since the last run (via a persisted watermark) and moves
just those to the top, newest first, leaving the rest of the manual
order (including anything dragged around by hand) untouched.

Dry run by default. Add --apply to write changes and advance the
watermark. The watermark file must be committed after a successful
--apply run (the GitHub Actions workflow does this automatically).
"""

import argparse
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from shopify_api import get_client_credentials_token, graphql_request


DEFAULT_API_VERSION = "2026-04"
DEFAULT_COLLECTION_HANDLE = "all"
DEFAULT_STATE_FILE = Path("tools/data/all-collection-sync-state.json")
JOB_POLL_INTERVAL_SECONDS = 2
JOB_POLL_TIMEOUT_SECONDS = 120


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
        description="Move products added since the last run to the top of the manual 'all' collection."
    )
    parser.add_argument("--shop")
    parser.add_argument("--handle", default=DEFAULT_COLLECTION_HANDLE)
    parser.add_argument("--api-version", default=DEFAULT_API_VERSION)
    parser.add_argument(
        "--apply", action="store_true", help="Write changes. Default is dry run."
    )
    parser.add_argument("--env-file")
    parser.add_argument("--state-file", default=str(DEFAULT_STATE_FILE))
    return parser.parse_args()


def get_collection(endpoint: str, token: str, handle: str) -> Dict[str, Any]:
    data = graphql_request(
        endpoint,
        token,
        """
        query GetCollection($handle: String!) {
          collectionByHandle(handle: $handle) {
            id
            title
            sortOrder
          }
        }
        """,
        {"handle": handle},
    )
    collection = data.get("collectionByHandle")
    if not collection:
        raise SystemExit(f"No collection found with handle '{handle}'.")
    return collection


def fetch_collection_products_in_order(
    endpoint: str, token: str, collection_id: str
) -> List[Dict[str, Any]]:
    products: List[Dict[str, Any]] = []
    after = None
    while True:
        data = graphql_request(
            endpoint,
            token,
            """
            query CollectionProducts($id: ID!, $after: String) {
              collection(id: $id) {
                products(first: 250, after: $after) {
                  nodes { id title handle createdAt status }
                  pageInfo { hasNextPage endCursor }
                }
              }
            }
            """,
            {"id": collection_id, "after": after},
        )
        page = data["collection"]["products"]
        products.extend(page["nodes"])
        page_info = page["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        after = page_info["endCursor"]
    return products


def wait_for_job(endpoint: str, token: str, job_id: str) -> None:
    deadline = time.monotonic() + JOB_POLL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        data = graphql_request(
            endpoint,
            token,
            """
            query CheckJob($id: ID!) {
              job(id: $id) { id done }
            }
            """,
            {"id": job_id},
        )
        job = data.get("job")
        if job and job.get("done"):
            return
        time.sleep(JOB_POLL_INTERVAL_SECONDS)
    raise SystemExit(f"Timed out waiting for job {job_id} to finish.")


def reorder_to_top(
    endpoint: str, token: str, collection_id: str, product_ids_newest_first: List[str]
) -> None:
    moves = [
        {"id": product_id, "newPosition": str(index)}
        for index, product_id in enumerate(product_ids_newest_first)
    ]
    data = graphql_request(
        endpoint,
        token,
        """
        mutation ReorderProducts($id: ID!, $moves: [MoveInput!]!) {
          collectionReorderProducts(id: $id, moves: $moves) {
            job { id }
            userErrors { field message }
          }
        }
        """,
        {"id": collection_id, "moves": moves},
    )
    result = data["collectionReorderProducts"]
    if result["userErrors"]:
        raise SystemExit(f"collectionReorderProducts errors: {result['userErrors']}")
    wait_for_job(endpoint, token, result["job"]["id"])


def load_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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

    collection = get_collection(endpoint, token, args.handle)
    if collection["sortOrder"] != "MANUAL":
        raise SystemExit(
            f"Collection '{args.handle}' has sortOrder={collection['sortOrder']!r}, "
            "expected MANUAL. Set the collection's sort order to Manual first."
        )

    state_path = Path(args.state_file)
    state = load_state(state_path)
    watermark = state.get("last_synced_created_at")

    products = fetch_collection_products_in_order(endpoint, token, collection["id"])
    active_products = [p for p in products if p["status"] == "ACTIVE"]

    print(f"Shop: {shop}")
    print(f"Collection: {collection['title']} ({args.handle}, {collection['id']})")
    print(f"Products currently in collection: {len(products)} ({len(active_products)} active)")
    print(f"Watermark (last synced createdAt): {watermark or '(none, first run)'}")

    if watermark is None:
        print("No prior state found. Recording current newest product as baseline; no reordering performed.")
        newest_created_at = max((p["createdAt"] for p in products), default=None)
        if args.apply:
            save_state(
                state_path,
                {
                    "last_synced_created_at": newest_created_at,
                    "last_run_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            print(f"Wrote baseline state to {state_path}.")
        else:
            print("Dry run only. Re-run with --apply to write the baseline state file.")
        return

    new_products = [p for p in active_products if p["createdAt"] > watermark]
    new_products.sort(key=lambda p: p["createdAt"], reverse=True)

    print(f"New products since watermark: {len(new_products)}")
    for product in new_products:
        print(f"  - {product['title']} ({product['handle']}, created {product['createdAt']})")

    if not new_products:
        print("Nothing to do.")
        return

    if not args.apply:
        print("Dry run only. Re-run with --apply to move these products to the top.")
        return

    product_ids = [p["id"] for p in new_products]
    reorder_to_top(endpoint, token, collection["id"], product_ids)

    newest_created_at = new_products[0]["createdAt"]
    save_state(
        state_path,
        {
            "last_synced_created_at": newest_created_at,
            "last_run_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    print(
        f"Moved {len(product_ids)} product(s) to the top and advanced the watermark to {newest_created_at}."
    )


if __name__ == "__main__":
    main()
