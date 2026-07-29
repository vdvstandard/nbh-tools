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
- If `SHOPIFY_SESSION_TOKEN` is empty or unavailable, use the client-credentials helper instead:
  `python tools/get-shopify-admin-token.py --write-env`.
- When Shopify Admin tools fail with `401 Unauthorized`, refresh `.env` first with
  `python tools/get-shopify-admin-token.py --write-env`, then retry the original command.
