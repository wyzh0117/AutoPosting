# 观点候选 + 经验分享 — Design Spec

Date: 2026-08-25
Status: Approved (remaining options auto-accepted: left tabs, public hot.json, selftext + top 5 comments, style id `经验分享`, lane-scoped refresh)

## Problem

Hotspot lane is RSS news. Operators also want current **AI usage/experience threads** from Reddit, and a writing type that **拆解、梳理、解读、分享** one such thread instead of reporting a company news item.

## Product

Left panel gains a second tab **观点候选** beside **热点候选**. Same right-hand generate flow. New style file `docs/article_styles/经验分享.md`, option value `经验分享`. Switching to 观点候选 selects that style; switching back restores the previous 1–8 value.

## Non-goals

- Reddit OAuth / PRAW / third-party packages
- Crawling beyond the locked subreddit list
- Mixing Reddit threads into the news heat ranking
- Per-subreddit “只刷这个” on the viewpoint tab
- Service worker / accounts for Reddit

## Reddit sources (locked)

| source_id | handle |
| --- | --- |
| reddit_chatgpt | ChatGPT |
| reddit_claude | ClaudeAI |
| reddit_localllama | LocalLLaMA |
| reddit_promptengineering | PromptEngineering |
| reddit_openai | OpenAI |
| reddit_chatgptpromptgenius | ChatGPTPromptGenius |

`type: "reddit"`, `tier: "S4"`, `enabled: true`, `topics: ["products"]`, `fetch_interval: "15m"`. Handle is a subreddit name, not a URL.

## Fetch

- `GET https://www.reddit.com/r/{sub}/hot.json?limit=15&raw_json=1`
- Per non-stickied post, `GET https://www.reddit.com/r/{sub}/comments/{id}.json?limit=25&sort=top&raw_json=1`
- Stdlib urllib, User-Agent `AutoPosting/{version} (local; viewpoint collector)`
- Skip stickied listing posts
- Top 5 comments by score; skip OP author, `[deleted]`, `[removed]`, empty bodies, `kind != t1`
- One subreddit failing (HTTP error, 429, timeout, bad JSON) records that source failed and does not abort the batch
- No HTML page fetch for `original()` on reddit items — stored `content_text` is the original

## Storage

Same `items` table. `canonical_url` = `https://www.reddit.com` + permalink. `external_id` = `t3_{id}`. `raw_json` includes `score` and `comments` (`num_comments`) so existing heat ranking works. Schema CHECK on `sources.type` adds `reddit` (migrate existing DBs).

## Lanes

- `lane=hotspots` (default): collect/list exclude `type=reddit`. Source health hides reddit rows.
- `lane=viewpoints`: collect/list only reddit. Hours filter uses `published_at` (created_utc). Sort heat = engagement weight then time; time = created then weight. Cap still 10. Refresh on this tab does not fetch RSS.

## Style

`ARTICLE_STYLE_PROMPT_IDS` includes `经验分享`. File name is `经验分享.md`, not `9.md`. Default generate style remains `8` on the hotspot tab.

## Version

`0.4.6`. Patch archive required. `graft init` / `graft build` after the code lands.
