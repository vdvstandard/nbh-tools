#!/usr/bin/env python3

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from lightspeed_variant_sync import (
    DEFAULT_API_VERSION,
    get_client_credentials_token,
    load_dotenv,
    normalize_shop,
    shopify_graphql,
)


DEFAULT_SOURCE_PLAN = (
    Path(".tmp") / "phase3-brand-content-plan-20260725.json"
)
DEFAULT_RESTORE_PLAN = (
    Path(".tmp") / "phase3-brand-content-restore-plan-20260725.json"
)
DEFAULT_RESULTS_FILE = (
    Path(".tmp") / "phase3-brand-content-restore-apply-20260725.json"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_value(value: Any) -> str:
    return hashlib.sha256(
        canonical_json(value).encode("utf-8")
    ).hexdigest()


def load_digest_checked_plan(path: Path) -> Dict[str, Any]:
    plan = json.loads(path.read_text(encoding="utf-8"))
    supplied_digest = plan.pop("planDigest", "")
    actual_digest = sha256_value(plan)
    plan["planDigest"] = supplied_digest
    if not supplied_digest or supplied_digest != actual_digest:
        raise ValueError(f"Invalid plan digest in {path}.")
    return plan


def content_snapshot(item: Dict[str, Any]) -> Dict[str, Any]:
    seo = item.get("seo") or {}
    return {
        "id": item["id"],
        "handle": item["handle"],
        "title": item["title"],
        "descriptionHtml": item.get("descriptionHtml") or "",
        "seo": {
            "title": seo.get("title") or "",
            "description": seo.get("description") or "",
        },
        "updatedAt": item.get("updatedAt"),
    }


def desired_from_original(action: Dict[str, Any]) -> Dict[str, Any]:
    original = action.get("expected") or {}
    seo = original.get("seo") or {}
    return {
        "descriptionHtml": original.get("descriptionHtml") or "",
        "seo": {
            "title": seo.get("title") or "",
            "description": seo.get("description") or "",
        },
    }


def changed_fields(
    current: Dict[str, Any],
    desired: Dict[str, Any],
) -> List[str]:
    changes: List[str] = []
    if current["descriptionHtml"] != desired["descriptionHtml"]:
        changes.append("descriptionHtml")
    if current["seo"]["title"] != desired["seo"]["title"]:
        changes.append("seo.title")
    if current["seo"]["description"] != desired["seo"]["description"]:
        changes.append("seo.description")
    return changes


def fetch_collections(
    endpoint: str,
    token: str,
) -> Dict[str, Dict[str, Any]]:
    query = """
      query RestoreBrandContentCollections($first: Int!, $after: String) {
        collections(first: $first, after: $after, sortKey: TITLE) {
          nodes {
            id
            handle
            title
            descriptionHtml
            updatedAt
            seo {
              title
              description
            }
          }
          pageInfo {
            hasNextPage
            endCursor
          }
        }
      }
    """
    collections: Dict[str, Dict[str, Any]] = {}
    after: Optional[str] = None
    while True:
        data = shopify_graphql(
            endpoint,
            token,
            query,
            {"first": 100, "after": after},
        )
        connection = data["collections"]
        for node in connection.get("nodes") or []:
            collections[node["handle"]] = content_snapshot(node)
        page_info = connection["pageInfo"]
        if not page_info["hasNextPage"]:
            return collections
        after = page_info["endCursor"]


def validate_source_plan(
    source: Dict[str, Any],
    shop: str,
    api_version: str,
) -> None:
    if source.get("schemaVersion") != 1:
        raise ValueError("Unsupported source plan schemaVersion.")
    if source.get("mode") != "dry_run":
        raise ValueError("Source plan must be a dry-run plan.")
    if source.get("shop") != shop:
        raise ValueError("Source plan shop does not match.")
    if source.get("apiVersion") != api_version:
        raise ValueError("Source plan API version does not match.")
    actions = source.get("actions")
    if not isinstance(actions, list) or not actions:
        raise ValueError("Source plan has no collection actions.")
    handles = [action.get("handle") for action in actions]
    if any(not handle for handle in handles):
        raise ValueError("Source plan contains an action without a handle.")
    if len(handles) != len(set(handles)):
        raise ValueError("Source plan contains duplicate handles.")
    for action in actions:
        original = action.get("expected") or {}
        if original.get("handle") != action["handle"]:
            raise ValueError(
                f"Original snapshot mismatch for {action['handle']}."
            )
        if "descriptionHtml" not in original or "seo" not in original:
            raise ValueError(
                f"Original content snapshot missing for {action['handle']}."
            )


def build_restore_plan(
    source: Dict[str, Any],
    source_path: Path,
    current_by_handle: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    actions: List[Dict[str, Any]] = []
    source_handles = {action["handle"] for action in source["actions"]}
    missing = sorted(source_handles - set(current_by_handle))
    if missing:
        raise ValueError(
            "Shopify collections missing for restore: "
            + ", ".join(missing)
        )

    for source_action in sorted(
        source["actions"],
        key=lambda item: item["handle"],
    ):
        handle = source_action["handle"]
        current = current_by_handle[handle]
        desired = desired_from_original(source_action)
        actions.append(
            {
                "handle": handle,
                "expected": current,
                "desired": desired,
                "changes": changed_fields(current, desired),
            }
        )

    plan_core = {
        "schemaVersion": 1,
        "mode": "dry_run",
        "operation": "restore_brand_content",
        "generatedAt": utc_now(),
        "shop": source["shop"],
        "apiVersion": source["apiVersion"],
        "sourcePlan": str(source_path),
        "sourcePlanDigest": source["planDigest"],
        "summary": {
            "collections": len(actions),
            "collectionsWithChanges": sum(
                bool(action["changes"]) for action in actions
            ),
            "descriptionChanges": sum(
                "descriptionHtml" in action["changes"]
                for action in actions
            ),
            "seoTitleChanges": sum(
                "seo.title" in action["changes"]
                for action in actions
            ),
            "metaDescriptionChanges": sum(
                "seo.description" in action["changes"]
                for action in actions
            ),
            "visibilityChanges": 0,
        },
        "actions": actions,
    }
    plan_core["planDigest"] = sha256_value(plan_core)
    return plan_core


def compare_expected(
    actions: List[Dict[str, Any]],
    current_by_handle: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    conflicts: List[Dict[str, Any]] = []
    for action in actions:
        handle = action["handle"]
        actual = current_by_handle.get(handle)
        if actual is None:
            conflicts.append(
                {"handle": handle, "reason": "collection_missing"}
            )
        elif actual != action["expected"]:
            conflicts.append(
                {
                    "handle": handle,
                    "reason": "current_state_changed_since_plan",
                    "expected": action["expected"],
                    "actual": actual,
                }
            )
    return conflicts


def update_collection(
    endpoint: str,
    token: str,
    action: Dict[str, Any],
) -> Dict[str, Any]:
    changed = set(action["changes"])
    update: Dict[str, Any] = {"id": action["expected"]["id"]}
    if "descriptionHtml" in changed:
        update["descriptionHtml"] = action["desired"]["descriptionHtml"]
    if {"seo.title", "seo.description"} & changed:
        update["seo"] = action["desired"]["seo"]
    if len(update) == 1:
        return {"skipped": True}

    data = shopify_graphql(
        endpoint,
        token,
        """
        mutation RestoreBrandCollectionContent($input: CollectionInput!) {
          collectionUpdate(input: $input) {
            collection {
              id
              handle
              updatedAt
            }
            userErrors {
              field
              message
            }
          }
        }
        """,
        {"input": update},
    )
    payload = data.get("collectionUpdate") or {}
    errors = payload.get("userErrors") or []
    if errors:
        detail = "; ".join(
            f"{'.'.join(str(part) for part in error.get('field') or [])}: "
            f"{error.get('message')}"
            for error in errors
        )
        raise RuntimeError(
            f"Shopify rejected restore for {action['handle']}: {detail}"
        )
    return payload.get("collection") or {}


def verify_desired(
    actions: List[Dict[str, Any]],
    current_by_handle: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    failures: List[Dict[str, Any]] = []
    for action in actions:
        handle = action["handle"]
        current = current_by_handle.get(handle)
        if current is None:
            failures.append(
                {"handle": handle, "reason": "collection_missing"}
            )
            continue
        actual = {
            "descriptionHtml": current["descriptionHtml"],
            "seo": current["seo"],
        }
        if actual != action["desired"]:
            failures.append(
                {
                    "handle": handle,
                    "expected": action["desired"],
                    "actual": actual,
                }
            )
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Restore Shopify brand collection descriptions and SEO fields "
            "from the original snapshots in a conflict-checked dry-run plan."
        )
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--shop", default="")
    parser.add_argument("--api-version", default=DEFAULT_API_VERSION)
    parser.add_argument(
        "--source-plan",
        type=Path,
        default=DEFAULT_SOURCE_PLAN,
    )
    parser.add_argument(
        "--plan-file",
        type=Path,
        default=DEFAULT_RESTORE_PLAN,
    )
    parser.add_argument(
        "--results-file",
        type=Path,
        default=DEFAULT_RESULTS_FILE,
    )
    args = parser.parse_args()

    load_dotenv(Path(".env"))
    shop = normalize_shop(args.shop or os.environ.get("SHOPIFY_SHOP", ""))
    client_id = os.environ.get("SHOPIFY_CLIENT_ID") or os.environ.get(
        "SHOPIFY_API_KEY",
        "",
    )
    client_secret = os.environ.get("SHOPIFY_CLIENT_SECRET") or os.environ.get(
        "SHOPIFY_API_SECRET",
        "",
    )
    if not shop or not client_id or not client_secret:
        raise SystemExit(
            "SHOPIFY_SHOP and Shopify client credentials are required."
        )

    source = load_digest_checked_plan(args.source_plan)
    validate_source_plan(source, shop, args.api_version)
    token = get_client_credentials_token(shop, client_id, client_secret)
    endpoint = (
        f"https://{shop}/admin/api/{args.api_version}/graphql.json"
    )

    if not args.apply:
        current = fetch_collections(endpoint, token)
        plan = build_restore_plan(source, args.source_plan, current)
        args.plan_file.parent.mkdir(parents=True, exist_ok=True)
        args.plan_file.write_text(
            json.dumps(plan, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(plan["summary"], indent=2))
        print(f"Plan digest: {plan['planDigest']}")
        print(f"Plan: {args.plan_file}")
        print("Shopify writes: 0")
        return 0

    if not args.plan_file.exists():
        raise SystemExit(
            "Apply requires an existing restore dry-run plan."
        )
    plan = load_digest_checked_plan(args.plan_file)
    if plan.get("mode") != "dry_run":
        raise SystemExit("Apply requires a dry-run restore plan.")
    if plan.get("operation") != "restore_brand_content":
        raise SystemExit("Plan is not a brand content restore plan.")
    if plan.get("shop") != shop:
        raise SystemExit("Restore plan shop does not match.")
    if plan.get("apiVersion") != args.api_version:
        raise SystemExit("Restore plan API version does not match.")
    if plan.get("sourcePlanDigest") != source["planDigest"]:
        raise SystemExit("Original source plan changed after dry-run.")

    current = fetch_collections(endpoint, token)
    conflicts = compare_expected(plan["actions"], current)
    if conflicts:
        args.results_file.parent.mkdir(parents=True, exist_ok=True)
        args.results_file.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "mode": "restore_blocked",
                    "generatedAt": utc_now(),
                    "shop": shop,
                    "planDigest": plan["planDigest"],
                    "conflicts": conflicts,
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        raise SystemExit(
            f"Current Shopify state has {len(conflicts)} conflict(s); "
            "nothing was written."
        )

    results: Dict[str, Any] = {
        "schemaVersion": 1,
        "mode": "restore_apply",
        "startedAt": utc_now(),
        "shop": shop,
        "planDigest": plan["planDigest"],
        "collectionUpdates": [],
        "errors": [],
        "verificationFailures": [],
    }
    try:
        for action in plan["actions"]:
            if action["changes"]:
                updated = update_collection(endpoint, token, action)
                results["collectionUpdates"].append(
                    {
                        "handle": action["handle"],
                        "updatedAt": updated.get("updatedAt"),
                    }
                )

        current = fetch_collections(endpoint, token)
        results["verificationFailures"] = verify_desired(
            plan["actions"],
            current,
        )
        if results["verificationFailures"]:
            raise RuntimeError(
                f"{len(results['verificationFailures'])} post-restore "
                "verification failure(s)."
            )
        results["verified"] = True
    except Exception as exc:
        results["verified"] = False
        results["errors"].append(str(exc))
        raise
    finally:
        results["completedAt"] = utc_now()
        args.results_file.parent.mkdir(parents=True, exist_ok=True)
        args.results_file.write_text(
            json.dumps(results, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    print(
        json.dumps(
            {
                "collectionUpdates": len(results["collectionUpdates"]),
                "verificationFailures": len(
                    results["verificationFailures"]
                ),
                "verified": results["verified"],
                "visibilityChanges": 0,
            },
            indent=2,
        )
    )
    print(f"Results: {args.results_file}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        raise SystemExit(130)
