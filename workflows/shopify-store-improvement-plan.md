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

- On 11 August 2026, 18 reviewed English product descriptions were applied:
  14 blank descriptions, three short placeholder descriptions and one incorrect
  Malin+Goetz description. Conflict checks passed for all 18 products, the
  Admin API read-back found zero content mismatches and the public product feed
  subsequently reported zero blank or short descriptions across 294 public
  products. The original descriptions remain available in
  `.tmp/product-description-apply-20260811/preflight-and-rollback.json`.
- `.tmp/product-description-apply-20260811/apply-results.json`
- `.tmp/product-description-post-apply-20260811.json`
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

- [x] Measure key templates with mobile and desktop browser audit runs.
- [x] Optimize oversized images and unnecessary eager loading.
- [x] Review JavaScript loaded on collection and product templates.
- [x] Test keyboard navigation, focus visibility, labels and modal behavior.
- [x] Check reduced-motion behavior and avoid new contrast regressions in touched
  components.
- [x] Verify layout stability while product imagery and fonts load.

Phase 6 evidence, 2026-07-29:

- Added `tools/audit-performance-accessibility.mjs` for repeatable CDP checks
  across home, brand collection, filtered collection, regular product,
  in-store-exclusive product, lookbook and cart in desktop and mobile
  viewports. In this Codex sandbox Chrome must be launched externally via
  PowerShell and the tool should be run with `--connect-port` because Node
  cannot spawn Chrome directly here.
- Final CDP audit: 7 templates x 2 viewports = 14 runs, `failedRuns: 0`, zero
  error-severity issues and 20 warning-severity issues. Remaining warnings are
  `many_script_resources` from the Shopify/theme preview stack on all runs and
  `initial_viewport_image_lazy_loaded` on collection/product secondary imagery
  where we intentionally avoid making the whole initial grid eager.
- Theme validation passed with `shopify.cmd theme check --path . --output json`
  returning `[]`. JS syntax checks passed for `assets/collection-title.js`,
  `assets/lookbook-viewer.js` and
  `tools/audit-performance-accessibility.mjs`.
- Storefront fixes applied in the theme: moved collection-title body script to
  deferred `assets/collection-title.js`, removed invalid inline CSS, moved
  large inline theme styles to `assets/base.css`, removed product-page loading
  of the collection image-mode toggle, reduced product/lookbook/hero image
  widths and fetch priorities, fixed mobile drawer nested links, added explicit
  logo link labelling, added `inert` handling for inactive split-hero and
  lookbook UI, tightened focus-visible and reduced-motion states, and fixed
  mobile collection/card containment.
- Browser evidence was generated in `.tmp/phase6-*.html`,
  `.tmp/phase6-*.png` and
  `.tmp/phase6-performance-accessibility-20260729.json`.

## Phase 7: Operational QA

- [ ] Test taxes, shipping zones, pickup location and order notifications.
- [ ] Place test orders for shipping, pickup, discount, refund and cancellation.
- [ ] Verify inventory changes flow correctly through Shopify and Lightspeed.
- [ ] Review policies, contact details, store hours and footer links.
- [ ] Confirm analytics, consent and conversion events without duplicate fires.
- [x] Create a launch rollback checklist and named recovery owner.

Phase 7 progress and evidence, 2026-08-05:

- Merchant confirmation received on 2026-08-11 that Shopify approved
  iDEAL | Wero for the store. Eligibility is no longer an open gate; activation
  and a successful payment still need to be confirmed during the live test-order
  pass.
- Added the read-only Admin API audit `tools/audit-operational-qa.py`. Its
  current report, `.tmp/phase7-operational-qa-20260805.json`, contains nine
  checks: three pass, two warn, four manual and zero blocked. Delivery-profile
  and order reads are limited by the current app scopes; both limitations are
  retained in `readErrors` instead of being treated as successful checks.
- Taxes are configured in EUR with tax-inclusive storefront prices. Shipping
  is not marked taxable. The `Neighbourhood Store` location is active,
  fulfills online orders and exposes local pickup with a four-hour preparation
  time.
- Shipping remains a launch blocker, but the Phase 9 live-store retest refined
  the cause. A stocked `beanie-onyx` cart returned NL `EUR 9.50`, DE `EUR
  15.00` and US `EUR 40.00` rates and passed all 14 cart flows. An online
  `105-standard-one-wash` cart at `EUR 309.50` still returned no NL or DE rate
  while US returned `EUR 40.00`. Review shipping-profile assignment and price
  thresholds, then rerun both a normal and a cart-over-EUR-300 case before test
  orders. Delivery-profile reads remain unavailable to the audit token.
- The main Shopify shop address contains `Rijnstraat 14-B` and `6811 EV`, but
  its city and phone fields are blank. The fulfillment/pickup location does
  contain Arnhem, and all public contact policies contain the complete address,
  phone and email. Complete the primary shop fields in Shopify Admin before
  launch.
