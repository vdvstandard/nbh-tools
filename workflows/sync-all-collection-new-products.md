# sync-all-collection-new-products

## Goal

Keep the "All products" collection (handle: `all`) sorted with newest
products on top, while still allowing manual drag-and-drop reordering
in Shopify admin for everything else.

## Background

The `all` collection is an automated collection (rule: price > 0) with
its sort order set to `MANUAL`. Shopify keeps membership automatic
(new priced products join on their own) but does not place new
arrivals at the top, so this tool moves only the products created
since the last run to the top, newest first, leaving the rest of the
manual order untouched.

## Inputs

- `SHOPIFY_SHOP` or `--shop`
- `SHOPIFY_API_KEY` / `SHOPIFY_API_SECRET` (or `SHOPIFY_ADMIN_ACCESS_TOKEN`)
- `--handle` (default: `all`)
- `--state-file` (default: `tools/data/all-collection-sync-state.json`)

## Outputs

- Reorders new products to the top of the collection when `--apply` is used.
- Updates the watermark in `tools/data/all-collection-sync-state.json`.

## Command

```powershell
python tools/sync-all-collection-new-products.py
python tools/sync-all-collection-new-products.py --apply
```

## Notes

- Dry run by default. Add `--apply` to write changes.
- Requires the collection's sort order to already be `MANUAL`. The
  tool refuses to run otherwise so it never fights an automatic sort.
- First run only records a baseline watermark and performs no
  reordering, so turning this on never causes a surprise reshuffle.
- Runs hourly via `.github/workflows/sync-all-collection.yml` on
  GitHub Actions, using the `SHOPIFY_SHOP`, `SHOPIFY_API_KEY`, and
  `SHOPIFY_API_SECRET` repository secrets. The workflow commits the
  updated state file back to the repo after a successful `--apply` run.
- If the state file and the live collection ever drift (e.g. someone
  edits `tools/data/all-collection-sync-state.json` by hand), delete
  the file and re-run with `--apply` to re-establish a baseline.
