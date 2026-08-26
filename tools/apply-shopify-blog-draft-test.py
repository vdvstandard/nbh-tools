#!/usr/bin/env python3
"""Change the guarded blog handle and create one guarded draft test article."""

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from shopify_api import get_client_credentials_token, graphql_request


DEFAULT_API_VERSION = "2026-04"
DEFAULT_MANIFEST = (
    ".tmp/phase8-shopify-blog-migration-20260805/blog-migration-manifest.json"
)
DEFAULT_OUTPUT_DIR = ".tmp/phase8-shopify-blog-draft-test-20260805"


def parse_args():
    parser = argparse.ArgumentParser(
        description="Apply one explicitly guarded Shopify blog draft test."
    )
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--env-file")
    parser.add_argument("--api-version", default=DEFAULT_API_VERSION)
    parser.add_argument("--confirm-blog-id", required=True)
    parser.add_argument("--new-handle", required=True)
    parser.add_argument("--review-id", required=True, type=int)
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
        return token_data["access_token"], token_data.get("scope", "")
    stored_token = os.environ.get("SHOPIFY_ADMIN_ACCESS_TOKEN") or os.environ.get(
        "SHOPIFY_ACCESS_TOKEN"
    )
    if stored_token:
        return stored_token, os.environ.get("SHOPIFY_ADMIN_ACCESS_TOKEN_SCOPE", "")
    raise SystemExit("Missing Shopify Admin API credentials.")


def user_error_message(label, errors):
    return f"{label} failed: " + "; ".join(
        f"{'.'.join(error.get('field') or [])}: {error.get('message')}"
        for error in errors
    )


def main():
    args = parse_args()
    if not args.apply:
        raise SystemExit("Refusing to mutate Shopify without --apply.")
    if args.new_handle != "blogs":
        raise SystemExit("This guarded run only permits --new-handle blogs.")

    project_root = Path(__file__).resolve().parent.parent
    manifest_path = Path(args.manifest)
    output_dir = Path(args.output_dir)
    env_path = Path(args.env_file) if args.env_file else project_root / ".env"
    if not manifest_path.is_absolute():
        manifest_path = project_root / manifest_path
    if not output_dir.is_absolute():
        output_dir = project_root / output_dir
    if not manifest_path.exists():
        raise SystemExit(f"Migration manifest not found: {manifest_path}")
    load_dotenv(env_path)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    blog_plan = manifest.get("blogPlan") or {}
    current_blog = blog_plan.get("current") or {}
    expected_blog_id = current_blog.get("admin_graphql_api_id")
    if args.confirm_blog_id != expected_blog_id:
        raise SystemExit(
            f"Blog ID guard failed: expected {expected_blog_id}, got {args.confirm_blog_id}."
        )
    if (blog_plan.get("recommended") or {}).get("handle") != args.new_handle:
        raise SystemExit("Manifest target handle does not match the requested handle.")

    candidates = [
        item
        for item in manifest.get("articles", [])
        if item.get("reviewId") == args.review_id
    ]
    if len(candidates) != 1:
        raise SystemExit(f"Expected one manifest article for review ID {args.review_id}.")
    article_row = candidates[0]
    article_input = article_row.get("proposedDraftInput") or {}
    if article_input.get("isPublished") is not False:
        raise SystemExit("Article guard failed: test article is not explicitly draft-only.")
    if not article_row.get("inlineImageUrls") or "<img" not in article_input.get("body", ""):
        raise SystemExit("Article guard failed: selected article has no inline image.")
    if article_input.get("blogId") != expected_blog_id:
        raise SystemExit("Article blog ID does not match the confirmed blog ID.")

    shop = normalize_shop(
        os.environ.get("SHOPIFY_SHOP")
        or os.environ.get("SHOPIFY_STORE_DOMAIN")
        or os.environ.get("SHOPIFY_STORE")
    )
    token, scopes = acquire_token(shop)
    endpoint = f"https://{shop}/admin/api/{args.api_version}/graphql.json"
    preflight_query = """
      query BlogDraftGuard($blogId: ID!, $articleQuery: String!) {
        blog(id: $blogId) {
          id
          title
          handle
        }
        articles(first: 10, query: $articleQuery) {
          nodes {
            id
            title
            handle
            isPublished
          }
        }
      }
    """
    article_handle = article_input["handle"]
    before = graphql_request(
        endpoint,
        token,
        preflight_query,
        {
            "blogId": expected_blog_id,
            "articleQuery": f"handle:{article_handle}",
        },
        retries=1,
    )
    if not before.get("blog"):
        raise SystemExit("Confirmed Shopify blog no longer exists.")
    if (before.get("articles") or {}).get("nodes"):
        raise SystemExit(f"Article handle already exists: {article_handle}")

    mutations = []
    blog_result = before["blog"]
    if blog_result.get("handle") != args.new_handle:
        update_mutation = """
          mutation UpdateBlogHandle($id: ID!, $blog: BlogUpdateInput!) {
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
        update_data = graphql_request(
            endpoint,
            token,
            update_mutation,
            {
                "id": expected_blog_id,
                "blog": {
                    "handle": args.new_handle,
                    "redirectArticles": True,
                    "redirectNewHandle": True,
                },
            },
            retries=1,
        )["blogUpdate"]
        if update_data.get("userErrors"):
            raise RuntimeError(
                user_error_message("Blog handle update", update_data["userErrors"])
            )
        blog_result = update_data["blog"]
        mutations.append("blogUpdate")

    create_mutation = """
      mutation CreateDraftArticle($article: ArticleCreateInput!) {
        articleCreate(article: $article) {
          article {
            id
            title
            handle
            isPublished
            image {
              url
              altText
            }
            blog {
              id
              handle
            }
          }
          userErrors {
            field
            message
            code
          }
        }
      }
    """
    create_data = graphql_request(
        endpoint,
        token,
        create_mutation,
        {"article": article_input},
        retries=1,
    )["articleCreate"]
    if create_data.get("userErrors"):
        raise RuntimeError(
            user_error_message("Draft article creation", create_data["userErrors"])
        )
    created_article = create_data["article"]
    mutations.append("articleCreate")
    if created_article.get("isPublished") is not False:
        raise RuntimeError("Post-write guard failed: created article is published.")
    if (created_article.get("blog") or {}).get("handle") != args.new_handle:
        raise RuntimeError("Post-write guard failed: article is attached to the wrong blog.")

    report = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "mode": "explicit_shopify_blog_draft_test_apply",
        "shop": shop,
        "apiVersion": args.api_version,
        "scope": scopes,
        "shopifyWritesPerformed": True,
        "mutations": mutations,
        "blogBefore": before["blog"],
        "blogAfter": blog_result,
        "article": created_article,
        "sourceReviewId": args.review_id,
        "sourceUrl": article_row.get("sourceUrl"),
        "inlineImageUrls": article_row.get("inlineImageUrls"),
        "inlineImageCount": len(article_row.get("inlineImageUrls") or []),
        "articleWasPublished": created_article.get("isPublished"),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "draft-test-apply.json"
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "blogHandle": blog_result.get("handle"),
                "articleTitle": created_article.get("title"),
                "articleHandle": created_article.get("handle"),
                "isPublished": created_article.get("isPublished"),
                "featuredImage": bool(created_article.get("image")),
                "inlineImages": report["inlineImageCount"],
                "mutations": mutations,
            },
            indent=2,
        )
    )
    print(f"Report: {report_path}")


if __name__ == "__main__":
    main()
