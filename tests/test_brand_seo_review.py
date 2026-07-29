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


review_builder = load_hyphenated_tool(
    "build_brand_seo_review_html",
    "build-brand-seo-review-html.py",
)


class BrandSeoReviewTests(unittest.TestCase):
    def setUp(self):
        self.review = review_builder.load_json(
            review_builder.DEFAULT_REVIEW_FILE
        )
        self.source = review_builder.load_json(
            review_builder.DEFAULT_SOURCE_PLAN
        )

    def test_review_covers_all_brands_without_validation_errors(self):
        result = review_builder.validate_review(
            self.review,
            self.source,
        )
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["summary"]["collections"], 34)
        self.assertEqual(result["summary"]["uniqueSeoTitles"], 34)
        self.assertEqual(result["summary"]["uniqueMetaDescriptions"], 34)
        self.assertFalse(result["shopifyWritePerformed"])

    def test_generated_html_is_an_approval_document(self):
        validation = review_builder.validate_review(
            self.review,
            self.source,
        )
        output = review_builder.build_html(
            self.review,
            self.source,
            validation,
        )
        self.assertIn("Pending approval. No Shopify write.", output)
        self.assertEqual(
            output.count('class="brand-review"'),
            34,
        )
        self.assertNotIn("X-Shopify-Access-Token", output)


if __name__ == "__main__":
    unittest.main()
