"""Shared catalog policy for Lightspeed-to-Shopify synchronization."""

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Optional, Sequence, Tuple


ACTIVE_VISIBILITY_VALUES = frozenset({"Y", "S"})
RETIRED_VENDORS = (
    "Messyweekend",
    "POP Trading Company",
    "encens d'auroville",
)
RETIRED_PRODUCT_HANDLES = (
    "vest",
    "button-up",
    "lumber-short-herringbone-cotton-washed-navy",
    "schoen",
    "varsity-jacket",
)


@dataclass(frozen=True)
class CatalogDecision:
    status: str
    reasons: Tuple[str, ...]


def clean(value) -> str:
    return str(value or "").strip()


def normalize_vendor_key(value: str) -> str:
    return collapse_space(clean(value).replace("\u2019", "'")).casefold()


def normalize_handle(value: str) -> str:
    base = clean(value).lower().replace("_", "-")
    base = re.sub(r"[^a-z0-9-]+", "-", base)
    base = re.sub(r"-{2,}", "-", base)
    return base.strip("-")


def collapse_space(value: str) -> str:
    return re.sub(r"\s+", " ", clean(value)).strip()


def is_retired_vendor(value: str) -> bool:
    key = normalize_vendor_key(value)
    return key in {normalize_vendor_key(item) for item in RETIRED_VENDORS}


def is_retired_product(handle: str, vendor: str = "") -> bool:
    return (
        normalize_handle(handle) in set(RETIRED_PRODUCT_HANDLES)
        or is_retired_vendor(vendor)
    )


def normalized_visibility(values: Iterable[str]) -> Tuple[str, ...]:
    return tuple(
        sorted(
            {
                clean(value).upper()
                for value in values
                if clean(value)
            }
        )
    )


def decide_catalog_status(
    visible_values: Iterable[str],
    total_inventory: Optional[Decimal],
    blocking_issues: Sequence[str] = (),
) -> CatalogDecision:
    visibility = normalized_visibility(visible_values)
    reasons = []
    if not ACTIVE_VISIBILITY_VALUES.intersection(visibility):
        reasons.append("source_hidden")
    if total_inventory is None:
        reasons.append("inventory_unknown")
    elif total_inventory <= 0:
        reasons.append("out_of_stock")
    reasons.extend(
        f"quality:{issue}"
        for issue in blocking_issues
        if clean(issue)
    )
    return CatalogDecision(
        status="DRAFT" if reasons else "ACTIVE",
        reasons=tuple(dict.fromkeys(reasons)),
    )


def creation_quality_issues(
    *,
    title: str,
    vendor: str,
    variant_ids: Sequence[str],
    variant_prices: Sequence[Optional[Decimal]],
    image_urls: Sequence[str],
) -> Tuple[str, ...]:
    issues = []
    if not clean(title):
        issues.append("missing_title")
    if not clean(vendor):
        issues.append("missing_vendor")
    if not variant_ids or any(not clean(value) for value in variant_ids):
        issues.append("missing_external_variant_id")
    if not variant_prices or any(value is None or value <= 0 for value in variant_prices):
        issues.append("missing_or_invalid_price")
    if not [value for value in image_urls if clean(value)]:
        issues.append("missing_image")
    return tuple(issues)
