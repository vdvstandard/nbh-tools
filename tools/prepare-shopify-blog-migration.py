#!/usr/bin/env python3
"""Prepare a no-write Shopify blog migration manifest and preflight report."""

import argparse
import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time as datetime_time, timezone
from html.parser import HTMLParser
from pathlib import Path


DEFAULT_DECISIONS = ".tmp/phase8-blog-decisions-20260805/blog-decisions.json"
DEFAULT_SHOPIFY_AUDIT = (
    ".tmp/phase8-shopify-blog-audit-20260805/shopify-blog-audit.json"
)
DEFAULT_REDIRECT_REPORT = (
    ".tmp/phase8-redirect-preparation-20260805/redirect-preparation.json"
)
DEFAULT_OUTPUT_DIR = ".tmp/phase8-shopify-blog-migration-20260805"
OLD_HOSTS = {"nbharnhem.com", "www.nbharnhem.com"}
VOID_TAGS = {"br", "hr", "img", "source"}
MOJIBAKE_MARKERS = ("\u00e2\u20ac", "\u00c3", "\u00c2")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Prepare a Shopify blog migration without any API mutations."
    )
    parser.add_argument("--decisions", default=DEFAULT_DECISIONS)
    parser.add_argument("--shopify-audit", default=DEFAULT_SHOPIFY_AUDIT)
    parser.add_argument("--redirect-report", default=DEFAULT_REDIRECT_REPORT)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--target-blog-handle", default="arnhem")
    parser.add_argument("--target-blog-title", default="Journal")
    parser.add_argument("--workers", type=int, default=4)
    return parser.parse_args()


def resolve_path(project_root, value):
    path = Path(value)
    return path if path.is_absolute() else project_root / path


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def normalize_path(value):
    path = urllib.parse.urlparse(value or "/").path or "/"
    path = re.sub(r"/{2,}", "/", path)
    return path if path.startswith("/") else f"/{path}"


def mojibake_count(value):
    text = str(value or "")
    return sum(text.count(marker) for marker in MOJIBAKE_MARKERS)


def publication_datetime(value):
    published_date = date.fromisoformat(value)
    local_noon = datetime.combine(
        published_date,
        datetime_time(hour=12),
        tzinfo=timezone.utc,
    )
    return local_noon.isoformat()


def build_link_map(redirect_report, kept_articles, target_blog_handle):
    link_map = {
        row["sourcePath"]: row["targetPath"]
        for row in redirect_report.get("rows", [])
        if row.get("targetPath") and row.get("status") == "ready_for_review"
    }
    link_map["/"] = "/"
    link_map["/blogs/arnhem/"] = f"/blogs/{target_blog_handle}/"
    for article in kept_articles:
        link_map[article["path"]] = (
            f"/blogs/{target_blog_handle}/{article['handle']}"
        )
    return link_map


class LinkRewriter(HTMLParser):
    def __init__(self, link_map):
        super().__init__(convert_charrefs=True)
        self.link_map = link_map
        self.parts = []
        self.rewrites = []
        self.unresolved = []

    def rewrite_href(self, value):
        parsed = urllib.parse.urlparse(value)
        is_old_internal = parsed.netloc.lower() in OLD_HOSTS or (
            not parsed.netloc and value.startswith("/")
        )
        if not is_old_internal:
            return value
        path = normalize_path(parsed.path)
        target = self.link_map.get(path)
        if not target:
            self.unresolved.append(value)
            return value
        rewritten = target
        if parsed.query:
            rewritten += f"?{parsed.query}"
        if parsed.fragment:
            rewritten += f"#{parsed.fragment}"
        if rewritten != value:
            self.rewrites.append({"from": value, "to": rewritten})
        return rewritten

    def serialize_start(self, tag, attrs):
        clean_attrs = []
        for key, value in attrs:
            value = value or ""
            if tag == "a" and key.lower() == "href":
                value = self.rewrite_href(value)
            clean_attrs.append(
                f' {key}="{html.escape(value, quote=True)}"' if value else f" {key}"
            )
        self.parts.append(f"<{tag}{''.join(clean_attrs)}>")

    def handle_starttag(self, tag, attrs):
        self.serialize_start(tag.lower(), attrs)

    def handle_startendtag(self, tag, attrs):
        self.serialize_start(tag.lower(), attrs)

    def handle_endtag(self, tag):
        if tag.lower() not in VOID_TAGS:
            self.parts.append(f"</{tag.lower()}>")

    def handle_data(self, data):
        self.parts.append(html.escape(data))

    def result(self):
        return re.sub(r">\s+<", "><", "".join(self.parts).strip())


