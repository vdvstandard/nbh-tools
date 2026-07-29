# sync-product-brand-metafields

## Goal

Keep product vendor values in sync with a Shopify product brand metafield.

## Inputs

- `SHOPIFY_SHOP` or `--shop`
- `SHOPIFY_ADMIN_ACCESS_TOKEN` or `--token`
- or `SHOPIFY_API_KEY` + `SHOPIFY_API_SECRET`
- `METAFIELD_NAMESPACE` or `--namespace`
- `METAFIELD_KEY` or `--key`
- `SHOPIFY_API_VERSION` or `--api-version`

## Outputs

- Dry run prints the planned metafield updates.
- `--apply` writes the brand metafield values to Shopify.

## Command

```powershell
python tools/sync-product-brand-metafields.py --apply
```

## Notes

- If no `token` is supplied, a temporary token is fetched automatically.
- If the command fails with `401 Unauthorized`, the saved `SHOPIFY_ADMIN_ACCESS_TOKEN` is likely expired. Refresh it with
  `python tools/get-shopify-admin-token.py --write-env`, then rerun the sync command.
- The tool reads `.env` by default from the repository root.
- If Shopify has no metafield definition for the configured namespace/key, the tool falls back to `METAFIELD_TYPE` or `--type`.
- Products without an existing brand metafield are treated as blank and can be safely populated.
- The visible Shopify Admin field "Brands" is `custom.brand` (singular), not `custom.brands`.
- If the brand metafield has a choices validation, use `--ensure-choices` before applying newly imported vendor values.
- Use `--only-blank` when filling imports to avoid overwriting existing non-blank curated brand values.
