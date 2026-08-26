#!/usr/bin/env python3
"""Apply reviewed Shopify product descriptions with conflict and rollback guards."""

import argparse
import hashlib
import html
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from shopify_api import get_client_credentials_token, graphql_request


DEFAULT_API_VERSION = "2026-04"
DEFAULT_PLAN = Path(".tmp/product-description-proposals-validated-20260811.json")
DEFAULT_OUTPUT_DIR = Path(".tmp/product-description-apply-20260811")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def meaningful_text(value: str) -> str:
    text = re.sub(r"(?is)<(?:script|style).*?</(?:script|style)>", " ", value or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


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


def acquire_token(shop: str) -> tuple[str, str]:
    client_id = os.environ.get("SHOPIFY_API_KEY") or os.environ.get(
        "SHOPIFY_CLIENT_ID"
    )
    client_secret = os.environ.get("SHOPIFY_API_SECRET") or os.environ.get(
        "SHOPIFY_CLIENT_SECRET"
    )
    if client_id and client_secret:
        token_data = get_client_credentials_token(shop, client_id, client_secret)
        return token_data["access_token"], token_data.get("scope", "")
    token = os.environ.get("SHOPIFY_ADMIN_ACCESS_TOKEN") or os.environ.get(
        "SHOPIFY_ACCESS_TOKEN"
    )
    if token:
        return token, os.environ.get("SHOPIFY_ADMIN_ACCESS_TOKEN_SCOPE", "")
    raise SystemExit("Missing Shopify Admin API credentials.")


def get_product(endpoint: str, token: str, handle: str) -> Dict[str, Any]:
    data = graphql_request(
        endpoint,
        token,
        """
        query ProductDescriptionGuard($handle: String!) {
          productByHandle(handle: $handle) {
            id
            title
            handle
            vendor
            status
            descriptionHtml
            updatedAt
          }
        }
        """,
        {"handle": handle},
        retries=2,
    )
    return data.get("productByHandle") or {}


def update_description(
    endpoint: str,
    token: str,
    product_id: str,
    description_html: str,
) -> Dict[str, Any]:
    data = graphql_request(
        endpoint,
        token,
        """
        mutation UpdateReviewedProductDescription($product: ProductUpdateInput!) {
          productUpdate(product: $product) {
            product {
              id
              title
              handle
              status
              descriptionHtml
              updatedAt
            }
            userErrors {
              field
              message
            }
          }
        }
        """,
        {"product": {"id": product_id, "descriptionHtml": description_html}},
        retries=2,
    )
    payload = data.get("productUpdate") or {}
    errors = payload.get("userErrors") or []
    if errors:
        detail = "; ".join(
            f"{'.'.join(str(part) for part in error.get('field') or [])}: "
            f"{error.get('message')}"
            for error in errors
        )
        raise RuntimeError(f"Shopify rejected productUpdate: {detail}")
    return payload.get("product") or {}


def current_matches_snapshot(current_html: str, proposal: Dict[str, Any]) -> bool:
    if sha256_text(current_html) == proposal["currentDescriptionSha256"]:
        return True
    return meaningful_text(current_html) == meaningful_text(
        proposal.get("currentDescriptionHtml") or ""
    )


def content_matches(current_html: str, desired_html: str) -> bool:
    return current_html == desired_html or meaningful_text(current_html) == meaningful_text(
        desired_html
    )


def build_actions(
    endpoint: str,
    token: str,
    proposals: List[Dict[str, Any]],
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    actions: List[Dict[str, Any]] = []
    conflicts: List[Dict[str, Any]] = []
    for proposal in proposals:
        handle = proposal["handle"]
        current = get_product(endpoint, token, handle)
        if not current:
            conflicts.append({"handle": handle, "reason": "product_missing"})
            continue
        desired_html = proposal["bodyHtml"]
        current_html = current.get("descriptionHtml") or ""
        if content_matches(current_html, desired_html):
            state = "already_applied"
        elif current_matches_snapshot(current_html, proposal):
            state = "ready"
        else:
            state = "conflict"
            conflicts.append(
                {
                    "handle": handle,
                    "reason": "description_changed_since_review",
                    "expectedText": meaningful_text(
                        proposal.get("currentDescriptionHtml") or ""
                    ),
                    "actualText": meaningful_text(current_html),
                }
            )
        actions.append(
            {
                "handle": handle,
                "title": current.get("title"),
                "productId": current.get("id"),
                "status": current.get("status"),
                "state": state,
                "before": {
                    "descriptionHtml": current_html,
                    "descriptionSha256": sha256_text(current_html),
                    "updatedAt": current.get("updatedAt"),
                },
                "desired": {
                    "descriptionHtml": desired_html,
                    "descriptionSha256": sha256_text(desired_html),
                },
            }
        )
    return actions, conflicts


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Apply reviewed Shopify product descriptions. Dry-run is default."
    )
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--api-version", default=DEFAULT_API_VERSION)
    parser.add_argument("--confirm-shop", required=True)
    parser.add_argument("--confirm-count", required=True, type=int)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent.parent
    plan_path = args.plan if args.plan.is_absolute() else project_root / args.plan
    output_dir = (
        args.output_dir if args.output_dir.is_absolute() else project_root / args.output_dir
    )
    env_path = args.env_file or project_root / ".env"
    if not plan_path.exists():
        raise SystemExit(f"Validated proposal plan not found: {plan_path}")

    load_dotenv(env_path)
    shop = normalize_shop(
        os.environ.get("SHOPIFY_SHOP")
        or os.environ.get("SHOPIFY_STORE_DOMAIN")
        or os.environ.get("SHOPIFY_STORE")
    )
    confirmed_shop = normalize_shop(args.confirm_shop)
    if shop != confirmed_shop:
        raise SystemExit(f"Shop guard failed: environment has {shop}, confirmation has {confirmed_shop}.")

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan.get("mode") != "validated_editorial_review_only":
        raise SystemExit("Plan guard failed: expected validated_editorial_review_only mode.")
    if plan.get("shopifyWritePerformed") is not False:
        raise SystemExit("Plan guard failed: source does not declare shopifyWritePerformed false.")
    proposals = plan.get("products") or []
    if len(proposals) != args.confirm_count:
        raise SystemExit(
            f"Count guard failed: plan has {len(proposals)}, confirmation has {args.confirm_count}."
        )

    token, scopes = acquire_token(shop)
    endpoint = f"https://{shop}/admin/api/{args.api_version}/graphql.json"
    actions, conflicts = build_actions(endpoint, token, proposals)
    preflight = {
        "schemaVersion": 1,
        "generatedAt": utc_now(),
        "mode": "apply" if args.apply else "dry_run",
        "shop": shop,
        "apiVersion": args.api_version,
        "plan": str(plan_path),
        "planSha256": sha256_text(plan_path.read_text(encoding="utf-8")),
        "tokenScopes": scopes,
        "summary": {
            "products": len(actions),
            "ready": sum(action["state"] == "ready" for action in actions),
            "alreadyApplied": sum(
                action["state"] == "already_applied" for action in actions
            ),
            "conflicts": len(conflicts),
        },
        "conflicts": conflicts,
        "actions": actions,
    }
    latest_preflight_path = output_dir / "preflight-latest.json"
    rollback_path = output_dir / "preflight-and-rollback.json"
    write_json(latest_preflight_path, preflight)
    if args.apply and not rollback_path.exists():
        write_json(rollback_path, preflight)
    print(json.dumps(preflight["summary"], indent=2))
    print(f"Latest preflight: {latest_preflight_path.resolve()}")
    if rollback_path.exists():
        print(f"Original rollback snapshot: {rollback_path.resolve()}")
    if conflicts:
        raise SystemExit("Refusing to write because preflight found conflicts.")
    if not args.apply:
        print("Dry run only. Add --apply after reviewing the preflight.")
        return

    results: List[Dict[str, Any]] = []
    for action in actions:
        if action["state"] == "already_applied":
            results.append({"handle": action["handle"], "state": "skipped_already_applied"})
            continue
        updated = update_description(
            endpoint,
            token,
            action["productId"],
            action["desired"]["descriptionHtml"],
        )
        results.append(
            {
                "handle": action["handle"],
                "state": "updated",
                "updatedAt": updated.get("updatedAt"),
            }
        )

    verification_failures: List[Dict[str, Any]] = []
    shopify_normalized_html = 0
    for action in actions:
        current = get_product(endpoint, token, action["handle"])
        actual_html = current.get("descriptionHtml") or ""
        if actual_html != action["desired"]["descriptionHtml"]:
            shopify_normalized_html += 1
        if not content_matches(actual_html, action["desired"]["descriptionHtml"]):
            verification_failures.append(
                {
                    "handle": action["handle"],
                    "expectedSha256": action["desired"]["descriptionSha256"],
                    "actualSha256": sha256_text(actual_html),
                }
            )

    report = {
        "schemaVersion": 1,
        "generatedAt": utc_now(),
        "mode": "applied_and_verified",
        "shop": shop,
        "summary": {
            "products": len(actions),
            "updated": sum(result["state"] == "updated" for result in results),
            "alreadyApplied": sum(
                result["state"] == "skipped_already_applied" for result in results
            ),
            "verificationFailures": len(verification_failures),
            "shopifyNormalizedHtml": shopify_normalized_html,
        },
        "results": results,
        "verificationFailures": verification_failures,
    }
    write_json(output_dir / "apply-results.json", report)
    print(json.dumps(report["summary"], indent=2))
    print(f"Results: {(output_dir / 'apply-results.json').resolve()}")
    if verification_failures:
        raise SystemExit("Shopify writes completed, but read-back verification failed.")


if __name__ == "__main__":
    main()
