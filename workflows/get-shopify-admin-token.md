# get-shopify-admin-token

## Goal

Request a Shopify Admin token using the `client_credentials` grant.

## Inputs

- `SHOPIFY_SHOP` or `--shop`
- `SHOPIFY_API_KEY` or `--api-key`
- `SHOPIFY_API_SECRET` or `--api-secret`

## Outputs

- Optional `.env` update when `--write-env` is used
- Printed token when `--print-token` is used

## Command

```powershell
python tools/get-shopify-admin-token.py --write-env
```

## Notes

- The tool only writes to `.env` when `--write-env` is included.
- If no `.env` exists, it will be created.
