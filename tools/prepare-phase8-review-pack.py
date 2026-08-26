#!/usr/bin/env python3
"""Build a local, non-importable Phase 8 redirect and blog review pack."""

import argparse
import csv
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List


DEFAULT_REDIRECT_REPORT = Path(
    ".tmp/phase8-redirect-preparation-20260811/redirect-preparation.json"
)
DEFAULT_BLOG_PREFLIGHT = Path(
    ".tmp/phase8-shopify-blog-migration-20260811/blog-migration-preflight.json"
)
DEFAULT_BLOG_DECISIONS = Path(
    ".tmp/phase8-blog-decisions-20260811/blog-decisions.json"
)
DEFAULT_CRAWL_HISTORY = Path(
    ".tmp/phase8-existing-site-crawl-history-20260811/existing-site-crawl-history.json"
)
DEFAULT_OUTPUT_DIR = Path(".tmp/phase8-master-review-20260811")


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_path(value: str) -> str:
    path = str(value or "").strip()
    if not path:
        return ""
    path = path if path.startswith("/") else f"/{path}"
    return path if path == "/" else path.rstrip("/")


def write_csv(path: Path, rows: Iterable[Dict[str, Any]], fields: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as target:
        writer = csv.DictWriter(target, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def classify_base_row(row: Dict[str, Any]) -> Dict[str, Any]:
    status = row.get("status")
    source = normalize_path(row.get("sourcePath") or "")
    target = normalize_path(row.get("targetPath") or "")
    reason = row.get("reason") or ""
    if target and source == target:
        state = "no_redirect_recommended"
        later_action = "do_not_import"
        target = ""
        reason = (
            f"{reason} The normalized source and target are the same, so no "
            "manual redirect should be imported."
        ).strip()
    elif status == "ready_for_review":
        state = "direct_candidate_for_later_review"
        later_action = "review_redirect_mapping"
    elif status == "no_redirect_needed":
        state = "no_redirect_recommended"
        later_action = "do_not_import"
    elif status == "needs_evidence":
        state = "needs_traffic_or_index_evidence"
        later_action = "collect_evidence_or_leave_unmapped"
    elif target and ("draft" in reason.lower() or "unpublished" in reason.lower()):
        state = "target_not_public"
        later_action = "review_only_after_target_is_public"
    else:
        state = "needs_destination_decision"
        later_action = "decide_destination_or_no_redirect"
    return {
        "source_path": source,
        "source_url": row.get("sourceUrl") or "",
        "source_type": row.get("category") or "",
        "base_status": status or "",
        "preparation_state": state,
        "proposed_target_path": target,
        "candidate_target_path": "",
        "confidence": row.get("confidence") or "",
        "target_dependency": "",
        "later_action": later_action,
        "import_ready": "false",
        "approval": "",
        "notes": "",
        "reason": reason,
        "source_evidence": row.get("sourceEvidence") or "",
        "crawl_status": row.get("crawlStatus") or "",
        "crawl_canonical": row.get("crawlCanonical") or "",
    }


def resolve_redirect_chain(
    source: str, mapping: Dict[str, str]
) -> tuple[str, int, bool]:
    current = source
    seen = {source}
    steps = 0
    while current in mapping:
        current = mapping[current]
        steps += 1
        if current in seen:
            return current, steps, True
        seen.add(current)
    return current, steps, False


def render_markdown(report: Dict[str, Any]) -> str:
    summary = report["summary"]
    validation = report["validation"]
    lines = [
        "# Phase 8 preparation review",
        "",
        "> Preparation only. No redirects were imported and no blogs were created or published.",
        "",
        "## Four workstreams",
        "",
        f"1. URL inventory: {summary['masterRows']} unique historical paths, including {summary['historicalOnlyUrls']} historical-only URLs.",
        f"2. Core mappings: {summary['directCandidates']} direct candidates for later review.",
        f"3. Blogs and tags: {summary['conditionalBlogRedirects']} conditional blog redirects, {summary['removedBlogs']} removed-blog decisions and {summary['legacyTagRoutes']} legacy tag routes.",
        f"4. Validation: {validation['duplicateSourcePaths']} duplicate sources, {validation['baseConflicts']} base conflicts, {validation['redirectLoops']} loops and {validation['existingRedirectChains']} existing chains.",
        "",
        "## Migration gates",
        "",
        f"- {summary['pendingDraftImports']} retained articles still exist only as prepared draft inputs.",
        f"- {summary['unresolvedInternalLinks']} internal blog links across {summary['uniqueUnresolvedInternalLinks']} unique URLs remain unresolved.",
        f"- {summary['inlineImages']} inline images remain a separate pre-publication file-migration task.",
        "- Legacy tag and pagination routes require traffic evidence or an explicit no-redirect decision.",
        "- Existing blog redirect chains must be flattened during the later redirect migration.",
        "",
        "## Review files",
        "",
        "- `phase8-direct-candidates.csv`: direct mapping candidates, not an import file.",
        "- `phase8-conditional-blog-redirects.csv`: retained article and Journal-index mappings that depend on publication.",
        "- `phase8-open-decisions.csv`: routes that still need a destination, evidence or no-redirect decision.",
        "- `phase8-existing-redirect-review.csv`: current redirects and chain-flattening recommendations.",
        "- `phase8-unresolved-blog-links.csv`: internal links that must be resolved before blog publication.",
        "",
        "Every CSV contains `import_ready=false` and a blank approval field.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare a local Phase 8 review pack without Shopify writes."
    )
    parser.add_argument("--redirect-report", type=Path, default=DEFAULT_REDIRECT_REPORT)
    parser.add_argument("--blog-preflight", type=Path, default=DEFAULT_BLOG_PREFLIGHT)
    parser.add_argument("--blog-decisions", type=Path, default=DEFAULT_BLOG_DECISIONS)
    parser.add_argument("--crawl-history", type=Path, default=DEFAULT_CRAWL_HISTORY)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent.parent
    input_paths = {
        "redirectReport": args.redirect_report,
        "blogPreflight": args.blog_preflight,
        "blogDecisions": args.blog_decisions,
        "crawlHistory": args.crawl_history,
    }
    input_paths = {
        key: path if path.is_absolute() else project_root / path
        for key, path in input_paths.items()
    }
    output_dir = (
        args.output_dir if args.output_dir.is_absolute() else project_root / args.output_dir
    )
    for label, path in input_paths.items():
        if not path.exists():
            raise SystemExit(f"Missing {label}: {path}")

    redirect_report = read_json(input_paths["redirectReport"])
    blog_preflight = read_json(input_paths["blogPreflight"])
    blog_decisions = read_json(input_paths["blogDecisions"])
    crawl_history = read_json(input_paths["crawlHistory"])

    safety_errors = []
    if redirect_report.get("shopifyWritesPerformed") is not False:
        safety_errors.append("redirect report does not declare zero Shopify writes")
    if redirect_report.get("applySupported") is not False:
        safety_errors.append("redirect report unexpectedly supports apply")
    for label, report in (
        ("blog preflight", blog_preflight),
        ("blog decisions", blog_decisions),
    ):
        if report.get("shopifyWritesPerformed") is not False:
            safety_errors.append(f"{label} does not declare zero Shopify writes")
        if report.get("liveSiteWritesPerformed") is not False:
            safety_errors.append(f"{label} does not declare zero live-site writes")
        if report.get("applySupported") is not False:
            safety_errors.append(f"{label} unexpectedly supports apply")
    if crawl_history.get("writesPerformed") is not False:
        safety_errors.append("crawl history does not declare zero writes")
    if crawl_history.get("applySupported") is not False:
        safety_errors.append("crawl history unexpectedly supports apply")
    if safety_errors:
        raise SystemExit("Safety validation failed: " + "; ".join(safety_errors))

    rows = [classify_base_row(row) for row in redirect_report.get("rows") or []]
    by_source = {row["source_path"]: row for row in rows}
    duplicate_sources = len(rows) - len(by_source)

    kept_articles = blog_preflight.get("articles") or []
    removed_articles = blog_preflight.get("removedArticles") or []
    for article in kept_articles:
        source = normalize_path(article.get("sourcePath") or "")
        target = normalize_path(article.get("targetPath") or "")
        if source not in by_source:
            raise SystemExit(f"Retained blog source missing from redirect plan: {source}")
        by_source[source].update(
            {
                "preparation_state": "conditional_retained_blog_redirect",
                "proposed_target_path": target,
                "candidate_target_path": "",
                "confidence": "high",
                "target_dependency": "retained_article_must_be_public",
                "later_action": "review_only_after_article_publication",
                "reason": "Retained legacy article maps to its prepared Journal handle.",
            }
        )
    for article in removed_articles:
        source = normalize_path(article.get("path") or "")
        if source not in by_source:
            raise SystemExit(f"Removed blog source missing from redirect plan: {source}")
        by_source[source].update(
            {
                "preparation_state": "removed_blog_needs_retirement_decision",
                "proposed_target_path": "",
                "candidate_target_path": "",
                "confidence": "low",
                "target_dependency": "",
                "later_action": "decide_no_redirect_or_closest_relevant_page",
                "reason": "The article was selected for removal; no unrelated fallback is assumed.",
            }
        )

    blog_index = normalize_path("/blogs/arnhem/")
    if blog_index in by_source:
        by_source[blog_index].update(
            {
                "preparation_state": "conditional_blog_index_redirect",
                "proposed_target_path": "/blogs/journal",
                "confidence": "high",
                "target_dependency": "journal_migration_launch",
                "later_action": "review_during_migration_batch",
                "reason": "The legacy blog index maps to the approved Journal handle.",
            }
        )

    kept_sources = {
        normalize_path(article.get("sourcePath") or "") for article in kept_articles
    }
    removed_sources = {
        normalize_path(article.get("path") or "") for article in removed_articles
    }
    for row in rows:
        if row["source_type"] == "tag":
            row.update(
                {
                    "preparation_state": "legacy_tag_needs_traffic_evidence",
                    "proposed_target_path": "",
                    "candidate_target_path": "",
                    "later_action": "collect_evidence_or_leave_unmapped",
                    "reason": "Prepared Journal articles have no migrated tags, so no useful tag archive target is assumed.",
                }
            )
        elif (
            row["source_type"] == "blog"
            and row["source_path"] not in kept_sources
            and row["source_path"] not in removed_sources
            and row["source_path"] != blog_index
        ):
            row.update(
                {
                    "preparation_state": "noncanonical_blog_route_needs_policy",
                    "proposed_target_path": "",
                    "candidate_target_path": "/blogs/journal",
                    "target_dependency": "traffic_evidence_or_explicit_policy",
                    "later_action": "review_pagination_or_archive_route",
                    "reason": "The old route canonicalizes to the blog index; redirect only with evidence or an explicit archive policy.",
                }
            )

    direct_candidates = [
        row
        for row in rows
        if row["preparation_state"] == "direct_candidate_for_later_review"
    ]
    conditional_blog = [
        row
        for row in rows
        if row["preparation_state"]
        in {"conditional_retained_blog_redirect", "conditional_blog_index_redirect"}
    ]
    no_redirect_rows = [
        row for row in rows if row["preparation_state"] == "no_redirect_recommended"
    ]
    open_decisions = [
        row
        for row in rows
        if row not in direct_candidates
        and row not in conditional_blog
        and row not in no_redirect_rows
    ]

    existing_redirects = redirect_report.get("existingRedirects") or []
    redirect_map = {
        normalize_path(item.get("path") or ""): normalize_path(item.get("target") or "")
        for item in existing_redirects
    }
    existing_redirect_review = []
    redirect_loops = 0
    existing_chains = 0
    for item in existing_redirects:
        source = normalize_path(item.get("path") or "")
        target = normalize_path(item.get("target") or "")
        final_target, chain_length, loop = resolve_redirect_chain(source, redirect_map)
        redirect_loops += int(loop)
        existing_chains += int(chain_length > 1 and not loop)
        existing_redirect_review.append(
            {
                "redirect_id": item.get("id"),
                "current_path": source,
                "current_target": target,
                "final_target": final_target,
                "chain_length": chain_length,
                "has_loop": str(loop).lower(),
                "later_recommendation": (
                    "flatten_source_to_final_target"
                    if chain_length > 1 and not loop
                    else "preserve_current_direct_redirect"
                ),
                "import_ready": "false",
                "approval": "",
                "notes": "",
            }
        )

    unresolved_rows = []
    for item in blog_preflight.get("linkAudit", {}).get("unresolved") or []:
        unresolved_rows.append(
            {
                "review_id": item.get("reviewId"),
                "article_title": item.get("title"),
                "unresolved_href": item.get("href"),
                "later_action": "choose_rewrite_remove_or_preserve_before_publication",
                "approval": "",
                "notes": "",
            }
        )

    direct_missing_targets = sum(not row["proposed_target_path"] for row in direct_candidates)
    source_target_same = sum(
        row["proposed_target_path"]
        and row["source_path"] == row["proposed_target_path"]
        for row in direct_candidates + conditional_blog
    )
    proposed_sources = {
        row["source_path"] for row in direct_candidates + conditional_blog
    }
    proposed_chains = sum(
        row["proposed_target_path"] in proposed_sources
        for row in direct_candidates + conditional_blog
    )
    state_counts = Counter(row["preparation_state"] for row in rows)
    blog_summary = blog_preflight.get("summary") or {}
    validation = {
        "safetyErrors": len(safety_errors),
        "duplicateSourcePaths": duplicate_sources,
        "baseConflicts": len(redirect_report.get("conflicts") or []),
        "directCandidatesMissingTargets": direct_missing_targets,
        "sourceTargetSame": source_target_same,
        "proposedChains": proposed_chains,
        "existingRedirectChains": existing_chains,
        "redirectLoops": redirect_loops,
        "retainedBlogCoverage": len(kept_sources),
        "removedBlogCoverage": len(removed_sources),
        "allDraftInputsUnpublished": all(
            item.get("proposedDraftInput", {}).get("isPublished") is False
            for item in blog_preflight.get("articles") or []
        ),
        "inaccessibleHeroImages": len(
            blog_preflight.get("imageAudit", {}).get("inaccessibleHeroImages") or []
        ),
        "inaccessibleInlineImages": len(
            blog_preflight.get("imageAudit", {}).get("inaccessibleInlineImages") or []
        ),
        "mojibakeOccurrences": blog_summary.get("mojibakeOccurrences", 0),
    }
    blocking_validation = {
        key: value
        for key, value in validation.items()
        if key
        in {
            "safetyErrors",
            "duplicateSourcePaths",
            "baseConflicts",
            "directCandidatesMissingTargets",
            "sourceTargetSame",
            "proposedChains",
            "redirectLoops",
            "inaccessibleHeroImages",
            "inaccessibleInlineImages",
            "mojibakeOccurrences",
        }
        and value
    }
    if blocking_validation:
        raise SystemExit(
            "Review-pack validation failed: " + json.dumps(blocking_validation)
        )

    report = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "mode": "phase8_preparation_only",
        "shopifyWritesPerformed": False,
        "liveSiteWritesPerformed": False,
        "redirectsImported": False,
        "blogsCreatedOrPublished": False,
        "applySupported": False,
        "importFileProduced": False,
        "inputs": {
            key: {"path": str(path), "sha256": sha256_file(path)}
            for key, path in input_paths.items()
        },
        "summary": {
            "masterRows": len(rows),
            "directCandidates": len(direct_candidates),
            "conditionalBlogRedirects": len(conditional_blog),
            "openDecisions": len(open_decisions),
            "noRedirectRecommended": len(no_redirect_rows),
            "retainedBlogs": len(kept_sources),
            "removedBlogs": len(removed_sources),
            "pendingDraftImports": blog_summary.get("pendingDraftImports", 0),
            "legacyTagRoutes": state_counts["legacy_tag_needs_traffic_evidence"],
            "noncanonicalBlogRoutes": state_counts[
                "noncanonical_blog_route_needs_policy"
            ],
            "historicalOnlyUrls": crawl_history.get("summary", {}).get(
                "historicalOnlyUrls", 0
            ),
            "inlineImages": blog_summary.get("inlineImages", 0),
            "unresolvedInternalLinks": blog_summary.get("unresolvedInternalLinks", 0),
            "uniqueUnresolvedInternalLinks": blog_summary.get(
                "uniqueUnresolvedInternalLinks", 0
            ),
            "preparationStates": dict(sorted(state_counts.items())),
        },
        "validation": validation,
        "existingRedirectReview": existing_redirect_review,
        "masterRows": rows,
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase8-preparation-review.json"
    markdown_path = output_dir / "phase8-preparation-summary.md"
    fields = [
        "source_path",
        "source_url",
        "source_type",
        "base_status",
        "preparation_state",
        "proposed_target_path",
        "candidate_target_path",
        "confidence",
        "target_dependency",
        "later_action",
        "import_ready",
        "approval",
        "notes",
        "reason",
        "source_evidence",
        "crawl_status",
        "crawl_canonical",
    ]
    json_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    write_csv(output_dir / "phase8-master-review.csv", rows, fields)
    write_csv(output_dir / "phase8-direct-candidates.csv", direct_candidates, fields)
    write_csv(
        output_dir / "phase8-conditional-blog-redirects.csv",
        conditional_blog,
        fields,
    )
    write_csv(output_dir / "phase8-open-decisions.csv", open_decisions, fields)
    write_csv(
        output_dir / "phase8-existing-redirect-review.csv",
        existing_redirect_review,
        [
            "redirect_id",
            "current_path",
            "current_target",
            "final_target",
            "chain_length",
            "has_loop",
            "later_recommendation",
            "import_ready",
            "approval",
            "notes",
        ],
    )
    write_csv(
        output_dir / "phase8-unresolved-blog-links.csv",
        unresolved_rows,
        [
            "review_id",
            "article_title",
            "unresolved_href",
            "later_action",
            "approval",
            "notes",
        ],
    )
    print(json.dumps(report["summary"], indent=2))
    print(json.dumps(report["validation"], indent=2))
    print("Shopify writes performed: false")
    print("Live-site writes performed: false")
    print("Redirect import file produced: false")
    print(f"Review pack: {output_dir.resolve()}")


if __name__ == "__main__":
    main()
