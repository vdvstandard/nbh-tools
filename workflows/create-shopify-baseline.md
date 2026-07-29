# create-shopify-baseline

## Goal

Create a reproducible, read-only baseline before changing the Shopify theme or
catalog data.

## Inputs

- `SHOPIFY_SHOP`
- `SHOPIFY_API_KEY`
- `SHOPIFY_API_SECRET`
- Optional `--api-version`
- Optional `--output-dir`

## Tool

Use `tools/export-shopify-baseline.py`.

The tool requests a fresh client-credentials token by default. Shopify tokens
from this grant expire after 24 hours, so automation must not assume that a
saved `SHOPIFY_ADMIN_ACCESS_TOKEN` is still valid.

## Command

```powershell
python tools/export-shopify-baseline.py
```

## Output

The tool creates a timestamped directory under `.tmp/` containing:

- `shop.json`
- `products.json`, including `custom.brand`
- `custom_collections.json`
- `smart_collections.json`
- `redirects.json`
- `pages.json`
- `manifest.json` with counts, file sizes and SHA-256 hashes

No access token or app secret is written to the export.

`redirects.json` and `pages.json` require Shopify's `read_content` scope. When
that scope is unavailable, the exporter still completes the catalog baseline
and records those resources as unavailable in `manifest.json`; it must never
report an inaccessible resource as an empty resource.

## Theme Baseline

1. Run `shopify theme list` and record the live and development theme IDs.
2. Pull the exact live theme into a temporary comparison directory.
3. Compare it with `C:\Users\david\Documents\neighbourhood-theme`.
4. Preserve local-only files such as `.gitignore`.
5. Pull the live theme into the fixed repository only after reviewing the diff.
6. Run Theme Check and commit the baseline before feature work starts.

## Verification

- Confirm every exported JSON file parses.
- Confirm every file listed in `manifest.json` matches its SHA-256 hash.
- Confirm product and collection counts against the Shopify audit.
- Treat an unexpected zero-product export as a failed baseline; never accept it
  as an empty store without an explicit `--minimum-products 0`.
- Confirm `manifest.json.resource_errors` is empty before treating redirects and
  pages as backed up.
- Confirm `git status` in the fixed theme repository contains only the expected
  live-baseline changes.
- Do not push or publish a theme during baseline creation.

## Failure Handling

- On `401 Unauthorized`, rerun normally so the tool requests a new token.
- On `429 Too Many Requests`, allow the built-in retry and backoff to finish.
- On partial export failure, discard that timestamped directory and rerun.
- Never continue to a theme or catalog write when baseline verification fails.

## Current Baseline

Recorded on 2026-07-25:

- Fixed theme repository:
  `C:\Users\david\Documents\neighbourhood-theme`
- Git branch: `codex-lookbook-viewer-updates`
- Baseline commit: `c55cb3c`
- Live theme: `Codex lookbook updates 2026-06-30`
- Live theme ID: `186898579784`
- Development theme ID: `187323318600`
- Catalog baseline:
  `.tmp/shopify-baseline-phase0-complete-20260725`
- Theme Check report:
  `.tmp/phase-0-theme-check-20260723.json`
- Product count: 1,060
- Product statuses: 975 active, 85 draft
- Collection count: 64
- Redirect count: 1
- Page count: 4
- Theme Check: 52 offenses across 48 files, 29 errors and 23 warnings

Baseline status:

- Complete: `manifest.json.resource_errors` is empty.
- The fresh client-credentials token includes `write_content`, which allowed
  the read-only pages and redirects export to complete.
