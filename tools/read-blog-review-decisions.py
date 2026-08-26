#!/usr/bin/env python3
"""Read a completed XLSX blog review and prepare local decision reports."""

import argparse
import csv
import hashlib
import json
import re
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


DEFAULT_REVIEW_REPORT = ".tmp/phase8-blog-review-20260805/blog-review.json"
DEFAULT_OUTPUT_DIR = ".tmp/phase8-blog-decisions-20260805"
MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

DECISION_ALIASES = {
    "keep": "keep",
    "houden": "keep",
    "behouden": "keep",
    "bewaren": "keep",
    "ja": "keep",
    "yes": "keep",
    "remove": "remove",
    "verwijderen": "remove",
    "weg": "remove",
    "nee": "remove",
    "no": "remove",
    "delete": "remove",
    "unsure": "unsure",
    "twijfel": "unsure",
    "misschien": "unsure",
    "later": "unsure",
    "check": "unsure",
}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Read an XLSX blog review without changing Shopify or the live site."
    )
    parser.add_argument("xlsx_path")
    parser.add_argument("--review-report", default=DEFAULT_REVIEW_REPORT)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--keep-ids",
        default="",
        help="Comma-separated review IDs to override to keep.",
    )
    return parser.parse_args()


def column_index(cell_reference):
    letters = re.match(r"[A-Z]+", cell_reference or "A").group(0)
    index = 0
    for character in letters:
        index = index * 26 + ord(character) - ord("A") + 1
    return index - 1


def xml_text(node):
    return "".join(node.itertext()) if node is not None else ""


def read_shared_strings(archive):
    path = "xl/sharedStrings.xml"
    if path not in archive.namelist():
        return []
    root = ET.fromstring(archive.read(path))
    return [xml_text(item) for item in root.findall(f"{{{MAIN_NS}}}si")]


def resolve_first_sheet(archive):
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {
        item.attrib["Id"]: item.attrib["Target"]
        for item in relationships.findall(f"{{{PACKAGE_REL_NS}}}Relationship")
    }
    sheet = workbook.find(f"{{{MAIN_NS}}}sheets/{{{MAIN_NS}}}sheet")
    if sheet is None:
        raise ValueError("Workbook has no worksheets")
    relationship_id = sheet.attrib[f"{{{REL_NS}}}id"]
    target = targets[relationship_id].lstrip("/")
    if not target.startswith("xl/"):
        target = str(PurePosixPath("xl") / target)
    return sheet.attrib.get("name", "Sheet1"), target


def cell_value(cell, shared_strings):
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return xml_text(cell.find(f"{{{MAIN_NS}}}is"))
    value = cell.findtext(f"{{{MAIN_NS}}}v", default="")
    if cell_type == "s" and value:
        return shared_strings[int(value)]
    if cell_type == "b":
        return "true" if value == "1" else "false"
    return value


def normalize_header(value):
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def read_first_sheet(path):
    with zipfile.ZipFile(path) as archive:
        shared_strings = read_shared_strings(archive)
        sheet_name, sheet_path = resolve_first_sheet(archive)
        root = ET.fromstring(archive.read(sheet_path))
    raw_rows = []
    for row in root.findall(f".//{{{MAIN_NS}}}sheetData/{{{MAIN_NS}}}row"):
        values = {}
        for cell in row.findall(f"{{{MAIN_NS}}}c"):
            values[column_index(cell.attrib.get("r"))] = cell_value(
                cell, shared_strings
            )
        if values:
            width = max(values) + 1
            raw_rows.append([values.get(index, "") for index in range(width)])
    if not raw_rows:
        raise ValueError("Worksheet is empty")
    headers = [normalize_header(value) for value in raw_rows[0]]
    rows = []
    for values in raw_rows[1:]:
        item = {
            header: values[index] if index < len(values) else ""
            for index, header in enumerate(headers)
            if header
        }
        if any(str(value or "").strip() for value in item.values()):
            rows.append(item)
    return sheet_name, headers, rows


def normalized_decision(value):
    clean = re.sub(r"\s+", " ", str(value or "").strip().lower())
    if not clean:
        return "unreviewed", None
    decision = DECISION_ALIASES.get(clean)
    return (decision, None) if decision else ("unknown", clean)