- The final cart report `.tmp/phase7-cart-checkout-20260805.json` records 12
  passing flows, one preview-only `not_applicable` checkout handoff and one
  failed shipping-rate flow. Desktop and mobile add, drawer, cart page,
  quantity, remove and empty-cart states all pass. Screenshots are stored in
  `.tmp/phase7-cart-checkout-20260805/`.
- All six policies render substantive content and all 12 internal footer links
  return successful responses. A temporary address, contact and opening-hours
  block on the Brick Store page was rejected because it disrupted the design;
  `templates/page.brickstore.json` was restored to its original concise
  `Arnhem, Netherlands` content. Contact details and store hours therefore
  remain an open presentation task and must be solved without recreating that
  large information block.
- The browser report `.tmp/phase7-operational-storefront-20260805.json` has
  five passing checks, two warnings and two expected content failures after
  restoring the original Brick Store design. Decline and accept are usable, no
  marketing request fires before consent or after decline, the consent
  controls fit at 390 px and no duplicate named events were observed.
- No third-party marketing analytics request or add-to-cart conversion event
  was observed after consent. Add-to-cart itself succeeds. Decide which
  analytics/customer-pixel stack is intended, configure its destination ID in
  Shopify Customer Events, and rerun the audit before checking this item off.
- Tagged shipping, pickup, discount, refund and cancellation orders have not
  been completed. The current app requires merchant approval for `read_orders`,
  so these flows and their delivered notification emails remain manual launch
  gates.
- Inventory drift is not current: the last zero-drift catalog evidence is from
  2026-07-25 and there are no fresh Lightspeed product/inventory CSV files in
  the workspace. Export both sources from the same current moment and rerun
  `tools/run-catalog-sync-audit.py` before launch.
- Added `workflows/shopify-launch-rollback.md`. David is the named recovery
  owner. Current IDs were reconfirmed with Shopify CLI: live theme
  `186898579784`, development theme `187323318600`. A replacement owner still
  needs to be assigned for periods when David is unavailable.

Open Phase 7 launch gates, in order:

1. Shipping rates for NL, DE and US are configured and verified as of
   2026-08-26 (see Phase 9 shipping-rate fix). Complete the shipping and
   pickup test orders.
2. Complete the primary Shopify shop city and phone fields.
3. Find a restrained place for public contact details and store hours that
   preserves the original Brick Store composition.
4. Approve `read_orders` or execute and document the five tagged test-order
   scenarios manually, including delivered notification emails.
5. Export fresh Lightspeed product and inventory CSV files and prove zero
   unexpected Shopify drift.
6. Configure the intended analytics destination and prove one add-to-cart
   conversion event after consent with no duplicate fire.
7. Assign a replacement recovery owner and rehearse the theme rollback once
   without changing the live storefront.

## Phase 8: Redirects and canonical URL migration

This phase is deliberately late because redirects should be created after
product, collection and page handles are final. The store is not live yet, so
temporary internal handle changes have limited customer impact. Redirects are
still a launch-critical requirement because an existing domain, old platform,
bookmarks, campaign links or previously indexed URLs can point at the old
paths.

- [ ] Export URLs from the current/previous storefront, sitemap, menus,
  analytics and search data.
- [x] Inventory every removed or changed product, collection and page handle.
- [ ] Map each old URL to the closest canonical replacement; avoid redirecting
  unrelated brand pages to the homepage.
- [ ] Add a redirect from typo path `/collections/stelff` to
  `/collections/steiff` if the typo URL has ever been shared or indexed.
- [ ] Decide mappings for removed old brand collections based on actual old
  traffic and replacement stock.
- [x] Preserve and verify the existing `/pages/contact` to `/pages/brickstore`
  redirect.
- [ ] Import redirects in one reviewed batch.
- [ ] Crawl all old URLs and verify one-hop `301` responses without chains or
  loops.
- [ ] Re-run the redirect crawl immediately before launch.

Phase 8 preparation progress, 2026-08-05:

- This phase is preparation-only until an explicit later approval. No redirect
  was created, imported, changed or deleted in Shopify. No theme or storefront
  write is part of the preparation tool.
- Exported a fresh read-only Shopify baseline to
  `.tmp/phase8-shopify-baseline-20260805`: 1,062 products, 61 collections, four
  pages and one existing redirect.
- Added `tools/prepare-shopify-redirects.py`. It intentionally has no
  `--apply` option and writes only local review files. The current report is
  `.tmp/phase8-redirect-preparation-20260805/redirect-preparation.json`; its
  companion spreadsheet-friendly review file is `redirect-review.csv`.
  `redirect-ready-for-review.csv` contains only the 368 strongest candidates
  and is deliberately not shaped as a Shopify import file.
