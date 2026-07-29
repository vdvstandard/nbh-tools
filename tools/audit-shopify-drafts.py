#!/usr/bin/env python3

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from shopify_api import get_client_credentials_token, graphql_request


DEFAULT_API_VERSION = "2026-04"


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
        (value or "")
        .strip()
        .replace("https://", "")
        .replace("http://", "")
        .split("/")[0]
    )
    return cleaned if "." in cleaned else f"{cleaned}.myshopify.com"


def acquire_token(shop):
    client_id = os.environ.get("SHOPIFY_API_KEY") or os.environ.get(
        "SHOPIFY_CLIENT_ID"
    )
    client_secret = os.environ.get("SHOPIFY_API_SECRET") or os.environ.get(
        "SHOPIFY_CLIENT_SECRET"
    )
    if client_id and client_secret:
        return get_client_credentials_token(shop, client_id, client_secret)[
            "access_token"
        ]

    token = os.environ.get("SHOPIFY_ADMIN_ACCESS_TOKEN") or os.environ.get(
        "SHOPIFY_ACCESS_TOKEN"
    )
    if token:
        return token
    raise SystemExit("Missing Shopify credentials in .env.")


def parse_timestamp(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def classify_product(product):
    created_at = parse_timestamp(product.get("createdAt"))
    status_events = [
        event
        for event in product.get("events", {}).get("nodes", [])
        if event.get("action") == "status_changed"
    ]
    active_to_draft = [
        event
        for event in status_events
        if "active to draft" in (event.get("message") or "").lower()
    ]
    immediate_events = []
    for event in active_to_draft:
        event_at = parse_timestamp(event.get("createdAt"))
        if created_at and event_at:
            delay_seconds = (event_at - created_at).total_seconds()
            if 0 <= delay_seconds <= 60:
                immediate_events.append((event, delay_seconds))

    if immediate_events:
        event, delay_seconds = min(immediate_events, key=lambda item: item[1])
        reason = "created_active_then_changed_to_draft_within_60_seconds"
    elif active_to_draft:
        event = sorted(
            active_to_draft,
            key=lambda item: item.get("createdAt") or "",
            reverse=True,
        )[0]
        delay_seconds = None
        reason = "changed_from_active_to_draft_later"
    elif status_events:
        event = sorted(
            status_events,
            key=lambda item: item.get("createdAt") or "",
            reverse=True,
        )[0]
        delay_seconds = None
        reason = "other_status_change_history"
    else:
        event = None
        delay_seconds = None
        reason = "no_status_change_event_found"

    return {
        "classification": reason,
        "status_change_delay_seconds": delay_seconds,
        "status_event": event,
    }


def fetch_drafts(graphql_url, token):
    query = """
      query DraftProductAudit($first: Int!, $after: String) {
        products(
          first: $first
          after: $after
          query: "status:draft"
          sortKey: ID
        ) {
          nodes {
            id
            legacyResourceId
            title
            handle
            vendor
            status
            createdAt
            updatedAt
            publishedAt
            productType
            totalInventory
            media(first: 1) {
              nodes {
                id
              }
            }
            brand: metafield(namespace: "custom", key: "brand") {
              value
            }
            source: metafield(namespace: "custom", key: "source") {
              value
            }
            events(first: 20, reverse: true) {
              nodes {
                __typename
                id
                action
                createdAt
                message
                appTitle
                attributeToApp
                attributeToUser
              }
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
    products = []
    while True:
        data = graphql_request(
            graphql_url,
            token,
            query,
            {"first": 100, "after": after},
        )
        connection = data["products"]
        products.extend(connection.get("nodes", []))
        page_info = connection.get("pageInfo", {})
        if not page_info.get("hasNextPage"):
            return products
        after = page_info.get("endCursor")
        time.sleep(0.55)


def main():
    parser = argparse.ArgumentParser(
        description="Audit why current Shopify draft products became drafts."
    )
    parser.add_argument("--shop", help="Shopify store domain.")
    parser.add_argument("--api-version", default=DEFAULT_API_VERSION)
    parser.add_argument("--env-file")
    parser.add_argument("--output")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    load_dotenv(Path(args.env_file or root / ".env"))
    shop = normalize_shop(
        args.shop
        or os.environ.get("SHOPIFY_SHOP")
        or os.environ.get("SHOPIFY_STORE_DOMAIN")
        or os.environ.get("SHOPIFY_STORE")
    )
    if not shop or shop == ".myshopify.com":
        raise SystemExit("Missing SHOPIFY_SHOP.")

    token = acquire_token(shop)
    graphql_url = (
        f"https://{shop}/admin/api/{args.api_version}/graphql.json"
    )
    products = fetch_drafts(graphql_url, token)
    records = []
    for product in products:
        record = dict(product)
        record.update(classify_product(product))
        records.append(record)

    classifications = Counter(
        record["classification"] for record in records
    )
    actors = Counter()
    apps = Counter()
    for record in records:
        event = record.get("status_event") or {}
        message = event.get("message") or ""
        actor = message.split(" changed product status", 1)[0].strip()
        if actor:
            actors[actor] += 1
        if event.get("appTitle"):
            apps[event["appTitle"]] += 1

    report = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "shop": shop,
        "api_version": args.api_version,
        "summary": {
            "draft_products": len(records),
            "classifications": dict(sorted(classifications.items())),
            "status_change_actors": dict(sorted(actors.items())),
            "status_change_apps": dict(sorted(apps.items())),
        },
        "products": records,
    }
    output = Path(
        args.output
        or root / ".tmp" / f"shopify-draft-audit-{datetime.now():%Y%m%d}.json"
    ).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report["summary"], indent=2, ensure_ascii=False))
    print(f"Draft audit written to {output}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
