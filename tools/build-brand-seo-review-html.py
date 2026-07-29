#!/usr/bin/env python3

import argparse
import html
import json
import re
from collections import Counter
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_REVIEW_FILE = (
    Path("tools") / "data" / "shopify-brand-content-review-v2.json"
)
DEFAULT_SOURCE_PLAN = (
    Path(".tmp") / "phase3-brand-content-plan-20260725.json"
)
DEFAULT_OUTPUT_FILE = Path("brand-seo-review-20260725.html")
DEFAULT_VALIDATION_FILE = (
    Path(".tmp") / "brand-seo-review-validation-20260725.json"
)
ALLOWED_TAGS = {"p", "br", "em", "a"}
STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "has",
    "in",
    "is",
    "it",
    "its",
    "of",
    "on",
    "or",
    "our",
    "that",
    "the",
    "their",
    "these",
    "this",
    "through",
    "to",
    "while",
    "with",
}
DUTCH_WORDS = re.compile(
    r"\b(?:collectie|ontdek|producten|winkel|onze|eigen|kleding)\b",
    re.IGNORECASE,
)
HYPE_PHRASES = (
    "ultimate destination",
    "discerning gentleman",
    "elevate your wardrobe",
    "shop now",
    "high-end fashion",
    "unparalleled",
    "exquisite",
    "opulence",
    "epitome of",
)


class HtmlAuditParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: List[str] = []
        self.stack: List[str] = []
        self.errors: List[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: List[Tuple[str, Optional[str]]],
    ) -> None:
        normalized = tag.lower()
        self.tags.append(normalized)
        if normalized != "br":
            self.stack.append(normalized)

    def handle_startendtag(
        self,
        tag: str,
        attrs: List[Tuple[str, Optional[str]]],
    ) -> None:
        self.tags.append(tag.lower())

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if not self.stack or self.stack[-1] != normalized:
            self.errors.append(f"mismatched closing tag: {normalized}")
            return
        self.stack.pop()


def meaningful_text(value: str) -> str:
    return re.sub(
        r"\s+",
        " ",
        html.unescape(re.sub(r"<[^>]+>", " ", value or "")),
    ).strip()


def words(value: str) -> List[str]:
    return re.findall(r"[A-Za-z0-9]+(?:['+&][A-Za-z0-9]+)*", value)


def word_count(value: str) -> int:
    return len(words(value))


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def top_content_word(value: str) -> Tuple[str, int]:
    counts = Counter(
        token.casefold()
        for token in words(value)
        if len(token) > 2 and token.casefold() not in STOP_WORDS
    )
    return counts.most_common(1)[0] if counts else ("", 0)


