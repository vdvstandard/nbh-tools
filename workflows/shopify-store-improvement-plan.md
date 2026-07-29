# Shopify store improvement plan

## Goal

Bring the Neighbourhood Shopify store to a reliable, consistent and
launch-ready state without publishing incomplete catalog data or introducing
untracked theme changes.

The fixed theme repository is:

`C:\Users\david\Documents\neighbourhood-theme`

Store data tools, workflows and disposable audit output live in:

`C:\Users\david\Documents\Neighbourhood`

## Operating rules

- Export and validate a fresh baseline before destructive catalog work.
- Use fresh client-credentials tokens in memory for automated Shopify runs.
- Run catalog tools in dry-run mode before `--apply`.
- Keep exact allowlists for deletions.
- Verify the resulting Shopify state independently after every apply run.
- Do not bulk-publish legacy drafts without a product-level quality review.
- Do not change the live theme until the corresponding development-theme
  change has passed Theme Check and visual QA.

## Phase 0: Baseline and recovery point

- [x] Identify the live and development theme IDs.
- [x] Pull the live theme into the fixed Git repository.
- [x] Commit and push the Phase 0 theme baseline.
- [x] Export products, collections, pages and redirects.
- [x] Hash the baseline files and validate resource counts.
- [x] Run Shopify Theme Check and save the report.
- [x] Confirm token scopes for products, content, metaobjects and definitions.

Evidence:

- `.tmp/shopify-baseline-phase0-complete-20260725`
- `.tmp/phase-0-theme-check-20260723.json`
- Theme commits `c55cb3c` and `6c6cad2`

## Phase 1: Catalog and brand foundation

### Completed

- [x] Audit all 85 legacy draft-product event histories.
- [x] Confirm the drafts did not originate from the current Lightspeed sync.
- [x] Delete the five approved test products from vendor
  `Neighbourhood Arnhem`.
- [x] Remove the empty obsolete brand collections `Blundstone`, `Danner`,
  `Paradise Found`, `Stelff`, `Sunray`, `Ukiyo` and `Wild Animals`.
- [x] Create a published automated collection for `Padmore and Barnes`.
- [x] Create a published automated collection for `Ralph Lauren`.
- [x] Replace typo collection `Stelff` with published automated collection
  `Steiff`.
- [x] Prepare automated collection `Plot` and keep it unpublished while its
  only product remains draft.
- [x] Convert `Atelier Neighbourhood` from a one-product manual collection to
  an automated vendor collection while preserving its description.
- [x] Verify actual collection membership, product statuses and publication.
- [x] Confirm the Brands page automatically resolves matching vendor handles
  to these collection pages.
- [x] Remove the retired `Messyweekend`, `POP Trading Company` and
  `encens d'auroville` products from Shopify.
- [x] Exclude these three retired vendors from normal Lightspeed CSV imports;
  require `--include-retired-vendors` to import them intentionally.

Verified collection state:

| Collection | Active | Draft | Published |
| --- | ---: | ---: | --- |
| Padmore and Barnes | 4 | 0 | Yes |
| Ralph Lauren | 7 | 0 | Yes |
| Steiff | 16 | 1 | Yes |
| Plot | 0 | 1 | No |
| Atelier Neighbourhood | 11 | 3 | Yes |

Draft conclusion:

- All 85 audited products were created active on 15 April 2026 by Pim Wiggers
  through Shopify Web and changed to draft within 60 seconds.
- After deletion of the approved Neighbourhood Arnhem test draft and the
  retired encens d'auroville draft, 83 legacy drafts remain.
- This is evidence of a deliberate migration selection, not an automatic
  status-sync failure. Missing images and incomplete content are common among
  these drafts, so they require review rather than bulk publication.
- All 83 now carry `custom.catalog_status_sync=manual_review`, so the normal
  Lightspeed status sync cannot publish or hide them accidentally.

Evidence:

- `.tmp/shopify-draft-audit-20260725.json`
- `.tmp/shopify-draft-audit-post-phase1-20260725.json`
- `.tmp/shopify-phase1-catalog-dry-run-20260725.json`
- `.tmp/shopify-phase1-catalog-apply-20260725.json`
- `.tmp/shopify-phase1-catalog-verified-20260725.json`
- `.tmp/shopify-baseline-phase1-complete-20260725`
- `.tmp/shopify-retired-brands-dry-run-20260725.json`
- `.tmp/shopify-retired-brands-apply-20260725.json`
- `.tmp/shopify-draft-audit-post-retired-brands-20260725.json`
- `.tmp/shopify-baseline-phase1-retired-brands-complete-20260725`

### Remaining

- [ ] Review all 83 legacy drafts by image, description, stock, SKU and
  commercial relevance.
- [ ] Keep `Plot` hidden until its product is approved and publish both
  product and collection together.
- [ ] Decide whether the single draft Steiff product should be completed and
  published or remain hidden.
- [ ] Review the three Atelier `Wonder T` drafts and publish only complete
  products.
- [x] Confirm every active store brand has one canonical collection handle,
  exact vendor rule and no duplicate typo collection.
- [x] Replace placeholder collection copy, especially
  `Onze eigen producten`, with final on-brand SEO text.
- [x] Add final SEO titles and meta descriptions for every canonical brand
  collection.

## Phase 2: Product data and sync reliability

- [x] Define the source of truth for title, description, price, stock, vendor,
  brand metafield, images and publication status.
- [x] Document which fields Lightspeed may overwrite and which stay curated in
  Shopify.
- [x] Add dry-run reports and explicit counters to each sync.
- [x] Add validation for missing SKU, duplicate SKU, negative inventory,
  missing images, blank descriptions and inconsistent vendor/brand values.
- [x] Run the first controlled apply with fresh exports and confirm visible,
  in-stock Lightspeed products become active while hidden or zero-stock
  products remain draft.
- [x] Add post-sync verification for created products, variant prices,
  inventory tracking, inventory quantities, statuses and publications.
- [x] Add one read-only command that produces a combined catalog drift report.
- [ ] Schedule the read-only report and deliver an alert for failures or drift.
- [ ] Add `write_publications` and replace REST Online Store publication with
  `publishablePublish`.
- [ ] Obtain R-Series API access, build its read-only adapter and validate its
  inventory against C-Series before cutover.

Phase 2 implementation:

- `tools/catalog_policy.py`
- `tools/audit-catalog-sync.py`
- `tools/run-catalog-sync-audit.py`
- `tools/import-lightspeed-products.py`
- `tools/sync-lightspeed-product-identities.py`
- `tools/sync-lightspeed-variant-identities.py`
- `tools/sync-lightspeed-variants.py`
- `tools/sync-lightspeed-inventory.py`
- `tools/sync-lightspeed-prices.py`
- `tools/sync-lightspeed-product-status.py`
- `tools/lock-legacy-draft-statuses.py`
- `workflows/sync-lightspeed-shopify-catalog.md`

Latest read-only evidence:

- `.tmp/phase2-final-zero-drift-locked-20260725`

Latest applied and verified evidence:

- `.tmp/phase2-variant-identity-apply-results-20260725.json`
- `.tmp/phase2-fresh-product-import-apply-results-20260725.json`
- `.tmp/phase2-fresh-variant-sync-apply-results-20260725.json`
- `.tmp/phase2-fresh-price-apply-results-20260725.json`
- `.tmp/phase2-fresh-inventory-apply-results-20260725.json`
- `.tmp/phase2-fresh-status-apply-results-20260725.json`
- `.tmp/phase2-product-identity-apply-results-20260725.json`
- `.tmp/phase2-final-status-apply-results-20260725.json`
- `.tmp/phase2-legacy-draft-lock-apply-results-20260725.json`
- `.tmp/phase2-legacy-draft-lock-verify-plan-20260725.json`

## Phase 3: Collection and SEO quality

- [x] Audit all brand descriptions for bold wrappers, malformed HTML and
  character-encoding damage.
- [x] Standardize collection descriptions as normal-weight paragraphs.
- [x] Check collection title, H1, SEO title, meta description and canonical URL.
- [x] Confirm collection image alt text is not applicable while none of the
  canonical brand collections uses a collection image.
