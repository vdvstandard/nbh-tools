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


manager = load_hyphenated_tool(
    "manage_shopify_brand_content",
    "manage-shopify-brand-content.py",
)
auditor = load_hyphenated_tool(
    "audit_shopify_brand_seo_for_content",
    "audit-shopify-brand-seo.py",
)


class BrandContentTests(unittest.TestCase):
    def test_content_spec_is_valid(self):
        content = manager.load_spec(manager.DEFAULT_SPEC_FILE)
        self.assertEqual(manager.validate_spec(content), [])
        self.assertEqual(len(content["collections"]), 34)

    def test_replacement_copy_passes_existing_content_rubric(self):
        content = manager.load_spec(manager.DEFAULT_SPEC_FILE)
        for handle, item in content["collections"].items():
            if item["descriptionAction"] != "replace":
                continue
            title = item["seoTitle"].split("|", maxsplit=1)[0].strip()
            result = auditor.analyze_collection(
                {
                    "id": 1,
                    "handle": handle,
                    "title": title,
                    "published_at": "2026-07-25T00:00:00Z",
                    "rules": [
                        {
                            "column": "vendor",
                            "relation": "equals",
                            "condition": title,
                        }
                    ],
                },
                {
                    "descriptionHtml": item["descriptionHtml"],
                    "seo": {
                        "title": item["seoTitle"],
                        "description": item["metaDescription"],
                    },
                    "image": None,
                },
                {
                    "total": 1,
                    "active": 1,
                    "draft": 0,
                    "archived": 0,
                },
                {},
            )
            defect_codes = {
                "blank_description",
                "placeholder_description",
                "generic_boilerplate",
                "duplicate_copy",
                "encoding_damage",
                "unsafe_html",
                "malformed_html",
                "full_bold_description",
                "stray_trailing_quote",
            }
            with self.subTest(handle=handle):
                actual_codes = {
                    issue["code"] for issue in result["issues"]
                }
                self.assertFalse(actual_codes & defect_codes)
                self.assertEqual(result["seoAction"], "preserve")

    def test_remove_trailing_quote_preserves_other_copy(self):
        original = '<p>Barbour says "useful outerwear". Extra quote."</p>'
        expected = '<p>Barbour says "useful outerwear". Extra quote.</p>'
        self.assertEqual(
            manager.remove_trailing_quote(original),
            expected,
        )
        self.assertEqual(
            manager.remove_trailing_quote(expected),
            expected,
        )

    def test_changed_fields_are_explicit(self):
        current = {
            "descriptionHtml": "<p>Old</p>",
            "seo": {"title": "Old", "description": "Old"},
            "published": True,
        }
        desired = {
            "descriptionHtml": "<p>New</p>",
            "seo": {"title": "New", "description": "New"},
            "published": False,
        }
        self.assertEqual(
            manager.changed_fields(current, desired),
            [
                "descriptionHtml",
                "seo.title",
                "seo.description",
                "published",
            ],
        )


if __name__ == "__main__":
    unittest.main()
