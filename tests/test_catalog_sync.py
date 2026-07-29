import importlib.util
import sys
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = PROJECT_ROOT / "tools"
sys.path.insert(0, str(TOOLS_DIR))

from catalog_policy import decide_catalog_status, is_retired_product
from lightspeed_variant_sync import filter_retired_catalog_rows


def load_hyphenated_tool(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(
        name,
        TOOLS_DIR / filename,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


status_sync = load_hyphenated_tool(
    "sync_lightspeed_product_status",
    "sync-lightspeed-product-status.py",
)
price_sync = load_hyphenated_tool(
    "sync_lightspeed_prices",
    "sync-lightspeed-prices.py",
)


class CatalogPolicyTests(unittest.TestCase):
    def test_active_requires_visibility_stock_and_quality(self):
        active = decide_catalog_status(["Y"], Decimal("1"))
        self.assertEqual(active.status, "ACTIVE")
        self.assertEqual(active.reasons, ())

        hidden = decide_catalog_status(["N"], Decimal("1"))
        self.assertEqual(hidden.status, "DRAFT")
        self.assertIn("source_hidden", hidden.reasons)

        empty = decide_catalog_status(["S"], Decimal("0"))
        self.assertEqual(empty.status, "DRAFT")
        self.assertIn("out_of_stock", empty.reasons)

        incomplete = decide_catalog_status(
            ["Y"],
            Decimal("1"),
            ["missing_image"],
        )
        self.assertEqual(incomplete.status, "DRAFT")
        self.assertIn("quality:missing_image", incomplete.reasons)

    def test_retired_vendors_and_test_handles_are_excluded(self):
        self.assertTrue(is_retired_product("anything", "Messyweekend"))
        self.assertTrue(
            is_retired_product(
                "lumber-short-herringbone-cotton-washed-navy",
                "Neighbourhood Arnhem",
            )
        )
        self.assertFalse(is_retired_product("bedale-jacket", "Barbour"))

    def test_inventory_sources_drop_retired_variants(self):
        product_rows = [
            {
                "Internal_ID": "1",
                "Internal_Variant_ID": "v1",
                "US_URL": "retired",
                "Brand": "POP Trading Company",
            },
            {
                "Internal_ID": "2",
                "Internal_Variant_ID": "v2",
                "US_URL": "kept",
                "Brand": "Drake's",
            },
        ]
        inventory_rows = [
            {"Internal_Variant_ID": "v1"},
            {"Internal_Variant_ID": "v2"},
        ]
        products, inventory, counts = filter_retired_catalog_rows(
            product_rows,
            inventory_rows,
        )
        self.assertEqual([row["Internal_ID"] for row in products], ["2"])
        self.assertEqual(
            [row["Internal_Variant_ID"] for row in inventory],
            ["v2"],
        )
        self.assertEqual(counts["products"], 1)
        self.assertEqual(counts["variants"], 1)


class SyncPlanTests(unittest.TestCase):
    def test_price_plan_matches_exact_variant_options(self):
        source = {
            "ls-v1": {
                "handle": "shirt",
                "combo": ("large",),
                "variant": "Size : Large",
                "title": "Shirt",
                "price": Decimal("120.00"),
            }
        }
        shopify = [
            {
                "id": "gid://shopify/ProductVariant/1",
                "price": "100.00",
                "selectedOptions": [{"name": "Size", "value": "Large"}],
                "product": {
                    "id": "gid://shopify/Product/1",
                    "handle": "shirt",
                },
            }
        ]
        plan = price_sync.build_price_plan(
            source,
            shopify,
            {"ageHours": 1},
            {"products": 0, "variants": 0, "inventoryRows": 0},
            {
                "products": 0,
                "variants": 0,
                "inventoryRows": 0,
                "items": [],
            },
            24,
        )
        self.assertEqual(plan["summary"]["updates"], 1)
        self.assertEqual(plan["summary"]["conflicts"], 0)
        self.assertTrue(plan["summary"]["safeToApply"])

    def test_status_plan_only_manages_identified_sync_products(self):
        audit = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "summary": {
                "sources_fresh": True,
                "safe_to_apply_statuses": True,
            },
            "records": [
                {
                    "internal_id": "10",
                    "handle": "managed",
                    "shopify": {
                        "managed_by_sync": True,
                        "status": "ACTIVE",
                    },
                    "decision": {
                        "desired_status": "DRAFT",
                        "reasons": ["out_of_stock"],
                    },
                },
                {
                    "internal_id": "11",
                    "handle": "manual",
                    "shopify": {
                        "managed_by_sync": False,
                        "status": "ACTIVE",
                    },
                    "decision": {
                        "desired_status": "DRAFT",
                        "reasons": ["out_of_stock"],
                    },
                },
            ],
        }
        live = [
            {
                "id": "gid://shopify/Product/10",
                "handle": "managed",
                "title": "Managed",
                "status": "ACTIVE",
                "publishedAt": "2026-01-01T00:00:00Z",
                "lightspeedSource": {"value": "c_series_csv"},
                "lightspeedInternalId": {"value": "10"},
            },
            {
                "id": "gid://shopify/Product/11",
                "handle": "manual",
                "title": "Manual",
                "status": "ACTIVE",
                "publishedAt": "2026-01-01T00:00:00Z",
                "lightspeedSource": None,
                "lightspeedInternalId": None,
            },
        ]
        plan = status_sync.build_status_plan(
            audit,
            live,
            max_audit_age_hours=2,
            skip_online_store_publish=False,
        )
        self.assertEqual(plan["summary"]["syncManagedProducts"], 1)
        self.assertEqual(plan["summary"]["manualProductsExcluded"], 1)
        self.assertEqual(plan["summary"]["statusUpdates"], 1)
        self.assertEqual(plan["summary"]["conflicts"], 0)
        self.assertTrue(plan["summary"]["safeToApply"])


if __name__ == "__main__":
    unittest.main()