def review_id(value):
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def write_csv(path, rows):
    fieldnames = [
        "review_id",
        "decision",
        "decision_source",
        "original_decision",
        "notes",
        "published_date",
        "title",
        "summary",
        "url",
        "path",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for item in rows:
            writer.writerow(
                {
                    "review_id": item["reviewId"],
                    "decision": item["decision"],
                    "decision_source": item.get("decisionSource") or "xlsx",
                    "original_decision": item.get("originalDecision") or "",
                    "notes": item.get("notes") or "",
                    "published_date": item.get("publishedDate") or "",
                    "title": item.get("title") or "",
                    "summary": item.get("summary") or "",
                    "url": item.get("url") or "",
                    "path": item.get("path") or "",
                }
            )


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    args = parse_args()
    project_root = Path(__file__).resolve().parent.parent
    xlsx_path = Path(args.xlsx_path)
    review_report_path = Path(args.review_report)
    output_dir = Path(args.output_dir)
    if not review_report_path.is_absolute():
        review_report_path = project_root / review_report_path
    if not output_dir.is_absolute():
        output_dir = project_root / output_dir
    if not xlsx_path.exists():
        raise SystemExit(f"XLSX file not found: {xlsx_path}")
    if not review_report_path.exists():
        raise SystemExit(f"Reference review report not found: {review_report_path}")

    reference_report = json.loads(review_report_path.read_text(encoding="utf-8"))
    reference_by_id = {
        int(item["reviewId"]): item for item in reference_report.get("articles", [])
    }
    sheet_name, headers, worksheet_rows = read_first_sheet(xlsx_path)
    keep_override_ids = {
        int(value.strip())
        for value in args.keep_ids.split(",")
        if value.strip()
    }
    required_headers = {"review_id", "decision", "notes", "url", "path"}
    missing_headers = sorted(required_headers - set(headers))
    if missing_headers:
        raise SystemExit(f"Worksheet is missing columns: {', '.join(missing_headers)}")

    duplicate_ids = []
    unknown_ids = []
    mismatches = []
    seen_ids = set()
    decisions = []
    unknown_values = Counter()
    for source in worksheet_rows:
        item_id = review_id(source.get("review_id"))
        if item_id is None:
            continue
        if item_id in seen_ids:
            duplicate_ids.append(item_id)
            continue
        seen_ids.add(item_id)
        reference = reference_by_id.get(item_id)
        if not reference:
            unknown_ids.append(item_id)
            continue
        for field in ("url", "path"):
            source_value = str(source.get(field) or "").strip()
            if source_value and source_value != str(reference.get(field) or "").strip():
                mismatches.append(
                    {
                        "reviewId": item_id,
                        "field": field,
                        "expected": reference.get(field),
                        "actual": source_value,
                    }
                )
        decision, unknown_value = normalized_decision(source.get("decision"))
        if unknown_value:
            unknown_values[unknown_value] += 1
        decisions.append(
            {
                **reference,
                "decision": decision,
                "decisionSource": "xlsx",
                "originalDecision": str(source.get("decision") or "").strip(),
                "notes": str(source.get("notes") or "").strip(),
            }
        )

    missing_review_ids = sorted(set(reference_by_id) - seen_ids)
    decisions.sort(key=lambda item: item["reviewId"])
    invalid_keep_override_ids = sorted(keep_override_ids - set(reference_by_id))
    decision_overrides = []
    for item in decisions:
        if item["reviewId"] not in keep_override_ids:
            continue
        decision_overrides.append(
            {
                "reviewId": item["reviewId"],
                "title": item["title"],
                "from": item["decision"],
                "to": "keep",
            }
        )
        item["decision"] = "keep"
        item["decisionSource"] = "keep_id_override"
    decision_counts = Counter(item["decision"] for item in decisions)
    summary = {
        "referenceArticles": len(reference_by_id),
        "worksheetRows": len(worksheet_rows),
        "matchedReviewRows": len(decisions),
        "decisions": dict(sorted(decision_counts.items())),
        "duplicateReviewIds": len(duplicate_ids),
        "unknownReviewIds": len(unknown_ids),
        "missingReviewIds": len(missing_review_ids),
        "urlOrPathMismatches": len(mismatches),
        "unknownDecisionValues": dict(sorted(unknown_values.items())),
        "decisionOverrides": len(decision_overrides),
        "invalidKeepOverrideIds": len(invalid_keep_override_ids),
    }
    report = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "mode": "read_only_blog_decision_preparation",
        "shopifyWritesPerformed": False,
        "liveSiteWritesPerformed": False,
        "applySupported": False,
        "sourceXlsx": str(xlsx_path.resolve()),
        "sourceXlsxBytes": xlsx_path.stat().st_size,
        "sourceXlsxModifiedAt": datetime.fromtimestamp(
            xlsx_path.stat().st_mtime, timezone.utc
        ).isoformat(),
        "sourceXlsxSha256": sha256_file(xlsx_path),
        "sourceWorksheet": sheet_name,
        "referenceReviewReport": str(review_report_path),
        "summary": summary,
        "validation": {
            "duplicateReviewIds": duplicate_ids,
            "unknownReviewIds": unknown_ids,
            "missingReviewIds": missing_review_ids,
            "urlOrPathMismatches": mismatches,
            "invalidKeepOverrideIds": invalid_keep_override_ids,
        },
        "decisionOverrides": decision_overrides,
        "decisions": decisions,
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "blog-decisions.json"
    all_csv_path = output_dir / "blog-decisions.csv"
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    write_csv(all_csv_path, decisions)
    for decision in ("keep", "remove", "unsure", "unreviewed", "unknown"):
        write_csv(
            output_dir / f"blog-{decision}.csv",
            [item for item in decisions if item["decision"] == decision],
        )

    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print("Shopify writes performed: false")
    print("Live-site writes performed: false")
    print(f"Report: {report_path}")
    print(f"Combined CSV: {all_csv_path}")


if __name__ == "__main__":
    main()
