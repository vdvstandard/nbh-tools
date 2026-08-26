#!/usr/bin/env python3
"""Unpublish the fixed allowlist of empty legacy category collections.

These 19 custom collections belong to the retired category taxonomy the
store no longer uses (the storefront now organizes by brand). They are
published and empty, so they are reachable/indexed with zero products. This
tool only ever touches this exact allowlist: it re-verifies each collection
is still empty on the live storefront immediately before writing, and refuses
to touch a collection outside the allowlist or one that has gained a public
product since the last audit.
"""

import argparse
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from lightspeed_variant_sync import (
    DEFAULT_API_VERSION,
    get_client_credentials_token,
    load_dotenv,
    normalize_shop,
)

ALLOWED_HANDLES = [
    "boots",
    "denim",
    "frontpage",
    "hoodies",
    "in-store-only",
    "jackets",
    "loafers",
    "outerwear",
    "overshirts",
    "pants",
    "polos",
    "sandals",
    "scarves",
    "shirts",
    "shoes",
    "shorts",
    "sweats",
    "t-shirts",
    "vests",
]

DEFAULT_RESULTS_FILE = Path(".tmp") / "unpublish-empty-category-collections.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def request_json(
    url: str,
    token: str,
    method: str = "GET",
    payload: Optional[Dict[str, Any]] = None,
    timeout: int = 60,
) -> Tuple[Dict[str, Any], Dict[str, str]]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": token,
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return (
                json.loads(response.read().decode("utf-8") or "{}"),
                dict(response.headers.items()),
            )
    except urllib.error.HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Shopify request failed ({exc.code}) for {url}: {response_body}"
        ) from exc


def next_link(link_header: str) -> Optional[str]:
    for part in link_header.split(","):
        if 'rel="next"' in part:
            return part.split(";")[0].strip()[1:-1]
    return None


def fetch_custom_collections(shop: str, token: str, api_version: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    url: Optional[str] = (
        f"https://{shop}/admin/api/{api_version}/custom_collections.json?limit=250"
    )
    while url:
        payload, headers = request_json(url, token)
        items.extend(payload.get("custom_collections") or [])
        url = next_link(headers.get("Link", ""))
    return items


def fetch_public_product_count(shop: str, handle: str) -> int:
    url = f"https://{shop}/collections/{handle}/products.json?limit=1"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8") or "{}")
    return len(payload.get("products") or [])


def unpublish_custom_collection(
    shop: str, token: str, api_version: str, collection_id: int, handle: str
) -> Dict[str, Any]:
    url = f"https://{shop}/admin/api/{api_version}/custom_collections/{collection_id}.json"
    payload, _ = request_json(
        url,
        token,
        method="PUT",
        payload={"custom_collection": {"id": collection_id, "published": False}},
    )
    updated = payload.get("custom_collection") or {}
    if updated.get("published_at") is not None:
        raise RuntimeError(f"Collection {handle} remained published.")
    return updated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--shop", default="")
    parser.add_argument("--api-version", default=DEFAULT_API_VERSION)
    parser.add_argument("--results-file", type=Path, default=DEFAULT_RESULTS_FILE)
    args = parser.parse_args()

    load_dotenv(Path(".env"))
    shop = normalize_shop(args.shop or os.environ.get("SHOPIFY_SHOP", ""))
    client_id = os.environ.get("SHOPIFY_CLIENT_ID") or os.environ.get("SHOPIFY_API_KEY", "")
    client_secret = os.environ.get("SHOPIFY_CLIENT_SECRET") or os.environ.get(
        "SHOPIFY_API_SECRET", ""
    )
    if not shop or not client_id or not client_secret:
        raise SystemExit("SHOPIFY_SHOP and Shopify client credentials are required.")

    token = get_client_credentials_token(shop, client_id, client_secret)
    collections = fetch_custom_collections(shop, token, args.api_version)
    by_handle = {c["handle"]: c for c in collections}

    missing = [h for h in ALLOWED_HANDLES if h not in by_handle]
    if missing:
        raise SystemExit(f"Allowlisted handles not found in Shopify: {missing}")

    rows: List[Dict[str, Any]] = []
    for handle in ALLOWED_HANDLES:
        collection = by_handle[handle]
        already_unpublished = collection.get("published_at") is None
        public_product_count = (
            0 if already_unpublished else fetch_public_product_count(shop, handle)
        )
        rows.append(
            {
                "handle": handle,
                "id": collection["id"],
                "title": collection.get("title"),
                "publishedAt": collection.get("published_at"),
                "alreadyUnpublished": already_unpublished,
                "livePublicProductCount": public_product_count,
                "safeToUnpublish": already_unpublished or public_product_count == 0,
            }
        )

    unsafe = [r for r in rows if not r["safeToUnpublish"]]

    result: Dict[str, Any] = {
        "schemaVersion": 1,
        "generatedAt": utc_now(),
        "shop": shop,
        "mode": "apply" if args.apply else "dry_run",
        "writesPerformed": False,
        "allowlistSize": len(ALLOWED_HANDLES),
        "rows": rows,
        "unsafeHandles": [r["handle"] for r in unsafe],
    }

    if unsafe:
        result["error"] = (
            "Refusing to apply: at least one allowlisted collection now has a "
            "live public product. Re-review before retrying."
        )
        args.results_file.parent.mkdir(parents=True, exist_ok=True)
        args.results_file.write_text(
            json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(json.dumps({"unsafeHandles": result["unsafeHandles"]}, indent=2))
        return 1

    if args.apply:
        applied: List[Dict[str, Any]] = []
        for row in rows:
            if row["alreadyUnpublished"]:
                applied.append({"handle": row["handle"], "skipped": "already_unpublished"})
                continue
            updated = unpublish_custom_collection(
                shop, token, args.api_version, row["id"], row["handle"]
            )
            applied.append(
                {
                    "handle": row["handle"],
                    "id": updated.get("id"),
                    "publishedAt": updated.get("published_at"),
                }
            )
        result["writesPerformed"] = True
        result["applied"] = applied

    args.results_file.parent.mkdir(parents=True, exist_ok=True)
    args.results_file.write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "mode": result["mode"],
                "writesPerformed": result["writesPerformed"],
                "collections": len(rows),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