- Added `tools/crawl-existing-storefront.py` and generated
  `.tmp/phase8-existing-site-crawl-20260805/existing-site-crawl.json` plus CSV
  inventories. The crawler is limited to internal `GET` requests, respects
  `robots.txt`, has no apply mode and blocks account, checkout, cart mutation,
  cookie-action and asset URLs. It found 1,321 unique URLs and safely fetched
  772 content URLs: 766 returned `200`, six returned `404` and none errored.
  Another 547 action URLs were guarded and two robots-disallowed URLs were not
  fetched. Validation confirmed zero guarded or robots-disallowed requests.
- The old Lightspeed sitemap contains 541 entries but only 539 unique paths;
  two blog URLs are duplicated. The crawl discovered 782 URLs outside the
  sitemap. Of those, 233 are fetched content URLs relevant to migration review
  and 549 are protected or robots-disallowed. The tool also reads 20 internal
  links from the old homepage and 18 from the local Shopify preview.
- The combined deduplicated plan now has 804 unique source paths: 368 are
  high-confidence candidates ready for human review, 431 require a destination
  decision, one typo candidate requires evidence and four action/root paths
  need no redirect. The crawl contributed 232 new rows. There are zero duplicate
  source paths, source conflicts or proposed chains. `ready for review` does
  not mean approved for import.
- The crawl found 61 non-canonical paths, all in pagination or equivalent
  listing routes: 20 shop, 19 blog, 17 other listing and five brand paths. Their
  canonical evidence is retained per review row. Six internally linked old
  URLs currently return `404`: an empty blog-tag route, a Cloudflare email
  endpoint, two product links, `/service/stores/` and `/ukiyo-kids/`. The
  Cloudflare endpoint needs no redirect; the other routes remain explicit
  mappings or decisions. Product metadata fetching now records HTTP `404`
  responses instead of aborting the preparation run.
- Of 337 old product URLs, 282 exactly match a public Shopify product handle.
  Fifty exact targets are draft or unpublished and five old product slugs have
  no exact Shopify handle, so those 55 remain decisions. The five unmatched
  pages were read individually: four are in-stock Messyweekend sunglasses and
  one is an in-stock `encens d'auroville` product. Both vendors are explicitly
  retired by the catalog policy, so these URLs require a retirement decision
  rather than an unrelated replacement redirect.
- Comparing the Phase 0 and current Shopify baselines found zero handle changes
  for surviving resource IDs, 21 removed resources, 21 added resources and one
  collection recreated at the same handle. The recreated
  `/collections/atelier-neighbourhood` path needs no redirect.
- The old public `/collections/stelff` collection is present in the Phase 0
  baseline and `/collections/steiff` exists now. A web-index search returned no
  Neighbourhood result for the typo, so the row remains `needs_evidence` until
  request logs, analytics or another sharing record confirms it was used.
- Thirteen old brand paths need decisions. Current replacement-stock evidence
  is included per vendor. `King & Tuckfield` and `Malin & Goetz` have public
  products and plausible normalized collection targets; inactive, unpublished
  or absent brands are not redirected to the homepage by default.
- The sitemap has 98 blog entries representing 96 unique paths: 95 articles and
  the blog index. Another 177 crawl-only blog pagination and tag paths are under
  `/blogs/arnhem/`. They remain unmapped because Shopify blog articles are not
  in the current baseline export and a migration versus retirement decision has
  not been made.
- Added `tools/prepare-blog-review.py` and generated the preparation-only review
  inventory in `.tmp/phase8-blog-review-20260805`. It confirms 95 unique live
  article URLs and 95 unique canonicals; every article returns `200` and has a
  title, publication date and short summary. The review CSV and Markdown files
  have blank decision and notes fields. The articles comprise three from 2025,
  25 from 2024 and 67 from 2023. No article was created, changed, published or
  removed during this inventory.
- Read the completed `blog bestand.xlsx` with
  `tools/read-blog-review-decisions.py` and stored a hashed, local decision
  report in `.tmp/phase8-blog-decisions-20260805`. All 95 IDs, paths and URLs
  match the source inventory with no duplicates, missing rows or unknown
  decision values. The completed sheet selected 79 articles to keep, 13 to
  remove and three as unsure. A later explicit local override retains all three
  unsure articles, bringing the final preparation decision to 82 keep and 13
  remove. No blog or Shopify resource was changed.
- Added `tools/audit-shopify-blogs.py` and confirmed via a read-only Admin API
  audit that Shopify already has one blog: title `Blogs`, handle `news`, with
  two published test articles. Both test articles have a featured image. The
  audit tool now prefers current client credentials because the stored Admin
  token returned `401`; this matches the existing baseline export behavior.
