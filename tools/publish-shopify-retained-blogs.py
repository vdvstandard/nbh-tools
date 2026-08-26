#!/usr/bin/env python3
"""Publish the retained Journal set and delete two explicitly confirmed tests."""

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from shopify_api import get_client_credentials_token, graphql_request


DEFAULT_API_VERSION = "2026-04"
DEFAULT_PREFLIGHT = Path(
    ".tmp/phase8-shopify-blog-migration-post-draft-upload-20260811/"
    "blog-migration-preflight.json"
)
DEFAULT_OUTPUT_DIR = Path(".tmp/phase8-shopify-blog-publication-20260811")
ALLOWED_TEST_HANDLES = {
    "dit-is-een-test",
    "inkoop-uitkoop-duurkoop",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def next_link(link_header: str) -> Optional[str]:
    for part in str(link_header or "").split(","):
        if 'rel="next"' not in part:
            continue
        match = re.search(r"<([^>]+)>", part)
        return match.group(1) if match else None
    return None


def request_json(
    url: str, token: str, attempts: int = 5
) -> tuple[Dict[str, Any], Dict[str, str]]:
    headers = {"Accept": "application/json", "X-Shopify-Access-Token": token}
    last_error: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = json.loads(response.read().decode("utf-8") or "{}")
                response_headers = {
                    key.lower(): value for key, value in response.getheaders()
                }
                return payload, response_headers
        except urllib.error.HTTPError as error:
            last_error = error
            if error.code != 429 and error.code < 500:
                message = error.read().decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"Shopify GET failed ({error.code}) for {url}: {message}"
                ) from error
            if attempt < attempts:
                time.sleep(float(error.headers.get("Retry-After") or attempt))
        except urllib.error.URLError as error:
            last_error = error
            if attempt < attempts:
                time.sleep(attempt)
    raise RuntimeError(f"Shopify GET failed after {attempts} attempts: {url}") from last_error


def list_articles(rest_base: str, token: str, blog_id: str) -> List[Dict[str, Any]]:
    url: Optional[str] = (
        f"{rest_base}/blogs/{blog_id}/articles.json?limit=250&published_status=any"
    )
    articles: List[Dict[str, Any]] = []
    while url:
        payload, headers = request_json(url, token)
        articles.extend(payload.get("articles") or [])
        url = next_link(headers.get("link", ""))
    return articles


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def user_error_message(label: str, errors: List[Dict[str, Any]]) -> str:
    return f"{label} failed: " + "; ".join(
        f"{'.'.join(str(part) for part in error.get('field') or [])}: "
        f"{error.get('message')}"
        for error in errors
    )


def publish_article(
    endpoint: str,
    token: str,
    article_id: str,
    publish_date: str,
) -> Dict[str, Any]:
    data = graphql_request(
        endpoint,
        token,
        """
        mutation PublishRetainedArticle($id: ID!, $article: ArticleUpdateInput!) {
          articleUpdate(id: $id, article: $article) {
            article {
              id
              title
              handle
              isPublished
              publishedAt
              image {
                url
              }
              blog {
                id
                handle
              }
            }
            userErrors {
              code
              field
              message
            }
          }
        }
        """,
        {
            "id": article_id,
            "article": {"isPublished": True, "publishDate": publish_date},
        },
        retries=1,
    )
    payload = data.get("articleUpdate") or {}
    if payload.get("userErrors"):
        raise RuntimeError(
            user_error_message("Article publication", payload["userErrors"])
        )
    article = payload.get("article") or {}
    if article.get("isPublished") is not True or not article.get("publishedAt"):
        raise RuntimeError("Post-write publication guard failed.")
    return article


def delete_article(endpoint: str, token: str, article_id: str) -> str:
    data = graphql_request(
        endpoint,
        token,
        """
        mutation DeleteConfirmedTestArticle($id: ID!) {
          articleDelete(id: $id) {
            deletedArticleId
            userErrors {
              code
              field
              message
            }
          }
        }
        """,
        {"id": article_id},
        retries=1,
    )
    payload = data.get("articleDelete") or {}
    if payload.get("userErrors"):
        raise RuntimeError(user_error_message("Test article deletion", payload["userErrors"]))
    deleted_id = payload.get("deletedArticleId")
    if deleted_id != article_id:
        raise RuntimeError("Post-write deletion guard failed: unexpected deleted ID.")
    return deleted_id


