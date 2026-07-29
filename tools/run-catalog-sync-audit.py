#!/usr/bin/env python3

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List


DEFAULT_MAX_SOURCE_AGE_HOURS = 24.0


def run_command(label: str, arguments: List[str], project_root: Path) -> None:
    print(f"\n[{label}]", flush=True)
    completed = subprocess.run(
        [sys.executable, *arguments],
        cwd=project_root,
        check=False,
    )
    if completed.returncode:
        raise SystemExit(
            f"{label} failed with exit code {completed.returncode}."
        )


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    project_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description=(
            "Run the complete read-only Lightspeed-to-Shopify catalog "
            "preflight and combine its reports."
        )
    )
    parser.add_argument("--products-source", required=True, help="Fresh Lightspeed products CSV export.")
    parser.add_argument("--inventory-source", required=True, help="Fresh Lightspeed inventory CSV export.")
    parser.add_argument(
        "--max-source-age-hours",
        type=float,
        default=DEFAULT_MAX_SOURCE_AGE_HOURS,
        help=f"Maximum safe source age. Default: {DEFAULT_MAX_SOURCE_AGE_HOURS:g}.",
    )
    parser.add_argument(
        "--output-dir",
        help="Report directory. Default: .tmp/catalog-sync-audit-<UTC timestamp>.",
    )
    args = parser.parse_args()

    if args.max_source_age_hours <= 0:
        raise SystemExit("--max-source-age-hours must be positive.")

    products_source = Path(args.products_source).resolve()
    inventory_source = Path(args.inventory_source).resolve()
    for source in (products_source, inventory_source):
        if not source.exists():
            raise SystemExit(f"Source file not found: {source}")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = Path(
        args.output_dir
        or project_root / ".tmp" / f"catalog-sync-audit-{timestamp}"
    ).resolve()
    baseline_dir = output_dir / "shopify-baseline"
    catalog_audit = output_dir / "catalog-audit.json"
    product_identity_plan = output_dir / "product-identity-plan.json"
    import_plan = output_dir / "product-import-plan.json"
    variant_plan = output_dir / "variant-plan.json"
    inventory_plan = output_dir / "inventory-plan.json"
    price_plan = output_dir / "price-plan.json"
    status_plan = output_dir / "status-plan.json"
    output_dir.mkdir(parents=True, exist_ok=True)

    run_command(
        "Shopify baseline",
        [
            "tools/export-shopify-baseline.py",
            "--output-dir",
            str(baseline_dir),
            "--minimum-products",
            "1000",
        ],
        project_root,
    )
    run_command(
        "Catalog eligibility and drift",
        [
            "tools/audit-catalog-sync.py",
            "--products-source",
            str(products_source),
            "--inventory-source",
            str(inventory_source),
            "--shopify-products",
            str(baseline_dir / "products.json"),
            "--output",
            str(catalog_audit),
            "--max-source-age-hours",
            str(args.max_source_age_hours),
        ],
        project_root,
    )
    run_command(
        "Product identity preflight",
        [
            "tools/sync-lightspeed-product-identities.py",
            "--products-source",
            str(products_source),
            "--prefer-client-credentials",
            "--plan-file",
            str(product_identity_plan),
            "--results-file",
            str(output_dir / "unused-product-identity-results.json"),
            "--max-source-age-hours",
            str(args.max_source_age_hours),
        ],
        project_root,
    )
    run_command(
        "New product import preflight",
        [
            "tools/import-lightspeed-products.py",
            "--source",
            str(products_source),
            "--prefer-client-credentials",
            "--plan-file",
            str(import_plan),
            "--results-file",
            str(output_dir / "unused-product-import-results.json"),
            "--max-source-age-hours",
            str(args.max_source_age_hours),
        ],
        project_root,
    )
    run_command(
        "Variant structure preflight",
        [
            "tools/sync-lightspeed-variants.py",
            "--products-source",
            str(products_source),
            "--prefer-client-credentials",
            "--plan-file",
            str(variant_plan),
            "--results-file",
            str(output_dir / "unused-variant-results.json"),
            "--max-source-age-hours",
            str(args.max_source_age_hours),
        ],
        project_root,
    )
    run_command(
        "Inventory preflight",
        [
            "tools/sync-lightspeed-inventory.py",
            "--products-source",
            str(products_source),
            "--inventory-source",
            str(inventory_source),
            "--prefer-client-credentials",
            "--plan-file",
            str(inventory_plan),
            "--results-file",
            str(output_dir / "unused-inventory-results.json"),
            "--max-source-age-hours",
            str(args.max_source_age_hours),
        ],
        project_root,
    )
    run_command(
        "Price preflight",
        [
            "tools/sync-lightspeed-prices.py",
            "--products-source",
            str(products_source),
            "--prefer-client-credentials",
            "--plan-file",
            str(price_plan),
            "--results-file",
            str(output_dir / "unused-price-results.json"),
            "--max-source-age-hours",
            str(args.max_source_age_hours),
        ],
        project_root,
    )
    run_command(
        "Status preflight",
        [
            "tools/sync-lightspeed-product-status.py",
            "--audit-report",
            str(catalog_audit),
            "--prefer-client-credentials",
            "--plan-file",
            str(status_plan),
            "--results-file",
            str(output_dir / "unused-status-results.json"),
        ],
        project_root,
    )

    baseline_manifest = read_json(baseline_dir / "manifest.json")
    catalog = read_json(catalog_audit)
    product_identities = read_json(product_identity_plan)
    product_import = read_json(import_plan)
    variants = read_json(variant_plan)
    inventory = read_json(inventory_plan)
    prices = read_json(price_plan)
    statuses = read_json(status_plan)
    blockers = {
        "catalogSourcesStale": not catalog["summary"]["sources_fresh"],
        "duplicateSourceSkus": catalog["summary"]["duplicate_source_sku_values"],
        "duplicateSourceEans": catalog["summary"]["duplicate_source_ean_values"],
        "productIdentityMissing": product_identities["summary"]["missing"],
        "productIdentityConflicts": product_identities["summary"]["conflicts"],
        "variantConflicts": variants["summary"]["conflicts"],
        "variantMissingProducts": variants["summary"]["missingProducts"],
        "inventoryUnmatched": inventory["summary"]["unmatched"],
        "inventoryDuplicateMatches": inventory["summary"]["duplicateMatches"],
        "priceConflicts": prices["summary"]["conflicts"],
        "statusConflicts": statuses["summary"]["conflicts"],
    }
    blocking = any(bool(value) for value in blockers.values())
    report = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "mode": "read-only",
        "outputDirectory": str(output_dir),
        "summary": {
            "shopifyProducts": baseline_manifest["counts"]["products"],
            "sourceProducts": catalog["summary"]["source_products_after_policy"],
            "productIdentityUpdates": product_identities["summary"]["updates"],
            "newProducts": product_import["summary"]["toCreate"],
            "variantChangedProducts": variants["summary"]["changedProducts"],
            "inventoryUpdates": inventory["summary"]["updates"],
            "priceUpdates": prices["summary"]["updates"],
            "statusUpdates": statuses["summary"]["statusUpdates"],
            "sourcesFresh": catalog["summary"]["sources_fresh"],
            "readyForReviewedApply": not blocking,
        },
        "blockers": blockers,
        "attention": {
            "invalidCatalogProducts": product_import[
                "summary"
            ]["invalidSourceProducts"],
            "orphanVariantsPreserved": variants[
                "summary"
            ]["orphanVariantsPreserved"],
        },
        "reports": {
            "baselineManifest": str(baseline_dir / "manifest.json"),
            "catalogAudit": str(catalog_audit),
            "productIdentityPlan": str(product_identity_plan),
            "productImportPlan": str(import_plan),
            "variantPlan": str(variant_plan),
            "inventoryPlan": str(inventory_plan),
            "pricePlan": str(price_plan),
            "statusPlan": str(status_plan),
        },
    }
    combined_report = output_dir / "catalog-sync-preflight.json"
    write_json(combined_report, report)
    print(f"\nCombined report written: {combined_report}")
    print(json.dumps(report["summary"], indent=2))


if __name__ == "__main__":
    main()
