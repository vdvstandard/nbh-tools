#!/usr/bin/env python3
"""Replace legacy Journal links with public Shopify targets or plain text."""

import argparse
import hashlib
import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from shopify_api import get_client_credentials_token, graphql_request


DEFAULT_API_VERSION = "2026-04"
DEFAULT_MIGRATION = Path(
    ".tmp/phase8-shopify-blog-migration-post-draft-upload-20260811/"
    "blog-migration-preflight.json"
)
DEFAULT_OUTPUT_DIR = Path(".tmp/phase8-shopify-blog-link-update-20260811")
OLD_HOSTS = {"nbharnhem.com", "www.nbharnhem.com"}
CACHE_REFRESH_MARKER = "\n<!-- neighbourhood-link-cache-refresh -->"


def rule(
    article: str,
    source: str,
    action: str,
    expected: int = 1,
    target: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "article": article,
        "source": source,
        "action": action,
        "expected": expected,
        "target": target,
    }


LINK_RULES = [
    rule(
        "arnhem-fashion-stores",
        "https://www.nbharnhem.com/brands/danner/",
        "unlink",
    ),
    rule(
        "blundstone-chelsea-boots",
        "https://www.nbharnhem.com/brands/blundstone/",
        "unlink",
    ),
    rule(
        "filson",
        "https://www.nbharnhem.com/brands/filson/",
        "unlink",
    ),
    rule(
        "harley-of-scotland",
        "https://www.nbharnhem.com/brands/harley-of-scotland/",
        "unlink",
    ),
    rule(
        "kardo",
        "https://www.nbharnhem.com/brands/kardo/",
        "unlink",
    ),
    rule(
        "king-and-tuckfield",
        "https://www.nbharnhem.com/brands/king-tuckfield/",
        "rewrite",
        target="/collections/king-and-tuckfield",
    ),
    rule(
        "malin-goetz",
        "https://www.nbharnhem.com/brands/malin-goetz/",
        "rewrite",
        expected=2,
        target="/collections/malin-and-goetz",
    ),
    rule(
        "malin-goetz-candles",
        "https://www.nbharnhem.com/brands/malin-goetz/",
        "rewrite",
        target="/collections/malin-and-goetz",
    ),
    rule(
        "paisley",
        "https://www.nbharnhem.com/brands/filson/",
        "rewrite",
        target="/blogs/journal/filson",
    ),
    rule(
        "paradise-found",
        "https://www.nbharnhem.com/brands/paradise-found/",
        "unlink",
    ),
    rule(
        "short-lined-cruiser",
        "https://www.nbharnhem.com/short-lined-cruiser-dark-tan.html",
        "unlink",
    ),
    rule(
        "summer-is-here",
        "https://www.nbharnhem.com/blogs/arnhem/messy-weekend/",
        "unlink",
    ),
    rule(
        "summer-is-here",
        "https://www.nbharnhem.com/hila-hat-kiss-from-a-velvet-rose.html",
        "unlink",
    ),
    rule(
        "tagliatore",
        "https://www.nbharnhem.com/brands/tagliatore/",
        "unlink",
    ),
    rule(
        "ukiyo",
        "https://www.nbharnhem.com/ukiyo-kids/",
        "unlink",
    ),
    rule(
        "welter-shelter",
        "https://www.nbharnhem.com/brands/welter-shelter/",
        "unlink",
    ),
    rule(
        "winter-jacket",
        "https://www.nbharnhem.com/brands/filson/",
        "rewrite",
        expected=3,
        target="/blogs/journal/filson",
    ),
    rule(
        "winter-jacket",
        "https://www.nbharnhem.com/brands/welter-shelter/",
        "rewrite",
        target="/blogs/journal/welter-shelter",
    ),
]
RULES_BY_KEY = {
    (item["article"], item["source"]): item for item in LINK_RULES
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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


def rest_update_article_body(
    rest_base: str,
    token: str,
    blog_id: str,
    article_id: str,
    body: str,
) -> Dict[str, Any]:
    url = f"{rest_base}/blogs/{blog_id}/articles/{article_id}.json"
    request = urllib.request.Request(
        url,
        data=json.dumps(
            {"article": {"id": int(article_id), "body_html": body}}
        ).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": token,
        },
        method="PUT",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as error:
        message = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Shopify REST article update failed ({error.code}): {message}"
        ) from error
    article = payload.get("article") or {}
    if article.get("body_html") != body:
        raise RuntimeError("REST post-write body guard failed.")
    return article


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


