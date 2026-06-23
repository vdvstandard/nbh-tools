# sync-product-classification

## Goal

Export, validate, and apply Shopify product classification recommendations.

## Inputs

- `SHOPIFY_SHOP` or `--shop`
- `SHOPIFY_ADMIN_ACCESS_TOKEN` or `--token`
- or `SHOPIFY_API_KEY` + `SHOPIFY_API_SECRET`
- `SHOPIFY_API_VERSION` or `--api-version`
- `SHOPIFY_ADMIN_ACCESS_TOKEN` or `--token`

## Outputs

- `.tmp/product-classification-review.json` when `--export-review` is used
- Validation output when `--validate-review` is used
- Shopify updates when `--apply` is used

## Commands

```powershell
python tools/sync-product-classification.py --export-review
python tools/sync-product-classification.py --validate-review
python tools/sync-product-classification.py --apply
python tools/sync-product-classification.py --debug-colors
```

## Notes

- `--export-review` builds suggested category and color updates.
- `--validate-review` checks approved rows without changing Shopify.
- `--apply` updates only rows marked `approved`.
- `--debug-colors` inspects color-pattern references without applying changes.
