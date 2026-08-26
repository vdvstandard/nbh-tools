#!/usr/bin/env python3
"""Audit Shopify article translations for legacy internal links."""

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from shopify_api import get_client_credentials_token, graphql_request


DEFAULT_API_VERSION = "2026-04"
DEFAULT_OUTPUT_DIR = Path(".tmp/phase8-shopify-blog-translation-audit-20260811")
OLD_LINK_PATTERN = re.compile(
    r"https?://(?:www\.)?nbharnhem\.com(?:/[^\"'<>\s]*)?",
    re.I,
)


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


def fetch_articles(
    endpoint: str, token: str, locale: str
) -> List[Dict[str, Any]]:
    query = """
    query JournalArticleTranslations($first: Int!, $after: String, $locale: String!) {
      articles(first: $first, after: $after, query: "blog_title:Journal") {
        nodes {
          id
          handle
          title
          body
          isPublished
          publishedAt
          blog {
            id
            handle
          }
          translations(locale: $locale) {
            key
            locale
            value
            outdated
            updatedAt
            market {
              id
              name
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
    articles = []
    after = None
    while True:
        data = graphql_request(
            endpoint,
            token,
            query,
            {"first": 100, "after": after, "locale": locale},
        )
        connection = data.get("articles") or {}
        articles.extend(connection.get("nodes") or [])
        page_info = connection.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            return articles
        after = page_info.get("endCursor")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read Shopify article translations without mutations."
    )
    parser.add_argument("--shop")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--api-version", default=DEFAULT_API_VERSION)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--locales",
        default="en,de,fr,es",
        help="Comma-separated published storefront locales.",
    )
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent.parent
    env_path = args.env_file or project_root / ".env"
    output_dir = (
        args.output_dir if args.output_dir.is_absolute() else project_root / args.output_dir
    )
    load_dotenv(env_path)
    shop = normalize_shop(
        args.shop
        or os.environ.get("SHOPIFY_SHOP")
        or os.environ.get("SHOPIFY_STORE_DOMAIN")
        or os.environ.get("SHOPIFY_STORE")
    )
    token, scopes = acquire_token(shop)
    endpoint = f"https://{shop}/admin/api/{args.api_version}/graphql.json"
    locale_codes = [
        value.strip() for value in args.locales.split(",") if value.strip()
    ]
    if not locale_codes:
        raise SystemExit("At least one storefront locale is required.")
    articles_by_handle: Dict[str, Dict[str, Any]] = {}
    for locale in locale_codes:
        for article in fetch_articles(endpoint, token, locale):
            current = articles_by_handle.setdefault(
                article["handle"],
                {**article, "translations": []},
            )
            current["translations"].extend(article.get("translations") or [])
    articles = list(articles_by_handle.values())

    translation_rows = []
    old_link_rows = []
    for article in articles:
        for translation in article.get("translations") or []:
            value = translation.get("value") or ""
            links = OLD_LINK_PATTERN.findall(value)
            row = {
                "articleId": article["id"],
                "handle": article["handle"],
                "title": article["title"],
                **translation,
                "oldLinkOccurrences": len(links),
                "oldLinks": links,
            }
            translation_rows.append(row)
            if links:
                old_link_rows.append(row)

    summary = {
        "articles": len(articles),
        "publishedArticles": sum(item.get("isPublished") is True for item in articles),
        "translations": len(translation_rows),
        "articlesWithTranslations": sum(
            bool(item.get("translations")) for item in articles
        ),
        "translationsWithOldLinks": len(old_link_rows),
        "oldLinkOccurrences": sum(
            item["oldLinkOccurrences"] for item in old_link_rows
        ),
    }
    report = {
        "schemaVersion": 1,
        "generatedAt": utc_now(),
        "mode": "read_only_shopify_blog_translation_audit",
        "shop": shop,
        "shopifyWritesPerformed": False,
        "availableScopes": sorted(
            part.strip() for part in scopes.split(",") if part.strip()
        ),
        "auditedLocales": locale_codes,
        "summary": summary,
        "translationsWithOldLinks": old_link_rows,
        "translations": translation_rows,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "shopify-blog-translation-audit.json"
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2))
    print("Shopify writes performed: false")
    print(f"Report: {report_path.resolve()}")


if __name__ == "__main__":
    main()
