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
LIGHTSPEED_NAMESPACE = "custom"
LIGHTSPEED_SOURCE_KEY = "lightspeed_source"
LIGHTSPEED_SOURCE_VALUE = "c_series_csv"
LIGHTSPEED_INTERNAL_ID_KEY = "lightspeed_internal_id"
DEFAULT_AUDIT_FILE = Path(".tmp") / "phase2-catalog-sync-audit.json"
DEFAULT_PLAN_FILE = Path(".tmp") / "lightspeed-product-status-plan.json"
DEFAULT_RESULTS_FILE = Path(".tmp") / "lightspeed-product-status-results.json"
DEFAULT_MAX_AUDIT_AGE_HOURS = 2.0


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


def http_post_json(url: str, payload: Dict[str, Any], headers: Dict[str, str], timeout: int = 45) -> Tuple[str, int, Dict[str, str]]:
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8"), response.status, dict(response.getheaders())
    except urllib.error.HTTPError as exc:
        return exc.read().decode("utf-8", errors="replace"), exc.code, dict(exc.headers.items())


def http_put_json(url: str, payload: Dict[str, Any], headers: Dict[str, str], timeout: int = 45) -> Tuple[str, int, Dict[str, str]]:
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="PUT")
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


def get_products(endpoint: str, token: str, page_size: int) -> List[Dict[str, Any]]:
    products: List[Dict[str, Any]] = []
    after = None
    while True:
        data = shopify_graphql(
            endpoint,
            token,
            """
            query LightspeedStatusProducts($first: Int!, $after: String) {
              products(first: $first, after: $after, sortKey: ID) {
                nodes {
                  id
                  title
                  handle
                  status
                  publishedAt
                  lightspeedSource: metafield(namespace: "custom", key: "lightspeed_source") {
                    value
                  }
                  lightspeedInternalId: metafield(namespace: "custom", key: "lightspeed_internal_id") {
                    value
                  }
                  catalogStatusSync: metafield(namespace: "custom", key: "catalog_status_sync") {
                    value
                  }
                }
                pageInfo {
                  hasNextPage
                  endCursor
                }
              }
            }
            """,
            {"first": page_size, "after": after},
        )
        connection = data.get("products") or {}
        products.extend(connection.get("nodes") or [])
        page_info = connection.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        after = page_info.get("endCursor")
    return products


def update_product_status(endpoint: str, token: str, product_id: str, status: str) -> Dict[str, Any]:
    data = shopify_graphql(
        endpoint,
        token,
        """
        mutation SetProductStatus($product: ProductUpdateInput!) {
          productUpdate(product: $product) {
            product {
              id
              title
              handle
              status
            }
            userErrors {
              field
              message
            }
          }
        }
        """,
        {"product": {"id": product_id, "status": status}},
    )
    payload = data.get("productUpdate") or {}
    errors = payload.get("userErrors") or []
    if errors:
        details = "; ".join(f"{'.'.join(error.get('field') or [])}: {error.get('message')}" for error in errors)
        raise RuntimeError(f"Shopify rejected productUpdate: {details}")
    return payload.get("product") or {}


def publish_product_to_online_store(shop: str, api_version: str, token: str, product_id: str) -> Dict[str, Any]:
    numeric_id = product_id.rsplit("/", 1)[-1]
    url = f"https://{shop}/admin/api/{api_version}/products/{numeric_id}.json"
    published_at = datetime.now(timezone.utc).isoformat()

    for attempt in range(1, 5):
        response_text, status, headers = http_put_json(
            url,
            {"product": {"id": int(numeric_id), "published_at": published_at}},
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Shopify-Access-Token": token,
            },
        )
        payload = json.loads(response_text or "{}")
        if status == 429 or status >= 500:
            if attempt < 4:
                time.sleep(int(headers.get("Retry-After", "0") or "0") or attempt)
                continue
        if status < 200 or status >= 300:
            raise RuntimeError(f"Shopify product publish failed with HTTP {status}: {json.dumps(payload)}")
        product = payload.get("product") or {}
        if not product.get("published_at"):
            raise RuntimeError("Shopify accepted the product update but returned no published_at value.")
        return product

    raise RuntimeError("Shopify product publish failed after retries.")


def parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def build_status_plan(
    audit: Dict[str, Any],
    live_products: List[Dict[str, Any]],
    max_audit_age_hours: float,
    skip_online_store_publish: bool,
) -> Dict[str, Any]:
    audit_created_at = parse_datetime(audit["created_at"])
    audit_age_hours = max(
        (datetime.now(timezone.utc) - audit_created_at).total_seconds() / 3600,
        0,
    )
    live_by_handle = {
        product["handle"]: product
        for product in live_products
        if product.get("handle")
    }
    managed_records = [
        record
        for record in audit.get("records") or []
        if (record.get("shopify") or {}).get("managed_by_sync")
    ]
    updates = []
    unchanged = []
    publications = []
    conflicts = []

    for record in managed_records:
        handle = record.get("handle")
        desired_status = (record.get("decision") or {}).get("desired_status")
        expected = record.get("shopify") or {}
        live = live_by_handle.get(handle)
        if not live:
            conflicts.append({
                "handle": handle,
                "reason": "missing_live_product",
            })
            continue

        live_source = (live.get("lightspeedSource") or {}).get("value")
        live_internal_id = (live.get("lightspeedInternalId") or {}).get("value")
        live_status_lock = (live.get("catalogStatusSync") or {}).get("value")
        identity_errors = []
        if live_source != LIGHTSPEED_SOURCE_VALUE:
            identity_errors.append("lightspeed_source_mismatch")
        if live_internal_id != str(record.get("internal_id") or ""):
            identity_errors.append("lightspeed_internal_id_mismatch")
        if live_status_lock == "manual_review":
            identity_errors.append("catalog_status_sync_locked")
        if desired_status not in {"ACTIVE", "DRAFT"}:
            identity_errors.append("unsupported_desired_status")
        if identity_errors:
            conflicts.append({
                "handle": handle,
                "productId": live.get("id"),
                "reason": ",".join(identity_errors),
            })
            continue

        item = {
            "productId": live["id"],
            "handle": handle,
            "title": live.get("title"),
            "currentStatus": live.get("status"),
            "desiredStatus": desired_status,
            "reasons": (record.get("decision") or {}).get("reasons") or [],
        }
        expected_status = expected.get("status")
        if live.get("status") not in {expected_status, desired_status}:
            conflicts.append({
                **item,
                "expectedAuditStatus": expected_status,
                "reason": "live_status_changed_since_audit",
            })
            continue
        if live.get("status") == desired_status:
            unchanged.append(item)
        else:
            updates.append(item)
        if (
            desired_status == "ACTIVE"
            and not skip_online_store_publish
            and not live.get("publishedAt")
        ):
            publications.append(item)

    audit_summary = audit.get("summary") or {}
    sources_fresh = bool(audit_summary.get("sources_fresh"))
    catalog_safe = bool(audit_summary.get("safe_to_apply_statuses"))
    audit_fresh = audit_age_hours <= max_audit_age_hours
    return {
        "schemaVersion": 2,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "mode": "dry-run",
        "audit": {
            "createdAt": audit_created_at.isoformat(),
            "ageHours": round(audit_age_hours, 2),
            "maxAgeHours": max_audit_age_hours,
            "sourcesFresh": sources_fresh,
            "catalogSafe": catalog_safe,
            "auditFresh": audit_fresh,
        },
        "summary": {
            "liveProductsScanned": len(live_products),
            "syncManagedProducts": len(managed_records),
            "manualProductsExcluded": max(
                len(audit.get("records") or []) - len(managed_records),
                0,
            ),
            "statusUpdates": len(updates),
            "unchanged": len(unchanged),
            "onlineStorePublications": len(publications),
            "conflicts": len(conflicts),
            "safeToApply": catalog_safe and audit_fresh and not conflicts,
        },
        "updates": updates,
        "unchanged": unchanged,
        "publications": publications,
        "conflicts": conflicts,
    }


