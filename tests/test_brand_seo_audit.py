import importlib.util
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = PROJECT_ROOT / "tools"
sys.path.insert(0, str(TOOLS_DIR))


def load_hyphenated_tool(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(
        name,
        TOOLS_DIR / filename,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


brand_seo = load_hyphenated_tool(
    "audit_shopify_brand_seo",
    "audit-shopify-brand-seo.py",
)
storefront = load_hyphenated_tool(
    "audit_brand_storefront",
    "audit-brand-storefront.py",
)


def collection(title: str):
    return {
        "id": 1,
        "handle": title.lower().replace(" ", "-"),
        "title": title,
        "published_at": "2026-07-25T00:00:00Z",
        "rules": [
            {
                "column": "vendor",
                "relation": "equals",
                "condition": title,
            }
        ],
    }


def analyze(title: str, body_html: str, seo=None):
    return brand_seo.analyze_collection(
        collection(title),
        {
            "descriptionHtml": body_html,
            "seo": seo or {},
            "image": None,
        },
        {"total": 1, "active": 1, "draft": 0, "archived": 0},
        {},
    )


class BrandSeoRubricTests(unittest.TestCase):
    def test_short_specific_copy_is_preserved(self):
        result = analyze(
            "Ralph Lauren",
            (
                "<p>Shop Ralph Lauren at Neighbourhood Arnhem. Explore "
                "timeless American menswear, refined fabrics and versatile "
                "wardrobe staples.</p>"
            ),
        )
        self.assertEqual(result["descriptionAction"], "preserve")
        self.assertIn(
            "thin_description",
            {issue["code"] for issue in result["issues"]},
        )

    def test_generic_long_copy_gets_content_proposal(self):
        repeated = (
            "Welcome to Neighbourhood, your destination for an "
            "uncompromising selection. We are delighted to present this "
            "brand to discerning customers. "
        )
        result = analyze("Example Brand", f"<p>{repeated * 8}</p>")
        self.assertEqual(result["descriptionAction"], "content_proposal")
        self.assertIn(
            "generic_boilerplate",
            {issue["code"] for issue in result["issues"]},
        )

    def test_dutch_template_metadata_is_replaced(self):
        result = analyze(
            "Drake's",
            "<p>Drake's is known for British menswear and shirting.</p>",
            {
                "title": "Drake's collectie - Neighbourhood Arnhem",
                "description": (
                    "Ontdek alle producten van Drake's bij "
                    "Neighbourhood Arnhem."
                ),
            },
        )
        self.assertEqual(result["descriptionAction"], "preserve")
        self.assertEqual(result["seoAction"], "replace_metadata")

    def test_unmatched_quote_is_a_technical_repair(self):
        result = analyze(
            "Barbour",
            "<p>Barbour is known for practical British outerwear.\"</p>",
        )
        self.assertEqual(result["descriptionAction"], "technical_repair")


class StorefrontAuditTests(unittest.TestCase):
    def test_paginated_canonical_must_be_self_referencing(self):
        self.assertTrue(
            storefront.canonical_expected(
                "https://example.com/collections/drakes?page=2",
                "drakes",
                2,
            )
        )
        self.assertFalse(
            storefront.canonical_expected(
                "https://example.com/collections/drakes",
                "drakes",
                2,
            )
        )


if __name__ == "__main__":
    unittest.main()