- Added `tools/prepare-shopify-blog-migration.py` and generated a draft-only,
  no-apply preflight in `.tmp/phase8-shopify-blog-migration-20260805`. It
  prepares all 82 retained articles with clean body HTML, original handles,
  date-only publication metadata and featured-image inputs. All 82 unique hero
  images are reachable. Three retained articles contain ten additional inline
  images; all ten are reachable but must be moved to Shopify Files and their
  body URLs rewritten before final publication.
- The initial URL-preserving recommendation was handle `arnhem`. The user first
  selected `blogs` and then finalized the blog as title `Journal`, handle
  `journal`. The migration target is therefore
  `/blogs/journal/<legacy-handle>`, and retained legacy article paths will
  require redirects.
- The migration preflight rewrote 46 high-confidence internal links. Eighteen
  occurrences across 14 unique old links remain unresolved because they point
  to removed brands, unpublished Shopify resources, a removed blog article or
  old `404` pages. They are intentionally not redirected or rewritten yet.
- The current theme is already compatible with the prepared content: the blog
  grid renders `article.image`, title and summary, while the article template
  renders a responsive featured image, title, publication date and body. The
  migration manifest contains draft inputs with `isPublished: false`; it has no
  execution or apply mode.
- On 5 August 2026, after explicit approval, the guarded
  `tools/apply-shopify-blog-draft-test.py` run changed the existing blog handle
  from `news` to `blogs`. Shopify's `redirectNewHandle` and `redirectArticles`
  options were enabled. The same run created legacy review item 47, `Instagram`,
  as draft article `gid://shopify/Article/615211368776`; post-write verification
  confirms `isPublished: false` and no publication date.
- A subsequent explicitly approved `tools/update-shopify-blog-settings.py` run
  changed title `Blogs` / handle `blogs` to title `Journal` / handle `journal`.
  The draft remains attached to the same blog and remains unpublished.
- The `Instagram` featured image was copied successfully to Shopify CDN with
  alt text. Its one inline body image renders from the original Lightspeed CDN.
  The current app token has `write_content` but not `write_files`, so moving
  inline images into Shopify Files remains a separate pre-publication step. A
  fresh read-only audit reports one blog, three articles, two published articles,
  one draft and featured images on all three articles. Shopify created six
  automatic redirects across the two handle changes: three `news` to `blogs`
  and three `blogs` to `journal`. These currently form redirect chains and must
  be flattened to direct `news` to `journal` routes before launch. The refreshed
  bulk manifest targets `/blogs/journal`, excludes the existing `instagram`
  draft and contains 81 remaining draft inputs, all with `isPublished: false`.
- The existing `/pages/contact` to `/pages/brickstore` redirect remains
  preserved alongside the six automatic blog-handle redirects.
- Source gaps are explicit: no analytics landing-page export and no Search
  Console URL export are available in the workspace. The first Phase 8 item
  stays open until those are supplied or explicitly waived.

Phase 8 preparation refresh, 2026-08-11:

- The merchant reconfirmed that Phase 8 remains preparation-only because the
  migration is not happening yet. No redirect was imported, no blog was
  created or published and no Shopify content was changed during this refresh.
- Re-crawled the existing storefront with four read-only workers. The fresh
  crawl found 1,310 URLs and zero errors. Merging it with the 5 August crawl
  preserves 13 historical-only URLs, including five product pages and one
  collection pagination route that disappeared from the latest crawl.
- Exported a fresh read-only Shopify baseline with 1,062 products, 61
  collections, four pages and seven existing redirects. Product and collection
  counts are unchanged; the six additional redirects are the automatic blog
  handle redirects already documented above.
- Rebuilt the redirect preparation from the merged crawl history and fresh
  baseline. The master inventory has 805 unique paths: 367 direct candidates
  for later review, 83 conditional Journal mappings, 350 open decisions and
  five routes where no manual redirect is recommended. All review CSV files
  contain `import_ready=false` and blank approval fields; no Shopify-shaped
  import file was produced.
- Re-read all 95 legacy articles with four workers and repaired the body-text
  encoding before regenerating decisions. Validation reports 82 retained and
  13 removed articles, zero fetch errors, zero missing dates or hero images and
  zero mojibake occurrences.
- Rebuilt the Journal draft manifest with four workers. It contains 81 pending
  unpublished draft inputs because the retained `instagram` test draft already
  exists. All 82 hero images and all ten inline images are reachable. Eighteen
  internal-link occurrences across 14 unique URLs remain explicit decisions.
- Added `tools/merge-storefront-crawl-history.py` and
  `tools/prepare-phase8-review-pack.py`. Final local validation reports zero
  duplicate source paths, zero base conflicts, zero proposed redirect chains,
  zero loops and three existing automatic blog chains that must be flattened
  only during the later migration.
- Review pack: `.tmp/phase8-master-review-20260811/`.

Phase 8 retained-blog draft upload, 2026-08-11:

- After a separate explicit approval, uploaded the 81 pending retained articles
  to the existing `Journal` blog as unpublished drafts. The guarded tool has no
  publication or redirect mutation, forces `isPublished=false` and writes a
  local creation register after every successful article.
- Post-write verification matched all 81 new drafts on handle, title, body
  text, summary, inline-image references, Journal ownership and unpublished
  status. All 81 featured images were copied to Shopify CDN and there were zero
  verification failures.
- The retained migration set is now complete in Shopify: 82 of 82 retained
  handles are present, all 82 are drafts, none is published and all 82 have a
  featured image. This includes the earlier `instagram` draft.
- Journal now contains 84 articles total. The two pre-existing published test
  articles `inkoop-uitkoop-duurkoop` and `dit-is-een-test` are not part of the
  retained migration set and were left unchanged.
- No redirect was created, imported, changed or deleted. No retained article
  was published. The ten inline images across three retained drafts still use
  the old Lightspeed CDN and must be moved to Shopify Files before publication;
  18 link occurrences across 14 unique old URLs also remain pre-publication
  decisions.
- Added `tools/apply-shopify-blog-drafts.py`.
- Apply evidence:
  `.tmp/phase8-shopify-blog-draft-upload-20260811/apply-results.json`.
- Independent post-upload audit:
  `.tmp/phase8-shopify-blog-audit-post-draft-upload-20260811/shopify-blog-audit.json`.
- The regenerated post-upload migration manifest contains zero pending draft
  inputs. A final idempotent dry-run recognizes all 81 bulk-uploaded handles as
  matching existing drafts with zero conflicts and zero creates required.
- Current review pack:
  `.tmp/phase8-master-review-post-draft-upload-20260811/`.

Phase 8 retained-blog publication, 2026-08-11:

- After explicit publication approval, a guarded live preflight matched exactly
  82 retained Journal drafts and the two confirmed test handles, with zero
  conflicts. A full pre-mutation rollback snapshot was stored locally.
- Published all 82 retained articles while preserving each legacy source
  publication date. Post-write comparison found zero missing or unexpected
  handles, zero drafts, zero title mismatches and zero source-date mismatches.
- Only after all 82 retained articles passed publication verification, deleted
  the two confirmed tests `dit-is-een-test` and
  `inkoop-uitkoop-duurkoop`.
- An independent Admin API audit now reports one blog, 82 articles, 82
  published articles and featured images on all 82. All 82 Shopify CDN
  featured-image URLs are publicly reachable.
- Public checks returned `200` for the Journal index and sampled oldest,
  existing and newest retained articles. Both deleted test URLs return `404`.
- No redirect was created, imported, changed or deleted. The six automatic
  blog-handle redirects remain unchanged for later migration work.
- The ten inline body images across three retained articles still use the old
  Lightspeed CDN and remain reachable. The 18 unresolved internal-link
  occurrences across 14 unique old URLs also remain explicit follow-up work;
  the merchant approved publication with these known items still open.
- Added `tools/publish-shopify-retained-blogs.py`.
- Preflight and rollback evidence:
  `.tmp/phase8-shopify-blog-publication-20260811/preflight-and-rollback.json`.
- Mutation register and apply evidence:
  `.tmp/phase8-shopify-blog-publication-20260811/mutation-register.json` and
  `.tmp/phase8-shopify-blog-publication-20260811/apply-results.json`.
- Independent post-publication audit:
  `.tmp/phase8-shopify-blog-audit-post-publication-20260811/shopify-blog-audit.json`.

Phase 8 retained-blog internal-link update, 2026-08-11:

- Audited the rendered HTML of all 82 Journal articles after publication. The
  previously reported 18 unresolved article/link pairs represented 21 actual
  anchor occurrences across 14 unique legacy URLs and 16 articles; repeated
  links inside the same article account for the difference.
- Rewrote nine anchors to four verified public Shopify targets: the canonical
  `King & Tuckfield` and `Malin & Goetz` collections and the relevant Filson
  and Welter Shelter Journal articles.
- Removed twelve anchors that had no public Shopify equivalent while
  preserving their complete visible text and nested formatting. This avoids
  sending readers to removed brands, draft products, a retired article or old
  `404` routes.
- Independent Admin API comparison found exactly 16 changed article bodies and
  66 unchanged bodies. All titles, handles, source publication timestamps,
  publication states and featured images remained unchanged. No old
  `nbharnhem.com` href remains in any Admin article body.
- The final article bodies contain 55 internal link occurrences across 24
  unique Shopify targets. All 24 targets return `200`. The four newly used
  targets occur exactly as planned: one King & Tuckfield collection link,
  three Malin & Goetz collection links, four Filson Journal links and one
  Welter Shelter Journal link.
- Journal content is English-only. Shopify contains zero article translations,
  and no localized Journal copy needs to be created or maintained. Any
  locale-prefixed storefront routes are inherited from the store-wide language
  configuration rather than separate article content.