def verify_status_plan(
    live_products: List[Dict[str, Any]],
    plan: Dict[str, Any],
) -> List[Dict[str, Any]]:
    live_by_id = {
        product.get("id"): product
        for product in live_products
        if product.get("id")
    }
    expected_statuses = {
        item["productId"]: item
        for item in plan.get("updates") or []
    }
    expected_publications = {
        item["productId"]: item
        for item in plan.get("publications") or []
    }
    failures = []
    for product_id in set(expected_statuses) | set(expected_publications):
        live = live_by_id.get(product_id)
        reasons = []
        status_item = expected_statuses.get(product_id)
        publication_item = expected_publications.get(product_id)
        if not live:
            reasons.append("missing_live_product")
        else:
            if (
                status_item
                and live.get("status") != status_item["desiredStatus"]
            ):
                reasons.append("status_mismatch")
            if publication_item and not live.get("publishedAt"):
                reasons.append("online_store_not_published")
        if reasons:
            item = status_item or publication_item or {}
            failures.append({
                "productId": product_id,
                "handle": item.get("handle"),
                "expectedStatus": (
                    status_item.get("desiredStatus")
                    if status_item
                    else None
                ),
                "actualStatus": (live or {}).get("status"),
                "publishedAt": (live or {}).get("publishedAt"),
                "reasons": reasons,
            })
    return failures