def old_internal_href(value: str) -> bool:
    parsed = urllib.parse.urlparse(html.unescape(value or ""))
    return parsed.netloc.lower() in OLD_HOSTS


class HrefCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.hrefs: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag.lower() != "a":
            return
        for key, value in attrs:
            if key.lower() == "href" and value is not None:
                self.hrefs.append(html.unescape(value))
                return


def collect_hrefs(body: str) -> List[str]:
    parser = HrefCollector()
    parser.feed(body or "")
    parser.close()
    return parser.hrefs


def replace_href(raw_tag: str, source: str, target: str) -> str:
    pattern = re.compile(r"(\bhref\s*=\s*)([\"'])(.*?)\2", re.I | re.S)

    def replacement(match: re.Match[str]) -> str:
        if html.unescape(match.group(3)) != source:
            return match.group(0)
        escaped = html.escape(target, quote=True)
        return f"{match.group(1)}{match.group(2)}{escaped}{match.group(2)}"

    updated, count = pattern.subn(replacement, raw_tag, count=1)
    if count != 1 or updated == raw_tag:
        raise RuntimeError(f"Could not replace expected href in tag: {raw_tag}")
    return updated


class BodyLinkEditor(HTMLParser):
    def __init__(self, article_handle: str) -> None:
        super().__init__(convert_charrefs=False)
        self.article_handle = article_handle
        self.parts: List[str] = []
        self.anchor_unlink_stack: List[bool] = []
        self.events: List[Dict[str, Any]] = []
        self.unexpected_old_links: List[str] = []

    def handle_starttag(
        self, tag: str, attrs: List[Tuple[str, Optional[str]]]
    ) -> None:
        raw_tag = self.get_starttag_text()
        if tag.lower() != "a":
            self.parts.append(raw_tag)
            return
        href = next(
            (html.unescape(value) for key, value in attrs if key.lower() == "href" and value),
            None,
        )
        rule_item = RULES_BY_KEY.get((self.article_handle, href or ""))
        if not rule_item:
            self.anchor_unlink_stack.append(False)
            self.parts.append(raw_tag)
            if href and old_internal_href(href):
                self.unexpected_old_links.append(href)
            return
        if rule_item["action"] == "rewrite":
            self.anchor_unlink_stack.append(False)
            self.parts.append(replace_href(raw_tag, href or "", rule_item["target"]))
        elif rule_item["action"] == "unlink":
            self.anchor_unlink_stack.append(True)
        else:
            raise RuntimeError(f"Unsupported link action: {rule_item['action']}")
        self.events.append(rule_item)

    def handle_startendtag(
        self, tag: str, attrs: List[Tuple[str, Optional[str]]]
    ) -> None:
        raw_tag = self.get_starttag_text()
        if tag.lower() != "a":
            self.parts.append(raw_tag)
            return
        href = next(
            (html.unescape(value) for key, value in attrs if key.lower() == "href" and value),
            None,
        )
        rule_item = RULES_BY_KEY.get((self.article_handle, href or ""))
        if not rule_item:
            self.parts.append(raw_tag)
            if href and old_internal_href(href):
                self.unexpected_old_links.append(href)
            return
        if rule_item["action"] == "rewrite":
            self.parts.append(replace_href(raw_tag, href or "", rule_item["target"]))
        self.events.append(rule_item)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a":
            unlink = self.anchor_unlink_stack.pop() if self.anchor_unlink_stack else False
            if unlink:
                return
        self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_entityref(self, name: str) -> None:
        self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.parts.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        self.parts.append(f"<!--{data}-->")

    def handle_decl(self, decl: str) -> None:
        self.parts.append(f"<!{decl}>")

    def handle_pi(self, data: str) -> None:
        self.parts.append(f"<?{data}>")

    def result(self) -> str:
        if self.anchor_unlink_stack:
            raise RuntimeError(f"Unclosed anchor in article {self.article_handle}.")
        return "".join(self.parts)