- Shopify's distributed article `page_cache` served mixed old and new Filson
  snapshots after the Admin update. Guarded two-write cache refreshes through
  both GraphQL and REST restored the exact final body byte-for-byte, but did
  not purge every pre-existing anonymous page-cache node. A final 20-request
  sample still received 14 stale snapshots. Admin, translation audits and
  uncached `view` renders are clean. No riskier handle or publication toggle
  was attempted; public cache expiry must be rechecked before domain migration.
- Redirects were not mutated and the six automatic blog-handle redirects are
  byte-for-byte unchanged.
- Added `tools/update-shopify-blog-internal-links.py` and
  `tools/audit-shopify-blog-translations.py`.
- Link-update evidence:
  `.tmp/phase8-shopify-blog-link-update-20260811/preflight-and-rollback.json`,
  `.tmp/phase8-shopify-blog-link-update-20260811/mutation-register.json` and
  `.tmp/phase8-shopify-blog-link-update-20260811/apply-results.json`.
- Cache-refresh evidence:
  `.tmp/phase8-shopify-blog-link-cache-refresh-20260811/` and
  `.tmp/phase8-shopify-blog-link-rest-cache-refresh-20260811/`.
- Independent post-link audit:
  `.tmp/phase8-shopify-blog-audit-post-link-update-20260811/shopify-blog-audit.json`.
- Final Admin audit after both cache-refresh attempts:
  `.tmp/phase8-shopify-blog-audit-final-link-state-20260811/shopify-blog-audit.json`.
- Translation audit:
  `.tmp/phase8-shopify-blog-translation-audit-20260811/shopify-blog-translation-audit.json`.

## Phase 9: Pre-launch and launch

- [ ] Freeze catalog handles and navigation.
- [x] Export a current pre-launch Shopify baseline; repeat immediately before
  cutover.
- [x] Run Theme Check, storefront crawl, visual QA and checkout tests in
  no-order mode.
- [x] Reconcile and validate the approved local theme in a development preview;
  keep it unpublished until the migration window.
- [x] Confirm no published empty collections or public draft-only journeys.
  The draft journey check passes; the 19 empty public collections were
  unpublished on 2026-08-26 (see below).
- [ ] Confirm robots, sitemap, domain, SSL and search-engine settings. Current
  robots/TLS/public-access checks pass; custom-domain cutover and post-cutover
  canonical/SSL verification remain open.
- [x] Publish the approved theme version. Published on 2026-08-26 (see below).
- [ ] Monitor orders, errors, 404s, sync drift and performance after launch.

Phase 9 read-only preflight evidence, 2026-08-11:

- No theme publish/push, domain or DNS change, redirect import, catalog
  publication, payment or order write was performed. The consolidated report is
  `.tmp/phase9-prelaunch-20260811/phase9-preflight.md`, with structured evidence
  in `.tmp/phase9-prelaunch-20260811/phase9-preflight.json`.
- Exported a fresh Shopify baseline to
  `.tmp/phase9-shopify-baseline-20260811`: 1,062 products (`294` active, `768`
  draft), 61 Admin collections, seven existing redirects and four pages. All
  294 active products have body copy and images and are published.
- The 294 active product handles match the 294 public product routes exactly.
  No draft handle is public and no active handle is missing from the storefront.
- Pulled the actual live theme `186898579784` to
  `.tmp/phase9-live-theme-20260811`. Compared with
  `C:\Users\david\Documents\neighbourhood-theme`, both sides contain 326 theme
  files, but there are 70 normalized content differences, two repository-only
  files and two live-only files. The comparison is saved as
  `.tmp/phase9-theme-divergence-20260811.json`.
- Theme Check on the live pull reports 29 errors and 23 warnings: one dynamic
  tag syntax error in `sections/header.liquid`, one invalid schema placement in
  `sections/email-signup-banner.liquid`, and 27 missing locale translations.
  The local theme repository returns `[]` with zero offenses, so the approved
  source must be reconciled with the live theme before publication.
- Improved `tools/crawl-existing-storefront.py` to follow Shopify sitemap
  indexes, select the root locale, retry throttled requests, use reusable HTTP
  sessions and guard query-dependent vendor and consent-action routes. The
  four-worker English storefront crawl processed 449/449 URLs from 438 sitemap
  entries with zero network errors and no crawl limit.
- The crawl's two raw 404 paths are functional-route artifacts, not broken
  content: `/collections/vendors` requires its recorded `?q=` parameter and
  `/policies/#shopifyReshowConsentBanner` is intercepted by Shopify's consent
  UI. Full vendor URLs return successfully and the consent UI reopens in the
  browser audit.