- [x] Check product structured data, collection pagination canonicals and
  indexability.
- [x] Ensure SEO text always starts below the final product row and never
  shares a row with a product title or price.
- [x] Ensure the final product page has no redundant `Load more` control.
- [x] Crawl all canonical brand pages for broken links, empty results and
  duplicate content.

Phase 3 result:

- All 34 canonical brand collections now have unique English SEO titles and
  meta descriptions.
- Twenty generic or placeholder descriptions were replaced with
  product-specific English copy.
- Thirteen useful existing descriptions were preserved and Barbour received
  only the required trailing-quote repair.
- `Atelier Neighbourhood` and `Quality Goods by Neighbourhood` are positioned
  as separate own-label lines.
- Empty published collections `Baracuta`, `Filson`,
  `Padmore and Barnes`, `Ralph Lauren` and `Tagliatore` were hidden after
  receiving complete metadata and, where required, collection copy.
- `Plot` remains hidden with complete copy and metadata while its product is
  draft.
- The post-apply content audit reports 34 preserved descriptions, 34
  preserved SEO sets and no errors or warnings requiring action.
- The post-apply storefront crawl checked 38 collection pages and 28 product
  samples with zero errors and zero warnings.
- Authenticated browser tests load 53 of 53 Drake's products and hide the
  endless-scroll control at the end. Single-page collections correctly render
  without that control.
- On 29 July 2026, the Phase 3B editorial SEO pass replaced the first full
  brand-copy round with more experience-led, shop-floor-specific copy for all
  34 canonical brand collections.
- The Phase 3B post-apply verification preserved all 34 descriptions and SEO
  sets, reported no duplicate or missing canonical vendor collections and
  returned no issues requiring action.

Phase 3 implementation and evidence:

- `tools/manage-shopify-brand-content.py`
- `tools/data/shopify-brand-content.json`
- `tools/audit-shopify-brand-seo.py`
- `tools/audit-brand-storefront.py`
- `tools/audit-endless-scroll.mjs`
- `.tmp/phase3-brand-content-plan-20260725.json`
- `.tmp/phase3-brand-content-apply-20260725.json`
- `.tmp/phase3-brand-content-post-apply-plan-20260725.json`
- `.tmp/phase3-brand-seo-post-apply-20260725.json`
- `.tmp/phase3-brand-storefront-post-apply-20260725.json`
- `.tmp/phase3-drakes-post-apply.json`
- `.tmp/phase3-akog-post-apply.json`
- `.tmp/phase3b-brand-content-apply-20260729.json`
- `.tmp/phase3b-brand-content-verify-apply-20260729.json`
- `.tmp/phase3b-brand-seo-post-apply-20260729.json`
- `.tmp/phase3b-eeat-review.md`

API compatibility note:

- Admin API `2026-04` still expects
  `collectionUpdate(input: CollectionInput!)`. Do not use the newer
  `collectionUpdate(collection: CollectionUpdateInput!)` signature until the
  configured API version is upgraded and verified.
- The current token still lacks `write_publications`. The five visibility
  changes therefore used the verified REST smart-collection publication
  field. Replacing this with `publishableUnpublish` remains in Phase 2.

## Phase 4: Storefront and theme behavior

- [x] Resolve the 29 Theme Check errors and 23 warnings by risk and template.
- [x] Regression-test collection filters, endless loading and product counts.
- [x] Test product cards with normal price, sale price, long titles, missing
  image and in-store-exclusive treatment.
- [x] Verify header, search, mobile menu, cart drawer and account flows.
- [x] Check that all buttons use consistent sizing and that `Contact us`
  matches its peer actions.
- [x] Test desktop and mobile layouts for overlap, overflow and accidental
  whitespace.
- [x] Run visual screenshots against the development theme before every push.

Phase 4 progress and evidence:

- Theme Check now reports zero offenses for the fixed theme repository after
  repairing header markup, section schema validity, translation coverage,
  unused Liquid assignments, variable naming and orphaned snippets.
- Collection, filter, endless-loading and product-count regression tests now
  pass for canonical brand pages and `/collections/all` brand-filter URLs.
