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
- The tool reads `.env` by default from the repository root.