- Added `tools/audit-public-shopify-collections.py`. It found 19 published empty
  collections, all present in the sitemap and internally referenced: `boots`,
  `denim`, `frontpage`, `hoodies`, `in-store-only`, `jackets`, `loafers`,
  `outerwear`, `overshirts`, `pants`, `polos`, `sandals`, `scarves`, `shirts`,
  `shoes`, `shorts`, `sweats`, `t-shirts` and `vests`. Populate, replace or
  unpublish them before launch.
- Core browser flows pass 7/7 on desktop and mobile. A stocked online product
  passes 14/14 cart, quantity, removal, shipping-rate and checkout-handoff
  checks without placing an order. Cart copy still does not explain delivery
  versus Arnhem pickup before checkout.
- Captured 26 desktop/mobile screenshots across home, collections, regular and
  in-store-exclusive products, search, contact, Journal, an imported article,
  Lookbook and cart. Home, contact and Journal imagery render correctly. The
  initial consent banner and preferences both fit at 390 px in the DOM audit.
- The performance/accessibility run covers seven templates in 14 viewport runs.
  Core pages pass; Lookbook fails desktop and mobile because 88 focusable
  elements remain inside `aria-hidden` content. Lookbook transferred about 6.8
  MB desktop and 3.6 MB mobile in this run and remains the heaviest template.
- The current Shopify URL is public and not password protected. `robots.txt`
  returns 200, does not globally disallow the storefront and points to the
  Shopify sitemap. Current TLS certificates validate. The sitemap index exposes
  root English content plus `de`, `es` and `fr` locale sitemaps; these global
  locale settings were intentionally left untouched.
- `nbharnhem.com` still serves the Lightspeed storefront and all checked Shopify
  canonicals use `neighbourhood-arnhem.myshopify.com`. Custom-domain/DNS
  cutover, redirect import, canonical refresh and Shopify SSL revalidation are
  launch-window tasks only.
- The primary Shopify shop profile still lacks city and phone. Public contact,
  legal-notice and policy pages contain the complete business details, but the
  Admin shop identity should be completed before launch.
- Journal remains healthy: one `journal` blog, 82 published articles and 82
  featured images. The root-locale crawl found the Journal index plus all 82
  articles.
- iDEAL | Wero approval is merchant-confirmed, but activation and real payment,
  notification, pickup, shipping, discount, refund and cancellation tests remain
  manual launch gates requiring explicit approval.
- Added `tools/audit-shopify-domain-seo.py` and
  `tools/build-shopify-phase9-preflight-report.py` so the same domain/SEO and
  consolidated checks can be repeated immediately before cutover.

Phase 9 theme reconciliation update, 2026-08-23:

- No live theme, domain, redirect, catalog or order write was performed. The
  current theme `186898579784` was pulled again and is byte-for-byte unchanged
  from the 2026-08-11 pull.
- Added `tools/compare-shopify-themes.py` and generated the read-only comparison
  in `.tmp/phase9-theme-reconciliation-20260823/`. It excludes non-theme files
  and distinguishes semantic JSON equality from deployable code differences.
- The local repository contains 325 theme files and the live pull 326. One file
  exists only locally (`assets/collection-title.js`), two obsolete snippets
  exist only in the live pull, 15 template JSON files differ only in Shopify's
  generated formatting and 55 files contain material changes.
- Git history confirms that the local repository is the approved source: its
  material changes contain the intended AKOG visibility, Theme Check, cart,
  performance and accessibility fixes. The stray zero-byte `assets/Infi` file
  was removed locally.
- Fresh Theme Check results are zero errors and zero warnings for the approved
  repository. The unchanged live pull still reports 29 errors and 23 warnings;
  this is expected because the approved repository has deliberately not been
  published.
- A non-published development theme (`188639904072`) passed all seven desktop
  and mobile browser flows. The 14-run performance/accessibility audit has no
  error-level findings; the previous Lookbook hidden-focus failure is resolved.
  Its 20 remaining findings are optimization warnings for script count and
  initial-viewport lazy-loaded images.
- The refreshed report is
  `.tmp/phase9-prelaunch-20260823/phase9-preflight.md`. Theme reconciliation and
  Lookbook accessibility moved to passed. Three blockers remain: 19 public
  empty collections, missing NL/DE rates for the EUR 309.50 shipping edge case,
  and missing city/phone in the primary Shopify shop identity.
- Publishing the reconciled theme remains an explicit launch-window gate.

Phase 9 theme publication, 2026-08-26:

- Committed and pushed the remaining flash-loading fixes on
  `codex-lookbook-viewer-updates` (`6615bc5`, `7e75b8b`, `0617e2f`, `9c4eaa9`),
  including a fix for an oversized caret flash confirmed with a dedicated
  browser check. Theme Check on the final local repository state still
  reports zero offenses.
- Built the reconciled repository into "Launch candidate 8a95917 2026-08-26",
  pushed it to Shopify as a new unpublished theme (`188754329928`) and
  independently verified 7/7 browser flows before touching the live role.