def source_date(row: Dict[str, Any]) -> str:
    publication_plan = row.get("publicationPlan") or {}
    value = str(publication_plan.get("sourceDate") or "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise SystemExit(f"Invalid source publication date for {row.get('sourcePath')}: {value}")
    return value


def publish_datetime(row: Dict[str, Any]) -> str:
    publication_plan = row.get("publicationPlan") or {}
    value = str(publication_plan.get("intendedPublishDate") or "")
    if not value:
        raise SystemExit(f"Missing intended publication date for {row.get('sourcePath')}.")
    datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Publish exactly 82 retained Journal articles and delete two confirmed tests."
    )
    parser.add_argument("--preflight", type=Path, default=DEFAULT_PREFLIGHT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--api-version", default=DEFAULT_API_VERSION)
    parser.add_argument("--confirm-shop", required=True)
    parser.add_argument("--confirm-blog-id", required=True)
    parser.add_argument("--confirm-blog-handle", required=True)
    parser.add_argument("--confirm-retained-count", required=True, type=int)
    parser.add_argument("--confirm-delete-handles", required=True)
    parser.add_argument("--delay", type=float, default=0.15)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent.parent
    preflight_path = (
        args.preflight if args.preflight.is_absolute() else project_root / args.preflight
    )
    output_dir = (
        args.output_dir if args.output_dir.is_absolute() else project_root / args.output_dir
    )
    env_path = args.env_file or project_root / ".env"
    if not preflight_path.exists():
        raise SystemExit(f"Blog preflight not found: {preflight_path}")
    if args.confirm_blog_handle != "journal":
        raise SystemExit("Blog handle guard failed: this run only permits journal.")
    confirmed_test_handles = {
        item.strip() for item in args.confirm_delete_handles.split(",") if item.strip()
    }
    if confirmed_test_handles != ALLOWED_TEST_HANDLES:
        raise SystemExit(
            "Test deletion guard failed: confirmation must contain exactly "
            + ", ".join(sorted(ALLOWED_TEST_HANDLES))
        )

    migration = json.loads(preflight_path.read_text(encoding="utf-8"))
    if migration.get("mode") != "no_write_shopify_blog_migration_preparation":
        raise SystemExit("Migration preflight has an unexpected mode.")
    if migration.get("shopifyWritesPerformed") is not False:
        raise SystemExit("Migration preflight does not declare zero Shopify writes.")
    retained_rows = migration.get("articles") or []
    if len(retained_rows) != args.confirm_retained_count:
        raise SystemExit(
            f"Retained count guard failed: found {len(retained_rows)}, "
            f"confirmed {args.confirm_retained_count}."
        )
    desired_by_handle = {
        row["proposedDraftInput"]["handle"]: row for row in retained_rows
    }
    if len(desired_by_handle) != len(retained_rows):
        raise SystemExit("Retained handle guard failed: duplicate handles.")
    for row in retained_rows:
        publish_datetime(row)
        source_date(row)

    load_dotenv(env_path)
    shop = normalize_shop(
        os.environ.get("SHOPIFY_SHOP")
        or os.environ.get("SHOPIFY_STORE_DOMAIN")
        or os.environ.get("SHOPIFY_STORE")
    )
    if shop != normalize_shop(args.confirm_shop):
        raise SystemExit("Shop guard failed.")
    token, scopes = acquire_token(shop)
    if "write_content" not in {part.strip() for part in scopes.split(",")}:
        raise SystemExit("Scope guard failed: token does not include write_content.")

    blog_numeric_id = args.confirm_blog_id.rsplit("/", 1)[-1]
    rest_base = f"https://{shop}/admin/api/{args.api_version}"
    endpoint = f"{rest_base}/graphql.json"
    blog_payload, _ = request_json(f"{rest_base}/blogs/{blog_numeric_id}.json", token)
    blog = blog_payload.get("blog") or {}
    if blog.get("admin_graphql_api_id") != args.confirm_blog_id:
        raise SystemExit("Live blog ID does not match the confirmation.")
    if blog.get("handle") != args.confirm_blog_handle:
        raise SystemExit("Live blog handle does not match journal.")

    current_articles = list_articles(rest_base, token, blog_numeric_id)
    current_by_handle = {article.get("handle"): article for article in current_articles}
    conflicts = []
    pending_publish = []
    already_published = []
    for handle, row in desired_by_handle.items():
        current = current_by_handle.get(handle)
        if not current:
            conflicts.append(f"{handle}: retained article missing")
            continue
        if current.get("title") != row["proposedDraftInput"].get("title"):
            conflicts.append(f"{handle}: title changed")
        if not current.get("image"):
            conflicts.append(f"{handle}: featured image missing")
        if current.get("published_at"):
            if str(current["published_at"])[:10] != source_date(row):
                conflicts.append(f"{handle}: published with unexpected date")
            else:
                already_published.append(handle)
        else:
            pending_publish.append(handle)

    test_articles = {}
    for handle in confirmed_test_handles:
        current = current_by_handle.get(handle)
        if not current:
            conflicts.append(f"{handle}: confirmed test article missing")
            continue
        if handle not in ALLOWED_TEST_HANDLES:
            conflicts.append(f"{handle}: test handle is not allowlisted")
        test_articles[handle] = current
    unexpected_handles = sorted(
        set(current_by_handle) - set(desired_by_handle) - confirmed_test_handles
    )
    if unexpected_handles:
        conflicts.append(
            "Unexpected Journal article handles: " + ", ".join(unexpected_handles)
        )

    snapshot = {
        "schemaVersion": 1,
        "generatedAt": utc_now(),
        "mode": "apply" if args.apply else "dry_run",
        "shop": shop,
        "blog": blog,
        "shopifyWritesPerformed": False,
        "redirectMutationsSupported": False,
        "summary": {
            "retainedArticles": len(retained_rows),
            "pendingPublication": len(pending_publish),
            "alreadyPublishedWithSourceDate": len(already_published),
            "confirmedTestArticles": len(test_articles),
            "conflicts": len(conflicts),
        },
        "pendingPublishHandles": pending_publish,
        "alreadyPublishedHandles": already_published,
        "confirmedTestHandles": sorted(test_articles),
        "conflicts": conflicts,
        "rollback": {
            "retainedBefore": [current_by_handle[handle] for handle in desired_by_handle],
            "testArticlesBeforeDeletion": [
                test_articles[handle] for handle in sorted(test_articles)
            ],
        },
    }
    snapshot_path = output_dir / "preflight-and-rollback.json"
    write_json(snapshot_path, snapshot)
    print(json.dumps(snapshot["summary"], indent=2))
    print(f"Preflight and rollback: {snapshot_path.resolve()}")
    if conflicts:
        raise SystemExit("Refusing to mutate Shopify because preflight found conflicts.")
    if not args.apply:
        print("Dry run only. Add --apply to publish retained articles and delete tests.")
        return

    progress = {
        "schemaVersion": 1,
        "startedAt": utc_now(),
        "mode": "retained_blog_publication_progress",
        "shopifyWritesPerformed": False,
        "redirectMutations": 0,
        "published": [],
        "deletedTests": [],
    }
    progress_path = output_dir / "mutation-register.json"
    write_json(progress_path, progress)

    ordered_pending = sorted(
        pending_publish,
        key=lambda handle: (handle != "instagram", source_date(desired_by_handle[handle]), handle),
    )
    for index, handle in enumerate(ordered_pending, start=1):
        row = desired_by_handle[handle]
        current = current_by_handle[handle]
        published = publish_article(
            endpoint,
            token,
            current["admin_graphql_api_id"],
            publish_datetime(row),
        )
        if (published.get("blog") or {}).get("id") != args.confirm_blog_id:
            raise RuntimeError(f"Post-write guard failed for {handle}: wrong blog.")
        if published.get("handle") != handle:
            raise RuntimeError(f"Post-write guard failed for {handle}: handle changed.")
        progress["shopifyWritesPerformed"] = True
        progress["published"].append(
            {
                "handle": handle,
                "sourceDate": source_date(row),
                "intendedPublishDate": publish_datetime(row),
                "article": published,
            }
        )
        progress["updatedAt"] = utc_now()
        write_json(progress_path, progress)
        if index % 10 == 0 or index == len(ordered_pending):
            print(f"Published retained articles: {index}/{len(ordered_pending)}")
        if args.delay:
            time.sleep(args.delay)

    after_publish = list_articles(rest_base, token, blog_numeric_id)
    after_publish_by_handle = {
        article.get("handle"): article for article in after_publish
    }
    publication_failures = []
    for handle, row in desired_by_handle.items():
        current = after_publish_by_handle.get(handle)
        if not current or not current.get("published_at"):
            publication_failures.append(f"{handle}: not published")
            continue
        if str(current["published_at"])[:10] != source_date(row):
            publication_failures.append(f"{handle}: source publication date not preserved")
        if not current.get("image"):
            publication_failures.append(f"{handle}: featured image missing after publication")
    if publication_failures:
        write_json(
            output_dir / "publication-verification-failures.json",
            {"generatedAt": utc_now(), "failures": publication_failures},
        )
        raise SystemExit(
            "Retained publication verification failed; test articles were not deleted."
        )

    for handle in sorted(confirmed_test_handles):
        current = after_publish_by_handle.get(handle)
        if not current:
            raise RuntimeError(f"Deletion guard failed: {handle} disappeared before delete.")
        deleted_id = delete_article(
            endpoint, token, current["admin_graphql_api_id"]
        )
        progress["deletedTests"].append(
            {"handle": handle, "deletedArticleId": deleted_id}
        )
        progress["updatedAt"] = utc_now()
        write_json(progress_path, progress)

    final_articles = list_articles(rest_base, token, blog_numeric_id)
    final_by_handle = {article.get("handle"): article for article in final_articles}
    final_failures = []
    for handle, row in desired_by_handle.items():
        current = final_by_handle.get(handle)
        if not current or not current.get("published_at"):
            final_failures.append(f"{handle}: missing or unpublished in final audit")
        elif str(current["published_at"])[:10] != source_date(row):
            final_failures.append(f"{handle}: final publication date mismatch")
        elif not current.get("image"):
            final_failures.append(f"{handle}: final featured image missing")
    for handle in confirmed_test_handles:
        if handle in final_by_handle:
            final_failures.append(f"{handle}: test article still exists")

    report = {
        "schemaVersion": 1,
        "generatedAt": utc_now(),
        "mode": "retained_blogs_published_tests_deleted",
        "shop": shop,
        "blog": {"id": args.confirm_blog_id, "handle": args.confirm_blog_handle},
        "shopifyWritesPerformed": True,
        "redirectMutations": 0,
        "summary": {
            "retainedArticles": len(retained_rows),
            "publishedThisRun": len(progress["published"]),
            "alreadyPublishedWithSourceDate": len(already_published),
            "publishedRetainedFinal": sum(
                bool(final_by_handle.get(handle, {}).get("published_at"))
                for handle in desired_by_handle
            ),
            "retainedWithFeaturedImageFinal": sum(
                bool(final_by_handle.get(handle, {}).get("image"))
                for handle in desired_by_handle
            ),
            "deletedTestArticles": len(progress["deletedTests"]),
            "journalArticlesFinal": len(final_articles),
            "finalFailures": len(final_failures),
        },
        "deletedTests": progress["deletedTests"],
        "finalFailures": final_failures,
    }
    results_path = output_dir / "apply-results.json"
    write_json(results_path, report)
    print(json.dumps(report["summary"], indent=2))
    print(f"Mutation register: {progress_path.resolve()}")
    print(f"Results: {results_path.resolve()}")
    if final_failures:
        raise SystemExit("Final Journal verification failed.")


if __name__ == "__main__":
    main()