def main() -> None:
    project_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description=(
            "Reconcile Shopify status for sync-managed Lightspeed products "
            "from a catalog audit."
        )
    )
    parser.add_argument(
        "--audit-report",
        default=str(project_root / DEFAULT_AUDIT_FILE),
        help="Catalog audit JSON generated by audit-catalog-sync.py.",
    )
    parser.add_argument("--apply", action="store_true", help="Write status changes to Shopify. Dry-run is default.")
    parser.add_argument("--shop", help="Shopify store domain.")
    parser.add_argument("--token", help="Shopify Admin access token.")
    parser.add_argument("--api-key", help="Shopify app client ID/API key.")
    parser.add_argument("--api-secret", help="Shopify app client secret.")
    parser.add_argument("--prefer-client-credentials", action="store_true", help="Use client_credentials even when an admin token is set.")
    parser.add_argument("--api-version", default=DEFAULT_API_VERSION, help="Shopify Admin API version.")
    parser.add_argument("--page-size", type=int, default=250, help="Shopify product page size.")
    parser.add_argument("--skip-online-store-publish", action="store_true", help="Do not publish products to the Online Store when setting ACTIVE.")
    parser.add_argument(
        "--max-audit-age-hours",
        type=float,
        default=DEFAULT_MAX_AUDIT_AGE_HOURS,
        help=f"Refuse --apply when the audit is older than this. Default: {DEFAULT_MAX_AUDIT_AGE_HOURS:g}.",
    )
    parser.add_argument(
        "--allow-stale-audit",
        action="store_true",
        help="Override source and audit freshness guards after explicit review.",
    )
    parser.add_argument("--continue-on-error", action="store_true", help="Continue applying remaining products after an error.")
    parser.add_argument("--env-file", help="Path to .env. Default: repository .env.")
    parser.add_argument("--plan-file", default=str(project_root / DEFAULT_PLAN_FILE), help="JSON plan output path.")
    parser.add_argument("--results-file", default=str(project_root / DEFAULT_RESULTS_FILE), help="JSON apply results output path.")
    args = parser.parse_args()

    if args.page_size < 1 or args.page_size > 250:
        raise SystemExit("--page-size must be between 1 and 250.")
    if args.max_audit_age_hours <= 0:
        raise SystemExit("--max-audit-age-hours must be positive.")

    audit_path = Path(args.audit_report)
    if not audit_path.exists():
        raise SystemExit(f"Audit report not found: {audit_path}")
    audit = json.loads(audit_path.read_text(encoding="utf-8"))

    load_dotenv(Path(args.env_file or project_root / ".env"))
    shop = normalize_shop(args.shop or os.environ.get("SHOPIFY_SHOP") or os.environ.get("SHOPIFY_STORE_DOMAIN") or os.environ.get("SHOPIFY_STORE"))
    token = args.token or os.environ.get("SHOPIFY_ADMIN_ACCESS_TOKEN") or os.environ.get("SHOPIFY_ACCESS_TOKEN")
    client_id = args.api_key or os.environ.get("SHOPIFY_API_KEY") or os.environ.get("SHOPIFY_CLIENT_ID")
    client_secret = args.api_secret or os.environ.get("SHOPIFY_API_SECRET") or os.environ.get("SHOPIFY_CLIENT_SECRET")
    api_version = args.api_version or os.environ.get("SHOPIFY_API_VERSION") or DEFAULT_API_VERSION
    if not shop or (not token and (not client_id or not client_secret)):
        raise SystemExit("Missing Shopify credentials.")
    endpoint = f"https://{shop}/admin/api/{api_version}/graphql.json"
    if args.prefer_client_credentials or not token:
        print("Getting temporary Shopify Admin token with client_credentials.")
        token = get_client_credentials_token(shop, client_id, client_secret)

    print(f"{'Apply' if args.apply else 'Dry run'}: reconciling Lightspeed product statuses on {shop}")
    products = get_products(endpoint, token, args.page_size)
    plan = build_status_plan(
        audit,
        products,
        args.max_audit_age_hours,
        args.skip_online_store_publish,
    )
    plan["mode"] = "apply" if args.apply else "dry-run"
    write_json(Path(args.plan_file), plan)
    summary = plan["summary"]
    print(f"Products scanned: {summary['liveProductsScanned']}")
    print(f"Sync-managed products: {summary['syncManagedProducts']}")
    print(f"Manual products excluded: {summary['manualProductsExcluded']}")
    print(f"Status updates needed: {summary['statusUpdates']}")
    print(f"Already correct: {summary['unchanged']}")
    print(f"Online Store publications needed: {summary['onlineStorePublications']}")
    print(f"Conflicts: {summary['conflicts']}")
    print(f"Audit fresh: {plan['audit']['auditFresh']}")
    print(f"Source files fresh: {plan['audit']['sourcesFresh']}")
    print(f"Catalog validations safe: {plan['audit']['catalogSafe']}")
    print(f"Safe to apply: {summary['safeToApply']}")
    for product in plan["updates"][:15]:
        print(
            f"- {product['title']} ({product['handle']}): "
            f"{product['currentStatus']} -> {product['desiredStatus']}"
        )
    if len(plan["updates"]) > 15:
        print(f"...and {len(plan['updates']) - 15} more status updates.")
    print(f"\nPlan written: {Path(args.plan_file)}")
    if not args.apply:
        print("No changes were written. Generate a fresh audit before applying.")
        return

    if plan["conflicts"]:
        raise SystemExit("Refusing --apply because live/audit identity conflicts exist.")
    if not summary["safeToApply"] and not args.allow_stale_audit:
        raise SystemExit(
            "Refusing --apply because the audit or its source files are stale. "
            "Generate a fresh audit or use --allow-stale-audit after explicit review."
        )

    results = {
        "schemaVersion": 2,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "updated": [],
        "published": [],
        "errors": [],
    }
    failed_ids = set()
    for index, product in enumerate(plan["updates"], start=1):
        try:
            updated = update_product_status(
                endpoint,
                token,
                product["productId"],
                product["desiredStatus"],
            )
            results["updated"].append(updated)
            print(
                f"Updated {index}/{len(plan['updates'])}: "
                f"{updated.get('title')} ({updated.get('handle')}) -> "
                f"{updated.get('status')}"
            )
        except Exception as exc:
            failed_ids.add(product["productId"])
            results["errors"].append({
                "operation": "status",
                "productId": product["productId"],
                "handle": product["handle"],
                "message": str(exc),
            })
            if not args.continue_on_error:
                write_json(Path(args.results_file), results)
                raise

    publications = [
        product
        for product in plan["publications"]
        if product["productId"] not in failed_ids
    ]
    for index, product in enumerate(publications, start=1):
        try:
            published = publish_product_to_online_store(
                shop,
                api_version,
                token,
                product["productId"],
            )
            results["published"].append({
                "productId": product["productId"],
                "handle": product["handle"],
                "publishedAt": published.get("published_at"),
            })
            print(
                f"Published {index}/{len(publications)}: "
                f"{published.get('title')} ({published.get('handle')})"
            )
        except Exception as exc:
            results["errors"].append({
                "operation": "publication",
                "productId": product["productId"],
                "handle": product["handle"],
                "message": str(exc),
            })
            if not args.continue_on_error:
                write_json(Path(args.results_file), results)
                raise

    live_after = get_products(endpoint, token, args.page_size)
    results["verificationFailures"] = verify_status_plan(live_after, plan)
    write_json(Path(args.results_file), results)
    print(f"\nResults written: {Path(args.results_file)}")
    print(
        f"Done. Status updates: {len(results['updated'])}. "
        f"Online Store publications: {len(results['published'])}. "
        f"Errors: {len(results['errors'])}. "
        f"Verification failures: {len(results['verificationFailures'])}."
    )
    if results["verificationFailures"]:
        raise SystemExit("Post-sync status verification failed.")


if __name__ == "__main__":
    main()