- Captured a pre-publish snapshot of the previous live theme
  (`.tmp/pre-live-theme-publish-20260826-135809`) and kept
  "Rollback before navigation update 2026-08-25" (`188735193416`) available
  as a named rollback theme, alongside the retained former live theme
  "Codex lookbook updates 2026-06-30" (`186898579784`).
- Published `188754329928` as the live theme. Independent post-publish
  verification: 7/7 browser flows pass with zero errors and zero warnings
  (`.tmp/post-live-theme-flows-20260826.json`).
- The live storefront `neighbourhood-arnhem.myshopify.com` is public and not
  password protected. `nbharnhem.com` still serves the old Lightspeed/webshopapp
  storefront unchanged; custom-domain DNS cutover has deliberately not been
  touched and remains a separate launch-window step.
- Update `workflows/shopify-launch-rollback.md`'s confirmed recovery point
  before the next live change, since the previous live theme ID it names is
  now out of date.

Phase 9 shipping-rate fix, 2026-08-26:

- Diagnosed the missing NL/DE shipping-rate edge case with a temporary
  read-only browser audit against the live store, adding a `melton-bomber-navy`
  cart (EUR 1,299.50) and requesting rates for NL, DE and US. Before the fix,
  NL and DE both returned `shipping_rates: []`; carts at EUR 210, 350, 560 and
  619 were also checked to confirm lower amounts were unaffected.
- Root cause: the `Algemeen profiel` delivery profile's `Buurlanden` zone
  (BE/FR/DE/LU) had a `TOTAL_CART_VALUE` rate condition with an upper bound, so
  carts above that bound matched no rate tier.
- Applied a `deliveryProfileUpdate` GraphQL mutation to give the zone's
  standard rate an unbounded `TOTAL_CART_VALUE` condition (min EUR 0, no max)
  at EUR 15. Zero `userErrors`. No product, catalog or theme write was
  performed.
- Re-ran the same live browser audit after the fix:
  `tools/audit-cart-checkout.mjs --base-url
  https://neighbourhood-arnhem.myshopify.com --product melton-bomber-navy
  --shipping-only`. All three destinations now pass. NL and DE return EUR 0.00
  because the store's existing free-shipping-over-EUR-200 rule applies at this
  cart value; US returns the standard worldwide rate. `writesPerformed: false`
  and `orderPlaced: false`.
- Evidence: `.tmp/shipping-live-210-20260826.json` through
  `.tmp/shipping-live-1299-50-20260826.json` (pre-fix),
  `.tmp/delivery-rates-pre-fix-20260826.json`,
  `.tmp/delivery-rates-new-api-pre-fix-20260826.json`,
  `.tmp/fix-delivery-rate-tiers-new-api-result-20260826.json`,
  `.tmp/delivery-rates-new-api-post-fix-20260826.json` and
  `.tmp/shipping-live-post-fix-20260826.json`.
- Shipping-rate configuration is now resolved. The shipping and pickup
  **test orders** themselves (Open Phase 7 launch gate 1) are still
  outstanding and remain a manual, merchant-approved step. Two Phase 9
  blockers remain: 19 public empty collections and missing city/phone in the
  primary Shopify shop identity.

Phase 9 shop-identity decision, 2026-08-26:

- David added the missing city to the primary Shopify shop profile
  (Settings > General > Store details).
- David explicitly decided not to add a phone number to that field. This is a
  deliberate merchant choice, not an oversight, so it is no longer tracked as
  an open blocker. One open question worth a manual check before launch: some
  carriers require a sender phone number for customs paperwork on
  international (e.g. US) shipments; confirm with the carrier/customs setup
  whether that specific field, rather than the public Admin shop phone, is
  what is actually required.

Phase 9 empty-collection unpublish, 2026-08-26:

- Confirmed with David that the 19 empty published collections belong to a
  retired category taxonomy: the storefront now organizes by brand instead.
  Old category copy exists from `nbharnhem.com` and can be reused later if
  these categories become active again, but populating them is not a launch
  requirement.
- Re-verified all 19 handles as still empty on the live storefront
  immediately before writing (0 public products each), then unpublished them
  with the new guarded, allowlist-only `tools/unpublish-empty-category-collections.py`
  (REST `custom_collections` `published: false`; the app token still lacks
  `write_publications`, matching the Phase 3 precedent). The `frontpage`
  handle was checked and is not referenced anywhere in the theme repository,
  so it carries no homepage risk.
- Independent post-write verification: all 19 `/collections/<handle>` pages
  now return `404`. Zero writes were performed outside the fixed allowlist.
- Evidence: `.tmp/unpublish-empty-category-collections.json`.
- Zero Phase 9 blockers remain in this refresh. Publishing the reconciled
  theme, the shipping/pickup test orders and the other Open Phase 7 launch
  gates are still outstanding.

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
