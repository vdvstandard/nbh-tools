# authorize-shopify-admin-token

## Goal

Authorize the Shopify app and obtain an Admin access token via OAuth.

## Inputs

- `SHOPIFY_SHOP` or `--shop`
- `SHOPIFY_API_KEY` or `--api-key`
- `SHOPIFY_API_SECRET` or `--api-secret`
- Optional: `SHOPIFY_SCOPES`, `SHOPIFY_OAUTH_REDIRECT_URI`

## Outputs

- `.env` updated with `SHOPIFY_ADMIN_ACCESS_TOKEN` by default
- `.tmp/shopify-oauth-session.json`
- `.tmp/shopify-oauth-result.json`

## Command

```powershell
python tools/authorize-shopify-admin-token.py --open
```

## Notes

- The tool automatically loads `.env` from the repository root unless `--env-file` is provided.
- Use `--no-write-env` to avoid writing the token to `.env`.
- Use `--from-stdin` or `--callback-url` to process a manual callback URL.
