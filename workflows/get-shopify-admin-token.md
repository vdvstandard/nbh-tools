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
- Automated Shopify tools should request a fresh client-credentials token at
  the start of each run and keep it in memory. Do not assume the token saved in
  `.env` is still valid.
- Client-credentials tokens expire after 24 hours. Repeating the same token
  request is the refresh mechanism.
- Use a stored token only when a tool explicitly lacks client-credentials
  support or when `--use-stored-token` is intentionally selected.
- Use this helper when a saved `SHOPIFY_ADMIN_ACCESS_TOKEN` has expired or a Shopify Admin command fails with `401 Unauthorized`.
- This is the fallback when `SHOPIFY_SESSION_TOKEN` is empty and `refresh-shopify-admin-token.py` cannot exchange an App Bridge session token.
