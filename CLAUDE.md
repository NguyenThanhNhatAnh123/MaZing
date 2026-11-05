# Project: mayzing-bulk-upload

Communicate with the user in Vietnamese. Write code and comments in English.

## Current architecture

Windows Flask dashboard for design preparation, quote reading, review, export, and Mayzing bulk upload.
Use normal installed Google Chrome with manual sign-in. Playwright is not a runtime dependency.
The Manifest V3 Mayzing Session Bridge extension connects one explicitly selected Mayzing tab to
loopback HTTP. Cookies remain in Chrome; do not copy profiles, decrypt cookies, enable debugging
ports, add stealth flags, or solve security challenges.

Pairing codes last 24 hours and are single-use. Pairing alone does not enable mutations. The user
must enable automation; StoresMinimal verifies the session first. The in-memory BrowserBridge
belongs to the dashboard process, so real uploads must run through the dashboard. Restarting Chrome
or the dashboard requires pairing again. Never terminate unrelated Chrome or Python processes.

Extension 1.1.0 supports product defaults with the same activeTab/scripting/storage and loopback
permissions. Keep lib/mayzing_operations.json and generated chrome-extension/operations.js in sync.
Product operations require extension_version >= 1.1.0. A disconnected browser or an uncertain
mutation result stops the job; never automatically resend an uncertain mutation.

## Mayzing operations

All requests use the selected tab's same-origin POST /api/graphql, credentials included, with
Apollo-Require-Preflight. Uploads use GraphQL multipart operations/map/file. Use only the fixed
operation documents in lib/mayzing_operations.json.

CloneGroup returns a list of operation tasks; normalize it and wait for every returned task.
Persist clone_state before submitting the mutation: the existing group IDs, uploaded image name,
and then returned task IDs. On retry, recover an unambiguous existing group or stop for a Task log
check. A saved group_id resumes metadata/default-product updates without cloning again.

The campaign default is changed with UpdateStoreGroupProductOrder, whose input is
{id, storeSlug, productIds}. The first product ID becomes heroProductId. Preserve every other
product. Do not add heroProductId to UpdateStoreGroupInput; that operation only edits details.
StoreGroupWithProducts supplies product IDs and baseProductId. Resolve a preferred type in each
new clone by baseProductId, require one match, and verify the returned order and hero ID.

## Data and user-facing rules

New titles use the start of the quote, at most 8 words / 60 characters by default, with whole-word
truncation when possible. Keep the complete quote separately. TITLE_MAX_WORDS, TITLE_MAX_LENGTH,
and TITLE_CASE can be configured in .env; Mayzing's upper character limit is 255.

Bulk clone inherits products, prices, colors, and mockups from the chosen origin. Origins are
selected by design tone, with per-card overrides. settings.default_products maps origin group IDs
to {base_product_id, label}; freeze the preferred base ID in the batch manifest. Failed cover
updates resume the existing campaign. Publish only as requested in the user-selected job options.

input/, output/, work/, auth/, logs/, settings.json, used_slugs.txt, and .env contain local state.
Never commit credentials, login profiles, user artwork, manifests, logs, or private shop history.
Public product-label caches may exist under work/; they are optional local fallbacks, not source.

## Files and checks

config.py: configuration and settings.json.
dashboard.py + lib/dashboard.html: local UI, one background job, one process per port.
lib/browser_bridge.py: paired relay with exactly-once dispatch.
lib/mayzing.py: fixed API operations and normal Chrome launcher.
lib/product_defaults.py: product choices and clone type matching.
lib/artwork.py, lib/ocr.py, lib/titles.py, lib/slug.py, lib/review.py: preparation and review.
lib/manifest.py + upload_batch.py: persistent, resumable upload state.

Run the existing offline checks with the project's Python environment:
- tools/selftest.py
- tools/test_browser.py
- tools/test_bridge.py
- tools/test_upload.py
- tools/test_product_defaults.py

Check dashboard and extension JavaScript syntax when editing them. Use mocked browser/API replies
for regression tests; do not create or modify live campaigns merely to run the test suite.