- The theme now shows the filtered visible product count for A Kind of Guise
  when only products with an active online sale window are rendered.
- `tools/audit-brand-storefront.py` records rendered product-count labels and
  treats the A Kind of Guise storefront visibility policy explicitly.
- `tools/audit-endless-scroll.mjs` records product-count labels, checks them
  against expected product totals and tolerates non-critical Chrome temp-file
  cleanup locks.
- Product-card regression tests crawled 293 rendered cards across published
  brand collections plus `in-store-exclusive`; normal price, sale price, long
  title and in-store-exclusive cards passed. No visible missing-image card was
  rendered, which matches the catalog policy that keeps missing-image products
  out of the storefront.
- Header, search, mobile menu, cart drawer and account flows pass browser QA.
  Search submits `drake` to `/search`, the cart drawer opens with the empty
  state and account links resolve to Shopify's customer-authentication
  handoff.
- Product action button QA passes on desktop and mobile. `Contact us` on the
  in-store-exclusive product uses the same full-width product-form button
  treatment as `Add to cart`: 45px tall, matching font sizing, matching width
  in its product column and no text overflow.
- Desktop and mobile layout smoke tests pass across collection, filtered
  collection, product, search and policy pages. The audit found no horizontal
  overflow, no product-grid overlaps and no empty main-content states.
- Visual screenshot QA captured 18 desktop/mobile screenshots across
  collection, filtered collection, A Kind of Guise, in-store-exclusive,
  product, search, contact policy and lookbook templates. The mobile lookbook
  image spacing was tightened after review and the screenshot batch was
  regenerated with zero blank-candidate errors.
- `.tmp/phase4-theme-check-clean-20260729.json`
- `.tmp/phase4-brand-storefront-regression-20260729.json`
- `.tmp/phase4-brand-storefront-regression-20260729.md`
- `.tmp/phase4-drakes-endless-scroll-20260729.json`
- `.tmp/phase4-drakes-endless-scroll-20260729.png`
- `.tmp/phase4-akog-endless-scroll-20260729.json`
- `.tmp/phase4-akog-endless-scroll-20260729.png`
- `.tmp/phase4-filter-drakes-endless-scroll-20260729.json`
- `.tmp/phase4-filter-drakes-endless-scroll-20260729.png`
- `.tmp/phase4-filter-akog-endless-scroll-20260729.json`
- `.tmp/phase4-filter-akog-endless-scroll-20260729.png`
- `.tmp/phase4-product-card-audit-20260729.json`
- `.tmp/phase4-product-card-audit-20260729.md`
- `.tmp/phase4-theme-flows-20260729.json`
- `.tmp/phase4-theme-flows-desktop-20260729.png`
- `.tmp/phase4-theme-flows-mobile-20260729.png`
- `.tmp/phase4-visual-screenshots-20260729.json`
- `.tmp/phase4-visual-screenshots-20260729/`

## Phase 5: Cart and checkout review

- [x] Map the current cart-to-checkout journey on desktop and mobile.
- [ ] Clarify delivery versus Arnhem store pickup before payment.
- [ ] Make line items, quantity changes, removals, discounts and totals easy to
  scan.
- [ ] Confirm shipping costs and delivery expectations appear at the right
  moment.
- [x] Review express checkout hierarchy without weakening the normal checkout.
- [ ] Check payment methods, legal links, error messages and recovery states.
- [ ] Validate abandoned-cart and confirmation emails.
- [x] Treat checkout changes as recommendations first; apply only after
  approval.

Phase 5 evidence, 2026-07-29:

- Added `tools/audit-cart-checkout.mjs`, a read-only CDP browser audit for
  product add-to-cart, cart drawer, cart page, quantity controls, removals,
  checkout handoff and desktop/mobile screenshots.
- Browser evidence was generated in
  `.tmp/phase5-cart-checkout-20260729.json` and
  `.tmp/phase5-cart-checkout-20260729/`.
- The cart journey renders on desktop and mobile: add-to-cart opens the drawer,
  line items show image/name/variant/price/quantity/remove controls, the cart
  page shows the normal checkout button first, and Shop Pay/Google Pay are
  secondary below it.
