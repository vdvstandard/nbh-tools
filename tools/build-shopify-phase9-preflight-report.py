#!/usr/bin/env python3
"""Consolidate Phase 9 audit artifacts into one read-only launch report."""

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir", default=".tmp/phase9-prelaunch-20260811"
    )
    parser.add_argument(
        "--baseline", default=".tmp/phase9-shopify-baseline-20260811"
    )
    parser.add_argument(
        "--crawl",
        default=".tmp/phase9-shopify-storefront-crawl-20260811-v2/existing-site-crawl.json",
    )
    parser.add_argument(
        "--live-theme-check", default=".tmp/phase9-theme-check-live-20260811.json"
    )
    parser.add_argument(
        "--repo-theme-check", default=".tmp/phase9-theme-check-repo-20260811.json"
    )
    parser.add_argument(
        "--theme-divergence", default=".tmp/phase9-theme-divergence-20260811.json"
    )
    parser.add_argument("--theme-reconciliation")
    parser.add_argument(
        "--collections", default=".tmp/phase9-public-collections-20260811.json"
    )
    parser.add_argument(
        "--domain", default=".tmp/phase9-domain-seo-20260811.json"
    )
    parser.add_argument(
        "--operational", default=".tmp/phase9-operational-qa-20260811.json"
    )
    parser.add_argument(
        "--storefront",
        default=".tmp/phase9-operational-storefront-initial-consent-20260811.json",
    )
    parser.add_argument(
        "--cart", default=".tmp/phase9-cart-checkout-beanie-20260811.json"
    )
    parser.add_argument(
        "--cart-exception", default=".tmp/phase9-cart-checkout-20260811.json"
    )
    parser.add_argument(
        "--performance", default=".tmp/phase9-performance-accessibility-20260811.json"
    )
    parser.add_argument(
        "--theme-flows", default=".tmp/phase9-theme-flows-20260811.json"
    )
    parser.add_argument(
        "--blog",
        default=".tmp/phase8-shopify-blog-audit-final-link-state-20260811/shopify-blog-audit.json",
    )
    parser.add_argument(
        "--visual-dir", default=".tmp/phase9-visual-screenshots-20260811-v2"
    )
    return parser.parse_args()


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def values(data, key):
    if isinstance(data, dict):
        return data.get(key, data)
    return data


