#!/usr/bin/env python3

import argparse
import hashlib
import html
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Set


DEFAULT_CONTEXT = Path(".tmp/product-description-review-context-20260811.json")
DEFAULT_PROPOSALS = Path(".tmp/product-description-proposals-20260811.json")
DEFAULT_VALIDATED = Path(".tmp/product-description-proposals-validated-20260811.json")
DEFAULT_MARKDOWN = Path(".tmp/product-description-proposals-20260811.md")
DEFAULT_HTML = Path(".tmp/product-description-proposals-20260811.html")
DEFAULT_IMAGES = Path(".tmp/product-description-images-20260811")
ALLOWED_TAGS = {"p", "strong", "ul", "li"}
FORBIDDEN_PHRASES = {
    "must-have",
    "game-changer",
    "revolutionary",
    "unbeatable",
    "ultimate essential",
    "perfect for every occasion",
    "discerning gentleman",
    "opulence",
    "exquisite creations",
    "hurry",
    "don't miss out",
}


def clean_text(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def word_count(value: str) -> int:
    return len(re.findall(r"\b[\w'+]+\b", clean_text(value)))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class TagCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: Set[str] = set()

    def handle_starttag(self, tag: str, attrs: List[Any]) -> None:
        self.tags.add(tag)

    def handle_startendtag(self, tag: str, attrs: List[Any]) -> None:
        self.tags.add(tag)


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def proposal_map(data: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    products = data.get("products") or []
    result: Dict[str, Dict[str, Any]] = {}
    for product in products:
        handle = str(product.get("handle") or "").strip()
        if not handle:
            raise ValueError("Proposal contains a product without a handle")
        if handle in result:
            raise ValueError(f"Duplicate proposal handle: {handle}")
        result[handle] = product
    return result


def validate(
    context: Dict[str, Any], proposals: Dict[str, Any]
) -> tuple[List[Dict[str, Any]], List[str]]:
    context_products = {
        product["handle"]: product for product in context.get("products") or []
    }
    proposed = proposal_map(proposals)
    errors: List[str] = []

    missing = sorted(set(context_products) - set(proposed))
    extra = sorted(set(proposed) - set(context_products))
    if missing:
        errors.append(f"Missing proposals: {', '.join(missing)}")
    if extra:
        errors.append(f"Unexpected proposals: {', '.join(extra)}")

    seen_copy: Dict[str, str] = {}
    validated: List[Dict[str, Any]] = []
    for handle in context_products:
        if handle not in proposed:
            continue
        context_product = context_products[handle]
        proposal = proposed[handle]
        body = str(proposal.get("bodyHtml") or "").strip()
        text = clean_text(body)
        count = word_count(body)
        collector = TagCollector()
        collector.feed(body)
        disallowed = sorted(collector.tags - ALLOWED_TAGS)

        if count < 80:
            errors.append(f"{handle}: only {count} words")
        if disallowed:
            errors.append(f"{handle}: disallowed HTML tags: {', '.join(disallowed)}")
        if "\u2013" in body or "\u2014" in body:
            errors.append(f"{handle}: contains an en dash or em dash")
        lower = text.lower()
        for phrase in FORBIDDEN_PHRASES:
            if phrase in lower:
                errors.append(f"{handle}: contains forbidden phrase '{phrase}'")
        normalized = re.sub(r"\s+", " ", lower)
        if normalized in seen_copy:
            errors.append(f"{handle}: duplicates copy for {seen_copy[normalized]}")
        seen_copy[normalized] = handle
        if not proposal.get("sourceUrls"):
            errors.append(f"{handle}: no source URLs recorded")

        validated.append(
            {
                **proposal,
                "title": context_product["title"],
                "vendor": context_product["vendor"],
                "issues": context_product["issues"],
                "currentDescriptionHtml": context_product["currentDescriptionHtml"],
                "currentDescriptionSha256": sha256_text(
                    context_product["currentDescriptionHtml"]
                ),
                "storefrontUrl": context_product["storefrontUrl"],
                "imageUrl": (context_product.get("images") or [{}])[0].get("src", ""),
                "wordCount": count,
            }
        )
    return validated, errors


def local_image_uri(images_dir: Path, handle: str, fallback: str) -> str:
    for extension in ("jpg", "jpeg", "png", "webp"):
        candidate = images_dir / f"{handle}.{extension}"
        if candidate.exists():
            return candidate.resolve().as_uri()
    return fallback


def render_markdown(products: List[Dict[str, Any]]) -> str:
    lines = [
        "# Product description proposals",
        "",
        "> Review only. Nothing in this file has been written to Shopify.",
        "",
        f"Products: **{len(products)}**",
        "",
    ]
    for product in products:
        lines.extend(
            [
                f"## {product['title']}",
                "",
                f"- Handle: `{product['handle']}`",
                f"- Vendor: `{product['vendor']}`",
                f"- Words: `{product['wordCount']}`",
                f"- Issues replaced: `{', '.join(product['issues'])}`",
                "",
                clean_text(product["bodyHtml"]),
                "",
                "Sources:",
                *[f"- {url}" for url in product["sourceUrls"]],
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def render_html(products: List[Dict[str, Any]], images_dir: Path) -> str:
    rows = []
    for index, product in enumerate(products, start=1):
        image_uri = local_image_uri(images_dir, product["handle"], product["imageUrl"])
        sources = "".join(
            f'<li><a href="{html.escape(url, quote=True)}">{html.escape(url)}</a></li>'
            for url in product["sourceUrls"]
        )
        current = product["currentDescriptionHtml"] or "<p><em>No current description.</em></p>"
        rows.append(
            f"""
            <section class="product" id="{html.escape(product['handle'])}">
              <div class="media">
                <img src="{html.escape(image_uri, quote=True)}" alt="{html.escape(product['title'])}">
              </div>
              <div class="copy">
                <p class="index">{index:02d} / {len(products):02d}</p>
                <h2>{html.escape(product['title'])}</h2>
                <p class="meta">{html.escape(product['vendor'])} | {product['wordCount']} words | {html.escape(', '.join(product['issues']))}</p>
                <h3>Proposed description</h3>
                <div class="description">{product['bodyHtml']}</div>
                <details>
                  <summary>Current description and sources</summary>
                  <h3>Current description</h3>
                  <div class="current">{current}</div>
                  <h3>Sources</h3>
                  <ul class="sources">{sources}</ul>
                  <p><a href="{html.escape(product['storefrontUrl'], quote=True)}">Open current Shopify product</a></p>
                </details>
              </div>
            </section>
            """
        )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Neighbourhood product copy review</title>
  <style>
    :root {{ color-scheme: light; --ink: #171717; --muted: #696969; --line: #d8d8d4; --paper: #f7f7f4; --accent: #7b201b; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; color: var(--ink); background: var(--paper); font: 16px/1.55 Georgia, 'Times New Roman', serif; }}
    header {{ padding: 48px max(24px, calc((100vw - 1240px) / 2)); border-top: 3px solid var(--accent); border-bottom: 1px solid var(--line); background: #fff; }}
    h1, h2, h3, p {{ margin-top: 0; }}
    h1 {{ max-width: 780px; margin-bottom: 10px; font-size: 56px; line-height: 1.05; font-weight: 500; }}
    h2 {{ margin-bottom: 6px; font-size: 30px; line-height: 1.15; font-weight: 500; }}
    h3 {{ margin: 26px 0 10px; font: 700 13px/1.2 Arial, sans-serif; text-transform: uppercase; }}
    .notice, .meta, .index {{ color: var(--muted); font-family: Arial, sans-serif; }}
    .notice {{ max-width: 720px; margin-bottom: 0; }}
    .product {{ display: grid; grid-template-columns: minmax(260px, 0.8fr) minmax(420px, 1.2fr); gap: 56px; max-width: 1240px; margin: 0 auto; padding: 56px 24px; border-bottom: 1px solid var(--line); }}
    .media {{ align-self: start; aspect-ratio: 4 / 5; background: #ecece8; overflow: hidden; }}
    .media img {{ width: 100%; height: 100%; object-fit: contain; display: block; }}
    .copy {{ max-width: 720px; }}
    .index, .meta {{ font-size: 12px; }}
    .index {{ margin-bottom: 18px; color: var(--accent); }}
    .description ul {{ padding-left: 20px; }}
    details {{ margin-top: 28px; padding-top: 18px; border-top: 1px solid var(--line); }}
    summary {{ cursor: pointer; font: 700 13px/1.2 Arial, sans-serif; }}
    .current {{ color: var(--muted); }}
    .sources {{ padding-left: 20px; overflow-wrap: anywhere; }}
    a {{ color: var(--accent); }}
    @media (max-width: 760px) {{
      header {{ padding-top: 32px; padding-bottom: 32px; }}
      .product {{ grid-template-columns: 1fr; gap: 28px; padding-top: 36px; padding-bottom: 40px; }}
      .media {{ max-height: 520px; }}
      h1 {{ max-width: 330px; font-size: 34px; }}
      .notice {{ max-width: 330px; }}
      h2 {{ font-size: 26px; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>Neighbourhood product copy review</h1>
    <p class="notice">18 English product descriptions for editorial review. Nothing has been written to Shopify.</p>
  </header>
  <main>{''.join(rows)}</main>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate product description proposals and render review files."
    )
    parser.add_argument("--context", type=Path, default=DEFAULT_CONTEXT)
    parser.add_argument("--proposals", type=Path, default=DEFAULT_PROPOSALS)
    parser.add_argument("--validated-output", type=Path, default=DEFAULT_VALIDATED)
    parser.add_argument("--markdown-output", type=Path, default=DEFAULT_MARKDOWN)
    parser.add_argument("--html-output", type=Path, default=DEFAULT_HTML)
    parser.add_argument("--images-dir", type=Path, default=DEFAULT_IMAGES)
    args = parser.parse_args()

    context = load_json(args.context)
    proposals = load_json(args.proposals)
    products, errors = validate(context, proposals)
    if errors:
        raise SystemExit("Validation failed:\n- " + "\n- ".join(errors))

    args.validated_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.html_output.parent.mkdir(parents=True, exist_ok=True)
    validated_report = {
        "schemaVersion": 1,
        "mode": "validated_editorial_review_only",
        "shopifyWritePerformed": False,
        "contextGeneratedAt": context.get("generatedAt"),
        "products": products,
    }
    args.validated_output.write_text(
        json.dumps(validated_report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    args.markdown_output.write_text(render_markdown(products), encoding="utf-8")
    args.html_output.write_text(render_html(products, args.images_dir), encoding="utf-8")
    print(f"Validated products: {len(products)}")
    print(f"Total words: {sum(product['wordCount'] for product in products)}")
    print(f"Validated JSON: {args.validated_output.resolve()}")
    print(f"Markdown: {args.markdown_output.resolve()}")
    print(f"HTML: {args.html_output.resolve()}")


if __name__ == "__main__":
    main()
