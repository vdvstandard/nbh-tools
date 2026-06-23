# Neighbourhood Shopify Tools

This repository contains Shopify Admin tools for the Neighbourhood project.

## Purpose

- Manage Shopify Admin tokens and OAuth authorization.
- Sync product brand metafields from Shopify vendor values.
- Review and apply product classification suggestions in Shopify.
- Keep deterministic execution in `tools/`, with workflow documentation in `workflows/`.

## Prerequisites

- Python 3.10+ installed.
- A Shopify store and app with API credentials.
- `.env` file at the repository root.

## Setup

1. Copy `.env.sample` to `.env`.
2. Fill in your Shopify configuration values.
3. Install dependencies if you want to use a Python environment for tooling:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

> No runtime third-party dependencies are required by these tools today; `requirements.txt` is included for future extensions or dev tooling.

## Environment variables

The tools use these variables when command-line options are omitted:

- `SHOPIFY_SHOP`
- `SHOPIFY_API_KEY`
- `SHOPIFY_API_SECRET`
- `SHOPIFY_ADMIN_ACCESS_TOKEN`
- `SHOPIFY_SESSION_TOKEN`
- `SHOPIFY_TOKEN_TYPE`
- `SHOPIFY_SCOPES`
- `SHOPIFY_OAUTH_REDIRECT_URI`
- `METAFIELD_NAMESPACE`
- `METAFIELD_KEY`
- `METAFIELD_TYPE`

## Tools

### Token and OAuth helpers

- `python tools/authorize-shopify-admin-token.py --open`
  - Start the OAuth authorization flow.
  - Store the returned token in `.env` by default.

- `python tools/get-shopify-admin-token.py --write-env`
  - Request a temporary Admin token using client_credentials.
  - Writes the token to `.env` if `--write-env` is set.

- `python tools/refresh-shopify-admin-token.py --session-token <token> --write-env`
  - Exchange an App Bridge session token for a Shopify Admin token.
  - Writes the refreshed token to `.env` if `--write-env` is enabled.

### Sync operations

- `python tools/sync-product-brand-metafields.py` 
  - Dry run by default.
  - Add `--apply` to write changes to Shopify.

- `python tools/sync-product-classification.py --export-review`
  - Export classification recommendations to `.tmp/product-classification-review.json`.
  - Use `--validate-review` to validate and `--apply` to update approved rows.
  - `--debug-colors` inspects color pattern definitions without writing changes.

## Workflow docs

Each major tool has a corresponding workflow document in `workflows/`.

- `workflows/authorize-shopify-admin-token.md`
- `workflows/get-shopify-admin-token.md`
- `workflows/refresh-shopify-admin-token.md`
- `workflows/sync-product-brand-metafields.md`
- `workflows/sync-product-classification.md`

## Repository structure

- `tools/` — deterministic Python scripts for Shopify operations.
- `workflows/` — documentation of how each tool should be used.
- `.tmp/` — temporary outputs and runtime artifacts.

## Notes

- `.tmp/` is temporary and should be regenerated when needed.
- `.env` contains secrets and is ignored by `.gitignore`.
Push test completed 2026-06-23. 
Push test completed 2026-06-23.
