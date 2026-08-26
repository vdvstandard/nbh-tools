#!/usr/bin/env python3
"""Upload an approved blog migration manifest as unpublished Shopify drafts."""

import argparse
import html
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional

from shopify_api import get_client_credentials_token, graphql_request


DEFAULT_API_VERSION = "2026-04"
DEFAULT_MANIFEST = Path(
    ".tmp/phase8-shopify-blog-migration-20260811/blog-migration-manifest.json"
)
DEFAULT_OUTPUT_DIR = Path(".tmp/phase8-shopify-blog-draft-upload-20260811")
MOJIBAKE_MARKERS = ("\u00e2\u20ac", "\u00c3", "\u00c2")


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


def request_json(url: str, token: str, attempts: int = 5) -> tuple[Dict[str, Any], Dict[str, str]]:
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


def list_articles(rest_base: str, token: str, blog_numeric_id: str) -> List[Dict[str, Any]]:
    url: Optional[str] = (
        f"{rest_base}/blogs/{blog_numeric_id}/articles.json?"
        "limit=250&published_status=any"
    )
    articles: List[Dict[str, Any]] = []
    while url:
        payload, headers = request_json(url, token)
        articles.extend(payload.get("articles") or [])
        url = next_link(headers.get("link", ""))
    return articles


def meaningful_text(value: str) -> str:
    text = re.sub(r"(?is)<(?:script|style).*?</(?:script|style)>", " ", value or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


class ImageSourceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.sources: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Any]) -> None:
        if tag.lower() != "img":
            return
        attributes = dict(attrs)
        if attributes.get("src"):
            self.sources.append(attributes["src"])


def image_sources(value: str) -> List[str]:
    parser = ImageSourceParser()
    parser.feed(value or "")
    return sorted(set(parser.sources))


def mojibake_count(value: str) -> int:
    return sum(str(value or "").count(marker) for marker in MOJIBAKE_MARKERS)


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


def create_draft(
    endpoint: str, token: str, article_input: Dict[str, Any]
) -> Dict[str, Any]:
    data = graphql_request(
        endpoint,
        token,
        """
        mutation CreatePreparedDraftArticle($article: ArticleCreateInput!) {
          articleCreate(article: $article) {
            article {
              id
              title
              handle
              isPublished
              publishedAt
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
        """,
        {"article": article_input},
        retries=1,
    )
    payload = data.get("articleCreate") or {}
    if payload.get("userErrors"):
        raise RuntimeError(
            user_error_message("Draft article creation", payload["userErrors"])
        )
    article = payload.get("article") or {}
    if article.get("isPublished") is not False or article.get("publishedAt"):
        raise RuntimeError("Post-write guard failed: created article is published.")
    return article


def validate_manifest(
    manifest: Dict[str, Any], expected_blog_id: str, expected_count: int
) -> List[Dict[str, Any]]:
    if manifest.get("mode") != "no_write_shopify_blog_migration_preparation":
        raise SystemExit("Manifest guard failed: unexpected mode.")
    if manifest.get("applySupported") is not False:
        raise SystemExit("Manifest guard failed: source unexpectedly supports apply.")
    articles = manifest.get("articles") or []
    if len(articles) != expected_count:
        raise SystemExit(
            f"Count guard failed: manifest has {len(articles)}, expected {expected_count}."
        )
    handles = [item.get("proposedDraftInput", {}).get("handle") for item in articles]
    if any(not handle for handle in handles) or len(set(handles)) != len(handles):
        raise SystemExit("Manifest guard failed: article handles are blank or duplicated.")
    errors = []
    for item in articles:
        article = item.get("proposedDraftInput") or {}
        handle = article.get("handle") or "(blank)"
        if article.get("blogId") != expected_blog_id:
            errors.append(f"{handle}: wrong blog ID")
        if article.get("isPublished") is not False:
            errors.append(f"{handle}: isPublished is not false")
        if article.get("publishedAt") or article.get("publishDate"):
            errors.append(f"{handle}: contains a publication timestamp")
        if not (article.get("image") or {}).get("url"):
            errors.append(f"{handle}: missing featured image input")
        if mojibake_count(article.get("title") or ""):
            errors.append(f"{handle}: title contains mojibake")
        if mojibake_count(article.get("summary") or ""):
            errors.append(f"{handle}: summary contains mojibake")
        if mojibake_count(article.get("body") or ""):
            errors.append(f"{handle}: body contains mojibake")
    if errors:
        raise SystemExit("Manifest guard failed:\n- " + "\n- ".join(errors))
    return articles