def edit_body(article_handle: str, body: str) -> tuple[str, List[Dict[str, Any]], List[str]]:
    editor = BodyLinkEditor(article_handle)
    editor.feed(body or "")
    editor.close()
    return editor.result(), editor.events, editor.unexpected_old_links


def public_check(shop: str, path: str) -> Dict[str, Any]:
    url = path if path.startswith("http") else f"https://{shop}{path}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "text/html,*/*",
            "Range": "bytes=0-0",
            "User-Agent": "Neighbourhood Journal link audit/1.0",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read(1)
            return {
                "path": path,
                "status": response.status,
                "finalUrl": response.geturl(),
                "reachable": response.status in {200, 206},
                "error": None,
            }
    except urllib.error.HTTPError as error:
        return {
            "path": path,
            "status": error.code,
            "finalUrl": error.geturl(),
            "reachable": False,
            "error": str(error),
        }
    except urllib.error.URLError as error:
        return {
            "path": path,
            "status": None,
            "finalUrl": None,
            "reachable": False,
            "error": str(error.reason),
        }


def public_internal_paths(articles: List[Dict[str, Any]], shop: str) -> List[str]:
    paths = set()
    for article in articles:
        for href in collect_hrefs(article.get("body_html") or ""):
            parsed = urllib.parse.urlparse(href)
            if not parsed.netloc and href.startswith("/"):
                paths.add(urllib.parse.urlunparse(("", "", parsed.path, "", parsed.query, "")))
            elif parsed.netloc.lower() == shop.lower():
                paths.add(urllib.parse.urlunparse(("", "", parsed.path, "", parsed.query, "")))
    return sorted(paths)


def user_error_message(errors: List[Dict[str, Any]]) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in error.get('field') or [])}: "
        f"{error.get('message')}"
        for error in errors
    )


def update_article_body(
    endpoint: str,
    token: str,
    article_id: str,
    body: str,
) -> Dict[str, Any]:
    data = graphql_request(
        endpoint,
        token,
        """
        mutation UpdateJournalInternalLinks($id: ID!, $article: ArticleUpdateInput!) {
          articleUpdate(id: $id, article: $article) {
            article {
              id
              title
              handle
              body
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
        {"id": article_id, "article": {"body": body}},
        retries=1,
    )
    payload = data.get("articleUpdate") or {}
    if payload.get("userErrors"):
        raise RuntimeError(
            "Article link update failed: " + user_error_message(payload["userErrors"])
        )
    article = payload.get("article") or {}
    if article.get("body") != body:
        raise RuntimeError("GraphQL post-write body guard failed.")
    return article


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Update legacy links in published Journal articles."
    )
    parser.add_argument("--migration", type=Path, default=DEFAULT_MIGRATION)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--api-version", default=DEFAULT_API_VERSION)
    parser.add_argument("--confirm-shop", required=True)
    parser.add_argument("--confirm-blog-id", required=True)
    parser.add_argument("--confirm-blog-handle", required=True)
    parser.add_argument("--confirm-retained-count", required=True, type=int)
    parser.add_argument(
        "--refresh-handles",
        default="",
        help="Comma-separated handles to touch and restore after link updates.",
    )
    parser.add_argument(
        "--cache-refresh-transport",
        choices=("graphql", "rest"),
        default="graphql",
    )
    parser.add_argument("--delay", type=float, default=0.15)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent.parent
    migration_path = (
        args.migration if args.migration.is_absolute() else project_root / args.migration
    )
    output_dir = (
        args.output_dir if args.output_dir.is_absolute() else project_root / args.output_dir
    )
    env_path = args.env_file or project_root / ".env"
    if not migration_path.exists():
        raise SystemExit(f"Migration preflight not found: {migration_path}")
    if args.confirm_blog_handle != "journal":
        raise SystemExit("Blog handle guard failed: only journal is permitted.")

    migration = json.loads(migration_path.read_text(encoding="utf-8"))
    retained_rows = migration.get("articles") or []
    expected_handles = {
        row["proposedDraftInput"]["handle"] for row in retained_rows
    }
    if len(retained_rows) != args.confirm_retained_count:
        raise SystemExit(
            f"Retained count guard failed: found {len(retained_rows)}, "
            f"confirmed {args.confirm_retained_count}."
        )
    if len(expected_handles) != len(retained_rows):
        raise SystemExit("Retained handle guard failed: duplicate handles.")
    refresh_handles = {
        value.strip() for value in args.refresh_handles.split(",") if value.strip()
    }
    unknown_refresh_handles = sorted(refresh_handles - expected_handles)
    if unknown_refresh_handles:
        raise SystemExit(
            "Unknown cache-refresh handles: " + ", ".join(unknown_refresh_handles)
        )

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
    if set(current_by_handle) != expected_handles:
        missing = sorted(expected_handles - set(current_by_handle))
        unexpected = sorted(set(current_by_handle) - expected_handles)
        conflicts.append(f"Missing retained handles: {', '.join(missing) or 'none'}")
        conflicts.append(f"Unexpected handles: {', '.join(unexpected) or 'none'}")
    for article in current_articles:
        if not article.get("published_at"):
            conflicts.append(f"{article.get('handle')}: article is not published")
        if not article.get("image"):
            conflicts.append(f"{article.get('handle')}: featured image is missing")

    article_plans = []
    all_events: List[Dict[str, Any]] = []
    unexpected_old_links = []
    for handle in sorted(expected_handles):
        article = current_by_handle.get(handle)
        if not article:
            continue
        current_body = article.get("body_html") or ""
        desired_body, events, unexpected = edit_body(handle, current_body)
        unexpected_old_links.extend(
            {"article": handle, "href": href} for href in unexpected
        )
        all_events.extend({**item, "article": handle} for item in events)
        if desired_body != current_body:
            article_plans.append(
                {
                    "handle": handle,
                    "articleId": article["admin_graphql_api_id"],
                    "publishedAtBefore": article.get("published_at"),
                    "beforeBodySha256": sha256_text(current_body),
                    "afterBodySha256": sha256_text(desired_body),
                    "beforeBody": current_body,
                    "afterBody": desired_body,
                    "actions": events,
                    "cacheRefresh": False,
                }
            )

    planned_handles = {plan["handle"] for plan in article_plans}
    for handle in sorted(refresh_handles - planned_handles):
        article = current_by_handle.get(handle)
        if not article:
            continue
        current_body = article.get("body_html") or ""
        if CACHE_REFRESH_MARKER in current_body:
            conflicts.append(f"{handle}: cache-refresh marker is already present")
            continue
        article_plans.append(
            {
                "handle": handle,
                "articleId": article["admin_graphql_api_id"],
                "publishedAtBefore": article.get("published_at"),
                "beforeBodySha256": sha256_text(current_body),
                "afterBodySha256": sha256_text(current_body),
                "beforeBody": current_body,
                "afterBody": current_body,
                "actions": [],
                "cacheRefresh": True,
            }
        )

    observed_counts: Dict[Tuple[str, str], int] = {}
    for event in all_events:
        key = (event["article"], event["source"])
        observed_counts[key] = observed_counts.get(key, 0) + 1
    for rule_item in LINK_RULES:
        observed = observed_counts.get((rule_item["article"], rule_item["source"]), 0)
        if observed > rule_item["expected"]:
            conflicts.append(
                f"{rule_item['article']}: expected at most {rule_item['expected']} "
                f"occurrence(s) of {rule_item['source']}, found {observed}"
            )
    if unexpected_old_links:
        conflicts.append(
            f"Found {len(unexpected_old_links)} old-domain link(s) outside the allowlist."
        )

    rewrite_targets = sorted(
        {item["target"] for item in all_events if item["action"] == "rewrite"}
    )
    target_checks = [public_check(shop, target) for target in rewrite_targets]
    for check in target_checks:
        if not check["reachable"]:
            conflicts.append(
                f"Rewrite target is not public: {check['path']} ({check['status']})"
            )

    summary = {
        "journalArticles": len(current_articles),
        "publishedArticles": sum(bool(item.get("published_at")) for item in current_articles),
        "articlesWithImage": sum(bool(item.get("image")) for item in current_articles),
        "articlesToUpdate": len(article_plans),
        "oldLinkOccurrencesToResolve": len(all_events),
        "rewriteOccurrences": sum(item["action"] == "rewrite" for item in all_events),
        "unlinkOccurrences": sum(item["action"] == "unlink" for item in all_events),
        "publicRewriteTargets": len(target_checks),
        "cacheRefreshArticles": sum(
            bool(plan.get("cacheRefresh")) for plan in article_plans
        ),
        "cacheRefreshTransport": args.cache_refresh_transport,
        "conflicts": len(conflicts),
    }
    snapshot = {
        "schemaVersion": 1,
        "generatedAt": utc_now(),
        "mode": "apply" if args.apply else "dry_run",
        "shop": shop,
        "blog": blog,
        "shopifyWritesPerformed": False,
        "redirectMutationsSupported": False,
        "summary": summary,
        "targetChecks": target_checks,
        "unexpectedOldLinks": unexpected_old_links,
        "conflicts": conflicts,
        "plans": article_plans,
    }
    snapshot_path = output_dir / "preflight-and-rollback.json"
    write_json(snapshot_path, snapshot)
    print(json.dumps(summary, indent=2))
    print(f"Preflight and rollback: {snapshot_path.resolve()}")
    if conflicts:
        raise SystemExit("Refusing to mutate Shopify because preflight found conflicts.")
    if not args.apply:
        print("Dry run only. Add --apply to update the article links.")
        return

    progress = {
        "schemaVersion": 1,
        "startedAt": utc_now(),
        "mode": "journal_internal_link_update_progress",
        "shopifyWritesPerformed": False,
        "redirectMutations": 0,
        "updatedArticles": [],
    }
    progress_path = output_dir / "mutation-register.json"
    write_json(progress_path, progress)
    for index, plan in enumerate(article_plans, start=1):
        if plan.get("cacheRefresh"):
            temporary_body = plan["afterBody"] + CACHE_REFRESH_MARKER
            if args.cache_refresh_transport == "rest":
                article_numeric_id = plan["articleId"].rsplit("/", 1)[-1]
                rest_update_article_body(
                    rest_base,
                    token,
                    blog_numeric_id,
                    article_numeric_id,
                    temporary_body,
                )
            else:
                update_article_body(
                    endpoint,
                    token,
                    plan["articleId"],
                    temporary_body,
                )
            time.sleep(max(args.delay, 0.5))
        if plan.get("cacheRefresh") and args.cache_refresh_transport == "rest":
            article_numeric_id = plan["articleId"].rsplit("/", 1)[-1]
            updated_rest = rest_update_article_body(
                rest_base,
                token,
                blog_numeric_id,
                article_numeric_id,
                plan["afterBody"],
            )
            if str(updated_rest.get("blog_id")) != str(blog_numeric_id):
                raise RuntimeError(f"{plan['handle']}: REST blog guard failed.")
            if updated_rest.get("handle") != plan["handle"]:
                raise RuntimeError(f"{plan['handle']}: REST handle guard failed.")
            if not updated_rest.get("published_at"):
                raise RuntimeError(f"{plan['handle']}: REST publication guard failed.")
            if not updated_rest.get("image"):
                raise RuntimeError(f"{plan['handle']}: REST featured-image guard failed.")
            if updated_rest["published_at"][:10] != plan["publishedAtBefore"][:10]:
                raise RuntimeError(f"{plan['handle']}: publication date changed.")
        else:
            updated = update_article_body(
                endpoint, token, plan["articleId"], plan["afterBody"]
            )
            if (updated.get("blog") or {}).get("id") != args.confirm_blog_id:
                raise RuntimeError(f"{plan['handle']}: post-write blog guard failed.")
            if updated.get("handle") != plan["handle"]:
                raise RuntimeError(f"{plan['handle']}: post-write handle guard failed.")
            if updated.get("isPublished") is not True or not updated.get("publishedAt"):
                raise RuntimeError(f"{plan['handle']}: publication guard failed.")
            if not updated.get("image"):
                raise RuntimeError(f"{plan['handle']}: featured-image guard failed.")
            if updated["publishedAt"][:10] != plan["publishedAtBefore"][:10]:
                raise RuntimeError(f"{plan['handle']}: publication date changed.")
        progress["shopifyWritesPerformed"] = True
        progress["updatedArticles"].append(
            {
                "handle": plan["handle"],
                "beforeBodySha256": plan["beforeBodySha256"],
                "afterBodySha256": plan["afterBodySha256"],
                "actions": plan["actions"],
                "cacheRefresh": bool(plan.get("cacheRefresh")),
                "cacheRefreshTransport": (
                    args.cache_refresh_transport if plan.get("cacheRefresh") else None
                ),
            }
        )
        progress["updatedAt"] = utc_now()
        write_json(progress_path, progress)
        print(f"Updated Journal article links: {index}/{len(article_plans)}")
        if args.delay:
            time.sleep(args.delay)

    final_articles = list_articles(rest_base, token, blog_numeric_id)
    final_by_handle = {article.get("handle"): article for article in final_articles}
    final_failures = []
    desired_by_handle = {plan["handle"]: plan for plan in article_plans}
    for handle, article in final_by_handle.items():
        if not article.get("published_at"):
            final_failures.append(f"{handle}: no longer published")
        if not article.get("image"):
            final_failures.append(f"{handle}: featured image missing")
        old_links = [
            href for href in collect_hrefs(article.get("body_html") or "")
            if old_internal_href(href)
        ]
        if old_links:
            final_failures.append(f"{handle}: {len(old_links)} old-domain link(s) remain")
        plan = desired_by_handle.get(handle)
        if plan:
            if article.get("body_html") != plan["afterBody"]:
                final_failures.append(f"{handle}: final body mismatch")
            if article.get("published_at") != plan["publishedAtBefore"]:
                final_failures.append(f"{handle}: exact publication timestamp changed")
    if set(final_by_handle) != expected_handles:
        final_failures.append("Final retained handle set changed.")

    internal_paths = public_internal_paths(final_articles, shop)
    internal_checks = [public_check(shop, path) for path in internal_paths]
    unreachable = [check for check in internal_checks if not check["reachable"]]
    final_failures.extend(
        f"Internal target is not public: {check['path']} ({check['status']})"
        for check in unreachable
    )

    final_report = {
        "schemaVersion": 1,
        "generatedAt": utc_now(),
        "mode": "journal_internal_links_updated",
        "shop": shop,
        "blog": {"id": args.confirm_blog_id, "handle": args.confirm_blog_handle},
        "shopifyWritesPerformed": bool(article_plans),
        "redirectMutations": 0,
        "summary": {
            "journalArticles": len(final_articles),
            "publishedArticles": sum(bool(item.get("published_at")) for item in final_articles),
            "articlesWithImage": sum(bool(item.get("image")) for item in final_articles),
            "updatedArticles": len(progress["updatedArticles"]),
            "cacheRefreshArticles": sum(
                bool(update.get("cacheRefresh"))
                for update in progress["updatedArticles"]
            ),
            "rewrittenLinkOccurrences": sum(
                item["action"] == "rewrite"
                for update in progress["updatedArticles"]
                for item in update["actions"]
            ),
            "unlinkedOccurrences": sum(
                item["action"] == "unlink"
                for update in progress["updatedArticles"]
                for item in update["actions"]
            ),
            "oldDomainLinksRemaining": sum(
                old_internal_href(href)
                for article in final_articles
                for href in collect_hrefs(article.get("body_html") or "")
            ),
            "uniqueInternalTargetsChecked": len(internal_checks),
            "unreachableInternalTargets": len(unreachable),
            "finalFailures": len(final_failures),
        },
        "internalTargetChecks": internal_checks,
        "finalFailures": final_failures,
    }
    results_path = output_dir / "apply-results.json"
    write_json(results_path, final_report)
    print(json.dumps(final_report["summary"], indent=2))
    print(f"Mutation register: {progress_path.resolve()}")
    print(f"Results: {results_path.resolve()}")
    if final_failures:
        raise SystemExit("Post-write Journal link verification failed.")


if __name__ == "__main__":
    main()