def validate_review(
    review: Dict[str, Any],
    source_plan: Dict[str, Any],
) -> Dict[str, Any]:
    errors: List[str] = []
    warnings: List[str] = []
    if review.get("schemaVersion") != 2:
        errors.append("Review schemaVersion must be 2.")
    if review.get("status") != "pending_approval":
        errors.append("Review status must remain pending_approval.")
    if review.get("language") != "en":
        errors.append("Customer-facing proposals must be English.")

    source_by_handle = {
        item["handle"]: item for item in source_plan.get("actions") or []
    }
    proposed = review.get("collections") or {}
    if set(proposed) != set(source_by_handle):
        missing = sorted(set(source_by_handle) - set(proposed))
        extra = sorted(set(proposed) - set(source_by_handle))
        errors.append(
            f"Handle mismatch. Missing: {missing}; extra: {extra}"
        )

    title_handles: Dict[str, str] = {}
    meta_handles: Dict[str, str] = {}
    rows: List[Dict[str, Any]] = []
    verdict_counts: Counter[str] = Counter()
    for handle, item in sorted(proposed.items()):
        source = source_by_handle.get(handle)
        if not source:
            continue
        title = (item.get("seoTitle") or "").strip()
        meta = (item.get("metaDescription") or "").strip()
        body_html = item.get("descriptionHtml") or ""
        body_text = meaningful_text(body_html.replace("-//-", " "))
        old_html = source["expected"]["descriptionHtml"]
        current_html = source["desired"]["descriptionHtml"]
        brand = source["expected"]["title"]
        verdict = item.get("verdict") or ""
        verdict_counts[verdict] += 1

        parser = HtmlAuditParser()
        parser.feed(body_html)
        invalid_tags = sorted(set(parser.tags) - ALLOWED_TAGS)
        if invalid_tags:
            errors.append(
                f"{handle}: invalid tags: {', '.join(invalid_tags)}"
            )
        if parser.errors or parser.stack:
            errors.append(f"{handle}: malformed HTML")
        if body_html.count("<p>-//-</p>") != 1:
            errors.append(
                f"{handle}: exactly one Shopify delimiter is required"
            )
        if "<strong" in body_html.lower() or "<b" in body_html.lower():
            errors.append(f"{handle}: bold HTML is not allowed")
        if any(mark in body_html for mark in ("\u2013", "\u2014")):
            errors.append(f"{handle}: en or em dash is not allowed")
        if not body_text:
            errors.append(f"{handle}: description is blank")
        if brand.casefold() not in body_text.casefold():
            warnings.append(
                f"{handle}: exact collection title is not present in copy"
            )
        if DUTCH_WORDS.search(body_text) or DUTCH_WORDS.search(title):
            errors.append(f"{handle}: Dutch wording detected")
        matched_hype = [
            phrase for phrase in HYPE_PHRASES
            if phrase in body_text.casefold()
        ]
        if matched_hype:
            errors.append(
                f"{handle}: hype language remains: {matched_hype}"
            )
        if len(title) >= 60:
            warnings.append(
                f"{handle}: SEO title length is {len(title)} characters"
            )
        if len(meta) >= 120:
            warnings.append(
                f"{handle}: meta description length is {len(meta)} characters"
            )
        if title.casefold() in title_handles:
            errors.append(
                f"{handle}: duplicate title with "
                f"{title_handles[title.casefold()]}"
            )
        if meta.casefold() in meta_handles:
            errors.append(
                f"{handle}: duplicate meta with "
                f"{meta_handles[meta.casefold()]}"
            )
        title_handles[title.casefold()] = handle
        meta_handles[meta.casefold()] = handle

        top_word, top_count = top_content_word(body_text)
        total_words = word_count(body_text)
        density = top_count / total_words if total_words else 0
        if density > 0.08 and top_count >= 6:
            warnings.append(
                f"{handle}: repeated word '{top_word}' has "
                f"{density:.1%} density"
            )

        rows.append(
            {
                "handle": handle,
                "brand": brand,
                "verdict": verdict,
                "oldScore": item.get("oldScore"),
                "oldWords": word_count(meaningful_text(old_html)),
                "currentWords": word_count(meaningful_text(current_html)),
                "proposedWords": total_words,
                "seoTitleCharacters": len(title),
                "metaDescriptionCharacters": len(meta),
                "topRepeatedWord": top_word,
                "topRepeatedWordCount": top_count,
                "topRepeatedWordDensity": round(density, 4),
            }
        )

    return {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "mode": "local_validation_only",
        "shopifyWritePerformed": False,
        "summary": {
            "collections": len(rows),
            "errors": len(errors),
            "warnings": len(warnings),
            "verdicts": dict(sorted(verdict_counts.items())),
            "uniqueSeoTitles": len(title_handles),
            "uniqueMetaDescriptions": len(meta_handles),
        },
        "errors": errors,
        "warnings": warnings,
        "collections": rows,
    }


def paragraph_blocks(body_html: str) -> Tuple[str, str]:
    parts = body_html.split("<p>-//-</p>", maxsplit=1)
    return parts[0].strip(), parts[1].strip() if len(parts) > 1 else ""


def render_list(items: List[str]) -> str:
    if not items:
        return "<p class=\"empty\">None.</p>"
    return "<ul>" + "".join(
        f"<li>{html.escape(item)}</li>" for item in items
    ) + "</ul>"


