# refresh-shopify-admin-token

## Goal

Exchange a Shopify App Bridge session token for an Admin access token.

## Inputs

- `SHOPIFY_SHOP` or `--shop`
- `SHOPIFY_API_KEY` or `--api-key`
- `SHOPIFY_API_SECRET` or `--api-secret`
- `SHOPIFY_SESSION_TOKEN` or `--session-token`
- `SHOPIFY_TOKEN_TYPE` or `--token-type` (default: `online`)

## Outputs

- Optional `.env` update when `--write-env` is used
- Printed token when `--print-token` is used

## Command

```powershell
python tools/refresh-shopify-admin-token.py --session-token <token> --write-env
```

## Notes

- This tool is useful when you already have a frontend App Bridge session token.
- Use `--expiring` to request an expiring offline token.
