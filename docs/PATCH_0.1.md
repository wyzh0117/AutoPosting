# Patch 0.1 Archive Report

Captured: 2026-08-09 CST
Package version after patch: 0.1.1
Runtime: Python 3.11 standard library and SQLite

## Pre-update state

The pre-update project was version 0.1.0 and exposed only a CLI. It could collect RSS feeds, list recent candidates, evaluate a transparent hotspot rule, validate a fact JSON file, generate deterministic WeChat and Xiaohongshu drafts, approve or reject drafts, and create local dry-run publication packages.

The pre-update baseline passed 21 unit tests and 10 offline self-check assertions. It had no graphical interface, no interactive Top 10 workflow, no article generation from a selected collected item, and no official platform publishing adapter.

Confirmed defects before this patch:

1. A historical DRY_RUN attempt could be returned before current approval checks, allowing a later rejected draft to return an old package.
2. Regenerating identical content reset approved drafts and their event to DRAFTED.
3. A fact supported only by an S4 source passed validation despite the documented trust boundary.
4. One RSS item with an invalid URL port could fail the entire source collection.

The separate source snapshot is archived as `archives/autoposting-patch-0.1-before-20260809.tar.gz`. It is not embedded in the update archive.

## Patch 0.1 changes

### Local web workbench

- Added `autoposting web`, bound only to `127.0.0.1` or `localhost`.
- Added a responsive, dependency-free single-page workbench using inline HTML, CSS, and native JavaScript.
- Added selectable collection windows: 6, 24, 72, 168, and 720 hours.
- Added a collection button that forces configured RSS collection and returns up to 10 recent candidates.
- Candidate ranking is deliberately simple: recent items first, with independent source count used as supporting evidence. No paid model or clustering service is used.
- Added hotspot selection and platform-specific article generation for WeChat or Xiaohongshu.
- Added one-click approval and publication from the generated article.
- Added loading, empty, error, configuration, manual-publication, and completed states.
- Desktop uses a three-column workbench; viewports at or below 900px use one column.

### Article and evidence workflow

- Added idempotent article creation from a selected collected item.
- Every generated article retains source IDs and a deterministic event/draft identity.
- Added single-platform draft generation while preserving the existing dual-platform CLI behavior.
- A single generated platform draft can complete its event after approval; when both platform drafts exist, both still require approval.
- S4-only evidence is rejected.

### Publication behavior

#### WeChat Official Account

Implemented the official API sequence with Python standard-library HTTP calls:

1. Obtain an access token.
2. Create a draft with `/cgi-bin/draft/add`.
3. Submit it with `/cgi-bin/freepublish/submit`.

Required first-use values are AppID, AppSecret, `thumb_media_id`, and optional author. Values exist only in the running WebApp process, are never written to SQLite or package files, and are never returned by status/configuration responses. Network and API errors are mapped to stable messages that do not include request URLs, access tokens, or secrets.

Actual publication still depends on the user's official-account type, API permissions, IP allowlist, valid credentials, and an existing cover material ID. The patch does not bypass platform review and does not poll review status.

#### Xiaohongshu

Xiaohongshu does not provide a general official publication API for ordinary accounts. The compliant implementation approves the article, creates a local dry-run package, and offers the official creator-center URL. Final submission remains manual. No cookies, private APIs, simulated login, or browser submission automation are used.

### Correctness and security fixes

- Approval checks now occur before returning a historical DRY_RUN package.
- Identical draft regeneration preserves approval and audit state.
- Changed content returns the draft to DRAFTED and clears prior approval/rejection fields.
- Invalid RSS item URLs are skipped per item instead of failing the source.
- JSON `live` accepts only a real boolean.
- WeChat transport failures are redacted to `wechat_api_unavailable`.
- Switching hotspot or platform clears the previous draft to prevent stale publication.
- A first WeChat publication attempt opens configuration and automatically resumes only after successful configuration; manually opening or cancelling configuration never triggers publication.
- The unauthenticated Web server refuses non-local bind addresses.
- POST requests require `application/json`; browser requests carrying an `Origin` header must match the local server host and port, preventing cross-site writes to the localhost service.

## Verification evidence

Automated verification after the final patch:

- Unit/integration/page-contract tests: 63 passed.
- Offline self-check: 10 of 10 checks passed.
- `compileall`: passed.
- Ruff: passed.
- Update-before archive SHA-256 remained unchanged during implementation.
- WeChat flow was exercised against a local HTTP stub through token, draft, and submit calls.
- Raw SQLite bytes did not contain the test AppID, AppSecret, or access token.

Browser acceptance with ego-browser:

- Desktop viewport: three columns, no horizontal overflow.
- Collection: three deterministic RSS candidates rendered in descending time order.
- Xiaohongshu: select, generate, approve, and package workflow completed; manual creator-center boundary was shown.
- WeChat: first publication opened the configuration dialog; saving test credentials automatically resumed and returned the local stub reference `acceptance-publish`; inputs were cleared and the completed button was disabled.
- Mobile viewport: 390 x 844, one-column layout, all section widths 366px, no horizontal overflow.

No request was sent to a real WeChat or Xiaohongshu account during verification.

## Files and archives

The two archives are intentionally independent:

1. `autoposting-patch-0.1-before-20260809.tar.gz`: pre-update source snapshot and baseline report.
2. `autoposting-patch-0.1-update-20260809.tar.gz`: updated source, this report, and final checksums. It excludes the first archive and all other archives.

Both archives exclude `.env`, local databases, generated packages, caches, virtual environments, and credentials.