def build_html(
    review: Dict[str, Any],
    source_plan: Dict[str, Any],
    validation: Dict[str, Any],
) -> str:
    source_by_handle = {
        item["handle"]: item for item in source_plan["actions"]
    }
    verdicts = validation["summary"]["verdicts"]
    nav_options = [
        ("all", "All", validation["summary"]["collections"]),
        *[
            (key, key.replace("_", " ").title(), count)
            for key, count in verdicts.items()
        ],
    ]
    filters = "".join(
        (
            f'<button class="filter-button{" is-active" if key == "all" else ""}" '
            f'data-filter="{html.escape(key)}">{html.escape(label)} '
            f'<span>{count}</span></button>'
        )
        for key, label, count in nav_options
    )

    articles: List[str] = []
    for handle, item in sorted(
        review["collections"].items(),
        key=lambda pair: source_by_handle[pair[0]]["expected"]["title"].casefold(),
    ):
        source = source_by_handle[handle]
        old_html = source["expected"]["descriptionHtml"]
        current_html = source["desired"]["descriptionHtml"]
        proposal_html = item["descriptionHtml"]
        intro_html, supporting_html = paragraph_blocks(proposal_html)
        metrics = next(
            row
            for row in validation["collections"]
            if row["handle"] == handle
        )
        code_id = f"code-{handle}"
        articles.append(
            f"""
            <article class="brand-review"
              id="{html.escape(handle)}"
              data-verdict="{html.escape(item['verdict'])}"
              data-search="{html.escape((source['expected']['title'] + ' ' + handle).casefold())}">
              <header class="brand-header">
                <div>
                  <p class="handle">/collections/{html.escape(handle)}</p>
                  <h2>{html.escape(source['expected']['title'])}</h2>
                </div>
                <div class="header-metrics">
                  <span class="verdict verdict-{html.escape(item['verdict'])}">{html.escape(item['verdict'].replace('_', ' ').title())}</span>
                  <span class="score">Old SEO quality: {item['oldScore']}/5</span>
                </div>
              </header>

              <div class="assessment-grid">
                <section>
                  <h3>Search intent</h3>
                  <p>{html.escape(item['searchIntent'])}</p>
                </section>
                <section>
                  <h3>Useful elements retained</h3>
                  {render_list(item['retainedFromOld'])}
                </section>
                <section>
                  <h3>SEO assessment</h3>
                  {render_list(item['issues'])}
                </section>
              </div>

              <details class="comparison">
                <summary>Compare old and currently applied copy</summary>
                <div class="comparison-grid">
                  <section>
                    <h3>Old collection copy <span>{metrics['oldWords']} words</span></h3>
                    <div class="copy-text">{old_html or '<p class="empty">Blank.</p>'}</div>
                  </section>
                  <section>
                    <h3>Currently applied copy <span>{metrics['currentWords']} words</span></h3>
                    <div class="copy-text">{current_html or '<p class="empty">Blank.</p>'}</div>
                  </section>
                </div>
              </details>

              <section class="proposal">
                <div class="section-heading">
                  <div>
                    <p class="eyebrow">Pending approval</p>
                    <h3>Proposed collection copy</h3>
                  </div>
                  <span>{metrics['proposedWords']} words</span>
                </div>
                <div class="storefront-preview">
                  <div class="preview-block">
                    <span class="preview-label">Above product grid</span>
                    {intro_html}
                  </div>
                  <div class="grid-placeholder" aria-label="Product grid position">
                    <span>Product grid</span>
                  </div>
                  <div class="preview-block supporting">
                    <span class="preview-label">Below product grid</span>
                    {supporting_html}
                  </div>
                </div>
              </section>

              <section class="metadata">
                <h3>Search appearance proposal</h3>
                <dl>
                  <div>
                    <dt>SEO title <span>{metrics['seoTitleCharacters']} characters</span></dt>
                    <dd>{html.escape(item['seoTitle'])}</dd>
                  </div>
                  <div>
                    <dt>Meta description <span>{metrics['metaDescriptionCharacters']} characters</span></dt>
                    <dd>{html.escape(item['metaDescription'])}</dd>
                  </div>
                </dl>
              </section>

              <section class="html-source">
                <div class="section-heading">
                  <h3>Shopify HTML</h3>
                  <button class="copy-button" data-copy="{code_id}" type="button">Copy HTML</button>
                </div>
                <pre><code id="{code_id}">{html.escape(proposal_html)}</code></pre>
              </section>
            </article>
            """
        )

    generated = datetime.now().astimezone().strftime("%d %B %Y, %H:%M")
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Neighbourhood Brand SEO Review</title>
  <style>
    :root {{
      --accent: #4a0404;
      --accent-soft: #f5eaea;
      --ink: #171717;
      --muted: #66625f;
      --line: #d9d7d3;
      --surface: #ffffff;
      --subtle: #f5f5f3;
      --success: #1d5b3a;
      --warning: #8a4f08;
      --info: #28546f;
    }}
    * {{ box-sizing: border-box; }}
    html {{ max-width: 100%; overflow-x: hidden; scroll-behavior: smooth; }}
    body {{
      max-width: 100%;
      margin: 0;
      overflow-x: hidden;
      color: var(--ink);
      background: #ececea;
      font-family: Arial, Helvetica, sans-serif;
      font-size: 15px;
      line-height: 1.55;
      letter-spacing: 0;
    }}
    button, input {{ font: inherit; letter-spacing: 0; }}
    a {{ color: var(--accent); }}
    p, li, dd, a {{ overflow-wrap: anywhere; }}
    .title-row > *, .method > *, .assessment-grid > *,
    .comparison-grid > *, .brand-header > * {{ min-width: 0; }}
    .app-header {{
      position: sticky;
      top: 0;
      z-index: 20;
      border-bottom: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.97);
    }}
    .header-inner {{
      width: min(1440px, calc(100% - 32px));
      margin: 0 auto;
      padding: 15px 0 12px;
    }}
    .title-row {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 24px;
    }}
    h1, h2, h3, p {{ margin-top: 0; }}
    h1 {{ margin-bottom: 4px; font-size: 24px; line-height: 1.2; }}
    h2 {{ margin-bottom: 0; font-size: 24px; line-height: 1.2; }}
    h3 {{ margin-bottom: 10px; font-size: 14px; line-height: 1.3; }}
    .subtitle {{ margin: 0; color: var(--muted); }}
    .approval-state {{
      flex: 0 0 auto;
      padding: 8px 10px;
      border: 1px solid #c89d9d;
      border-radius: 4px;
      color: var(--accent);
      background: var(--accent-soft);
      font-weight: 700;
      text-transform: uppercase;
      font-size: 12px;
    }}
    .toolbar {{
      display: flex;
      align-items: center;
      gap: 8px;
      margin-top: 14px;
      overflow-x: auto;
      padding-bottom: 2px;
    }}
    .filter-button, .copy-button {{
      min-height: 36px;
      border: 1px solid var(--line);
      border-radius: 4px;
      background: var(--surface);
      color: var(--ink);
      cursor: pointer;
      white-space: nowrap;
    }}
    .filter-button {{ padding: 7px 10px; }}
    .filter-button span {{ color: var(--muted); margin-left: 4px; }}
    .filter-button.is-active {{
      border-color: var(--accent);
      color: #fff;
      background: var(--accent);
    }}
    .filter-button.is-active span {{ color: #fff; }}
    .search {{
      min-width: 220px;
      min-height: 36px;
      margin-left: auto;
      border: 1px solid var(--line);
      border-radius: 4px;
      padding: 7px 10px;
      background: #fff;
    }}
    main {{
      width: min(1440px, calc(100% - 32px));
      margin: 20px auto 56px;
    }}
    .method {{
      display: grid;
      grid-template-columns: 1.5fr 1fr;
      gap: 24px;
      padding: 20px;
      margin-bottom: 18px;
      border: 1px solid var(--line);
      border-left: 4px solid var(--accent);
      background: var(--surface);
    }}
    .method p:last-child, .method ul:last-child {{ margin-bottom: 0; }}
    .method ul {{ margin: 0; padding-left: 20px; }}
    .source-links {{ color: var(--muted); }}
    .brand-review {{
      margin-bottom: 18px;
      border: 1px solid var(--line);
      background: var(--surface);
    }}
    .brand-review[hidden] {{ display: none; }}
    .brand-header {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 20px;
      padding: 18px 20px;
      border-bottom: 1px solid var(--line);
    }}
    .handle {{
      margin-bottom: 4px;
      color: var(--muted);
      font-family: Consolas, monospace;
      font-size: 12px;
    }}
    .header-metrics {{ display: flex; align-items: center; gap: 8px; }}
    .verdict, .score {{
      padding: 5px 8px;
      border-radius: 4px;
      font-size: 12px;
      font-weight: 700;
      white-space: nowrap;
    }}
    .verdict-rewrite, .verdict-write {{
      color: var(--accent);
      background: var(--accent-soft);
    }}
    .verdict-refine {{ color: var(--warning); background: #fff1dc; }}
    .verdict-expand {{ color: var(--info); background: #e8f2f7; }}
    .score {{ color: var(--muted); background: var(--subtle); }}
    .assessment-grid {{
      display: grid;
      grid-template-columns: 1fr 1.25fr 1.5fr;
      gap: 0;
      border-bottom: 1px solid var(--line);
    }}
    .assessment-grid section {{
      padding: 18px 20px;
      border-right: 1px solid var(--line);
    }}
    .assessment-grid section:last-child {{ border-right: 0; }}
    .assessment-grid p, .assessment-grid ul {{ margin-bottom: 0; }}
    ul {{ padding-left: 19px; }}
    .comparison {{ border-bottom: 1px solid var(--line); }}
    summary {{
      padding: 14px 20px;
      cursor: pointer;
      font-weight: 700;
      background: var(--subtle);
    }}
    .comparison-grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      border-top: 1px solid var(--line);
    }}
    .comparison-grid section {{ padding: 18px 20px; }}
    .comparison-grid section + section {{ border-left: 1px solid var(--line); }}
    .comparison-grid h3 span, .section-heading > span, dt span {{
      color: var(--muted);
      font-weight: 400;
    }}
    .copy-text {{ max-height: 280px; overflow: auto; color: #4c4946; }}
    .copy-text p:last-child {{ margin-bottom: 0; }}
    .proposal, .metadata, .html-source {{
      padding: 20px;
      border-bottom: 1px solid var(--line);
    }}
    .html-source {{ border-bottom: 0; }}
    .section-heading {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 12px;
    }}
    .section-heading h3 {{ margin-bottom: 0; }}
    .eyebrow {{
      margin-bottom: 4px;
      color: var(--accent);
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
    }}
    .storefront-preview {{ border: 1px solid var(--line); }}
    .preview-block {{ position: relative; padding: 24px 26px 18px; }}
    .preview-block p {{ max-width: 760px; margin-bottom: 0; }}
    .preview-block.supporting p {{ max-width: 680px; margin-left: auto; }}
    .preview-label {{
      display: block;
      margin-bottom: 9px;
      color: var(--muted);
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
    }}
    .grid-placeholder {{
      display: grid;
      place-items: center;
      min-height: 90px;
      border-top: 1px solid var(--line);
      border-bottom: 1px solid var(--line);
      color: #85817c;
      background-color: #f1f1ef;
      background-image:
        linear-gradient(#dededb 1px, transparent 1px),
        linear-gradient(90deg, #dededb 1px, transparent 1px);
      background-size: 33.333% 100%;
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
    }}
    .metadata dl {{ margin: 0; }}
    .metadata dl > div {{
      display: grid;
      grid-template-columns: 180px 1fr;
      gap: 16px;
      padding: 10px 0;
      border-top: 1px solid var(--line);
    }}
    dt {{ font-weight: 700; }}
    dt span {{ display: block; font-size: 12px; }}
    dd {{ margin: 0; }}
    .copy-button {{ padding: 7px 11px; }}
    .copy-button:hover {{ border-color: var(--accent); color: var(--accent); }}
    pre {{
      max-height: 240px;
      margin: 0;
      overflow: auto;
      border: 1px solid #2c2c2c;
      border-radius: 4px;
      padding: 14px;
      color: #f5f5f5;
      background: #202020;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }}
    code {{ font-family: Consolas, monospace; font-size: 12px; }}
    .empty {{ color: var(--muted); font-style: italic; }}
    .footer-note {{
      text-align: center;
      color: var(--muted);
      font-size: 12px;
    }}
    @media (max-width: 900px) {{
      .title-row, .brand-header {{ align-items: stretch; flex-direction: column; }}
      .approval-state {{ align-self: flex-start; }}
      .search {{ order: -1; min-width: 180px; margin-left: 0; }}
      .method, .assessment-grid, .comparison-grid {{ grid-template-columns: 1fr; }}
      .assessment-grid section, .comparison-grid section + section {{
        border-right: 0;
        border-left: 0;
        border-top: 1px solid var(--line);
      }}
      .assessment-grid section:first-child {{ border-top: 0; }}
      .metadata dl > div {{ grid-template-columns: 1fr; gap: 5px; }}
    }}
    @media (max-width: 560px) {{
      .header-inner, main {{ width: min(100% - 20px, 1440px); }}
      h1, h2 {{ font-size: 20px; }}
      .toolbar {{
        display: grid;
        grid-template-columns: repeat(3, minmax(0, 1fr));
        overflow: visible;
      }}
      .filter-button {{ width: 100%; padding-left: 5px; padding-right: 5px; }}
      .search {{ grid-column: 1 / -1; width: 100%; }}
      .header-metrics {{ align-items: flex-start; flex-direction: column; }}
      .brand-header, .assessment-grid section, .proposal, .metadata, .html-source {{
        padding-left: 14px;
        padding-right: 14px;
      }}
      .preview-block {{ padding: 20px 16px 16px; }}
    }}
  </style>
</head>
<body>
  <header class="app-header">
    <div class="header-inner">
      <div class="title-row">
        <div>
          <h1>Neighbourhood brand SEO review</h1>
          <p class="subtitle">Old content, expert assessment and revised Shopify HTML. Generated {html.escape(generated)}.</p>
        </div>
        <div class="approval-state">Pending approval. No Shopify write.</div>
      </div>
      <div class="toolbar" aria-label="Review filters">
        {filters}
        <input class="search" id="brand-search" type="search" placeholder="Search brand" aria-label="Search brand">
      </div>
    </div>
  </header>

  <main>
    <section class="method">
      <div>
        <h2>Review method</h2>
        {render_list(review['method']['principles'])}
      </div>
      <div>
        <h3>Validation</h3>
        <p><strong>{validation['summary']['collections']} collections</strong>, {validation['summary']['uniqueSeoTitles']} unique titles, {validation['summary']['uniqueMetaDescriptions']} unique meta descriptions, {validation['summary']['errors']} errors.</p>
        <p class="source-links">Criteria: <a href="https://developers.google.com/search/docs/fundamentals/creating-helpful-content">Google people-first content</a>, <a href="https://developers.google.com/search/docs/appearance/title-link">title links</a>, and <a href="https://developers.google.com/search/docs/appearance/snippet">meta descriptions</a>.</p>
        <p>This file is for approval only. Its generator has no Shopify API code.</p>
      </div>
    </section>

    <div id="reviews">
      {''.join(articles)}
    </div>
    <p class="footer-note">Neighbourhood internal review. Nothing in this document has been published by the review generator.</p>
  </main>

  <script>
    const filters = [...document.querySelectorAll('.filter-button')];
    const search = document.getElementById('brand-search');
    const reviews = [...document.querySelectorAll('.brand-review')];
    let activeFilter = 'all';

    function updateReviews() {{
      const query = search.value.trim().toLowerCase();
      reviews.forEach((review) => {{
        const matchesFilter = activeFilter === 'all' || review.dataset.verdict === activeFilter;
        const matchesSearch = !query || review.dataset.search.includes(query);
        review.hidden = !(matchesFilter && matchesSearch);
      }});
    }}

    filters.forEach((button) => {{
      button.addEventListener('click', () => {{
        activeFilter = button.dataset.filter;
        filters.forEach((item) => item.classList.toggle('is-active', item === button));
        updateReviews();
      }});
    }});
    search.addEventListener('input', updateReviews);

    document.querySelectorAll('.copy-button').forEach((button) => {{
      button.addEventListener('click', async () => {{
        const code = document.getElementById(button.dataset.copy);
        await navigator.clipboard.writeText(code.textContent);
        const original = button.textContent;
        button.textContent = 'Copied';
        setTimeout(() => {{ button.textContent = original; }}, 1200);
      }});
    }});
  </script>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build a local HTML approval report for proposed brand SEO copy. "
            "This tool has no Shopify API integration."
        )
    )
    parser.add_argument(
        "--review-file",
        type=Path,
        default=DEFAULT_REVIEW_FILE,
    )
    parser.add_argument(
        "--source-plan",
        type=Path,
        default=DEFAULT_SOURCE_PLAN,
    )
    parser.add_argument(
        "--output-file",
        type=Path,
        default=DEFAULT_OUTPUT_FILE,
    )
    parser.add_argument(
        "--validation-file",
        type=Path,
        default=DEFAULT_VALIDATION_FILE,
    )
    args = parser.parse_args()

    review = load_json(args.review_file)
    source_plan = load_json(args.source_plan)
    validation = validate_review(review, source_plan)
    args.validation_file.parent.mkdir(parents=True, exist_ok=True)
    args.validation_file.write_text(
        json.dumps(validation, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    if validation["errors"]:
        raise SystemExit(
            "Review validation failed:\n- "
            + "\n- ".join(validation["errors"])
        )

    output = build_html(review, source_plan, validation)
    args.output_file.parent.mkdir(parents=True, exist_ok=True)
    args.output_file.write_text(output, encoding="utf-8")
    print(json.dumps(validation["summary"], indent=2))
    if validation["warnings"]:
        print("Warnings:")
        for warning in validation["warnings"]:
            print(f"- {warning}")
    print(f"HTML review: {args.output_file.resolve()}")
    print(f"Validation: {args.validation_file.resolve()}")
    print("Shopify writes: 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
