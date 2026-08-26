#!/usr/bin/env python3
"""Compare a Shopify theme repository with a pulled theme deterministically."""

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path


IGNORED_PARTS = {".git", ".shopify", "node_modules"}
THEME_DIRECTORIES = {
    "assets",
    "blocks",
    "config",
    "layout",
    "locales",
    "sections",
    "snippets",
    "templates",
}


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-theme", required=True)
    parser.add_argument("--live-theme", required=True)
    parser.add_argument("--previous-live-theme")
    parser.add_argument(
        "--approved-source",
        choices=("repository", "live", "undecided"),
        default="undecided",
    )
    parser.add_argument("--decision-note", default="")
    parser.add_argument(
        "--output-dir", default=".tmp/phase9-theme-reconciliation"
    )
    return parser.parse_args()


def theme_files(root):
    root = Path(root).resolve()
    return {
        path.relative_to(root).as_posix(): path
        for path in root.rglob("*")
        if path.is_file()
        and path.relative_to(root).parts[0] in THEME_DIRECTORIES
        and not IGNORED_PARTS.intersection(path.relative_to(root).parts)
    }


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_json(path):
    text = path.read_text(encoding="utf-8-sig")
    while True:
        stripped = text.lstrip()
        if not stripped.startswith("/*"):
            break
        match = re.match(r"/\*.*?\*/", stripped, flags=re.DOTALL)
        if not match:
            break
        text = stripped[match.end() :]
    return json.loads(text)


def normalized_text(path):
    text = path.read_text(encoding="utf-8-sig")
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def compare_theme_files(repository, live):
    repository_names = set(repository)
    live_names = set(live)
    common = sorted(repository_names & live_names)
    raw_differences = []
    semantic_json_equal = []
    normalized_text_equal = []
    material_differences = []

    for name in common:
        repository_path = repository[name]
        live_path = live[name]
        if digest(repository_path) == digest(live_path):
            continue
        raw_differences.append(name)

        if name.endswith(".json"):
            try:
                if parse_json(repository_path) == parse_json(live_path):
                    semantic_json_equal.append(name)
                    continue
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass

        try:
            if normalized_text(repository_path) == normalized_text(live_path):
                normalized_text_equal.append(name)
                continue
        except UnicodeDecodeError:
            pass

        material_differences.append(name)

    return {
        "onlyRepository": sorted(repository_names - live_names),
        "onlyLive": sorted(live_names - repository_names),
        "rawContentDifferences": raw_differences,
        "semanticJsonFormattingOnly": semantic_json_equal,
        "normalizedTextOnly": normalized_text_equal,
        "materialContentDifferences": material_differences,
    }


def exact_theme_match(left, right):
    left_names = set(left)
    right_names = set(right)
    return left_names == right_names and all(
        digest(left[name]) == digest(right[name]) for name in left_names
    )


def main():
    args = parse_args()
    repository_root = Path(args.repository_theme).resolve()
    live_root = Path(args.live_theme).resolve()
    repository = theme_files(repository_root)
    live = theme_files(live_root)
    comparison = compare_theme_files(repository, live)

    previous_live_unchanged = None
    previous_live_root = None
    if args.previous_live_theme:
        previous_live_root = Path(args.previous_live_theme).resolve()
        previous_live_unchanged = exact_theme_match(
            live, theme_files(previous_live_root)
        )

    summary = {
        "repositoryFiles": len(repository),
        "liveFiles": len(live),
        "onlyRepository": len(comparison["onlyRepository"]),
        "onlyLive": len(comparison["onlyLive"]),
        "rawContentDifferences": len(comparison["rawContentDifferences"]),
        "semanticJsonFormattingOnly": len(
            comparison["semanticJsonFormattingOnly"]
        ),
        "normalizedTextOnly": len(comparison["normalizedTextOnly"]),
        "materialContentDifferences": len(
            comparison["materialContentDifferences"]
        ),
        "exactByteMatch": not any(
            (
                comparison["onlyRepository"],
                comparison["onlyLive"],
                comparison["rawContentDifferences"],
            )
        ),
        "liveUnchangedFromPreviousPull": previous_live_unchanged,
    }
    report = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "mode": "read_only_theme_reconciliation",
        "writesPerformed": False,
        "repositoryRoot": str(repository_root),
        "livePullRoot": str(live_root),
        "previousLivePullRoot": (
            str(previous_live_root) if previous_live_root else None
        ),
        "approvedSource": args.approved_source,
        "decisionNote": args.decision_note,
        "summary": summary,
        **comparison,
    }

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "theme-reconciliation.json"
    markdown_path = output_dir / "theme-reconciliation.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    lines = [
        "# Shopify theme reconciliation",
        "",
        f"Generated: {report['generatedAt']}",
        "",
        "This comparison is read-only. No theme was pushed or published.",
        "",
        "## Decision",
        "",
        f"- Approved source: `{args.approved_source}`",
        f"- Note: {args.decision_note or 'No decision note supplied.'}",
        "",
        "## Summary",
        "",
        f"- Repository files: {summary['repositoryFiles']}",
        f"- Live files: {summary['liveFiles']}",
        f"- Repository-only files: {summary['onlyRepository']}",
        f"- Live-only files: {summary['onlyLive']}",
        f"- Raw content differences: {summary['rawContentDifferences']}",
        f"- JSON formatting-only differences: {summary['semanticJsonFormattingOnly']}",
        f"- Other normalized text-only differences: {summary['normalizedTextOnly']}",
        f"- Material content differences: {summary['materialContentDifferences']}",
        f"- Live unchanged from previous pull: {summary['liveUnchangedFromPreviousPull']}",
        "",
        "## Files",
        "",
        f"- Repository only: {', '.join(comparison['onlyRepository']) or 'none'}",
        f"- Live only: {', '.join(comparison['onlyLive']) or 'none'}",
        f"- JSON formatting only: {', '.join(comparison['semanticJsonFormattingOnly']) or 'none'}",
        f"- Material differences: {', '.join(comparison['materialContentDifferences']) or 'none'}",
    ]
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(json.dumps(summary, indent=2))
    print(f"JSON report: {json_path.resolve()}")
    print(f"Markdown report: {markdown_path.resolve()}")


if __name__ == "__main__":
    main()