- Policy links are visible in the cart-page footer. Payment icons are not
  rendered in this local cart-page footer configuration.
- Product pages show pickup availability for online-purchasable products, but
  cart drawer and cart page only mention taxes/discounts/shipping calculated at
  checkout. Recommendation: add approved cart-level copy that explains delivery
  versus Arnhem store pickup before payment.
- Local checkout handoff from the theme preview changed URL but landed on a
  Chrome/HTTP 401 error page, so checkout payment methods, shipping rates and
  legal links still need live-store or authenticated-preview verification.
- Notification email validation is outside storefront theme scope; validate
  abandoned-cart and confirmation emails through Shopify notification previews
  or Phase 7 test orders.
- Theme cart fixes applied in `assets/global.js` and `assets/cart.js`: quantity
  button handling now uses the clicked button instead of nested icon targets,
  and Shopify 422 cart responses now surface `description/message` text in the
  cart error UI.
- Follow-up verification is pending because repeated cart endpoint checks
  temporarily triggered Shopify preview `429 Too Many Requests` responses on
  `/cart.js`.

## Phase 6: Performance and accessibility

- [ ] Measure key templates with mobile and desktop Lighthouse runs.
- [ ] Optimize oversized images and unnecessary eager loading.
- [ ] Review JavaScript loaded on collection and product templates.
- [ ] Test keyboard navigation, focus visibility, labels and modal behavior.
- [ ] Check color contrast and reduced-motion behavior.
- [ ] Verify layout stability while product imagery and fonts load.

## Phase 7: Operational QA

- [ ] Test taxes, shipping zones, pickup location and order notifications.
- [ ] Place test orders for shipping, pickup, discount, refund and cancellation.
- [ ] Verify inventory changes flow correctly through Shopify and Lightspeed.
- [ ] Review policies, contact details, store hours and footer links.
- [ ] Confirm analytics, consent and conversion events without duplicate fires.
- [ ] Create a launch rollback checklist and named recovery owner.

## Phase 8: Redirects and canonical URL migration

This phase is deliberately late because redirects should be created after
product, collection and page handles are final. The store is not live yet, so
temporary internal handle changes have limited customer impact. Redirects are
still a launch-critical requirement because an existing domain, old platform,
bookmarks, campaign links or previously indexed URLs can point at the old
paths.

- [ ] Export URLs from the current/previous storefront, sitemap, menus,
  analytics and search data.
- [ ] Inventory every removed or changed product, collection and page handle.
- [ ] Map each old URL to the closest canonical replacement; avoid redirecting
  unrelated brand pages to the homepage.
- [ ] Add a redirect from typo path `/collections/stelff` to
  `/collections/steiff` if the typo URL has ever been shared or indexed.
- [ ] Decide mappings for removed old brand collections based on actual old
  traffic and replacement stock.
- [ ] Preserve and verify the existing `/pages/contact` to `/pages/brickstore`
  redirect.
- [ ] Import redirects in one reviewed batch.
- [ ] Crawl all old URLs and verify one-hop `301` responses without chains or
  loops.
- [ ] Re-run the redirect crawl immediately before launch.

## Phase 9: Pre-launch and launch

- [ ] Freeze catalog handles and navigation.
- [ ] Export a final Shopify baseline.
- [ ] Run Theme Check, storefront crawl, visual QA and checkout tests.
- [ ] Confirm no published empty collections or public draft-only journeys.
- [ ] Confirm robots, sitemap, domain, SSL and search-engine settings.
- [ ] Publish the approved theme version.
- [ ] Monitor orders, errors, 404s, sync drift and performance after launch.

## Commands

Read-only draft audit:

```powershell
python tools\audit-shopify-drafts.py
```

Phase 1 catalog dry-run:

```powershell
python tools\manage-shopify-phase1-catalog.py
```

Apply the exact allowlisted Phase 1 catalog plan:

```powershell
python tools\manage-shopify-phase1-catalog.py --apply
```

The apply command must only be used after reviewing its dry-run report and
confirming a valid current baseline exists.
