#!/usr/bin/env python3
"""Check published Shopify collections for at least one public product."""

import argparse
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import requests


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36 "
    "Neighbourhood-Phase9-Audit/1.0"
)
HTTP_STATE = threading.local()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Read-only public collection availability audit."
    )
    parser.add_argument(
        "--base-url", default="https://neighbourhood-arnhem.myshopify.com"
    )
    parser.add_argument(
        "--custom-collections",
        default=".tmp/phase9-shopify-baseline-20260811/custom_collections.json",
    )
    parser.add_argument(
        "--smart-collections",
        default=".tmp/phase9-shopify-baseline-20260811/smart_collections.json",
    )
    parser.add_argument(
        "--output", default=".tmp/phase9-public-collections-20260811.json"
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--delay", type=float, default=0.5)
    parser.add_argument("--retries", type=int, default=4)
    return parser.parse_args()


def load_collection_file(path, key):
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    values = data.get(key, data) if isinstance(data, dict) else data
    return [item for item in values if item.get("published_at")]


def session():
    value = getattr(HTTP_STATE, "session", None)
    if value is None:
        value = requests.Session()
        value.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Accept-Language": "en-GB,en;q=0.9",
            }
        )
        HTTP_STATE.session = value
    return value


def audit_collection(base_url, collection, collection_type, delay, retries):
    handle = collection["handle"]
    page_url = f"{base_url}/collections/{handle}"
    endpoint = f"{page_url}/products.json?limit=1"
    attempts = 0
    while True:
        attempts += 1
        if delay:
            time.sleep(delay)
        try:
            response = session().get(endpoint, timeout=30)
        except requests.RequestException as error:
            if attempts <= retries:
                time.sleep(min(2 ** (attempts - 1), 8))
                continue
            return {
                "handle": handle,
                "title": collection["title"],
                "type": collection_type,
                "pageUrl": page_url,
                "endpoint": endpoint,
                "status": None,
                "hasPublicProduct": None,
                "attempts": attempts,
                "error": str(error),
            }

        if response.status_code in {429, 502, 503, 504} and attempts <= retries:
            retry_after = response.headers.get("Retry-After")
            try:
                wait_seconds = float(retry_after)
            except (TypeError, ValueError):
                wait_seconds = min(2 ** (attempts - 1), 8)
            time.sleep(max(wait_seconds, delay))
            continue

        error = None
        products = []
        try:
            payload = response.json()
            products = payload.get("products", [])
        except (ValueError, AttributeError) as parse_error:
            error = str(parse_error)
        return {
            "handle": handle,
            "title": collection["title"],
            "type": collection_type,
            "pageUrl": page_url,
            "endpoint": endpoint,
            "status": response.status_code,
            "hasPublicProduct": bool(products) if error is None else None,
            "sampleProductHandle": products[0].get("handle") if products else None,
            "attempts": attempts,
            "error": error,
        }


def main():
    args = parse_args()
    if args.workers < 1 or args.workers > 8:
        raise SystemExit("--workers must be between 1 and 8")
    if args.retries < 0:
        raise SystemExit("--retries cannot be negative")

    base_url = args.base_url.rstrip("/")
    collections = [
        (item, "custom")
        for item in load_collection_file(
            args.custom_collections, "custom_collections"
        )
    ] + [
        (item, "smart")
        for item in load_collection_file(
            args.smart_collections, "smart_collections"
        )
    ]

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        results = list(
            executor.map(
                lambda value: audit_collection(
                    base_url,
                    value[0],
                    value[1],
                    args.delay,
                    args.retries,
                ),
                collections,
            )
        )

    results.sort(key=lambda item: (item["type"], item["handle"]))
    empty = [item for item in results if item["hasPublicProduct"] is False]
    errors = [
        item
        for item in results
        if item["error"] or item["status"] != 200
    ]
    report = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "mode": "read_only_public_collection_audit",
        "httpMethods": ["GET"],
        "writesPerformed": False,
        "baseUrl": base_url,
        "guards": {
            "workers": args.workers,
            "delaySecondsPerWorker": args.delay,
            "retries": args.retries,
        },
        "summary": {
            "publishedCollections": len(results),
            "withPublicProduct": len(
                [item for item in results if item["hasPublicProduct"] is True]
            ),
            "emptyPublicCollections": len(empty),
            "errors": len(errors),
        },
        "emptyPublicCollections": empty,
        "errors": errors,
        "collections": results,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report["summary"], indent=2))
    print("Writes performed: false")
    print(f"Report: {output.resolve()}")
    raise SystemExit(1 if errors or empty else 0)


if __name__ == "__main__":
    main()