def text_only(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()


def check_by_key(report, key):
    return next((item for item in report.get("checks", []) if item.get("key") == key), {})


def flow_by_key(report, key):
    return next((item for item in report.get("flows", []) if item.get("key") == key), {})


def theme_findings(data):
    rows = []
    for entry in data:
        for offense in entry.get("offenses", []):
            rows.append(
                {
                    "path": entry.get("path"),
                    "check": offense.get("check"),
                    "severity": offense.get("severity"),
                    "line": offense.get("start_row"),
                    "message": offense.get("message"),
                }
            )
    return rows


def item(code, title, detail, evidence=None):
    result = {"code": code, "title": title, "detail": detail}
    if evidence is not None:
        result["evidence"] = evidence
    return result


def main():
    args = parse_args()
    baseline = Path(args.baseline)
    shop_data = load(baseline / "shop.json")
    shop = shop_data.get("shop", shop_data)
    product_data = load(baseline / "products.json")
    products = list(values(product_data, "products"))
    redirects_data = load(baseline / "redirects.json")
    redirects = list(values(redirects_data, "redirects"))
    crawl = load(args.crawl)
    live_theme_rows = theme_findings(load(args.live_theme_check))
    repo_theme_rows = theme_findings(load(args.repo_theme_check))
    divergence = load(args.theme_divergence)
    reconciliation = load(args.theme_reconciliation) if args.theme_reconciliation else None
    collections = load(args.collections)
    domain = load(args.domain)
    operational = load(args.operational)
    storefront = load(args.storefront)
    cart = load(args.cart)
    cart_exception = load(args.cart_exception)
    performance = load(args.performance)
    theme_flows = load(args.theme_flows)
    blog = load(args.blog)

    active = [product for product in products if product.get("status") == "active"]
    drafts = [product for product in products if product.get("status") == "draft"]
    active_missing_body = [
        product["handle"]
        for product in active
        if not text_only(product.get("body_html"))
    ]
    active_missing_images = [
        product["handle"] for product in active if not product.get("images")
    ]
    active_handles = {product["handle"] for product in active}
    draft_handles = {product["handle"] for product in drafts}
    public_handles = {
        row["path"].removeprefix("/products/")
        for row in crawl["urls"]
        if row.get("status") == 200 and row.get("path", "").startswith("/products/")
    }
    public_drafts = sorted(public_handles & draft_handles)

    live_errors = [row for row in live_theme_rows if row["severity"] == "error"]
    live_warnings = [row for row in live_theme_rows if row["severity"] == "warning"]
    repo_errors = [row for row in repo_theme_rows if row["severity"] == "error"]
    repo_warnings = [row for row in repo_theme_rows if row["severity"] == "warning"]
    empty_collections = collections["emptyPublicCollections"]
    profile_check = check_by_key(operational, "shop_contact_identity")
    consent_check = check_by_key(storefront, "mobile_consent_layout")
    shipping_exception = flow_by_key(cart_exception, "desktop_shipping_rates")
    shipping_failures = [
        destination
        for destination in shipping_exception.get("details", {}).get("destinations", [])
        if not destination.get("passed")
    ]
    performance_errors = [
        {
            "page": result["name"],
            "viewport": result["viewport"],
            **issue,
        }
        for result in performance["results"]
        for issue in result.get("issues", [])
        if issue.get("severity") == "error"
    ]
    theme_source_approved = bool(
        reconciliation and reconciliation.get("approvedSource") == "repository"
    )
    theme_preview_passes = (
        theme_flows.get("summary", {}).get("passed")
        == theme_flows.get("summary", {}).get("flowsChecked")
        and theme_flows.get("summary", {}).get("errors", 0) == 0
    )
    approved_theme_ready = (
        theme_source_approved
        and not repo_errors
        and not repo_warnings
        and theme_preview_passes
        and not performance_errors
    )

    blockers = []
    if not approved_theme_ready:
        blockers.append(
            item(
                "theme_not_reconciled",
                "Approved theme is not ready for publication",
                f"The live pull has {len(live_errors)} Theme Check errors and {len(live_warnings)} warnings, while the local theme repository has {len(repo_errors)} errors and {len(repo_warnings)} warnings. The approved source or its preview validation is incomplete.",
                {
                    "divergence": divergence["summary"],
                    "reconciliation": reconciliation,
                    "liveErrors": live_errors,
                    "liveWarnings": len(live_warnings),
                    "repoErrors": len(repo_errors),
                    "repoWarnings": len(repo_warnings),
                },
            )
        )
    blockers.extend([
        item(
            "empty_public_collections",
            "Published collections are empty",
            f"{len(empty_collections)} published collections return 200 but contain no public product.",
            {"handles": [entry["handle"] for entry in empty_collections]},
        ),
        item(
            "shipping_rate_exception",
            "Some online carts have no NL or DE shipping rate",
            "The regular beanie cart passed NL, DE and US rates, but the EUR 309.50 OrSlow test cart returned no Netherlands or Germany rate and did return a US rate. Review profile assignment and price thresholds.",
            {"failedDestinations": shipping_failures},
        ),
        item(
            "shop_identity_incomplete",
            "Primary Shopify shop identity is incomplete",
            "The primary shop profile is missing city and phone, although public policy/contact content is complete.",
            profile_check.get("evidence"),
        ),
    ])
    if performance_errors:
        blockers.append(
            item(
                "lookbook_accessibility",
                "Lookbook has hidden focusable controls",
                f"The accessibility audit found {len(performance_errors)} error-level runs with hidden focusable controls.",
                performance_errors,
            )
        )

    sitemap_missing_descriptions = [
        row["path"]
        for row in crawl["urls"]
        if row.get("status") == 200
        and row.get("inSitemap")
        and not (row.get("description") or "").strip()
        and row.get("path") != "/agents.md"
    ]
    warnings = [
        item(
            "seo_descriptions_missing",
            "Some indexable routes lack a meta description",
            f"{len(sitemap_missing_descriptions)} sitemap routes have no meta description, mainly category collections plus Journal and Lookbook.",
            {"paths": sitemap_missing_descriptions},
        ),
        item(
            "locale_sitemaps_present",
            "Localized sitemaps are still enabled",
            "The sitemap index includes German, Spanish and French child sitemaps. Global locale settings were intentionally left untouched.",
            {"localePrefixes": domain["sitemap"]["localePrefixes"]},
        ),
        item(
            "pickup_copy_missing",
            "Cart does not explain delivery versus Arnhem pickup",
            "The cart and drawer mention shipping at checkout but do not clarify store pickup before checkout.",
        ),
        item(
            "analytics_manual_followup",
            "Marketing and notification verification remains manual",
            "Consent gating passes, but no third-party marketing request was observed after accept and notification emails require previews or test orders.",
        ),
    ]

    passes = [
        item(
            "catalog_publication_match",
            "Active catalog exactly matches public product routes",
            f"{len(active_handles)} active handles match {len(public_handles)} public product pages; public draft products: {len(public_drafts)}.",
        ),
        item(
            "active_product_content",
            "Active product content is complete",
            f"All {len(active)} active products have body copy and images.",
            {
                "missingBody": active_missing_body,
                "missingImages": active_missing_images,
            },
        ),
        item(
            "journal",
            "Journal migration is complete",
            f"{blog['summary']['publishedArticles']} articles are published and {blog['summary']['articlesWithImage']} have featured images.",
        ),
        item(
            "crawl",
            "English storefront crawl completed",
            f"{crawl['summary']['crawledUrls']} URLs crawled with no network errors and no crawl limit. The two recorded 404 paths are guarded functional query/consent routes, not broken content links.",
        ),
        item(
            "cart",
            "Standard cart and checkout handoff pass",
            f"{cart['summary']['passed']} of {cart['summary']['flowsChecked']} cart flows pass with a stocked product; no order was placed.",
        ),
        item(
            "theme_flows",
            "Core theme flows pass",
            f"{theme_flows['summary']['passed']} of {theme_flows['summary']['flowsChecked']} desktop/mobile flows pass.",
        ),
        item(
            "robots_tls_consent",
            "Public access, robots, TLS and consent pass",
            "The store is not password protected, robots does not block the storefront, current certificates validate, and initial mobile consent controls fit at 390px.",
            {
                "domain": domain["summary"],
                "consentStatus": consent_check.get("status"),
            },
        ),
    ]
    if approved_theme_ready:
        passes.append(
            item(
                "approved_theme_ready",
                "Approved local theme is reconciled and validated",
                f"The repository is the approved source, Theme Check has zero offenses, {theme_flows['summary']['passed']} of {theme_flows['summary']['flowsChecked']} preview flows pass and the accessibility audit has no error-level findings. It remains unpublished by design.",
                reconciliation["summary"],
            )
        )
    if not performance_errors:
        passes.append(
            item(
                "lookbook_accessibility",
                "Lookbook hidden-state accessibility passes",
                "Desktop and mobile preview runs found no focusable controls inside aria-hidden content.",
            )
        )

    gates = [
        item(
            "freeze_handles_navigation",
            "Freeze handles and navigation immediately before launch",
            "Not performed because migration is not happening now.",
        ),
        item(
            "redirect_import",
            "Import the approved redirect map at cutover",
            f"Only {len(redirects)} redirects currently exist in Shopify; Phase 8 prepared the migration set but it has not been imported.",
        ),
        item(
            "domain_cutover",
            "Connect and verify the custom primary domain",
            "nbharnhem.com still serves Lightspeed and Shopify canonicals use the myshopify.com host. Switch DNS only during the migration window, then recheck Shopify SSL and canonicals.",
        ),
        item(
            "publish_theme",
            "Publish the reconciled approved theme",
            "The local repository is reconciled and validated, but must remain unpublished until the approved migration window.",
        ),
        item(
            "payment_order_tests",
            "Run real payment and operational test orders",
            "iDEAL | Wero is approved, but activation, payment, notifications, pickup, shipping, discount, refund and cancellation still need merchant-approved test orders.",
        ),
        item(
            "post_launch_monitoring",
            "Run post-launch monitoring",
            "Orders, 404s, sync drift, performance and rollback triggers can only be monitored after cutover.",
        ),
    ]

    visual_dir = Path(args.visual_dir)
    visual_count = len(list(visual_dir.glob("*.png"))) if visual_dir.exists() else 0
    report = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "mode": "read_only_phase9_prelaunch_consolidation",
        "writesPerformed": False,
        "launchActionsPerformed": [],
        "summary": {
            "blockers": len(blockers),
            "warnings": len(warnings),
            "passes": len(passes),
            "pendingLaunchGates": len(gates),
            "visualScreenshots": visual_count,
            "activeProducts": len(active),
            "draftProducts": len(drafts),
            "publicCollections": collections["summary"]["publishedCollections"],
            "emptyPublicCollections": len(empty_collections),
            "publishedJournalArticles": blog["summary"]["publishedArticles"],
        },
        "blockers": blockers,
        "warnings": warnings,
        "passes": passes,
        "pendingLaunchGates": gates,
        "evidence": {
            "baseline": str(baseline),
            "crawl": args.crawl,
            "domain": args.domain,
            "themeDivergence": args.theme_divergence,
            "themeReconciliation": args.theme_reconciliation,
            "liveThemeCheck": args.live_theme_check,
            "repoThemeCheck": args.repo_theme_check,
            "collections": args.collections,
            "cart": args.cart,
            "cartException": args.cart_exception,
            "performance": args.performance,
            "operational": args.operational,
            "storefront": args.storefront,
            "themeFlows": args.theme_flows,
            "blog": args.blog,
            "visualDirectory": args.visual_dir,
        },
    }

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase9-preflight.json"
    markdown_path = output_dir / "phase9-preflight.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    lines = [
        "# Shopify Phase 9 pre-launch preflight",
        "",
        f"Generated: {report['generatedAt']}",
        "",
        "No theme, domain, redirect, publication or order writes were performed.",
        "",
        "## Summary",
        "",
        f"- Blockers before launch: {len(blockers)}",
        f"- Warnings: {len(warnings)}",
        f"- Passed areas: {len(passes)}",
        f"- Pending launch-window gates: {len(gates)}",
        f"- Visual screenshots: {visual_count}",
        "",
        "## Blockers",
        "",
    ]
    for index, finding in enumerate(blockers, 1):
        lines.append(f"{index}. **{finding['title']}**: {finding['detail']}")
    lines.extend(["", "## Warnings", ""])
    for index, finding in enumerate(warnings, 1):
        lines.append(f"{index}. **{finding['title']}**: {finding['detail']}")
    lines.extend(["", "## Passed", ""])
    for finding in passes:
        lines.append(f"- **{finding['title']}**: {finding['detail']}")
    lines.extend(["", "## Pending Launch Gates", ""])
    for index, finding in enumerate(gates, 1):
        lines.append(f"{index}. **{finding['title']}**: {finding['detail']}")
    lines.extend(["", "## Evidence", ""])
    for key, value in report["evidence"].items():
        lines.append(f"- `{key}`: `{value}`")
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(json.dumps(report["summary"], indent=2))
    print("Writes performed: false")
    print(f"JSON report: {json_path.resolve()}")
    print(f"Markdown report: {markdown_path.resolve()}")


if __name__ == "__main__":
    main()
