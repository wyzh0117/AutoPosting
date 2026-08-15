# Patch 0.3.4 Report

Captured: 2026-08-15 CST
Package version after patch: 0.3.4
Runtime: Python 3.11 standard library and SQLite (no new dependencies)

## X tweets (copy AIHOT’s method, not their feed)

Looked at `https://aihot.virxact.com/` and `/all?channel=x`. Their X items are:

- per-account user posts, not a scraped X search page
- original posts and quote tweets; reply threads are not the main list
- avatars (`pbs.twimg.com/profile_images`, `mode=avatar`) stay in the byline
- article photos come from tweet media (`pbs.twimg.com/media`)

AutoPosting now does the same on the official API:

- `GET /2/users/:id/tweets?exclude=replies` plus media expansions
- drop native retweets
- store only tweet media URLs on the item
- do not fetch `x.com` HTML when picking article images
- example registry adds official lab accounts and a short watch list (all disabled until a Bearer is set)

Still no AIHOT aggregate subscription, no X webpage crawl.

## Article images

Candidates were every `<img>` on the page, so author avatars and header icons showed up as 配图.

Now:

- skip `profile_images`, `avatar`, gravatar, favicon, and similar URL chrome
- skip header/nav/aside/footer and `author-avatar` / byline images
- skip probed squares smaller than 200px
- keep og/twitter images only when they are not avatars
- tweet items use API media only

## Verification

- 179 unit/integration tests, including avatar/header drop, skip x.com page fetch, retweet skip, tweet media keep, and the expanded X roster.
- Offline self-check `ok`.

Source snapshot: `archives/autoposting-patch-0.3.4-update-20260815.tar.gz`.