def audit_image(url):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Neighbourhood blog migration image audit/1.0",
            "Range": "bytes=0-65535",
        },
        method="GET",
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read(65536)
            return {
                "url": url,
                "status": response.status,
                "contentType": response.headers.get("Content-Type"),
                "contentLength": response.headers.get("Content-Length"),
                "elapsedMs": round((time.monotonic() - started) * 1000),
                "accessible": response.status in {200, 206}
                and str(response.headers.get("Content-Type") or "").startswith("image/"),
                "error": None,
            }
    except urllib.error.HTTPError as error:
        return {
            "url": url,
            "status": error.code,
            "contentType": error.headers.get("Content-Type"),
            "contentLength": error.headers.get("Content-Length"),
            "elapsedMs": round((time.monotonic() - started) * 1000),
            "accessible": False,
            "error": str(error),
        }
    except urllib.error.URLError as error:
        return {
            "url": url,
            "status": None,
            "contentType": None,
            "contentLength": None,
            "elapsedMs": round((time.monotonic() - started) * 1000),
            "accessible": False,
            "error": str(error.reason),
        }


def main():
    args = parse_args()
    project_root = Path(__file__).resolve().parent.parent
    decisions_path = resolve_path(project_root, args.decisions)
    shopify_audit_path = resolve_path(project_root, args.shopify_audit)
    redirect_report_path = resolve_path(project_root, args.redirect_report)
    output_dir = resolve_path(project_root, args.output_dir)
    for path in (decisions_path, shopify_audit_path, redirect_report_path):
        if not path.exists():
            raise SystemExit(f"Required input not found: {path}")

    decision_report = read_json(decisions_path)
    shopify_audit = read_json(shopify_audit_path)
    redirect_report = read_json(redirect_report_path)
    kept = [
        {**item, "handle": normalize_path(item["path"]).strip("/").split("/")[-1]}
        for item in decision_report.get("decisions", [])
        if item.get("decision") == "keep"
    ]
    removed = [
        item
        for item in decision_report.get("decisions", [])
        if item.get("decision") == "remove"
    ]
    blogs = shopify_audit.get("blogs", [])
    existing_articles = shopify_audit.get("articles", [])
    target_blog = blogs[0] if len(blogs) == 1 else None
    link_map = build_link_map(redirect_report, kept, args.target_blog_handle)

    article_rows = []
    all_rewrites = []
    all_unresolved = []
    for item in kept:
        rewriter = LinkRewriter(link_map)
        rewriter.feed(item.get("bodyHtml") or "")
        body_html = rewriter.result()
        all_rewrites.extend(
            {"reviewId": item["reviewId"], **rewrite}
            for rewrite in rewriter.rewrites
        )
        all_unresolved.extend(
            {
                "reviewId": item["reviewId"],
                "title": item["title"],
                "href": href,
            }
            for href in sorted(set(rewriter.unresolved))
        )
        image_input = {
            "url": item.get("heroImageUrl"),
            "altText": item.get("heroImageAlt") or item["title"],
        }
        article_rows.append(
            {
                "reviewId": item["reviewId"],
                "sourceUrl": item["url"],
                "sourcePath": item["path"],
                "targetPath": f"/blogs/{args.target_blog_handle}/{item['handle']}",
                "inlineImageUrls": item.get("bodyImages") or [],
                "unresolvedInternalLinks": sorted(set(rewriter.unresolved)),
                "proposedDraftInput": {
                    "author": {"name": "Neighbourhood"},
                    "blogId": target_blog.get("admin_graphql_api_id") if target_blog else None,
                    "body": body_html,
                    "handle": item["handle"],
                    "image": image_input,
                    "isPublished": False,
                    "summary": f"<p>{html.escape(item['summary'])}</p>",
                    "tags": [],
                    "title": item["title"],
                },
                "publicationPlan": {
                    "sourceDate": item["publishedDate"],
                    "intendedPublishDate": publication_datetime(item["publishedDate"]),
                    "datePrecision": "date_only_source; 12:00 UTC placeholder",
                },
            }
        )

    hero_urls = sorted(
        {item["proposedDraftInput"]["image"]["url"] for item in article_rows}
    )
    inline_urls = sorted(
        {url for item in article_rows for url in item["inlineImageUrls"]}
    )
    image_urls = hero_urls + [url for url in inline_urls if url not in set(hero_urls)]
    image_audits = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {executor.submit(audit_image, url): url for url in image_urls}
        for future in as_completed(futures):
            image_audits.append(future.result())
    image_audits.sort(key=lambda item: item["url"])
    image_audit_by_url = {item["url"]: item for item in image_audits}

    existing_handles = {item.get("handle") for item in existing_articles}
    imported_handles = {item["proposedDraftInput"]["handle"] for item in article_rows}
    handle_conflicts = sorted(existing_handles & imported_handles)
    pending_article_rows = [
        item
        for item in article_rows
        if item["proposedDraftInput"]["handle"] not in existing_handles
    ]
    inaccessible_hero_images = [
        url for url in hero_urls if not image_audit_by_url[url]["accessible"]
    ]
    inaccessible_inline_images = [
        url for url in inline_urls if not image_audit_by_url[url]["accessible"]
    ]
    unresolved_by_path = Counter(item["href"] for item in all_unresolved)
    summary = {
        "keptArticles": len(article_rows),
        "removedArticles": len(removed),
        "existingShopifyBlogs": len(blogs),
        "existingShopifyArticles": len(existing_articles),
        "targetBlogFound": bool(target_blog),
        "targetBlogHandleChangeRequired": bool(
            target_blog and target_blog.get("handle") != args.target_blog_handle
        ),
        "articleHandleConflicts": len(handle_conflicts),
        "existingRetainedArticles": len(article_rows) - len(pending_article_rows),
        "pendingDraftImports": len(pending_article_rows),
        "heroImages": len(hero_urls),
        "accessibleHeroImages": len(hero_urls) - len(inaccessible_hero_images),
        "inlineImages": len(inline_urls),
        "accessibleInlineImages": len(inline_urls) - len(inaccessible_inline_images),
        "articlesWithInlineImages": sum(bool(item["inlineImageUrls"]) for item in article_rows),
        "rewrittenInternalLinks": len(all_rewrites),
        "unresolvedInternalLinks": len(all_unresolved),
        "uniqueUnresolvedInternalLinks": len(unresolved_by_path),
        "mojibakeOccurrences": sum(
            mojibake_count(item["proposedDraftInput"].get("title"))
            + mojibake_count(item["proposedDraftInput"].get("summary"))
            + mojibake_count(item["proposedDraftInput"].get("body"))
            for item in article_rows
        ),
    }
    if summary["mojibakeOccurrences"]:
        raise SystemExit(
            "Refusing to prepare a blog migration manifest with "
            f"{summary['mojibakeOccurrences']} mojibake occurrence(s)."
        )
    report = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "mode": "no_write_shopify_blog_migration_preparation",
        "shopifyWritesPerformed": False,
        "liveSiteWritesPerformed": False,
        "applySupported": False,
        "summary": summary,
        "blogPlan": {
            "reuseExistingBlog": bool(target_blog),
            "current": target_blog,
            "recommended": {
                "title": args.target_blog_title,
                "handle": args.target_blog_handle,
            },
            "reason": (
                "Using the arnhem handle preserves retained legacy article paths."
                if args.target_blog_handle == "arnhem"
                else (
                    f"Using the {args.target_blog_handle} handle matches the approved "
                    "Journal URL structure and requires redirects from retained legacy paths."
                )
            ),
            "requiresExplicitApproval": True,
        },
        "existingArticlesRequiringDecision": existing_articles,
        "handleConflicts": handle_conflicts,
        "imageAudit": {
            "inaccessibleHeroImages": inaccessible_hero_images,
            "inaccessibleInlineImages": inaccessible_inline_images,
            "results": image_audits,
        },
        "linkAudit": {
            "rewrites": all_rewrites,
            "unresolved": all_unresolved,
            "unresolvedCounts": dict(sorted(unresolved_by_path.items())),
        },
        "removedArticles": removed,
        "articles": article_rows,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "blog-migration-preflight.json"
    manifest_path = output_dir / "blog-migration-manifest.json"
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    manifest_path.write_text(
        json.dumps(
            {
                "mode": report["mode"],
                "applySupported": False,
                "blogPlan": report["blogPlan"],
                "articles": pending_article_rows,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2))
    print("Shopify writes performed: false")
    print("Live-site writes performed: false")
    print(f"Preflight: {report_path}")
    print(f"Manifest: {manifest_path}")


if __name__ == "__main__":
    main()