def verify_article(
    desired_row: Dict[str, Any], current: Dict[str, Any], blog_numeric_id: str
) -> List[str]:
    desired = desired_row["proposedDraftInput"]
    failures = []
    handle = desired["handle"]
    if str(current.get("blog_id")) != str(blog_numeric_id):
        failures.append(f"{handle}: attached to wrong blog")
    if current.get("published_at") is not None:
        failures.append(f"{handle}: unexpectedly published")
    if current.get("title") != desired.get("title"):
        failures.append(f"{handle}: title mismatch")
    if meaningful_text(current.get("body_html") or "") != meaningful_text(
        desired.get("body") or ""
    ):
        failures.append(f"{handle}: body text mismatch")
    if image_sources(current.get("body_html") or "") != image_sources(
        desired.get("body") or ""
    ):
        failures.append(f"{handle}: inline image mismatch")
    if meaningful_text(current.get("summary_html") or "") != meaningful_text(
        desired.get("summary") or ""
    ):
        failures.append(f"{handle}: summary mismatch")
    image = current.get("image") or {}
    if not image.get("src"):
        failures.append(f"{handle}: featured image missing")
    elif not str(image.get("src")).startswith("https://cdn.shopify.com/"):
        failures.append(f"{handle}: featured image is not on Shopify CDN")
    if mojibake_count(current.get("body_html") or ""):
        failures.append(f"{handle}: body contains mojibake after upload")
    return failures


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Upload prepared Shopify blog articles as unpublished drafts only."
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--api-version", default=DEFAULT_API_VERSION)
    parser.add_argument("--confirm-shop", required=True)
    parser.add_argument("--confirm-blog-id", required=True)
    parser.add_argument("--confirm-blog-handle", required=True)
    parser.add_argument("--confirm-count", required=True, type=int)
    parser.add_argument("--delay", type=float, default=0.2)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent.parent
    manifest_path = (
        args.manifest if args.manifest.is_absolute() else project_root / args.manifest
    )
    output_dir = (
        args.output_dir if args.output_dir.is_absolute() else project_root / args.output_dir
    )
    env_path = args.env_file or project_root / ".env"
    if not manifest_path.exists():
        raise SystemExit(f"Manifest not found: {manifest_path}")
    if args.confirm_blog_handle != "journal":
        raise SystemExit("Blog handle guard failed: this run only permits journal.")
    if not args.confirm_blog_id.startswith("gid://shopify/Blog/"):
        raise SystemExit("Blog ID guard failed: expected a Shopify Blog GID.")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows = validate_manifest(manifest, args.confirm_blog_id, args.confirm_count)
    load_dotenv(env_path)
    shop = normalize_shop(
        os.environ.get("SHOPIFY_SHOP")
        or os.environ.get("SHOPIFY_STORE_DOMAIN")
        or os.environ.get("SHOPIFY_STORE")
    )
    confirmed_shop = normalize_shop(args.confirm_shop)
    if shop != confirmed_shop:
        raise SystemExit(f"Shop guard failed: environment has {shop}, expected {confirmed_shop}.")
    token, scopes = acquire_token(shop)
    if "write_content" not in {part.strip() for part in scopes.split(",")}:
        raise SystemExit("Scope guard failed: token does not include write_content.")

    blog_numeric_id = args.confirm_blog_id.rsplit("/", 1)[-1]
    rest_base = f"https://{shop}/admin/api/{args.api_version}"
    endpoint = f"{rest_base}/graphql.json"
    blog_payload, _ = request_json(f"{rest_base}/blogs/{blog_numeric_id}.json", token)
    blog = blog_payload.get("blog") or {}
    if blog.get("admin_graphql_api_id") != args.confirm_blog_id:
        raise SystemExit("Live blog ID no longer matches the confirmed blog ID.")
    if blog.get("handle") != args.confirm_blog_handle:
        raise SystemExit("Live blog handle no longer matches journal.")

    current_articles = list_articles(rest_base, token, blog_numeric_id)
    current_by_handle = {article.get("handle"): article for article in current_articles}
    manifest_handles = {
        item["proposedDraftInput"]["handle"] for item in rows
    }
    conflicts = []
    already_present = []
    for row in rows:
        handle = row["proposedDraftInput"]["handle"]
        current = current_by_handle.get(handle)
        if not current:
            continue
        failures = verify_article(row, current, blog_numeric_id)
        if failures:
            conflicts.extend(failures)
        else:
            already_present.append(handle)
    pending_rows = [
        row
        for row in rows
        if row["proposedDraftInput"]["handle"] not in current_by_handle
    ]
    preflight = {
        "schemaVersion": 1,
        "generatedAt": utc_now(),
        "mode": "apply" if args.apply else "dry_run",
        "shop": shop,
        "blog": {"id": blog.get("admin_graphql_api_id"), "handle": blog.get("handle")},
        "shopifyWritesPerformed": False,
        "publicationMutationSupported": False,
        "redirectMutationSupported": False,
        "summary": {
            "manifestDrafts": len(rows),
            "existingJournalArticles": len(current_articles),
            "alreadyPresentMatchingDrafts": len(already_present),
            "pendingDraftCreates": len(pending_rows),
            "conflicts": len(conflicts),
            "manifestHandles": len(manifest_handles),
        },
        "alreadyPresentHandles": sorted(already_present),
        "pendingHandles": [row["proposedDraftInput"]["handle"] for row in pending_rows],
        "conflicts": conflicts,
    }
    write_json(output_dir / "preflight.json", preflight)
    print(json.dumps(preflight["summary"], indent=2))
    print(f"Preflight: {(output_dir / 'preflight.json').resolve()}")
    if conflicts:
        raise SystemExit("Refusing to upload because preflight found conflicts.")
    if not args.apply:
        print("Dry run only. Add --apply to create unpublished drafts.")
        return

    progress = {
        "schemaVersion": 1,
        "startedAt": utc_now(),
        "mode": "unpublished_draft_upload_progress",
        "shop": shop,
        "blog": {"id": args.confirm_blog_id, "handle": args.confirm_blog_handle},
        "shopifyWritesPerformed": False,
        "publicationMutations": 0,
        "redirectMutations": 0,
        "created": [],
    }
    progress_path = output_dir / "created-draft-register.json"
    write_json(progress_path, progress)
    for index, row in enumerate(pending_rows, start=1):
        article_input = dict(row["proposedDraftInput"])
        article_input["isPublished"] = False
        article_input.pop("publishedAt", None)
        article_input.pop("publishDate", None)
        created = create_draft(endpoint, token, article_input)
        if (created.get("blog") or {}).get("id") != args.confirm_blog_id:
            raise RuntimeError(
                f"Post-write guard failed for {article_input['handle']}: wrong blog."
            )
        progress["shopifyWritesPerformed"] = True
        progress["created"].append(
            {
                "reviewId": row.get("reviewId"),
                "sourceUrl": row.get("sourceUrl"),
                "article": created,
            }
        )
        progress["updatedAt"] = utc_now()
        write_json(progress_path, progress)
        if index % 10 == 0 or index == len(pending_rows):
            print(f"Created unpublished drafts: {index}/{len(pending_rows)}")
        if args.delay:
            time.sleep(args.delay)

    verified_articles: List[Dict[str, Any]] = []
    verification_failures: List[str] = []
    after_articles: List[Dict[str, Any]] = []
    for attempt in range(1, 6):
        after_articles = list_articles(rest_base, token, blog_numeric_id)
        after_handles = {item.get("handle") for item in after_articles}
        if manifest_handles <= after_handles:
            break
        if attempt < 5:
            time.sleep(attempt * 2)
    after_by_handle = {article.get("handle"): article for article in after_articles}
    for row in rows:
        handle = row["proposedDraftInput"]["handle"]
        current = after_by_handle.get(handle)
        if not current:
            verification_failures.append(f"{handle}: missing after upload")
            continue
        failures = verify_article(row, current, blog_numeric_id)
        verification_failures.extend(failures)
        verified_articles.append(
            {
                "id": current.get("id"),
                "handle": handle,
                "title": current.get("title"),
                "publishedAt": current.get("published_at"),
                "featuredImage": (current.get("image") or {}).get("src"),
                "inlineImages": image_sources(current.get("body_html") or ""),
            }
        )

    report = {
        "schemaVersion": 1,
        "generatedAt": utc_now(),
        "mode": "unpublished_drafts_uploaded_and_verified",
        "shop": shop,
        "blog": {"id": args.confirm_blog_id, "handle": args.confirm_blog_handle},
        "shopifyWritesPerformed": bool(progress["created"]),
        "publicationMutations": 0,
        "redirectMutations": 0,
        "summary": {
            "manifestDrafts": len(rows),
            "createdDrafts": len(progress["created"]),
            "alreadyPresentMatchingDrafts": len(already_present),
            "verifiedDrafts": len(verified_articles),
            "verificationFailures": len(verification_failures),
            "journalArticlesAfter": len(after_articles),
            "publishedManifestArticles": sum(
                bool(article.get("publishedAt")) for article in verified_articles
            ),
            "manifestArticlesWithFeaturedImage": sum(
                bool(article.get("featuredImage")) for article in verified_articles
            ),
        },
        "verificationFailures": verification_failures,
        "articles": verified_articles,
    }
    write_json(output_dir / "apply-results.json", report)
    print(json.dumps(report["summary"], indent=2))
    print(f"Created draft register: {progress_path.resolve()}")
    print(f"Results: {(output_dir / 'apply-results.json').resolve()}")
    if verification_failures:
        raise SystemExit("Draft upload completed, but verification found failures.")


if __name__ == "__main__":
    main()
