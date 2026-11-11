# Project: mayzing-bulk-upload

Communicate with the user in Vietnamese. Write code and comments in English.

## Current architecture

Windows Flask dashboard for design preparation, quote reading, review, export, and Mayzing bulk upload.
Use normal installed Google Chrome with manual sign-in. Playwright is not a runtime dependency.
The Manifest V3 Mayzing Session Bridge extension connects one explicitly selected Mayzing tab to
the exact HTTPS production origin or loopback HTTP. Production uses signed HttpOnly per-device
sessions: bridge, job, review, shop cache and data folders are isolated per browser profile.
lib/context.py explicitly binds device context into background and OCR worker threads. Never
resolve device data paths at module import into a fixed Path, or switch module globals per request.
The public dashboard requires the shared account configured in auth/web-login.json. Opaque,
revocable login sessions live in auth/web-sessions.sqlite3 and expire 30 days after sign-in.
Login is independent of shop cookies and per-browser workspaces; never merge devices by account.
Health, the guide/downloads and the explicitly authenticated extension relay remain accessible
without a dashboard login cookie. Require the login CSRF token and same-origin POST for logout.
Cookies remain in Chrome; do not copy profiles, decrypt cookies, enable debugging
ports, add stealth flags, or solve security challenges.

Pairing codes last 24 hours and are single-use. Pairing alone does not enable mutations. The user
must enable automation; StoresMinimal verifies the session first. The in-memory BrowserBridge
belongs to the dashboard process, so real uploads must run through the dashboard. Restarting Chrome
or the dashboard requires pairing again. Never terminate unrelated Chrome or Python processes.

Extension 1.2.0 adds the exact HTTPS production origin with the same activeTab/scripting/storage
permissions. The server never opens Chrome for remote users; the UI opens a link on the client.
Keep lib/mayzing_operations.json and generated chrome-extension/operations.js in sync.
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

input/, output/, work/, auth/, logs/, devices/, .session-key, settings.json, used_slugs.txt, and .env contain local state.
Never commit credentials, login profiles, user artwork, manifests, logs, or private shop history.
Public product-label caches may exist under work/; they are optional local fallbacks, not source.

## Files and checks

config.py: configuration and settings.json.
dashboard.py + lib/dashboard.html: one background job per device, one process per port.
production.py: threaded Waitress server behind Cloudflare Tunnel.
deploy/: CI-approved main release supervisor, health checks, drain and rollback.
lib/browser_bridge.py: paired relay with exactly-once dispatch.
lib/web_auth.py + lib/login.html: shared-account login, CSRF form and persistent 30-day sessions.
lib/claude_http.py: standard-library HTTPS Messages transport; no native Anthropic SDK/parser dependency.
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
- tools/test_production.py
- node tools/test_extension.js

`python deploy/check.py` runs the full offline suite. CI also tests the deployment supervisor
and starts a real WSGI smoke server. Production state is outside immutable release folders through
MAYZING_DATA_ROOT/MAYZING_ENV_FILE. Never stop a production job to deploy; use the drain marker
and wait for both active_jobs and active_requests to reach zero. See docs/deployment.md.

Check dashboard and extension JavaScript syntax when editing them. Use mocked browser/API replies
for regression tests; do not create or modify live campaigns merely to run the test suite.
