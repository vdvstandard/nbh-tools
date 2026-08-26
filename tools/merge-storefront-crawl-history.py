#!/usr/bin/env python3
"""Merge read-only storefront crawls while preserving historical URL evidence."""

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List


DEFAULT_OUTPUT = Path(
    ".tmp/phase8-existing-site-crawl-history-20260811/existing-site-crawl-history.json"
)


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_report(path: Path, report: Dict[str, Any]) -> None:
    if report.get("writesPerformed") is not False:
        raise SystemExit(f"Crawl is not explicitly read-only: {path}")
    if report.get("applySupported") is not False:
        raise SystemExit(f"Crawl unexpectedly supports apply: {path}")
    if set(report.get("httpMethods") or []) != {"GET"}:
        raise SystemExit(f"Crawl used methods other than GET: {path}")


def source_label(path: Path, report: Dict[str, Any]) -> str:
    generated = str(report.get("generatedAt") or "unknown-date")
    return f"{path.as_posix()}@{generated}"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Merge read-only storefront crawl history without site writes."
    )
    parser.add_argument("--input", action="append", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if len(args.input) < 2:
        raise SystemExit("Provide at least two --input crawl reports, oldest first.")

    project_root = Path(__file__).resolve().parent.parent
    input_paths = [
        path if path.is_absolute() else project_root / path for path in args.input
    ]
    output_path = (
        args.output if args.output.is_absolute() else project_root / args.output
    )
    reports: List[Dict[str, Any]] = []
    for path in input_paths:
        if not path.exists():
            raise SystemExit(f"Crawl report not found: {path}")
        report = read_json(path)
        validate_report(path, report)
        reports.append(report)

    latest = reports[-1]
    by_path: Dict[str, Dict[str, Any]] = {}
    sources_by_path: Dict[str, List[str]] = {}
    for path, report in zip(input_paths, reports):
        label = source_label(path, report)
        for item in report.get("urls") or []:
            source_path = item.get("path")
            if not source_path:
                continue
            by_path[source_path] = dict(item)
            sources_by_path.setdefault(source_path, []).append(label)

    latest_paths = {
        item.get("path") for item in latest.get("urls") or [] if item.get("path")
    }
    latest_sitemap_paths = {
        item.get("path")
        for item in latest.get("urls") or []
        if item.get("path") and item.get("inSitemap")
    }
    merged_urls = []
    for path in sorted(by_path):
        item = dict(by_path[path])
        item["seenInCrawlReports"] = sources_by_path[path]
        item["presentInLatestCrawl"] = path in latest_paths
        item["inLatestSitemap"] = path in latest_sitemap_paths
        item["historicalOnly"] = path not in latest_paths
        if path not in latest_sitemap_paths:
            item["inSitemap"] = False
        merged_urls.append(item)

    discovered_not_in_sitemap = [
        item for item in merged_urls if not item.get("inLatestSitemap")
    ]
    status_counts = Counter(
        str(item.get("status"))
        for item in merged_urls
        if item.get("crawled") and item.get("status") is not None
    )
    category_counts = Counter(item.get("category") or "other" for item in merged_urls)
    skipped_counts = Counter(
        item.get("skipReason")
        for item in merged_urls
        if item.get("skipReason")
    )
    historical_only = [item for item in merged_urls if item["historicalOnly"]]

    output = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "mode": "read_only_storefront_crawl_history_merge",
        "httpMethods": ["GET"],
        "writesPerformed": False,
        "applySupported": False,
        "baseUrl": latest.get("baseUrl"),
        "sitemap": latest.get("sitemap"),
        "robots": latest.get("robots"),
        "guards": latest.get("guards"),
        "sources": [
            {
                "path": str(path),
                "generatedAt": report.get("generatedAt"),
                "summary": report.get("summary"),
            }
            for path, report in zip(input_paths, reports)
        ],
        "summary": {
            "sourceReports": len(reports),
            "discoveredUrls": len(merged_urls),
            "latestCrawlUrls": len(latest_paths),
            "latestSitemapUrls": len(latest_sitemap_paths),
            "historicalOnlyUrls": len(historical_only),
            "historicalOnlyCrawledContentUrls": sum(
                bool(item.get("crawled")) and not item.get("skipReason")
                for item in historical_only
            ),
            "discoveredNotInSitemap": len(discovered_not_in_sitemap),
            "statuses": dict(sorted(status_counts.items())),
            "categories": dict(sorted(category_counts.items())),
            "skipped": dict(sorted(skipped_counts.items())),
            "errors": sum(bool(item.get("error")) for item in merged_urls),
            "redirectedResponses": sum(bool(item.get("redirected")) for item in merged_urls),
            "canonicalMismatches": sum(
                bool(item.get("canonical"))
                and item.get("canonical") != item.get("url")
                for item in merged_urls
            ),
            "crawlLimitReached": any(
                bool(report.get("summary", {}).get("crawlLimitReached"))
                for report in reports
            ),
        },
        "urls": merged_urls,
        "discoveredNotInSitemap": discovered_not_in_sitemap,
        "historicalOnlyUrls": historical_only,
        "errors": [item for item in merged_urls if item.get("error")],
        "redirectedResponses": [item for item in merged_urls if item.get("redirected")],
        "canonicalMismatches": [
            item
            for item in merged_urls
            if item.get("canonical") and item.get("canonical") != item.get("url")
        ],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(output, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(output["summary"], indent=2))
    print("Writes performed: false")
    print(f"Merged crawl history: {output_path.resolve()}")


if __name__ == "__main__":
    main()
