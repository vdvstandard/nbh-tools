#!/usr/bin/env python3
"""Build a local review inventory of live legacy blog articles using GET only."""

import argparse
import csv
import html
import json
import re
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path


DEFAULT_CRAWL_REPORT = (
    ".tmp/phase8-existing-site-crawl-20260805/existing-site-crawl.json"
)
DEFAULT_OUTPUT_DIR = ".tmp/phase8-blog-review-20260805"
ARTICLE_PATH = re.compile(r"^/blogs/arnhem/[^/]+/$")
VOID_TAGS = {"br", "hr", "img", "source"}
ALLOWED_BODY_TAGS = {
    "a",
    "b",
    "blockquote",
    "br",
    "em",
    "figure",
    "figcaption",
    "h2",
    "h3",
    "h4",
    "hr",
    "i",
    "iframe",
    "img",
    "li",
    "ol",
    "p",
    "strong",
    "u",
    "ul",
}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Prepare a read-only review inventory of live legacy blogs."
    )
    parser.add_argument("--crawl-report", default=DEFAULT_CRAWL_REPORT)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--delay", type=float, default=0.1)
    return parser.parse_args()


def legacy_clean_text(value):
    text = html.unescape(str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    replacements = {
        "\u00e2\u20ac\u2122": "\u2019",
        "\u00e2\u20ac\u0153": "\u201c",
        "\u00e2\u20ac\u009d": "\u201d",
        "\u00e2\u20ac\u201c": "\u2013",
        "\u00e2\u20ac\u201d": "\u2014",
        "\u00c2\u00a0": " ",
    }
    for broken, repaired in replacements.items():
        text = text.replace(broken, repaired)
    if any(marker in text for marker in ("â€", "â€™", "Ã", "Â")):
        try:
            repaired = text.encode("cp1252").decode("utf-8")
            if sum(repaired.count(marker) for marker in ("â€", "â€™", "Ã", "Â")) < sum(
                text.count(marker) for marker in ("â€", "â€™", "Ã", "Â")
            ):
                text = repaired
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return text


MOJIBAKE_MARKERS = ("\u00e2\u20ac", "\u00c3", "\u00c2")


def mojibake_count(value):
    text = str(value or "")
    return sum(text.count(marker) for marker in MOJIBAKE_MARKERS)


def repair_mojibake(value):
    text = str(value or "")
    replacements = {
        "\u00e2\u20ac\u2122": "\u2019",
        "\u00e2\u20ac\u02dc": "\u2018",
        "\u00e2\u20ac\u0153": "\u201c",
        "\u00e2\u20ac\u009d": "\u201d",
        "\u00e2\u20ac\u201c": "\u2013",
        "\u00e2\u20ac\u201d": "\u2014",
        "\u00e2\u20ac\u00a6": "\u2026",
        "\u00c2\u00a0": " ",
    }
    for broken, repaired in replacements.items():
        text = text.replace(broken, repaired)
    if mojibake_count(text):
        try:
            repaired = text.encode("cp1252").decode("utf-8")
            if mojibake_count(repaired) < mojibake_count(text):
                text = repaired
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return text


def clean_text(value):
    text = repair_mojibake(html.unescape(str(value or "")))
    return re.sub(r"\s+", " ", text).strip()


def shorten(value, limit=260):
    text = clean_text(value)
    if len(text) <= limit:
        return text
    shortened = text[: limit + 1].rsplit(" ", 1)[0].rstrip(" ,;:")
    return f"{shortened}..."


class ArticleParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.date_parts = {}
        self.capture_date_part = None
        self.meta = {}
        self.canonical = None
        self.in_content = False
        self.content_depth = 0
        self.suppressed_depth = 0
        self.body_parts = []
        self.body_links = []
        self.body_images = []

    def sanitized_attributes(self, tag, attributes):
        allowed = {
            "a": {"href", "title"},
            "iframe": {"src", "title", "width", "height", "allow", "allowfullscreen"},
            "img": {"src", "alt", "title", "width", "height"},
        }.get(tag, set())
        if tag == "img" and not attributes.get("src") and attributes.get("data-src"):
            attributes["src"] = attributes["data-src"]
        return [
            (key, value)
            for key, value in attributes.items()
            if key in allowed and value
        ]

    def append_starttag(self, tag, attributes):
        if tag not in ALLOWED_BODY_TAGS:
            return
        clean_attributes = self.sanitized_attributes(tag, attributes)
        serialized = "".join(
            f' {key}="{html.escape(value, quote=True)}"'
            for key, value in clean_attributes
        )
        self.body_parts.append(f"<{tag}{serialized}>")
        if tag == "a" and attributes.get("href"):
            self.body_links.append(attributes["href"])
        if tag == "img" and attributes.get("src"):
            self.body_images.append(attributes["src"])

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attributes = {key.lower(): value or "" for key, value in attrs}
        classes = set(attributes.get("class", "").split())
        for part in ("day", "month", "year"):
            if f"article-date-{part}" in classes:
                self.capture_date_part = part
        if tag == "meta":
            key = (attributes.get("property") or attributes.get("name") or "").lower()
            content = attributes.get("content", "").strip()
            if key and content:
                self.meta[key] = content
        elif tag == "link" and "canonical" in attributes.get("rel", "").lower():
            self.canonical = attributes.get("href") or None
        if not self.in_content and tag == "section" and attributes.get("id") == "blog-content":
            self.in_content = True
            self.content_depth = 1
            return
        if not self.in_content:
            return
        if tag not in VOID_TAGS:
            self.content_depth += 1
        if tag in {"script", "style"}:
            self.suppressed_depth += 1
            return
        if not self.suppressed_depth:
            self.append_starttag(tag, attributes)

    def handle_startendtag(self, tag, attrs):
        if tag.lower() in {"meta", "link"}:
            self.handle_starttag(tag, attrs)
            return
        if not self.in_content or self.suppressed_depth:
            return
        attributes = {key.lower(): value or "" for key, value in attrs}
        self.append_starttag(tag.lower(), attributes)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "span":
            self.capture_date_part = None
        if not self.in_content:
            return
        if tag == "section" and self.content_depth == 1:
            self.in_content = False
            self.content_depth = 0
            return
        if self.suppressed_depth:
            if tag in {"script", "style"}:
                self.suppressed_depth -= 1
            if tag not in VOID_TAGS:
                self.content_depth = max(1, self.content_depth - 1)
            return
        if tag in ALLOWED_BODY_TAGS and tag not in VOID_TAGS:
            self.body_parts.append(f"</{tag}>")
        if tag not in VOID_TAGS:
            self.content_depth = max(1, self.content_depth - 1)

    def handle_data(self, data):
        if self.capture_date_part:
            value = clean_text(data)
            if value:
                self.date_parts[self.capture_date_part] = value
        if self.in_content and not self.suppressed_depth:
            self.body_parts.append(html.escape(repair_mojibake(data)))

    def body_html(self):
        value = "".join(self.body_parts).strip()
        return re.sub(r">\s+<", "><", value)

    def published_date(self):
        day = self.date_parts.get("day")
        month = self.date_parts.get("month")
        year = self.date_parts.get("year")
        if not all((day, month, year)):
            return None
        try:
            return datetime.strptime(f"{day} {month} {year}", "%d %b %Y").date().isoformat()
        except ValueError:
            return f"{day} {month} {year}"


def fetch_article(item, delay):
    if delay:
        time.sleep(delay)
    request = urllib.request.Request(
        item["url"],
        headers={"User-Agent": "Neighbourhood legacy blog inventory/1.0"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
            status = response.status
            content_type = response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as error:
        body = error.read()
        status = error.code
        content_type = error.headers.get("Content-Type", "")
    except urllib.error.URLError as error:
        return {**item, "liveStatus": None, "error": str(error.reason)}

    encoding = "utf-8"
    match = re.search(r"charset=([^;\s]+)", content_type, re.I)
    if match:
        encoding = match.group(1).strip('"\'')
    parser = ArticleParser()
    parser.feed(body.decode(encoding, errors="replace"))
    title = clean_text(item.get("title"))
    title = re.sub(r"\s+-\s+Neighbourhood$", "", title, flags=re.I)
    description = (
        parser.meta.get("description")
        or parser.meta.get("og:description")
        or item.get("description")
    )
    return {
        **item,
        "title": title,
        "summary": shorten(description),
        "publishedDate": parser.published_date(),
        "canonical": parser.canonical or item.get("canonical"),
        "heroImageUrl": parser.meta.get("og:image") or parser.meta.get("twitter:image"),
        "heroImageAlt": clean_text(parser.meta.get("og:image:alt") or title),
        "bodyHtml": parser.body_html(),
        "bodyLinks": sorted(set(parser.body_links)),
        "bodyImages": sorted(set(parser.body_images)),
        "liveStatus": status,
        "bytes": len(body),
        "error": None,
    }


def markdown_cell(value):
    return clean_text(value).replace("|", "\\|")


def write_csv(path, rows):
    fieldnames = [
        "review_id",
        "decision",
        "notes",
        "published_date",
        "title",
        "summary",
        "url",
        "path",
        "live_status",
        "canonical",
        "hero_image_url",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "review_id": row["reviewId"],
                    "decision": "",
                    "notes": "",
                    "published_date": row.get("publishedDate") or "",
                    "title": row.get("title") or "",
                    "summary": row.get("summary") or "",
                    "url": row["url"],
                    "path": row["path"],
                    "live_status": row.get("liveStatus") or "",
                    "canonical": row.get("canonical") or "",
                    "hero_image_url": row.get("heroImageUrl") or "",
                }
            )


def write_markdown(path, rows, summary):
    lines = [
        "# Legacy blog review",
        "",
        "Preparation only. No article was created, changed, published or removed.",
        "",
        f"Live articles: {summary['liveArticles']}",
        f"Fetch errors: {summary['errors']}",
        "",
        "Use `keep`, `remove` or `unsure` in the Decision column.",
        "",
        "| ID | Decision | Date | Title | Short summary | URL |",
        "|---:|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            "| {id} |  | {date} | {title} | {summary} | [open]({url}) |".format(
                id=row["reviewId"],
                date=markdown_cell(row.get("publishedDate") or "unknown"),
                title=markdown_cell(row.get("title")),
                summary=markdown_cell(row.get("summary")),
                url=row["url"],
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    args = parse_args()
    project_root = Path(__file__).resolve().parent.parent
    crawl_report_path = Path(args.crawl_report)
    output_dir = Path(args.output_dir)
    if not crawl_report_path.is_absolute():
        crawl_report_path = project_root / crawl_report_path
    if not output_dir.is_absolute():
        output_dir = project_root / output_dir
    if not crawl_report_path.exists():
        raise SystemExit(f"Crawl report not found: {crawl_report_path}")

    crawl = json.loads(crawl_report_path.read_text(encoding="utf-8"))
    article_seeds = [
        item
        for item in crawl.get("urls", [])
        if item.get("status") == 200
        and ARTICLE_PATH.fullmatch(item.get("path", ""))
        and item.get("path") != "/blogs/arnhem/"
    ]
    fetched = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(fetch_article, item, args.delay): item
            for item in article_seeds
        }
        for future in as_completed(futures):
            fetched.append(future.result())

    fetched.sort(
        key=lambda item: (
            item.get("publishedDate") or "0000-00-00",
            item.get("title") or "",
        ),
        reverse=True,
    )
    for index, item in enumerate(fetched, start=1):
        item["reviewId"] = index
        item["decision"] = ""
        item["notes"] = ""

    status_counts = Counter(str(item.get("liveStatus")) for item in fetched)
    year_counts = Counter(
        item["publishedDate"][:4]
        for item in fetched
        if item.get("publishedDate") and re.match(r"^\d{4}", item["publishedDate"])
    )
    summary = {
        "articleSeeds": len(article_seeds),
        "liveArticles": sum(item.get("liveStatus") == 200 for item in fetched),
        "errors": sum(bool(item.get("error")) for item in fetched),
        "missingDates": sum(not item.get("publishedDate") for item in fetched),
        "missingHeroImages": sum(not item.get("heroImageUrl") for item in fetched),
        "articlesWithBodyImages": sum(bool(item.get("bodyImages")) for item in fetched),
        "mojibakeOccurrences": sum(
            mojibake_count(item.get("title"))
            + mojibake_count(item.get("summary"))
            + mojibake_count(item.get("bodyHtml"))
            for item in fetched
        ),
        "statuses": dict(sorted(status_counts.items())),
        "articlesByYear": dict(sorted(year_counts.items(), reverse=True)),
    }
    if summary["mojibakeOccurrences"]:
        raise SystemExit(
            "Refusing to prepare blog review with "
            f"{summary['mojibakeOccurrences']} mojibake occurrence(s)."
        )
    report = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "mode": "read_only_legacy_blog_inventory",
        "httpMethods": ["GET"],
        "writesPerformed": False,
        "applySupported": False,
        "sourceCrawlReport": str(crawl_report_path),
        "summary": summary,
        "articles": fetched,
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "blog-review.json"
    csv_path = output_dir / "blog-review.csv"
    markdown_path = output_dir / "blog-review.md"
    json_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    write_csv(csv_path, fetched)
    write_markdown(markdown_path, fetched, summary)
    print(json.dumps(summary, indent=2))
    print("Writes performed: false")
    print(f"JSON: {json_path}")
    print(f"CSV: {csv_path}")
    print(f"Markdown: {markdown_path}")


if __name__ == "__main__":
    main()
