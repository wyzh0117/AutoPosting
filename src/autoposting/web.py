from __future__ import annotations

import json
import logging
import threading
import traceback
import uuid
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from .background import RELATED_SELECTION_LIMIT
from .article_context import ArticleContextError
from .legacy_compat import SCOPE_ALL, resolve_scope
from .config import load_sources
from .content import ContentWorkflow
from .decision.adapters.jev import JevAdapter
from .diagnostics import make_diagnostic, parse_diagnostic
from .db import Database, model_endpoint_id
from .llm import (
    LLM_EFFORT,
    LLM_MODEL,
    LLM_PROVIDER,
    LLMError,
    LLMService,
    MacOSKeychainCredentialStore,
    _has_cjk,
    validate_chat_messages,
)
from .copy_validate import enrichment_error, require_enriched_copy
from .github_weekly import (
    XHS_TITLE,
    build_xhs_copy,
    default_github_get,
    fetch_weekly_ai_repos,
    set_github_token,
    week_window_start,
)
from .image_gen import build_weekly_render_plan
from .skill_finder import (
    POSTER_RENDER_CAP,
    _github_search,
    _skills_sh_search,
    aggregate_skills,
    apply_skill_selection,
    crawl_sources,
    fetch_manual_hits,
    find_skills,
    merge_manual_hits,
    render_skill_package,
    skill_identity,
)
from .skill_links import parse_skill_links
from .visual.tokens import normalize_poster_template, parse_palette
from .visual.weekly import render_weekly_visual
from .images import default_page_fetcher, guess_image_content_type, normalize_assets
from .publishing import PublicationError, PublicationService
from .reader import ArticleDocument, ReaderService, playwright_render_fetcher
from .resource_discovery import (
    GITHUB_WEEKLY_PRESET,
    ResourceDiscoveryError,
    ResourceDiscoveryService,
)
from .research.service import ResearchError
from .scheduler import PublishScheduler, parse_run_at
from .article_prompt import DEFAULT_ARTICLE_PROMPT_STYLE
from .workbench import (
    DEFAULT_WECHAT_BASE_URL,
    HOTSPOT_HOUR_MAX,
    HOTSPOT_HOUR_MIN,
    HotspotService,
    WeChatPublisher,
    WechatConfigurationRequired,
    Workbench,
    XIAOHONGSHU_CREATOR_URL,
    _download_image as download_image,
    _summary,
    xhs_fields,
)

APP_VERSION = "3.2.2"
ALLOWED_HOSTS = ("127.0.0.1", "localhost")
MAX_JSON_BODY_BYTES = 1_048_576
MAX_CHAT_JSON_BODY_BYTES = 4_194_304
ACCOUNT_STORE_IDS = {
    "wechat": "account:wechat",
    "x": "account:x",
    "xiaohongshu": "account:xiaohongshu",
}
# Optional GitHub token: raises the finder's api.github.com quota from 60
# core requests/hour to 5000. Kept in the same credential store as the other
# secrets and never echoed back to the page.
GITHUB_TOKEN_STORE_ID = "github:token"
# One paste may not exceed the package's own skill cap, so nothing is silently
# dropped later by cap_skills; the overflow is reported as a `too_many` reject.
MAX_PASTE_LINKS = 16
# The paste box is a multi-line textarea now, so a real paste (16 links plus
# prose and markdown bullets) is far longer than the old 4000-char input limit.
MAX_PASTE_CHARS = 20000
# How many rejected paste tokens are echoed back (the count is always exact).
MAX_PASTE_REJECTS = 20


class _MemoryResourceSetRepository:
    """Small process-local fallback until the database contract is available."""

    def __init__(self) -> None:
        self._rows: dict[str, dict[str, object]] = {}
        self._lock = threading.RLock()

    def save_resource_set(self, resource_set: object) -> None:
        payload = resource_set.to_dict() if hasattr(resource_set, "to_dict") else resource_set
        if not isinstance(payload, dict):
            raise TypeError("resource_set_must_be_object")
        set_id = str(payload.get("set_id") or "").strip()
        if not set_id:
            raise ValueError("set_id required")
        with self._lock:
            self._rows[set_id] = dict(payload)

    def get_resource_set(self, set_id: str) -> dict[str, object] | None:
        with self._lock:
            value = self._rows.get(str(set_id))
            return dict(value) if value is not None else None

    def list_resource_sets(self, limit: int = 20) -> list[dict[str, object]]:
        with self._lock:
            return [dict(value) for value in list(self._rows.values())[-limit:][::-1]]

    def delete_resource_set(self, set_id: str) -> bool:
        with self._lock:
            return self._rows.pop(str(set_id), None) is not None


def _json_default(value: object) -> object:
    """Serialize the non-JSON values deliberately exposed by web payloads."""
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _looks_english(text: str) -> bool:
    cjk = sum("\u3400" <= char <= "\u9fff" for char in text)
    latin = sum(char.isascii() and char.isalpha() for char in text)
    return latin > cjk


def _decorate_source_health(rows: object) -> list[dict[str, object]]:
    decorated: list[dict[str, object]] = []
    if not isinstance(rows, list):
        return decorated
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        row = dict(raw)
        last_error = row.get("last_error")
        diagnostic = parse_diagnostic(last_error)
        if diagnostic is None and last_error:
            diagnostic = make_diagnostic(
                str(row.get("source_id") or "unknown"),
                "collect",
                code="legacy_source_error",
                cause_code="legacy_error",
                detail=last_error,
            )
        if diagnostic is not None:
            row["diagnostic"] = diagnostic
        decorated.append(row)
    return decorated


def _runtime_source_health(row: object) -> dict[str, object]:
    """Decorate one authoritative DB source row for the source registry UI."""
    payload = dict(row) if isinstance(row, dict) else {}
    enabled = bool(payload.get("enabled"))
    try:
        interval = int(payload.get("fetch_interval_minutes") or 15)
    except (TypeError, ValueError):
        interval = 15
    last_error = payload.get("last_error")
    last_fetched = str(payload.get("last_fetched_at") or "")
    fetched_at: datetime | None = None
    if last_fetched:
        try:
            fetched_at = datetime.fromisoformat(last_fetched.replace("Z", "+00:00"))
        except ValueError:
            fetched_at = None
    if not enabled:
        status = "disabled"
    elif last_error:
        status = "error"
    elif fetched_at is None:
        status = "never"
    else:
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        status = (
            "stale"
            if datetime.now(timezone.utc)
            >= fetched_at.astimezone(timezone.utc)
            + timedelta(minutes=max(interval, 1) * 2)
            else "ok"
        )
    payload["enabled"] = enabled
    payload["fetch_interval_minutes"] = interval
    payload["status"] = status
    return payload

INDEX_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AutoPosting</title>
<style>
:root {
  /* Ink ramp: one warm near-black, five steps. */
  --ink-000: #0e0d0a;
  --ink-050: #15140f;
  --ink-100: #1c1a14;
  --ink-200: #24211a;
  --ink-300: #2d2921;

  /* Hairlines: translucent so they read on every surface step. */
  --line-1: rgba(244, 239, 229, 0.07);
  --line-2: rgba(244, 239, 229, 0.13);
  --line-3: rgba(244, 239, 229, 0.22);

  /* Paper ramp: warm off-white to muted. */
  --paper-1: #f6f2e9;
  --paper-2: #cfc7b8;
  --paper-3: #948c7f;

  /* Amber accent ramp. */
  --amber-1: #efd096;
  --amber-2: #cfa96b;
  --amber-3: #8a6d34;
  --amber-a1: rgba(207, 169, 107, 0.10);
  --amber-a2: rgba(207, 169, 107, 0.20);
  --amber-a3: rgba(207, 169, 107, 0.36);
  /* darkest amber permitted under dark (§1f190e) label text: 5.9:1 */
  --amber-deep: #b8914f;

  /* Semantic status ramp: hue + soft tint + border. */
  --jade: #7fb894;
  --jade-soft: rgba(127, 184, 148, 0.13);
  --jade-line: rgba(127, 184, 148, 0.42);
  --gold: #e0b055;
  --gold-soft: rgba(224, 176, 85, 0.13);
  --gold-line: rgba(224, 176, 85, 0.42);
  --coral: #e2907c;
  --coral-soft: rgba(226, 144, 124, 0.13);
  --coral-line: rgba(226, 144, 124, 0.42);
  --azure: #8ab0e0;
  --azure-soft: rgba(138, 176, 224, 0.13);
  --azure-line: rgba(138, 176, 224, 0.42);
  --slate: #978f81;
  --slate-soft: rgba(151, 143, 129, 0.15);
  --slate-line: rgba(151, 143, 129, 0.42);

  /* Type: one UI stack, one display stack, one mono for identifiers. */
  --font-ui: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI",
    system-ui, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  --font-display: "Iowan Old Style", "Palatino Linotype", Palatino, "Songti SC",
    Georgia, "Times New Roman", serif;
  --font-mono: ui-monospace, "SF Mono", SFMono-Regular, Menlo, Consolas,
    "Liberation Mono", monospace;

  /* Modular type scale (≈1.15 ratio) — nothing below 11.5px. */
  --fs-2xs: 11.5px;
  --fs-xs: 12.5px;
  --fs-sm: 13.5px;
  --fs-md: 15px;
  --fs-lg: 17.5px;
  --fs-xl: 21px;
  --fs-2xl: 26px;
  --fs-3xl: clamp(30px, 3.4vw, 46px);
  --lh-tight: 1.2;
  --lh-snug: 1.4;
  --lh-base: 1.62;

  /* Spacing: 4px base. */
  --s-1: 4px;
  --s-2: 8px;
  --s-3: 12px;
  --s-4: 16px;
  --s-5: 20px;
  --s-6: 24px;
  --s-8: 32px;
  --s-10: 40px;
  --s-12: 56px;

  /* Radius: three steps + pill. */
  --r-1: 8px;
  --r-2: 12px;
  --r-3: 18px;
  --r-pill: 999px;

  /* Elevation: three levels, warm-black shadows. */
  --e-1: 0 1px 2px rgba(0, 0, 0, 0.35);
  --e-2: 0 10px 28px -16px rgba(0, 0, 0, 0.75);
  --e-3: 0 28px 64px -28px rgba(0, 0, 0, 0.9);
  --ring: 0 0 0 3px var(--amber-a2);

  --shell-max: 1440px;
  --pane-left: minmax(0, 25rem);

  /* ---- Legacy aliases (kept so the pre-3.2.2 rules re-skin for free) ---- */
  --bg-main: var(--ink-000);
  --bg-card: var(--ink-050);
  --bg-input: var(--ink-100);
  --bg-raised: var(--ink-200);
  --text-main: var(--paper-1);
  --text-muted: var(--paper-3);
  --accent: var(--amber-2);
  --accent-glow: var(--amber-a2);
  --accent-gradient-1: var(--amber-1);
  --accent-gradient-2: var(--amber-3);
  --border: var(--line-2);
  --ok: var(--jade);
  --err: var(--coral);
  --warn: var(--gold);
  --radius-lg: var(--r-3);
  --radius-md: var(--r-2);
  --radius-sm: var(--r-1);
  --font: var(--font-ui);
  --display: var(--font-display);
  --ease: cubic-bezier(0.22, 1, 0.36, 1);
  --t-fast: 150ms;
  --t-mid: 260ms;
  --t-slow: 420ms;
  --hotspot-list-h: 30rem;
  --favorites-drawer-w: 20rem;
}

*{box-sizing:border-box; letter-spacing:0;}
html,body{margin:0; padding:0;}
body{
  background-color:var(--bg-main); color:var(--text-main);
  font:14px/1.6 var(--font); padding:1.5rem 2rem 2rem;
  display:flex; flex-direction:column; align-items:center;
  min-height:100dvh; height:auto; overflow-x:hidden; overflow-y:auto;
  background-image:
    radial-gradient(circle at 88% -8%, rgba(207, 169, 107, 0.12), transparent 42%),
    radial-gradient(circle at 0% 110%, rgba(111, 158, 122, 0.06), transparent 34%);
}
.sr-only{position:absolute; width:1px; height:1px; padding:0; margin:-1px; overflow:hidden; clip:rect(0,0,0,0); white-space:nowrap; border:0;}
@keyframes rise{from{opacity:0; transform:translateY(10px);} to{opacity:1; transform:translateY(0);}}
@keyframes fade{from{opacity:0;} to{opacity:1;}}

header{
  width:100%; max-width:1400px; display:flex; align-items:center; gap:1rem;
  margin-bottom:1.15rem; flex-wrap:wrap;
  animation:rise var(--t-slow) var(--ease) both;
}
.brand{display:flex; flex-direction:column; gap:.1rem; min-width:0;}
header h1.logo{
  font-family:var(--display); font-size:1.85rem; font-weight:600; letter-spacing:.02em; margin:0;
  color:var(--text-main);
}
.brand-kicker{
  margin:0; font-size:.68rem; letter-spacing:.18em; text-transform:uppercase;
  color:var(--accent); font-weight:600;
}
#service-status{
  font-size:.72rem; color:var(--text-muted);
  background:rgba(207, 169, 107, 0.06); padding:.28rem .8rem;
  border-radius:99px; border:1px solid var(--border);
}
#config-btn{
  margin-left:auto; border-radius:99px; padding:.5rem 1.2rem;
  border:1px solid var(--border); background:var(--bg-card);
  color:var(--text-main); font-weight:500;
}
#config-btn:hover:not(:disabled){background:var(--bg-input); border-color:var(--accent); box-shadow:none;}
#favorites-btn{
  border-radius:99px; padding:.5rem 1.2rem;
  border:1px solid var(--border); background:var(--bg-card);
  color:var(--text-main); font-weight:500;
}
#favorites-btn:hover:not(:disabled){background:var(--bg-input); border-color:var(--accent); box-shadow:none;}
#favorites-btn[aria-expanded="true"]{border-color:var(--accent); color:var(--accent);}
body.favorites-open #favorites-drawer{display:flex;}
body.favorites-open header,
body.favorites-open #app-nav,
body.favorites-open main,
body.favorites-open #wait-banner{
  margin-right:var(--favorites-drawer-w);
  width:calc(100% - var(--favorites-drawer-w));
}
.favorites-drawer-head{
  display:flex; align-items:center; gap:.75rem; margin-bottom:.85rem;
}
.favorites-drawer-head h2{margin:0; font-size:1rem; font-weight:600;}
#favorites-close{
  margin-left:auto; border-radius:99px; padding:.35rem .8rem;
  border:1px solid var(--border); background:var(--bg-card); color:var(--text-main);
}
#favorites-list{
  list-style:none; margin:0; padding:0; overflow-y:auto; min-height:0; flex:1;
  display:flex; flex-direction:column; gap:.75rem;
}
.fav-item{
  position:relative; background:var(--bg-input); border:1px solid transparent;
  border-radius:var(--radius-md); padding:.8rem 2.4rem 2.1rem .9rem;
}
.fav-item .ht-pick{bottom:auto; top:2.5rem;}
.fav-item .fav-open-original{margin-right:1.6rem;}
.fav-item.is-picked, .hotspot-item.is-picked{
  border-color:var(--accent);
  background:linear-gradient(145deg, rgba(207, 169, 107, 0.16), rgba(207, 169, 107, 0.05));
  box-shadow:inset 0 0 0 1px rgba(207, 169, 107, 0.35);
}
.fav-open-original{
  display:inline-flex; margin-top:.15rem; height:auto; min-height:0;
  padding:.2rem .6rem; font-size:.72rem; font-weight:600; line-height:1.2;
  border-radius:99px;
}
.hotspot-row{position:relative;}
.ht-star{
  position:absolute; top:.45rem; right:.45rem; z-index:2;
  width:1.8rem; height:1.8rem; padding:0; border:0; border-radius:999px;
  background:transparent; color:var(--text-muted); font-size:1.05rem; line-height:1;
  cursor:pointer;
}
.ht-star[aria-pressed="true"]{color:var(--accent);}

#app-nav{
  width:100%; max-width:1400px;
  display:flex; justify-content:space-between; align-items:center;
  gap:.75rem; flex-wrap:wrap;
  margin:0 0 1rem;
}
#app-nav .platform-tabs[aria-label="候选栏"]{
  width:auto; max-width:none; flex:1 1 32rem;
}
#app-nav .platform-tabs[aria-label="候选栏"] button.seg{
  flex:1 1 0; font-size:.82rem; padding:.42rem .7rem;
}
#app-nav .platform-tabs[aria-label="平台选择"]{
  margin-left:auto; flex:0 0 auto;
}

#wait-banner{
  width:100%; max-width:1400px; position:relative; z-index:2;
  display:flex; flex-direction:column; gap:.55rem;
  margin:0 0 1rem; padding:0; border:none; background:transparent; box-shadow:none;
  pointer-events:none;
}
#wait-banner[hidden]{display:none !important;}
#wait-jobs{display:flex; flex-direction:column; gap:.55rem; pointer-events:none;}
.wait-job{
  display:grid; grid-template-columns:auto minmax(0,1fr) auto auto; gap:.35rem .7rem; align-items:center;
  padding:.85rem 1rem .8rem;
  border-radius:var(--radius-md); border:1px solid var(--accent);
  background:linear-gradient(180deg, #322a1c, #1f1b14);
  box-shadow:0 16px 36px -12px rgba(0,0,0,.7), 0 0 0 4px var(--accent-glow);
}
.wait-job.is-done{border-color:var(--ok); box-shadow:0 0 0 4px rgba(127, 184, 148,.18);}
.wait-job.is-error{border-color:var(--err); box-shadow:0 0 0 4px rgba(226, 144, 124,.18);}
.wait-spinner{
  width:1.55rem; height:1.55rem; border-radius:50%; flex:0 0 auto;
  border:3px solid rgba(196,164,106,.22); border-top-color:var(--accent);
  animation:waitSpin .7s linear infinite;
}
.wait-job.is-done .wait-spinner,.wait-job.is-error .wait-spinner{animation:none; border-color:currentColor;}
@keyframes waitSpin{to{transform:rotate(360deg);}}
.wait-title{margin:0; font-size:.95rem; font-weight:700; color:var(--text-main);}
.wait-detail{margin:.12rem 0 0; font-size:.78rem; color:var(--text-muted); line-height:1.4;}
.wait-time{margin:0; font-variant-numeric:tabular-nums; font-weight:700; color:var(--accent); white-space:nowrap;}
.wait-stop{
  pointer-events:auto;
  height:32px; min-height:32px; padding:.2rem .85rem; border-radius:99px;
  font-size:.78rem; font-weight:700; color:var(--err); border-color:rgba(226, 144, 124,.55);
  background:rgba(226, 144, 124,.08);
}
.wait-stop:hover:not(:disabled){background:rgba(226, 144, 124,.18); border-color:var(--err);}
.wait-job.is-done .wait-stop,.wait-job.is-error .wait-stop{display:none;}
.wait-job:not(.is-done):not(.is-error){animation:waitJobPulse 1.8s ease-in-out infinite;}
@keyframes waitJobPulse{
  0%,100%{box-shadow:0 16px 36px -12px rgba(0,0,0,.7), 0 0 0 3px var(--accent-glow);}
  50%{box-shadow:0 16px 36px -12px rgba(0,0,0,.7), 0 0 0 8px var(--accent-glow);}
}
@keyframes waitPulse{
  0%,100%{box-shadow:0 0 0 0 var(--accent-glow);}
  50%{box-shadow:0 0 0 7px var(--accent-glow);}
}
.gh-tile.is-running{border-color:var(--accent); animation:waitPulse 1.15s ease-in-out infinite;}
.gh-tile.is-queued img{opacity:.28; filter:grayscale(.35);}
.gh-tile.is-error{border-color:var(--err);}
.gh-tile .gh-overlay{
  position:absolute; left:0; right:0; bottom:0; z-index:1; margin:0;
  background:rgba(12,11,8,.8); color:var(--accent); font-weight:700;
  text-align:center; padding:.85rem .5rem;
}
.gh-tile.is-running .gh-overlay{color:#1a160f; background:rgba(196,164,106,.92);}
.gh-tile.is-error .gh-overlay{color:#f3eee4; background:rgba(226, 144, 124,.92);}

main{
  width:100%; max-width:1400px;
  display:grid; grid-template-columns:1fr 2fr; gap:1.25rem;
  flex:1 0 auto; min-height:calc(100dvh - 8.1rem); min-width:0;
  align-items:start;
}
.panel{
  background:linear-gradient(180deg, rgba(36, 33, 26, 0.92), rgba(22, 20, 16, 0.94));
  border-radius:var(--radius-lg); border:1px solid var(--border);
  padding:1.15rem 1.2rem 1.1rem; display:flex; flex-direction:column;
  box-shadow:0 18px 40px -18px rgba(0, 0, 0, 0.72), inset 0 1px 0 rgba(244, 239, 229, 0.04);
  min-width:0; max-width:100%; height:auto; overflow:visible;
  animation:rise var(--t-slow) var(--ease) both;
}
#hotspots-panel{
  height:auto; max-height:none;
  min-height:0; overflow:hidden;
}
#hotspots-panel.sources-open{
  height:auto; max-height:none; overflow:visible;
}
.panel-header{display:flex; justify-content:space-between; align-items:flex-start; gap:.75rem; flex-wrap:wrap; margin-bottom:.75rem;}
.panel-title{font-size:1.05rem; font-weight:700; color:var(--text-main); margin:0; letter-spacing:.01em;}
.panel-sub{font-size:.72rem; color:var(--text-muted); margin:.2rem 0 0; line-height:1.45;}

.row{display:flex; gap:.5rem; align-items:center; flex-wrap:wrap;}
#article-paste{
  flex:1 1 14rem; width:auto; min-width:10rem; max-width:min(32rem, 100%);
  min-height:40px; max-height:12rem; height:40px; resize:none; overflow:hidden;
  padding:.45rem .7rem; font-size:.82rem; line-height:1.35; font-weight:400;
}
.row > select,.row > input,.row > button{height:40px; min-height:40px; padding-top:0; padding-bottom:0;}
select, input[type=text], input[type=password], textarea{
  border:1px solid var(--border); border-radius:var(--radius-sm);
  padding:.5rem .8rem; font:inherit;
  background:var(--bg-input); color:var(--text-main); min-width:0; max-width:100%;
}
input[type=number], input[type=datetime-local]{
  border:1px solid var(--border); border-radius:var(--radius-sm);
  background:var(--bg-input); color:var(--text-main); height:40px;
  font:inherit; color-scheme:dark; min-width:0;
}
select option{background:var(--bg-input); color:var(--text-main);}
input[type=text], input[type=password], textarea{width:100%; border-radius:var(--radius-md); padding:.8rem 1rem;}
select,input[type=text],input[type=password]{height:40px; padding-top:0; padding-bottom:0;}
select:hover, input:hover, textarea:hover{border-color:var(--accent);}
select:focus, input:focus, textarea:focus{
  border-color:var(--accent); background:rgba(255,255,255,0.02);
  box-shadow:inset 0 0 0 1px var(--accent-glow); outline:none;
}
label{font-size:.85rem; font-weight:600; color:var(--text-muted);}
button{
  font:inherit; border:1px solid var(--border); border-radius:var(--radius-sm);
  padding:.5rem 1rem; background:var(--bg-input); color:var(--text-main);
  cursor:pointer; outline:none;
  transition:background var(--t-fast) var(--ease), border-color var(--t-fast) var(--ease), transform var(--t-fast) var(--ease), box-shadow var(--t-fast) var(--ease), color var(--t-fast) var(--ease), filter var(--t-fast) var(--ease);
}
button:hover:not(:disabled){
  background:var(--bg-raised); border-color:var(--accent);
  box-shadow:0 0 0 3px var(--accent-glow); transform:translateY(-1px);
}
button:disabled{opacity:.5; cursor:not-allowed;}
button,.model-picker{display:inline-flex; align-items:center; justify-content:center; vertical-align:middle;}
button.primary{
  background:linear-gradient(135deg, var(--accent-gradient-1), var(--accent-gradient-2));
  color:#1a160f; border:1px solid var(--accent);
  font-weight:700; padding:.8rem 2.5rem; border-radius:99px; font-size:1rem;
  box-shadow:0 8px 18px var(--accent-glow);
}
button.primary:hover:not(:disabled){
  background:linear-gradient(135deg, #ddc08a, var(--accent-gradient-2));
  transform:translateY(-1px); box-shadow:0 10px 22px var(--accent-glow);
}
button.seg{flex:1 1 auto; min-width:0; white-space:nowrap; border-radius:99px; border-color:transparent;}
button.seg.active{
  background:var(--bg-card); color:var(--text-main);
  box-shadow:0 2px 10px rgba(0,0,0,0.35); border-color:var(--border);
}
.platform-tabs{
  display:flex; background:var(--bg-input); border-radius:99px;
  padding:.25rem; border:1px solid rgba(244, 239, 229, 0.04);
}
.platform-tabs button{flex:0 1 auto;}
.platform-tabs[aria-label="候选栏"]{width:100%; max-width:36rem;}
.platform-tabs[aria-label="候选栏"] button.seg{
  flex:1 1 0; font-size:.82rem; padding:.42rem .55rem; letter-spacing:0;
}
.btn-magic{
  background:linear-gradient(135deg, var(--accent-gradient-2), var(--accent-gradient-1));
  color:#1a160f; border:1px solid var(--accent); font-weight:700;
  display:flex; align-items:center; gap:.5rem;
  border-radius:99px; padding:.55rem 1.35rem;
  box-shadow:0 6px 16px var(--accent-glow);
}
.btn-magic:hover:not(:disabled){filter:brightness(1.08); transform:translateY(-1px);}

.rank-toolbar{display:flex; align-items:center; justify-content:space-between; gap:.75rem; margin:0 0 .65rem;}
#rank-tabs{margin:0; align-self:flex-start;}
#prompt-style-select{width:auto; min-width:8.5rem;}
#hotspot-pane{flex:0 0 auto; min-height:0; height:auto; display:flex; flex-direction:column; min-width:0; overflow:hidden;}
#hotspots-panel.sources-open #hotspot-pane{
  flex:0 0 auto; height:auto; min-height:0; overflow:hidden;
}
/* The #source-health* rules live in the design layer below; only the dot
   colours are declared here. */
.source-health .sh-dot{width:7px; height:7px; border-radius:50%; flex:0 0 auto;}
.source-health .sh-dot.ok{background:var(--ok);}
#picked-dock{
  position:fixed; left:1.5rem; bottom:1.5rem; z-index:45;
  display:flex; flex-direction:column; align-items:stretch;
  gap:.35rem; min-width:0; max-width:18rem; max-height:40vh; overflow-y:auto;
  padding:.65rem .75rem; pointer-events:auto;
  border:1px solid var(--border); border-radius:var(--radius-md);
  background:var(--bg-raised);
  box-shadow:0 18px 40px -18px rgba(0, 0, 0, 0.72), inset 0 1px 0 rgba(244, 239, 229, 0.04);
}
#picked-dock[hidden]{display:none !important;}
.picked-dock-label{
  font-size:.72rem; color:var(--text-muted); font-weight:600;
}
.picked-dock-chip{
  pointer-events:auto; max-width:100%; width:100%;
  font:inherit; font-size:.82rem; font-weight:600; line-height:1.5;
  color:var(--text-main); text-align:left; justify-content:flex-start; align-items:flex-start;
  background:rgba(207, 169, 107, 0.12);
  border:1px solid rgba(207, 169, 107, 0.35);
  border-radius:var(--radius-sm); padding:.75rem .8rem; min-height:3.4rem;
  cursor:pointer; display:flex;
}
.picked-dock-chip-label{
  display:-webkit-box; width:100%; min-width:0; overflow:hidden;
  -webkit-box-orient:vertical; -webkit-line-clamp:2;
  text-overflow:ellipsis; white-space:normal; word-break:break-word;
}
#picked-dock .picked-dock-chip.is-static{
  pointer-events:none; cursor:default; max-width:100%;
  color:var(--text-muted); background:transparent; border-color:transparent;
  font-weight:500;
}
.ht-pick{
  position:absolute; right:.45rem; bottom:.45rem; z-index:2;
  width:1.5rem; height:1.5rem; padding:0; border-radius:6px;
  display:flex; align-items:center; justify-content:center;
  background:var(--bg-raised); border:1px solid var(--border);
  color:var(--text-muted); font-size:.85rem; line-height:1;
  cursor:pointer;
}
.ht-pick:hover{border-color:rgba(207, 169, 107, 0.45);}
.ht-pick.is-on{border-color:var(--accent); color:var(--accent); font-weight:700;}
.source-health .sh-dot.error{background:var(--err);}
.source-health .sh-dot.stale{background:var(--warn);}
.source-health .sh-dot.never,.source-health .sh-dot.disabled{background:var(--slate);}
#hotspot-list{
  list-style:none; margin:0; padding:0 .45rem 0 0;
  flex:0 0 auto; overflow-y:auto; min-height:0;
  height:var(--hotspot-list-h); max-height:var(--hotspot-list-h);
  display:flex; flex-direction:column; gap:.75rem;
  overscroll-behavior:contain; scrollbar-gutter:stable; scrollbar-width:thin;
}
#hotspot-list li{margin:0; animation:rise var(--t-mid) var(--ease) both;}
#hotspot-list li:nth-child(1){animation-delay:40ms;}
#hotspot-list li:nth-child(2){animation-delay:80ms;}
#hotspot-list li:nth-child(3){animation-delay:120ms;}
#hotspot-list li:nth-child(4){animation-delay:160ms;}
#hotspot-list li:nth-child(5){animation-delay:200ms;}
#hotspot-list li:nth-child(n+6){animation-delay:240ms;}
.hotspot-item{
  display:block; width:100%; text-align:left;
  background:var(--bg-input); border:1px solid transparent;
  border-radius:var(--radius-md); padding:1rem 2.4rem 1rem .9rem;
  cursor:pointer; position:relative; word-break:break-word;
  transition:background var(--t-fast) var(--ease), border-color var(--t-fast) var(--ease), transform var(--t-fast) var(--ease), box-shadow var(--t-fast) var(--ease);
}
.hotspot-item:hover:not(:disabled){border-color:rgba(207, 169, 107, 0.35); transform:translateY(-2px);}
.hotspot-item.selected{
  background:var(--bg-input);
}
.hotspot-item.selected::before{
  content:''; position:absolute; left:-1px; top:20%; height:60%; width:3px;
  background:linear-gradient(to bottom, var(--accent-gradient-1), var(--accent-gradient-2));
  border-radius:0 4px 4px 0;
}
.idx{display:inline; color:var(--accent); font-weight:600; margin-right:.3rem;}
.ht-label{
  display:inline-block; font-size:.68rem; color:var(--accent);
  background:rgba(207, 169, 107, 0.08); border:1px solid rgba(207, 169, 107, 0.18);
  padding:.15rem .45rem; border-radius:4px; font-weight:600; margin:0 0 .4rem;
}
.ht-title{display:block; font-size:.95rem; font-weight:600; line-height:1.4; color:var(--text-main); margin-bottom:.35rem; word-break:break-word; cursor:pointer;}
.ht-summary{
  display:block; font-size:.82rem; color:var(--text-muted); line-height:1.45; margin-bottom:.55rem;
  word-break:break-word; display:-webkit-box; -webkit-line-clamp:1; -webkit-box-orient:vertical; overflow:hidden;
}
.ht-zh-unavailable{display:block; color:var(--warn); margin:0 0 .4rem; font-size:.82rem;}
.ht-meta{display:block; font-size:.72rem; color:var(--text-muted); border-top:1px solid rgba(244, 239, 229, 0.05); padding-top:.65rem;}
.hotspot-empty{padding:.8rem .2rem; color:var(--text-muted);}

#article-empty{color:var(--text-muted); font-size:.9rem; padding:.75rem 0; margin:0; text-align:center;}
#article-panel{min-height:0; height:auto; overflow:visible;}
#article-view{flex:0 0 auto; display:flex; flex-direction:column; min-height:0; overflow:visible;}
.article-scroll{
  flex:0 0 auto; min-height:0; overflow:visible;
  border:1px solid var(--border); border-radius:var(--radius-md);
  background:rgba(0,0,0,0.22); padding:1rem;
  display:flex; flex-direction:column; gap:.8rem;
}
.article-scroll[hidden]{display:none;}
.article-field{display:flex; flex-direction:column; gap:.5rem; margin:0; font-size:.85rem; font-weight:600; color:var(--text-muted);}
.article-field input,.article-field textarea{margin:0; font-weight:400;}
.article-field-head{display:flex; align-items:center; justify-content:space-between; gap:.75rem;}
#article-body{
  flex:1 0 auto; min-height:260px; height:auto; overflow:hidden; resize:none;
  overflow-anchor:none; white-space:pre-wrap; word-break:break-word; line-height:1.8;
}
textarea[name=style]{resize:vertical; max-height:50vh;}
.article-meta{margin:0; font-size:.8rem;}
#image-picker{
  display:flex; flex-direction:column; gap:.55rem; margin-top:.2rem;
  overflow:hidden;
  border:1px solid var(--border); border-radius:var(--radius-md);
  background:var(--bg-raised); padding:.7rem .75rem .65rem;
  box-shadow:inset 0 1px 0 rgba(244, 239, 229, 0.04);
}
#image-picker[hidden]{display:none;}
.image-picker-head{display:flex; justify-content:space-between; align-items:flex-start; gap:.6rem;}
.image-picker-head .panel-sub{margin:0; flex:1 1 auto;}
#image-picker-list{
  display:flex; flex-direction:column; gap:.55rem;
  flex:1 1 auto; min-height:0;
  overflow:hidden;
}
.image-carousel-nav{
  display:flex; align-items:center; justify-content:center; gap:.65rem;
}
.image-carousel-nav[hidden]{display:none;}
.image-carousel-nav button{height:32px; min-height:32px; padding:0 .9rem; font-size:.78rem;}
.image-carousel-counter{
  min-width:4.8rem; text-align:center; font-size:.8rem; font-variant-numeric:tabular-nums;
  color:var(--text-muted);
}
.image-card{
  display:flex; flex-direction:column; gap:.55rem;
  align-items:stretch; background:rgba(0,0,0,0.22);
  border:1px solid var(--border); border-radius:var(--radius-sm); padding:.65rem;
  animation:rise var(--t-mid) var(--ease) both;
  transition:border-color var(--t-fast) var(--ease), transform var(--t-fast) var(--ease), box-shadow var(--t-fast) var(--ease);
}
.image-card:hover{transform:translateY(-1px); border-color:rgba(207, 169, 107, 0.35);}
.image-card img{
  width:100%; height:auto; max-height:18rem; object-fit:contain;
  border-radius:6px; background:#111; display:block; cursor:zoom-in;
}
.image-card .image-meta{min-width:0; font-size:.75rem; color:var(--text-muted);}
.image-card .image-url{overflow:hidden; text-overflow:ellipsis; white-space:nowrap;}
.image-card .image-actions{display:flex; flex-wrap:wrap; gap:.45rem; align-items:center;}
.image-card .image-actions button{height:28px; min-height:28px; padding:0 .55rem; font-size:.72rem;}
.image-check{
  display:inline-flex; align-items:center; gap:.28rem;
  font-size:.75rem; color:var(--text-muted); font-weight:600;
  cursor:pointer; user-select:none; white-space:nowrap;
}
.image-check input{margin:0; accent-color:var(--accent); width:1rem; height:1rem;}
.image-card.cover{border-color:var(--accent); box-shadow:0 0 0 1px var(--accent-glow);}
.article-actions{
  display:flex; justify-content:space-between; align-items:center; gap:1rem; flex-wrap:wrap;
  margin-top:.75rem; padding-top:.75rem; border-top:1px solid var(--border);
}
.article-actions .model-picker{width:min(260px, 100%);}
.article-actions > .primary,.article-actions .row > button,.article-actions .row > .model-picker{height:40px; min-height:40px; padding-top:0; padding-bottom:0;}
.article-actions input{width:min(240px, 100%);}
.article-actions button{white-space:nowrap;}
.model-picker{
  background:var(--bg-input); border:1px solid var(--border); border-radius:99px;
  padding:.5rem 1rem; color:var(--text-main); font-weight:600; font-size:.85rem;
}
#save-article-btn{
  background:transparent; border:none; color:var(--text-muted);
  font-weight:600; font-size:.9rem; padding:.5rem 1rem;
}
#save-article-btn:hover:not(:disabled){
  color:var(--text-main); background:transparent; border:none; box-shadow:none;
}
#manual-box{
  background:var(--bg-input); border:1px solid var(--border);
  border-radius:var(--radius-md); padding:1rem; margin-top:1rem;
}
.muted{color:var(--text-muted); font-size:12px; word-break:break-all;}
#package-path{overflow-wrap:anywhere; word-break:normal; background:rgba(0,0,0,0.3); border:1px solid var(--border); border-radius:6px; padding:4px 6px;}
[role=status]{color:var(--text-muted); font-size:.8rem;}
[role=status]:empty{display:none;}
.status-error{color:var(--err);}
.status-ok{color:var(--ok);}
[role=alert]{color:var(--err); font-size:.8rem;}
#config-dialog{
  background:var(--bg-card); color:var(--text-main);
  border:1px solid var(--border); border-radius:var(--radius-lg); padding:1.5rem;
  width:min(640px, calc(100% - 32px)); max-height:calc(100vh - 32px); overflow:hidden;
  box-shadow:0 25px 50px -12px rgba(0,0,0,0.8);
  animation:rise var(--t-mid) var(--ease);
}
#config-dialog::backdrop{background:rgba(12, 11, 8, 0.62); backdrop-filter:blur(4px); animation:fade var(--t-mid) var(--ease);}
#config-dialog form{display:grid; grid-template-rows:auto minmax(0, 1fr) auto; gap:1rem; max-height:calc(100vh - 66px);}
#config-dialog h2{margin:0; font-size:1.1rem; color:#f6f2e9;}
#config-dialog label{display:flex; flex-direction:column; gap:.4rem; font-size:.85rem; font-weight:600; color:var(--text-muted);}
#config-dialog fieldset{border:0; border-top:1px solid var(--border); padding:1rem 0 0; margin:0; min-width:0;}
#config-dialog legend{font-size:.85rem; font-weight:600; padding:0 6px 0 0; color:#948c7f;}
#config-dialog fieldset > .row{align-items:flex-end; flex-wrap:nowrap;}
#config-dialog fieldset > .row > label{min-width:0;}
#config-dialog fieldset > .row > button{flex:0 0 auto;}
.purpose-stack{display:flex; flex-direction:column; gap:1.15rem; margin:1.1rem 0 1.6rem;}
.purpose-card{
  background:var(--bg-input); border:1px solid var(--border);
  border-radius:var(--radius-md); padding:1rem 1.05rem 1.15rem;
  display:flex; flex-direction:column; gap:.85rem;
}
.purpose-card h3{margin:0; font-size:.95rem; color:var(--text-main);}
.purpose-grid{display:grid; grid-template-columns:minmax(0, 1fr) minmax(0, 1.15fr); gap:.8rem 1rem; align-items:end;}
.purpose-actions{display:flex; gap:.65rem; align-items:end; flex-wrap:wrap;}
.purpose-actions button{height:40px; min-width:7rem; white-space:nowrap;}
.model-role-row{
  display:grid; grid-template-columns:minmax(0, 1fr) 7.5rem auto;
  align-items:end; gap:.65rem; margin-top:.7rem;
}
.model-role-row.simple{grid-template-columns:minmax(0, 1fr) auto;}
.model-role-row button{height:40px; min-width:7rem; white-space:nowrap;}
.provider-section{margin-top:.4rem; display:flex; flex-direction:column; gap:.9rem;}
#provider-list,#endpoint-list{list-style:none; margin:0; padding:0; display:flex; flex-direction:column; gap:.85rem;}
.provider-row,.endpoint-card{background:var(--bg-input); border:1px solid var(--border); border-radius:var(--radius-sm); padding:.95rem 1rem;}
.provider-row{display:flex; flex-wrap:wrap; align-items:center; gap:.75rem 1rem;}
.provider-row.active{border-color:var(--accent);}
.provider-dot{width:.55rem; height:.55rem; border-radius:50%; flex:0 0 auto; background:var(--err);}
.provider-dot.ok{background:var(--ok);}
.provider-row-body{min-width:0; flex:1 1 auto; cursor:pointer;}
.provider-row-title{font-size:.9rem; font-weight:600; word-break:break-all;}
.provider-row-meta{font-size:.72rem; color:var(--text-muted); margin-top:.2rem;}
.provider-tag{font-size:.68rem; border:1px solid var(--border); border-radius:99px; padding:.12rem .45rem; color:var(--text-muted); flex:0 0 auto;}
.provider-expand{width:100%; display:flex; flex-wrap:wrap; gap:.75rem 1rem; align-items:end; padding-top:.35rem;}
.provider-expand label{flex:1 1 14rem; min-width:0;}
#provider-editor{background:var(--bg-raised); border:1px solid var(--border); border-radius:var(--radius-md); padding:1.1rem 1.15rem; display:flex; flex-direction:column; gap:1rem; margin-top:.15rem;}
#provider-editor[hidden]{display:none;}
.account-form[hidden],.account-delete[hidden]{display:none;}
.endpoint-card .endpoint-url{word-break:break-all; font-size:.85rem; margin-bottom:.5rem;}
#hours-custom{width:5.5rem;}
#schedule-at{width:auto; max-width:13rem;}
#config-dialog .row:last-child{justify-content:flex-end;}
#settings-platforms{display:flex;}
#settings-platforms button{flex:1 1 0;}
.settings-panel[hidden]{display:none;}
.dialog-content{display:flex; flex-direction:column; gap:.8rem; min-height:0; overflow-y:auto; overscroll-behavior:contain; padding-right:2px;}
#image-lightbox{
  border:0; padding:0; width:100vw; height:100vh; max-width:none; max-height:none;
  background:rgba(12, 11, 8, 0.9); color:var(--text-main);
  animation:fade var(--t-mid) var(--ease);
}
#image-lightbox::backdrop{background:rgba(12, 11, 8, 0.72);}
.original-dialog{
  border:0; padding:0; width:min(980px, calc(100vw - 32px)); height:min(88vh, 900px);
  max-width:none; max-height:none; background:var(--bg-card); color:var(--text-main);
  border-radius:var(--radius-lg); box-shadow:0 25px 60px rgba(0,0,0,.72);
}
.original-dialog::backdrop{background:rgba(12, 11, 8, .72); backdrop-filter:blur(4px);}
.original-page{height:100%; display:grid; grid-template-rows:auto auto minmax(0, 1fr);}
.original-head{display:flex; width:100%; min-width:0; align-items:center; justify-content:space-between; gap:1rem; padding:1rem 1.4rem; border-bottom:1px solid var(--border);}
.original-head h2{margin:0; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:1.15rem;}
.original-head-actions{display:flex; flex:0 0 auto; flex-wrap:nowrap; align-items:center; gap:.35rem;}
#original-translate{
  height:36px; width:max-content; flex:0 0 auto; padding:0 .65rem;
  white-space:nowrap; border-color:transparent; background:transparent;
  color:var(--accent); font-size:.82rem; font-weight:600;
}
#original-translate:hover:not(:disabled){background:var(--accent-glow); box-shadow:none;}
#original-close{
  width:36px; height:36px; flex:0 0 36px; padding:0; border-radius:50%; font-size:1.25rem;
  color:var(--text-muted); line-height:1;
}
#original-sources{padding:0 1.4rem; flex-wrap:wrap;}
#original-sources a{color:var(--accent); font-size:.8rem; word-break:break-all;}
#original-scroll{overflow:auto; padding:1rem 1.4rem 2rem;}
#original-note{margin:0 0 .8rem; font-size:.78rem; color:var(--text-muted);}
.original-section h3{margin:.25rem 0 .75rem; font-size:.86rem; color:var(--text-muted);}
#original-content,#original-translation{white-space:pre-wrap; line-height:1.75; word-break:break-word;}
#original-translation-wrap{margin-top:1.6rem; padding-top:1.25rem; border-top:1px solid var(--border);}
.lightbox-stage{
  position:relative; display:flex; flex-direction:column; align-items:center; justify-content:center;
  width:100%; height:100%; padding:1.25rem; box-sizing:border-box; gap:.75rem;
}
.lightbox-toolbar{position:absolute; top:1rem; right:1rem; display:flex; gap:.45rem; z-index:1;}
.lightbox-toolbar button{height:32px; min-height:32px; padding:0 .75rem; font-size:.78rem;}
#lightbox-image{
  max-width:min(92vw, 1100px); max-height:min(78vh, 900px);
  object-fit:contain; border-radius:8px; background:#111; cursor:ew-resize;
}
#lightbox-caption{margin:0; font-size:.8rem; color:var(--text-muted); text-align:center; word-break:break-all; max-width:min(92vw, 1100px);}

@media (max-width: 900px){
  body{padding:1rem; height:auto; min-height:100dvh; overflow-y:auto; overflow-x:hidden;}
  main{grid-template-columns:minmax(0, 1fr); height:auto; min-height:calc(100dvh - 6.1rem);}
  #hotspots-panel{height:auto; max-height:none; min-height:0; overflow:visible;}
  #hotspot-pane{height:auto; min-height:0;}
  #hotspot-list{max-height:50vh; overflow-y:auto;}
  #hotspots-panel.sources-open #hotspot-pane{height:auto; max-height:none;}
  #article-view{overflow:visible;}
  .article-scroll{max-height:none;}
  .article-actions{align-items:stretch;}
  .article-actions .model-picker,.article-actions input{width:100%; min-width:0; flex:1 1 100%;}
  .model-role-row,.model-role-row.simple{grid-template-columns:minmax(0, 1fr);}
  .model-role-row button{width:100%;}
}
/* Iteration 2: the creation page owns the retained visual studios (weekly 3:4
   and skill posters), so they are no longer gated behind the retired lanes. */
#github-studio,#skills-studio{display:flex; flex-direction:column; gap:1rem; min-height:0;}
.studio-heading{margin:0; font-size:1rem;}
/* Skill package sources and degradation are part of the visible studio contract
   now: they used to live in a hidden lane block, which made them unreadable. */
#skills-sources{display:flex; flex-direction:column; gap:.45rem;}
#skills-source-chips{display:flex; flex-wrap:wrap; gap:.4rem; margin:0; padding:0; list-style:none;}
#skills-source-chips li{display:inline-flex;}
#skills-source-chips button{
  font-size:.72rem; padding:.24rem .6rem; border-radius:999px;
  border:1px solid var(--border); background:var(--bg-card); color:var(--text-muted); cursor:pointer;
}
#skills-source-chips button:hover{border-color:var(--accent); color:var(--text-main);}
#skills-source-chips li.is-manual button{border-style:dashed;}
#skills-degraded-note{
  margin:0; padding:.45rem .6rem; font-size:.76rem; line-height:1.5;
  border:1px solid rgba(226, 144, 124,.45); border-radius:var(--radius-sm);
  background:rgba(226, 144, 124,.08); color:var(--err); word-break:break-word;
}
#skills-degraded-note[hidden]{display:none;}
#article-context{margin:0 0 .9rem;}
#article-context[hidden]{display:none;}
#article-context h3{margin:0 0 .3rem;}
#article-context .page-description{margin:.2rem 0 .55rem;}
/* Both poster studios live in the creation page now, so the template picker is
   a normal visible control instead of a lane-gated one. */
#poster-template-dock{margin:0 0 1rem; padding:.8rem .9rem; border:1px solid var(--border);
  border-radius:var(--radius-md); background:var(--bg-card);}
.poster-template-cards{display:flex; gap:.7rem;}
.poster-template-card{
  flex:1 1 0; min-width:0; display:flex; flex-direction:column; gap:.4rem; align-items:center;
  padding:.55rem .5rem .65rem; border:1px solid var(--border); border-radius:var(--radius-md);
  background:var(--bg-card); color:var(--text-muted); cursor:pointer;
}
.poster-template-card.active{border-color:var(--accent); color:var(--text-main);}
.poster-template-preview{
  width:100%; aspect-ratio:3/4; border-radius:8px; overflow:hidden; position:relative;
}
.preview-geometric{
  background:
    linear-gradient(134deg, transparent 0 63%, #101F63 63% 100%),
    linear-gradient(180deg, #101F63 0 8%, transparent 8% 100%),
    #06155A;
}
.preview-toc{
  background:
    radial-gradient(circle at 14% 22%, rgba(255,255,255,.22) 0 2px, transparent 2.1px),
    #ded3c0;
  box-shadow:inset 0 0 0 2px #b39163, inset 0 0 0 10px rgba(177,149,110,.28);
}
#skill-title{font-size:.92rem;}
#skills-article{
  flex:0 0 auto; min-height:6rem; max-height:24rem; overflow-y:auto;
  padding:.85rem 1rem 1rem;
  border:1px solid var(--border); border-radius:var(--radius-md);
  background:rgba(0,0,0,0.22);
}
#skills-article.is-empty #skills-title,
#skills-article.is-empty #skills-body{color:var(--text-muted);}
#skills-article h3{margin:0 0 .4rem; font-family:var(--display); font-size:1.35rem;}
#skills-body{white-space:pre-wrap; line-height:1.7; color:var(--text-main);}
#github-render-controls{display:flex; align-items:center; gap:.45rem; flex-wrap:wrap;}
.gh-swatch{display:flex; align-items:center; gap:.28rem; font-size:.72rem; color:var(--text-muted);}
.gh-swatch input[type=color]{
  width:1.85rem; height:1.85rem; padding:0; border:1px solid var(--border);
  border-radius:6px; background:transparent; cursor:pointer;
}
.gh-toolbar{display:flex; gap:.65rem; flex-wrap:wrap;}
.gh-gallery{display:flex; justify-content:center; align-items:flex-start; min-height:12rem;}
.gh-gallery .gh-tile{width:min(100%, 20rem);}
.gh-tile{
  position:relative; aspect-ratio:4/3; border-radius:0; overflow:hidden;
  background:#141210; border:2px solid #2a261f; cursor:zoom-in;
}
.gh-tile.selected{border-color:var(--accent); box-shadow:0 0 0 1px var(--accent);}
.gh-tile img{width:100%; height:100%; object-fit:contain; display:block; background:#06155a;}
.gh-tile.gh-cover{aspect-ratio:3/4;}
.gh-tile.gh-poster{aspect-ratio:3/4;}
.gh-tile.gh-cover,.gh-tile.gh-poster{
  width:auto; padding:0; border-radius:0; background:#06155a; box-shadow:none;
  display:block; color:#f4efe6;
}
.gh-tile .gh-pending{margin:0; padding:1rem; color:#9a9284; font-size:.78rem;}
.gh-check{
  position:absolute; top:.45rem; right:.45rem; z-index:2;
  width:1.6rem; height:1.6rem; min-height:1.6rem; padding:0; border-radius:99px;
  font-size:.75rem; line-height:1;
}
.gh-cover,.gh-poster{
  width:min(100%, 18rem); border-radius:22px;
  background:#141210; color:#f4efe6; padding:1.4rem 1.25rem 1.2rem;
  box-shadow:0 18px 40px rgba(0,0,0,.28); border:1px solid #2a261f;
  display:flex; flex-direction:column; gap:.7rem; overflow:hidden;
}
.gh-cover{aspect-ratio:3/4;}
.gh-poster{aspect-ratio:3/4;}
.gh-cover .gh-kicker{color:#d4a574; font-size:.78rem; letter-spacing:.16em; text-transform:uppercase;}
.gh-cover h3{margin:0; font-size:2.4rem; line-height:.95; font-weight:700;}
.gh-cover h4{margin:0; color:#d4a574; font-size:1.05rem; font-weight:500;}
.gh-cover-list{display:flex; flex-direction:column; gap:.42rem; margin-top:.4rem;}
.gh-cover-item{
  display:grid; grid-template-columns:1.2rem 1.45rem minmax(0,1fr) auto; gap:.45rem;
  align-items:center; font-size:.78rem;
}
.gh-cover-item img{width:1.45rem; height:1.45rem; border-radius:6px; object-fit:cover;}
.gh-poster .gh-logo{width:3.4rem; height:3.4rem; border-radius:14px; object-fit:cover;}
.gh-poster h3{margin:0; font-size:1.45rem;}
.gh-poster .gh-summary{margin:0; color:#c9c1b4; font-size:.88rem; line-height:1.45;}
.gh-block{background:#1c1916; border-radius:14px; padding:.7rem .8rem;}
.gh-block span{display:block; color:#d4a574; font-size:.68rem; letter-spacing:.08em; margin-bottom:.25rem;}
.gh-block p{margin:0; font-size:.82rem; line-height:1.4; color:#f4efe6;}
.gh-posters{display:flex; gap:.9rem; overflow-x:auto; padding-bottom:.4rem;}
#github-xhs-body{min-height:10rem;}
#xhs-studio[hidden]{display:none !important;}
#xhs-studio{display:flex; flex-direction:column; gap:.7rem;}
.xhs-toolbar{display:flex; gap:.65rem; flex-wrap:wrap;}
.xhs-cards{display:grid; grid-template-columns:repeat(3, minmax(0,1fr)); gap:.7rem;}
.xhs-paper{
  aspect-ratio:9/16; border-radius:18px; overflow:hidden;
  background:
    radial-gradient(120% 70% at 12% 0%, rgba(255,255,255,.55), transparent 46%),
    repeating-linear-gradient(180deg, rgba(92,68,38,.05) 0 1px, transparent 1px 8px),
    linear-gradient(180deg, #f7f1e4 0%, #efe4d2 100%);
  color:#3c342b; padding:.7rem .65rem .8rem;
  box-shadow:inset 0 1px 0 rgba(255,255,255,.55), 0 10px 24px rgba(48,36,22,.12);
  border:1px solid rgba(120,96,64,.16);
  display:flex; flex-direction:column; gap:.45rem;
}
.xhs-head{display:flex; align-items:center; gap:.4rem;}
.xhs-avatar{
  width:1.35rem; height:1.35rem; border-radius:99px; flex:0 0 auto;
  background:#d7c4a8; color:#5a4c3c; font-size:.7rem; font-weight:700;
  display:flex; align-items:center; justify-content:center;
}
.xhs-id{font-size:.68rem; color:#6a5b4a; letter-spacing:.02em;}
.xhs-paper h3{margin:0; font-size:1.02rem; line-height:1.25; font-weight:700; color:#2f291f;}
.xhs-paper img{width:100%; flex:1 1 auto; min-height:0; object-fit:cover; border-radius:12px;}
.xhs-lede,.xhs-copy{margin:0; font-size:.72rem; line-height:1.55; color:#4a4034; overflow:hidden;}
@media (max-width: 900px){
  .xhs-cards{grid-template-columns:repeat(2, minmax(0,1fr));}
}
@media (max-width: 360px){
  .article-actions .model-picker{font-size:.75rem; padding:.45rem .55rem;}
}
@media (prefers-reduced-motion: reduce){
  *, *::before, *::after{animation-duration:0.01ms !important; animation-iteration-count:1 !important; transition-duration:0.01ms !important;}
}

/* Information architecture: the eight primary pages.  Iteration 2 removed the
   older lane controls from the DOM entirely rather than hiding them. */
.app-page{width:100%; max-width:1400px; min-width:0;}
.app-page[hidden]{display:none !important;}
#app-nav{align-items:stretch; gap:.75rem;}
#primary-nav{display:flex; gap:.35rem; flex:1 1 auto; flex-wrap:wrap;}
#primary-nav button{border-radius:999px; padding:.48rem .9rem;}
#primary-nav button[aria-current="page"]{background:var(--bg-card); border-color:var(--accent); color:var(--accent);}
#platform-switcher{margin-left:auto; flex:0 0 auto;}
.app-page > .panel{width:100%;}
#page-home .home-hero{display:grid; grid-template-columns:minmax(0,1.35fr) minmax(16rem,.65fr); gap:1.2rem; align-items:stretch;}
.home-hero-main,.home-hero-side,.ia-card{border:1px solid var(--border); border-radius:var(--radius-lg); background:linear-gradient(145deg, rgba(49,43,31,.92), rgba(24,22,18,.94)); padding:1.4rem;}
.home-hero-main h2{margin:0; font-family:var(--display); font-size:clamp(1.7rem,4vw,3.2rem); line-height:1.05;}
.home-hero-main p{max-width:42rem; color:var(--text-muted); margin:.8rem 0 1.1rem;}
.home-search{display:flex; gap:.55rem; align-items:center;}
.home-search input{flex:1 1 auto; min-width:0;}
.home-shortcuts,.ia-grid{display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:.7rem; margin-top:1rem;}
.ia-card{padding:1rem; min-height:8rem;}
.ia-card h3{margin:0 0 .35rem; font-size:.95rem;}
.ia-card p{margin:0; color:var(--text-muted); font-size:.78rem; line-height:1.5;}
.ia-card button{margin-top:.75rem;}
.page-kicker{margin:0 0 .25rem; color:var(--accent); font-size:.7rem; letter-spacing:.14em; text-transform:uppercase;}
.page-heading{margin:0; font-size:1.25rem;}
.page-description{margin:.25rem 0 1rem; color:var(--text-muted); font-size:.82rem;}
#page-sources .source-summary,#page-runs .run-summary{display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); gap:.55rem; margin:.75rem 0 1rem;}
.summary-stat{padding:.75rem .8rem; border:1px solid var(--border); border-radius:var(--radius-md); background:var(--bg-card);}
.summary-stat strong{display:block; font-size:1.2rem; color:var(--text-main);}
.summary-stat span{display:block; color:var(--text-muted); font-size:.72rem; margin-top:.15rem;}
#source-registry-list,#collection-run-sources{list-style:none; margin:0; padding:0; display:flex; flex-direction:column; gap:.45rem;}
.registry-row,.run-source-row{display:grid; grid-template-columns:minmax(0,1.4fr) .7fr .7fr minmax(0,1fr) auto; gap:.65rem; align-items:center; padding:.7rem .8rem; border:1px solid var(--border); border-radius:var(--radius-md); background:var(--bg-card);}
.registry-row .meta,.run-source-row .meta{color:var(--text-muted); font-size:.75rem; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;}
.source-controls{display:flex; align-items:center; justify-content:flex-end; gap:.45rem; flex-wrap:wrap;}
.source-controls input[type=number]{width:5rem; padding:.3rem .4rem;}
.source-toggle{display:inline-flex; align-items:center; gap:.25rem; color:var(--text-muted); font-size:.72rem; white-space:nowrap;}
.status-pill{display:inline-flex; align-items:center; width:max-content; max-width:100%; border-radius:999px; padding:.18rem .55rem; font-size:.7rem; color:var(--text-muted); border:1px solid var(--border);}
.status-pill.ok{color:var(--ok); border-color:rgba(127, 184, 148,.45);}
.status-pill.error{color:var(--err); border-color:rgba(226, 144, 124,.45);}
.run-empty{padding:1.2rem; color:var(--text-muted); border:1px dashed var(--border); border-radius:var(--radius-md);}
#page-resources .resource-toolbar{display:flex; gap:.55rem; flex-wrap:wrap; align-items:center;}
#page-resources .resource-toolbar input{flex:1 1 22rem;}
#page-resources .resource-options{display:flex; gap:.75rem; flex-wrap:wrap; align-items:center; margin-top:.8rem;}
#page-resources .resource-options label{display:inline-flex; gap:.3rem; align-items:center; color:var(--text-muted);}
#page-resources #resource-pasted-text{width:100%; margin-top:.8rem; min-height:4rem; resize:vertical;}
#resource-results{margin-top:1rem;}
#resource-results[hidden]{display:none;}
#research-summary,#research-diagnostics{margin:.75rem 0 1rem;}
#research-evidence-list{list-style:none; margin:0; padding:0; display:flex; flex-direction:column; gap:.55rem;}
.research-evidence{padding:.8rem; border:1px solid var(--border); border-radius:var(--radius-md); background:var(--bg-card);}
.research-evidence h3{margin:0 0 .3rem; font-size:.95rem;}
.research-evidence p{margin:.25rem 0; color:var(--text-muted); font-size:.8rem; line-height:1.45;}
.research-diagnostic{color:var(--err) !important;}
#settings-runtime-summary{margin:.8rem 0 0;}
#page-creation,#page-publishing{display:block;}
#page-publishing .publishing-actions{display:flex; gap:.6rem; flex-wrap:wrap;}
#page-publishing .publishing-note{color:var(--text-muted); max-width:48rem;}
#page-publishing .publishing-draft-actions{display:flex; gap:.5rem; flex-wrap:wrap; margin-top:.6rem;}
#page-publishing .publishing-draft h3{margin:0 0 .3rem;}
@media (max-width: 900px){
  #page-home .home-hero{grid-template-columns:1fr;}
  .home-shortcuts,.ia-grid{grid-template-columns:1fr;}
  #page-sources .source-summary,#page-runs .run-summary{grid-template-columns:repeat(2,minmax(0,1fr));}
  .registry-row,.run-source-row{grid-template-columns:1fr 1fr;}
  .registry-row > :first-child,.run-source-row > :first-child{grid-column:1 / -1;}
}
/* --- B. Base ------------------------------------------------------------- */
body {
  background-color: var(--ink-000);
  color: var(--paper-1);
  font: var(--fs-sm) / var(--lh-base) var(--font-ui);
  padding: 0 0 var(--s-12);
  background-image:
    radial-gradient(1100px 520px at 88% -12%, rgba(207, 169, 107, 0.10), transparent 60%),
    radial-gradient(760px 420px at -6% 108%, rgba(127, 184, 148, 0.05), transparent 62%);
  background-attachment: fixed;
}

/* One global hidden rule replaces a dozen per-component [hidden] overrides. */
[hidden] { display: none !important; }

::selection { background: var(--amber-a3); color: var(--paper-1); }

:focus-visible {
  outline: 2px solid var(--amber-2);
  outline-offset: 2px;
  border-radius: var(--r-1);
}

* { scrollbar-width: thin; scrollbar-color: var(--ink-300) transparent; }
*::-webkit-scrollbar { width: 10px; height: 10px; }
*::-webkit-scrollbar-thumb {
  background: var(--ink-300); border-radius: var(--r-pill);
  border: 3px solid transparent; background-clip: content-box;
}
*::-webkit-scrollbar-thumb:hover { background-clip: content-box; background-color: #3b3529; }

h1, h2, h3, h4 { letter-spacing: -0.011em; }

a {
  color: var(--amber-2);
  text-decoration-color: var(--amber-a3);
  text-underline-offset: 2px;
  transition: color var(--t-fast) var(--ease);
}
a:hover { color: var(--amber-1); text-decoration-color: var(--amber-2); }
a:visited { color: var(--amber-2); }
#skills-body a, .resource-candidate a, .ia-card a { font-weight: 600; }

code, kbd, samp, .mono, #package-path, .draft-id, .provider-row-meta, .image-url {
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
}

/* Utilities */
.meta, .muted, .panel-sub, .page-description, .article-meta {
  color: var(--paper-3);
  font-size: var(--fs-xs);
  line-height: var(--lh-snug);
}
.panel-sub { font-size: var(--fs-2xs); }
.num { font-variant-numeric: tabular-nums; }
.clamp-2 {
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  overflow: hidden;
}

/* --- C. App shell -------------------------------------------------------- */
header {
  width: 100%; max-width: var(--shell-max);
  margin: 0 auto; padding: var(--s-5) var(--s-6) var(--s-4);
  display: flex; align-items: center; gap: var(--s-4);
  flex-wrap: wrap;
  animation: rise var(--t-slow) var(--ease) both;
  position: relative; z-index: 30;
}
.brand { display: flex; align-items: center; gap: var(--s-3); min-width: 0; }
.brand-mark {
  flex: 0 0 auto; width: 2.5rem; height: 2.5rem; border-radius: var(--r-2);
  display: inline-flex; align-items: center; justify-content: center;
  font-family: var(--font-display); font-size: 1.05rem; font-weight: 700;
  letter-spacing: 0.02em;
  color: #201a0f;
  background: linear-gradient(150deg, var(--amber-1), var(--amber-2) 52%, var(--amber-deep));
  box-shadow: 0 6px 18px -8px rgba(207, 169, 107, 0.55), inset 0 1px 0 rgba(255, 255, 255, 0.35);
}
.brand-text { display: flex; flex-direction: column; gap: 1px; min-width: 0; }
header h1.logo {
  font-family: var(--font-display); font-size: 1.5rem; font-weight: 600;
  letter-spacing: 0.005em; margin: 0; line-height: 1.1; color: var(--paper-1);
}
.brand-kicker {
  margin: 0; font-size: var(--fs-2xs); letter-spacing: 0.14em; text-transform: uppercase;
  color: var(--amber-2); font-weight: 650; line-height: 1.3;
}
.header-meta { display: flex; align-items: center; gap: var(--s-2); margin-left: auto; }
.header-actions { display: flex; align-items: center; gap: var(--s-2); }

#service-status {
  display: inline-flex; align-items: center; gap: var(--s-2);
  font-size: var(--fs-2xs); font-weight: 600; letter-spacing: 0.01em;
  color: var(--paper-2);
  padding: 0.34rem 0.75rem;
  border-radius: var(--r-pill);
  border: 1px solid var(--line-2);
  background: var(--ink-050);
  font-variant-numeric: tabular-nums;
}
#service-status::before {
  content: ""; width: 6px; height: 6px; border-radius: 50%;
  background: var(--jade); box-shadow: 0 0 0 3px var(--jade-soft);
}

#config-btn, #favorites-btn {
  margin-left: 0; border-radius: var(--r-pill);
  padding: 0.42rem 1rem; height: 34px;
  font-size: var(--fs-xs); font-weight: 600;
  border: 1px solid var(--line-2); background: var(--ink-050); color: var(--paper-2);
}
#config-btn:hover:not(:disabled), #favorites-btn:hover:not(:disabled) {
  background: var(--ink-200); border-color: var(--amber-a3); color: var(--paper-1);
  box-shadow: none; transform: none;
}
#favorites-btn[aria-expanded="true"] {
  border-color: var(--amber-2); color: var(--amber-1); background: var(--amber-a1);
}

#app-nav {
  width: 100%; max-width: var(--shell-max);
  margin: 0 auto var(--s-5);
  padding: 0 var(--s-6) var(--s-3);
  border-bottom: 1px solid var(--line-1);
  display: flex; align-items: center; gap: var(--s-3); flex-wrap: wrap;
}

main {
  width: 100%; max-width: var(--shell-max);
  margin: 0 auto; padding: 0 var(--s-6);
  display: block; min-height: 0;
}

/* --- D. Navigation ------------------------------------------------------ */
#primary-nav {
  display: flex; align-items: center; gap: 2px; flex: 1 1 auto; flex-wrap: wrap;
  min-width: 0;
}
.nav-item, #primary-nav button {
  position: relative; border: 0; background: transparent;
  border-radius: var(--r-1);
  padding: 0.44rem 0.85rem; height: 34px;
  font-size: var(--fs-xs); font-weight: 600; letter-spacing: 0.01em;
  color: var(--paper-3); white-space: nowrap;
  display: inline-flex; align-items: center; gap: 0.4rem;
  transition: color var(--t-fast) var(--ease), background var(--t-fast) var(--ease);
}
.nav-item:hover:not(:disabled), #primary-nav button:hover:not(:disabled) {
  color: var(--paper-1); background: var(--ink-100);
  border-color: transparent; box-shadow: none; transform: none;
}
.nav-item[aria-current="page"], #primary-nav button[aria-current="page"] {
  color: var(--amber-1); background: var(--amber-a1);
  border-color: transparent; box-shadow: inset 0 0 0 1px var(--amber-a3);
}
.nav-item[aria-current="page"]::after {
  content: ""; position: absolute; left: 0.85rem; right: 0.85rem; bottom: -1px;
  height: 2px; border-radius: 2px; background: var(--amber-2);
}
.nav-divider {
  width: 1px; height: 18px; background: var(--line-2);
  margin: 0 var(--s-2); flex: 0 0 auto;
}
#platform-switcher { margin-left: auto; flex: 0 0 auto; }
.platform-tabs {
  display: inline-flex; background: var(--ink-050);
  border-radius: var(--r-pill); padding: 3px; border: 1px solid var(--line-1);
  gap: 2px;
}
.platform-tabs button, .platform-tabs button.seg {
  flex: 0 1 auto; min-width: 0; white-space: nowrap;
  border: 0; border-radius: var(--r-pill); background: transparent;
  padding: 0.34rem 0.85rem; height: 30px;
  font-size: var(--fs-2xs); font-weight: 600; color: var(--paper-3);
  transition: color var(--t-fast) var(--ease), background var(--t-fast) var(--ease);
}
.platform-tabs button:hover:not(:disabled) {
  color: var(--paper-1); background: var(--ink-200);
  box-shadow: none; transform: none; border-color: transparent;
}
.platform-tabs button.active, .platform-tabs button.seg.active {
  color: #201a0f; background: linear-gradient(150deg, var(--amber-1), var(--amber-2));
  border-color: transparent; box-shadow: var(--e-1);
}

/* --- E. Buttons --------------------------------------------------------- */
button {
  border: 1px solid var(--line-2); border-radius: var(--r-1);
  padding: 0 0.85rem; min-height: 34px;
  background: var(--ink-100); color: var(--paper-1);
  font: inherit; font-size: var(--fs-xs); font-weight: 600;
  cursor: pointer; outline: none;
  transition: background var(--t-fast) var(--ease), border-color var(--t-fast) var(--ease),
              color var(--t-fast) var(--ease), box-shadow var(--t-fast) var(--ease);
}
button:hover:not(:disabled) {
  background: var(--ink-200); border-color: var(--amber-a3); color: var(--paper-1);
  box-shadow: none; transform: none;
}
button:active:not(:disabled) { background: var(--ink-300); }
button:disabled { opacity: 0.42; cursor: not-allowed; }

button.primary, .btn-primary {
  background: linear-gradient(150deg, var(--amber-1), var(--amber-2) 58%, var(--amber-deep));
  color: #1f190e; border: 1px solid var(--amber-2);
  font-weight: 700; border-radius: var(--r-pill);
  padding: 0 1.35rem; font-size: var(--fs-sm); letter-spacing: 0.01em;
  box-shadow: 0 8px 20px -12px rgba(207, 169, 107, 0.75);
}
button.primary:hover:not(:disabled), .btn-primary:hover:not(:disabled) {
  background: linear-gradient(150deg, #f6ddaa, var(--amber-2) 62%, var(--amber-deep));
  color: #1b1509; border-color: var(--amber-1);
  box-shadow: 0 10px 24px -12px rgba(207, 169, 107, 0.85);
  transform: translateY(-1px);
}
.btn-magic {
  background: linear-gradient(150deg, var(--amber-1), var(--amber-deep));
  color: #1f190e; border: 1px solid var(--amber-2); font-weight: 700;
  display: inline-flex; align-items: center; gap: 0.45rem;
  border-radius: var(--r-pill); padding: 0 1.15rem;
  box-shadow: 0 6px 16px -10px rgba(207, 169, 107, 0.8);
}
.btn-magic:hover:not(:disabled) { filter: brightness(1.06); transform: translateY(-1px); }

/* Quiet / destructive helpers used by the redesigned rows. */
.btn-ghost {
  background: transparent; border-color: var(--line-2); color: var(--paper-2);
}
.btn-ghost:hover:not(:disabled) { background: var(--ink-100); color: var(--paper-1); }
.btn-danger { color: var(--coral); border-color: var(--coral-line); background: var(--coral-soft); }
.btn-danger:hover:not(:disabled) { background: rgba(226, 144, 124, 0.22); border-color: var(--coral); color: #f4c3b6; }
.btn-sm, .btn-sm:hover { min-height: 28px; height: 28px; padding: 0 0.6rem; font-size: var(--fs-2xs); }
.model-picker {
  background: var(--ink-100); border: 1px solid var(--line-2); border-radius: var(--r-pill);
  padding: 0 0.85rem; min-height: 34px; color: var(--paper-1);
  font-weight: 600; font-size: var(--fs-xs);
}
#save-article-btn {
  background: transparent; border: 0; color: var(--paper-3);
  font-weight: 600; font-size: var(--fs-sm); padding: 0 0.85rem;
}
#save-article-btn:hover:not(:disabled) { color: var(--paper-1); background: transparent; box-shadow: none; }

/* --- F. Chips, pills, dots, badges -------------------------------------- */
.chip {
  display: inline-flex; align-items: center; gap: 0.3rem;
  border: 1px solid var(--line-2); border-radius: var(--r-pill);
  padding: 0.16rem 0.55rem; min-height: 22px;
  font-size: var(--fs-2xs); font-weight: 600; color: var(--paper-3);
  background: var(--ink-100);
  white-space: nowrap; max-width: 100%;
  overflow: hidden; text-overflow: ellipsis;
}
.chip-tier { color: var(--amber-1); border-color: var(--amber-a3); background: var(--amber-a1); }
.chip-type { color: var(--paper-2); }
.chip-repo { color: var(--azure); border-color: var(--azure-line); background: var(--azure-soft); }
.chip-community { color: var(--jade); border-color: var(--jade-line); background: var(--jade-soft); }
.chip-media { color: var(--gold); border-color: var(--gold-line); background: var(--gold-soft); }
.chip-official { color: var(--amber-1); border-color: var(--amber-a3); background: var(--amber-a1); }

.status-pill {
  display: inline-flex; align-items: center; gap: 0.3rem;
  width: max-content; max-width: 100%;
  border-radius: var(--r-pill); padding: 0.16rem 0.55rem; min-height: 22px;
  font-size: var(--fs-2xs); font-weight: 650; letter-spacing: 0.01em;
  color: var(--paper-3); border: 1px solid var(--line-2); background: var(--ink-100);
  white-space: nowrap;
}
.status-pill::before {
  content: ""; width: 6px; height: 6px; border-radius: 50%; flex: 0 0 auto;
  background: currentColor;
}
.status-pill.ok { color: var(--jade); border-color: var(--jade-line); background: var(--jade-soft); }
.status-pill.stale { color: var(--gold); border-color: var(--gold-line); background: var(--gold-soft); }
.status-pill.error { color: var(--coral); border-color: var(--coral-line); background: var(--coral-soft); }
.status-pill.disabled, .status-pill.never { color: var(--slate); border-color: var(--slate-line); background: var(--slate-soft); }
.status-pill.run { color: var(--azure); border-color: var(--azure-line); background: var(--azure-soft); }

.sh-dot, .provider-dot, .status-dot {
  width: 7px; height: 7px; border-radius: 50%; flex: 0 0 auto;
  background: var(--slate);
}
.sh-dot.ok, .provider-dot.ok, .status-dot.ok { background: var(--jade); box-shadow: 0 0 0 3px var(--jade-soft); }
.sh-dot.error, .provider-dot.error, .status-dot.error { background: var(--coral); box-shadow: 0 0 0 3px var(--coral-soft); }
.sh-dot.stale, .status-dot.stale { background: var(--gold); box-shadow: 0 0 0 3px var(--gold-soft); }
.sh-dot.never, .sh-dot.disabled, .status-dot.never, .status-dot.disabled { background: var(--slate); }

/* --- G. Cards & panels -------------------------------------------------- */
.panel {
  background: var(--ink-050);
  border: 1px solid var(--line-2); border-radius: var(--r-3);
  padding: var(--s-5) var(--s-5) var(--s-4);
  box-shadow: var(--e-2), inset 0 1px 0 rgba(244, 239, 229, 0.035);
  display: flex; flex-direction: column;
  min-width: 0; max-width: 100%; height: auto; overflow: visible;
  animation: rise var(--t-slow) var(--ease) both;
}
.panel-header {
  display: flex; justify-content: space-between; align-items: flex-start;
  gap: var(--s-3); flex-wrap: wrap; margin-bottom: var(--s-4);
}
.panel-title { font-size: var(--fs-md); font-weight: 700; color: var(--paper-1); margin: 0; }
.panel-sub { margin: 0.18rem 0 0; color: var(--paper-3); }

/* Page header pattern shared by all eight pages. */
.page-head {
  display: flex; align-items: flex-end; justify-content: space-between;
  gap: var(--s-4); flex-wrap: wrap; margin: 0 0 var(--s-5);
  padding-bottom: var(--s-4); border-bottom: 1px solid var(--line-1);
}
.page-head-body { min-width: 0; }
.page-kicker {
  margin: 0 0 0.3rem; color: var(--amber-2);
  font-size: var(--fs-2xs); font-weight: 700;
  letter-spacing: 0.17em; text-transform: uppercase;
}
.page-heading {
  margin: 0; font-family: var(--font-display);
  font-size: var(--fs-2xl); font-weight: 600; line-height: var(--lh-tight);
  color: var(--paper-1);
}
.page-description { margin: 0.4rem 0 0; max-width: 46rem; font-size: var(--fs-sm); color: var(--paper-3); }
.page-head-actions { display: flex; align-items: center; gap: var(--s-2); flex-wrap: wrap; }

.section-title {
  margin: 0 0 var(--s-3); font-size: var(--fs-2xs); font-weight: 700;
  letter-spacing: 0.14em; text-transform: uppercase; color: var(--paper-3);
  display: flex; align-items: center; gap: var(--s-3);
}
.section-title::after { content: ""; flex: 1 1 auto; height: 1px; background: var(--line-1); }

.app-page { width: 100%; max-width: none; min-width: 0; }
.app-page > .panel { width: 100%; }

/* --- H. KPI tiles ------------------------------------------------------- */
.summary-stat, .stat-tile {
  position: relative; overflow: hidden;
  padding: var(--s-3) var(--s-4);
  border: 1px solid var(--line-2); border-radius: var(--r-2);
  background: linear-gradient(180deg, var(--ink-100), var(--ink-050));
  display: flex; flex-direction: column; gap: 0.15rem; min-width: 0;
}
.summary-stat strong, .stat-tile strong {
  display: block; font-size: var(--fs-xl); font-weight: 700;
  color: var(--paper-1); font-variant-numeric: tabular-nums;
  line-height: 1.15; letter-spacing: -0.02em;
}
.summary-stat span, .stat-tile span {
  display: block; color: var(--paper-3); font-size: var(--fs-2xs);
  font-weight: 600; letter-spacing: 0.04em; margin: 0;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.summary-stat::after, .stat-tile::after {
  content: ""; position: absolute; inset: 0 auto 0 0; width: 2px;
  background: var(--amber-3); opacity: 0.7;
}
.summary-stat.is-ok::after, .stat-tile.is-ok::after { background: var(--jade); }
.summary-stat.is-warn::after, .stat-tile.is-warn::after { background: var(--gold); }
.summary-stat.is-err::after, .stat-tile.is-err::after { background: var(--coral); }
.summary-stat.is-ok strong { color: var(--jade); }
.summary-stat.is-warn strong { color: var(--gold); }
.summary-stat.is-err strong { color: var(--coral); }

/* --- H2. Filter bar ---------------------------------------------------- */
.filter-bar {
  display: flex; align-items: center; gap: var(--s-2); flex-wrap: wrap;
  margin: 0 0 var(--s-4);
}
.filter-bar input[type=text] {
  flex: 1 1 14rem; width: auto; min-width: 0;
  height: 32px; min-height: 32px; padding: 0 0.75rem;
  font-size: var(--fs-2xs); border-radius: var(--r-pill);
}
.chip-filter {
  border-radius: var(--r-pill); border: 1px solid var(--line-2);
  background: var(--ink-050); color: var(--paper-3);
  height: 32px; min-height: 32px; padding: 0 0.85rem;
  font-size: var(--fs-2xs); font-weight: 650;
}
.chip-filter:hover:not(:disabled) { color: var(--paper-1); border-color: var(--amber-a3); background: var(--ink-100); }
.chip-filter.is-on {
  color: var(--amber-1); border-color: var(--amber-2); background: var(--amber-a1);
}
.filter-bar .filter-count { margin-left: auto; font-size: var(--fs-2xs); color: var(--paper-3); font-variant-numeric: tabular-nums; }

/* --- I. Data rows (source registry / collection run) -------------------- */
#source-registry-list, #collection-run-sources, #research-evidence-list,
#publishing-draft-list, #provider-list, #endpoint-list {
  list-style: none; margin: 0; padding: 0;
  display: flex; flex-direction: column; gap: var(--s-2);
}

.registry-row, .run-source-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto auto;
  gap: var(--s-2) var(--s-4);
  align-items: center;
  padding: var(--s-3) var(--s-4);
  border: 1px solid var(--line-1); border-radius: var(--r-2);
  background: var(--ink-050);
  transition: border-color var(--t-fast) var(--ease), background var(--t-fast) var(--ease);
}
.registry-row:hover, .run-source-row:hover {
  border-color: var(--line-2); background: var(--ink-100);
}
.registry-row.is-off, .run-source-row.is-off { opacity: 0.62; }
.registry-row.is-bad { border-left: 2px solid var(--coral); }
.run-source-row.is-bad { border-left: 2px solid var(--coral); }
.run-source-row.is-ok { border-left: 2px solid var(--jade); }
.run-source-row.is-skipped { border-left: 2px solid var(--slate); }

.row-main { min-width: 0; display: flex; flex-direction: column; gap: 0.28rem; }
.row-title {
  display: flex; align-items: center; gap: 0.45rem; flex-wrap: wrap;
  font-size: var(--fs-sm); font-weight: 650; color: var(--paper-1);
  min-width: 0;
}
.row-title > .row-name {
  min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  max-width: 100%;
}
.row-name { font-weight: 650; color: var(--paper-1); }
.row-meta {
  display: flex; align-items: center; gap: 0.45rem; flex-wrap: wrap;
  color: var(--paper-3); font-size: var(--fs-2xs); min-width: 0;
}
.row-meta .sep { color: var(--line-3); }
.row-detail {
  min-width: 0; color: var(--paper-3); font-size: var(--fs-2xs);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.row-detail.is-err { color: var(--coral); }
.registry-row .meta, .run-source-row .meta {
  color: var(--paper-3); font-size: var(--fs-2xs); min-width: 0;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.source-controls {
  display: flex; align-items: center; justify-content: flex-end;
  gap: var(--s-2); flex-wrap: wrap;
}
.source-controls input[type=number] {
  width: 4.6rem; min-height: 30px; height: 30px; padding: 0 0.4rem;
  font-size: var(--fs-2xs); text-align: right;
}
.source-toggle {
  display: inline-flex; align-items: center; gap: 0.35rem;
  color: var(--paper-3); font-size: var(--fs-2xs); font-weight: 600;
  white-space: nowrap; cursor: pointer;
}
.source-toggle input[type=checkbox], .image-check input {
  accent-color: var(--amber-2); width: 0.95rem; height: 0.95rem; margin: 0;
}

/* Outcome bar: fetched vs newly inserted vs deduped. */
.outcome-bar {
  grid-column: 1 / -1; display: flex; height: 4px; border-radius: var(--r-pill);
  overflow: hidden; background: var(--ink-300); margin-top: 2px;
}
.outcome-bar i { display: block; height: 100%; }
.outcome-bar .o-new { background: var(--jade); }
.outcome-bar .o-dupe { background: var(--amber-3); }
.outcome-bar .o-none { background: var(--slate); opacity: 0.5; }

.run-empty, .empty-state {
  padding: var(--s-6) var(--s-5); color: var(--paper-3); text-align: center;
  border: 1px dashed var(--line-2); border-radius: var(--r-2);
  background: rgba(0, 0, 0, 0.16);
  font-size: var(--fs-sm);
}
.empty-state strong { display: block; color: var(--paper-2); font-size: var(--fs-md); margin-bottom: 0.3rem; }

/* Run list item */
.run-card {
  border: 1px solid var(--line-1); border-radius: var(--r-2);
  background: var(--ink-050); padding: var(--s-3) var(--s-4);
  display: grid; grid-template-columns: auto minmax(0, 1fr) auto;
  gap: var(--s-3); align-items: center; cursor: pointer;
  transition: border-color var(--t-fast) var(--ease), background var(--t-fast) var(--ease);
}
.run-card:hover { border-color: var(--amber-a3); background: var(--ink-100); }
.run-card.is-active { border-color: var(--amber-2); background: var(--amber-a1); }
.run-card-id {
  font-family: var(--font-mono); font-size: var(--fs-2xs);
  color: var(--paper-2); font-weight: 600;
}

/* --- J. Forms ----------------------------------------------------------- */
label { font-size: var(--fs-xs); font-weight: 600; color: var(--paper-3); }

select, input[type=text], input[type=password], input[type=number],
input[type=datetime-local], textarea {
  border: 1px solid var(--line-2); border-radius: var(--r-1);
  background: var(--ink-100); color: var(--paper-1);
  font: inherit; font-size: var(--fs-xs);
  min-width: 0; max-width: 100%;
  min-height: 34px; padding: 0.4rem 0.7rem;
  transition: border-color var(--t-fast) var(--ease), background var(--t-fast) var(--ease),
              box-shadow var(--t-fast) var(--ease);
}
input[type=text], input[type=password], textarea {
  width: 100%; border-radius: var(--r-2); padding: 0.65rem 0.85rem;
  min-height: 38px; font-size: var(--fs-sm);
}
textarea { line-height: var(--lh-snug); resize: vertical; }
select { color-scheme: dark; padding-right: 1.6rem; }
select option { background: var(--ink-200); color: var(--paper-1); }
select:hover, input:hover, textarea:hover { border-color: var(--amber-a3); }
select:focus, input:focus, textarea:focus {
  border-color: var(--amber-2); background: var(--ink-200);
  box-shadow: var(--ring); outline: none;
}
::placeholder { color: var(--slate); opacity: 1; }
.article-field { display: flex; flex-direction: column; gap: 0.45rem; margin: 0; font-size: var(--fs-xs); font-weight: 600; color: var(--paper-3); }
.article-field input, .article-field textarea { margin: 0; font-weight: 400; }
.article-field-head { display: flex; align-items: center; justify-content: space-between; gap: var(--s-3); }

/* --- K. Callouts -------------------------------------------------------- */
.callout {
  display: flex; gap: var(--s-3); align-items: flex-start;
  border: 1px solid var(--line-2); border-left-width: 2px;
  border-radius: var(--r-2); padding: var(--s-3) var(--s-4);
  background: var(--ink-050); font-size: var(--fs-xs); line-height: var(--lh-snug);
  color: var(--paper-2); margin: 0 0 var(--s-4);
}
.callout-info { border-left-color: var(--azure); background: var(--azure-soft); }
.callout-warn { border-left-color: var(--gold); background: var(--gold-soft); }
.callout-err { border-left-color: var(--coral); background: var(--coral-soft); }
.callout-ok { border-left-color: var(--jade); background: var(--jade-soft); }
.callout strong { color: var(--paper-1); }

/* --- L. Page: Home ------------------------------------------------------ */
#page-home .home-hero {
  display: grid; grid-template-columns: minmax(0, 1.5fr) minmax(17rem, 0.85fr);
  gap: var(--s-4); align-items: stretch;
}
.home-hero-main, .home-hero-side, .ia-card {
  position: relative; overflow: hidden;
  border: 1px solid var(--line-2); border-radius: var(--r-3);
  background: linear-gradient(158deg, var(--ink-100), var(--ink-050) 62%);
  padding: var(--s-6);
  box-shadow: var(--e-2), inset 0 1px 0 rgba(244, 239, 229, 0.04);
}
.home-hero-main::before {
  content: ""; position: absolute; inset: -40% -20% auto auto;
  width: 26rem; height: 26rem; border-radius: 50%;
  background: radial-gradient(circle, rgba(207, 169, 107, 0.13), transparent 66%);
  pointer-events: none;
}
.home-hero-main h2 {
  margin: 0; font-family: var(--font-display);
  font-size: var(--fs-3xl); font-weight: 600; line-height: 1.06;
  letter-spacing: -0.02em;
}
.home-hero-main p { max-width: 40rem; color: var(--paper-3); margin: 0.85rem 0 1.4rem; font-size: var(--fs-md); }
.home-search { display: flex; gap: var(--s-2); align-items: center; max-width: 42rem; }
.home-search input { flex: 1 1 auto; min-width: 0; height: 42px; min-height: 42px; }
.home-search .primary { height: 42px; min-height: 42px; }
.home-hero-side h3 { margin: 0.2rem 0 0.5rem; font-size: var(--fs-lg); font-weight: 650; }
.home-hero-side .page-description { margin: 0 0 var(--s-4); }
.home-shortcuts, .ia-grid {
  display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: var(--s-3); margin-top: var(--s-4);
}
.ia-card {
  padding: var(--s-5); min-height: 9rem;
  display: flex; flex-direction: column; gap: 0.4rem;
  transition: border-color var(--t-fast) var(--ease), transform var(--t-fast) var(--ease);
}
.ia-card:hover { border-color: var(--amber-a3); transform: translateY(-2px); }
.ia-card h3 { margin: 0; font-size: var(--fs-md); font-weight: 650; color: var(--paper-1); }
.ia-card p { margin: 0; color: var(--paper-3); font-size: var(--fs-xs); line-height: var(--lh-snug); flex: 1 1 auto; }
.ia-card button { margin-top: var(--s-3); align-self: flex-start; }

/* --- M. Page: News ------------------------------------------------------ */
#page-news > .page-head { grid-column: 1 / -1; }
#page-news { display: grid; } grid-template-columns: var(--pane-left) minmax(0, 1fr); gap: var(--s-4); align-items: start; }
#hotspots-panel { height: auto; max-height: none; min-height: 0; overflow: visible; }
#hotspot-pane { flex: 0 0 auto; min-height: 0; height: auto; display: flex; flex-direction: column; min-width: 0; overflow: visible; }
.rank-toolbar { display: flex; align-items: center; justify-content: space-between; gap: var(--s-3); margin: 0 0 var(--s-3); }
#rank-tabs { margin: 0; align-self: flex-start; }
#prompt-style-select { width: auto; min-width: 8.5rem; }

#hotspot-list {
  list-style: none; margin: 0; padding: 0 var(--s-1) 0 0;
  flex: 0 0 auto; overflow-y: auto; min-height: 0;
  height: auto; max-height: var(--hotspot-list-h);
  display: flex; flex-direction: column; gap: var(--s-2);
  overscroll-behavior: contain; scrollbar-gutter: stable;
}
#hotspot-list li { margin: 0; animation: rise var(--t-mid) var(--ease) both; }
.hotspot-row { position: relative; }
.hotspot-item {
  display: block; width: 100%; text-align: left;
  background: var(--ink-100); border: 1px solid var(--line-1);
  border-radius: var(--r-2); padding: var(--s-3) 2.3rem var(--s-3) var(--s-4);
  cursor: pointer; position: relative; word-break: break-word;
  transition: border-color var(--t-fast) var(--ease), background var(--t-fast) var(--ease);
}
.hotspot-item:hover:not(:disabled) {
  border-color: var(--amber-a3); background: var(--ink-200); transform: none; box-shadow: none;
}
.hotspot-item.selected {
  /* Quiet focus: reading one item is not the same as picking it into the batch.
     Gold is reserved for .is-picked, so this only lifts the surface and adds
     the 3px reading bar — no gold fill, no inset ring, no gradient. */
  background: var(--ink-200); border-color: var(--line-3);
}
.hotspot-item.selected::before {
  content: ""; position: absolute; left: 0; top: 20%; height: 60%; width: 3px;
  background: linear-gradient(to bottom, var(--amber-1), var(--amber-3));
  border-radius: 0 3px 3px 0;
}
.idx {
  display: inline-block; min-width: 1.35rem; color: var(--amber-2);
  font-weight: 700; font-size: var(--fs-2xs);
  font-variant-numeric: tabular-nums;
}
.ht-label {
  display: inline-flex; align-items: center;
  font-size: var(--fs-2xs); font-weight: 650; letter-spacing: 0.02em;
  color: var(--amber-1); background: var(--amber-a1);
  border: 1px solid var(--amber-a3);
  padding: 0.1rem 0.45rem; border-radius: var(--r-1); margin: 0 0 0.4rem;
}
.ht-title {
  display: block; font-size: var(--fs-sm); font-weight: 650; line-height: var(--lh-snug);
  color: var(--paper-1); margin-bottom: 0.3rem; word-break: break-word; cursor: pointer;
}
.ht-summary {
  font-size: var(--fs-xs); color: var(--paper-3); line-height: var(--lh-snug);
  margin-bottom: 0.5rem; word-break: break-word;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}
.ht-zh-unavailable { display: block; color: var(--gold); margin: 0 0 0.35rem; font-size: var(--fs-2xs); }
.ht-meta {
  display: flex; align-items: center; gap: 0.4rem; flex-wrap: wrap;
  font-size: var(--fs-2xs); color: var(--paper-3);
  border-top: 1px solid var(--line-1); padding-top: 0.5rem;
}
.hotspot-empty { padding: var(--s-5) var(--s-3); color: var(--paper-3); text-align: center; }
.ht-star {
  position: absolute; top: 0.4rem; right: 0.4rem; z-index: 2;
  width: 1.7rem; height: 1.7rem; min-height: 1.7rem; padding: 0;
  border: 0; border-radius: var(--r-pill); background: transparent;
  color: var(--slate); font-size: 1rem; line-height: 1; cursor: pointer;
}
.ht-star:hover { color: var(--amber-1); background: var(--ink-300); border-color: transparent; }
.ht-star[aria-pressed="true"] { color: var(--amber-2); }
.ht-pick {
  position: absolute; right: 0.55rem; bottom: 0.55rem; z-index: 2;
  width: 1.4rem; height: 1.4rem; min-height: 1.4rem; padding: 0; border-radius: var(--r-1);
  display: flex; align-items: center; justify-content: center;
  background: var(--ink-200); border: 1px solid var(--line-2);
  color: var(--paper-3); font-size: 0.78rem; line-height: 1; cursor: pointer;
}
.ht-pick:hover { border-color: var(--amber-a3); color: var(--paper-1); }
.ht-pick.is-on { border-color: var(--amber-2); color: var(--amber-1); font-weight: 700; background: var(--amber-a1); }
.ht-pick-box { accent-color: var(--amber-2); }
.hotspot-item.is-picked, .fav-item.is-picked {
  border-color: var(--amber-2); background: var(--amber-a1);
  box-shadow: inset 0 0 0 1px var(--amber-a3);
}

/* Source health disclosure on the news page (was force-hidden until 3.2.2). */
#source-health-wrap {
  flex: 0 0 auto; margin-top: var(--s-4); padding-top: var(--s-3);
  border-top: 1px solid var(--line-1); display: block;
}
#source-health-toggle {
  width: 100%; justify-content: space-between; text-align: left;
  height: auto; min-height: 34px; padding: 0.5rem 0.75rem;
  font-size: var(--fs-2xs); font-weight: 600; color: var(--paper-2);
  background: var(--ink-050); border: 1px solid var(--line-2);
  border-radius: var(--r-2); display: flex; align-items: center; gap: var(--s-2);
}
#source-health-toggle:hover:not(:disabled) { border-color: var(--amber-a3); background: var(--ink-100); }
#source-health-toggle[aria-expanded="true"] { border-color: var(--amber-2); color: var(--amber-1); }
#source-health-legend {
  display: block; margin: 0.5rem 0 0; font-size: var(--fs-2xs);
  color: var(--paper-3); line-height: var(--lh-snug);
}
#source-health {
  list-style: none; margin: var(--s-2) 0 0; padding: 0;
  display: flex; flex-direction: column; gap: 2px;
  max-height: 18rem; overflow-y: auto; min-height: 0;
}
.source-health {
  display: grid; grid-template-columns: auto minmax(0, 1fr) minmax(0, 1.05fr) auto;
  align-items: center; gap: 0.5rem; min-width: 0;
  padding: 0.34rem 0.5rem; border-radius: var(--r-1);
  font-size: var(--fs-2xs); color: var(--paper-3);
}
.source-health:hover { background: var(--ink-100); }
.source-health .sh-name {
  min-width: 0; color: var(--paper-2); font-weight: 600;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.source-health .sh-meta { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.source-health button { height: 24px; min-height: 24px; padding: 0 0.5rem; font-size: var(--fs-2xs); }

/* Article panel */
#article-panel { min-height: 0; height: auto; overflow: visible; position: sticky; top: var(--s-4); }
#article-view { flex: 0 0 auto; display: flex; flex-direction: column; min-height: 0; overflow: visible; }
#article-empty { color: var(--paper-3); font-size: var(--fs-sm); padding: var(--s-8) 0; margin: 0; text-align: center; }
.article-scroll {
  flex: 0 0 auto; min-height: 0; overflow: visible;
  border: 1px solid var(--line-1); border-radius: var(--r-2);
  background: rgba(0, 0, 0, 0.2); padding: var(--s-4);
  display: flex; flex-direction: column; gap: var(--s-4);
}
#article-body {
  flex: 1 0 auto; min-height: 300px; height: auto; overflow: hidden; resize: none;
  overflow-anchor: none; white-space: pre-wrap; word-break: break-word;
  line-height: 1.85; font-size: var(--fs-sm);
}
.article-actions {
  display: flex; justify-content: space-between; align-items: center;
  gap: var(--s-3); flex-wrap: wrap;
  margin-top: var(--s-4); padding-top: var(--s-4); border-top: 1px solid var(--line-1);
}
.article-actions > .primary, .article-actions .row > button,
.article-actions .row > .model-picker { height: 36px; min-height: 36px; }
#article-paste { max-width: min(32rem, 100%); }

/* Picked dock */
#picked-dock {
  position: fixed; left: var(--s-5); bottom: var(--s-5); z-index: 45;
  display: flex; flex-direction: column; align-items: stretch; gap: var(--s-2);
  min-width: 0; max-width: 18rem; max-height: 40vh; overflow-y: auto;
  padding: var(--s-3); pointer-events: auto;
  border: 1px solid var(--line-2); border-radius: var(--r-2);
  background: var(--ink-200); box-shadow: var(--e-3);
}
.picked-dock-label { font-size: var(--fs-2xs); color: var(--paper-3); font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; }
.picked-dock-chip {
  pointer-events: auto; max-width: 100%; width: 100%;
  font: inherit; font-size: var(--fs-xs); font-weight: 600; line-height: var(--lh-snug);
  color: var(--paper-1); text-align: left; justify-content: flex-start; align-items: flex-start;
  background: var(--amber-a1); border: 1px solid var(--amber-a3);
  border-radius: var(--r-1); padding: 0.6rem 0.7rem; min-height: 3rem;
  cursor: pointer; display: flex;
}
.picked-dock-chip:hover { background: var(--amber-a2); border-color: var(--amber-2); }
.picked-dock-chip-label {
  display: -webkit-box; width: 100%; min-width: 0; overflow: hidden;
  -webkit-box-orient: vertical; -webkit-line-clamp: 2;
  text-overflow: ellipsis; white-space: normal; word-break: break-word;
}
#picked-dock .picked-dock-chip.is-static {
  pointer-events: none; cursor: default; max-width: 100%;
  color: var(--paper-3); background: transparent; border-color: transparent; font-weight: 500;
}

/* Favorites drawer */
.favorites-drawer-head { display: flex; align-items: center; gap: var(--s-3); margin-bottom: var(--s-4); }
.favorites-drawer-head h2 { margin: 0; font-size: var(--fs-md); font-weight: 650; }
#favorites-close { margin-left: auto; border-radius: var(--r-pill); padding: 0 0.8rem; min-height: 30px; font-size: var(--fs-2xs); }
#favorites-list {
  list-style: none; margin: 0; padding: 0; overflow-y: auto; min-height: 0; flex: 1;
  display: flex; flex-direction: column; gap: var(--s-3);
}
.fav-item {
  position: relative; background: var(--ink-100); border: 1px solid var(--line-1);
  border-radius: var(--r-2); padding: 0.7rem 2.2rem 2rem 0.85rem;
}
.fav-item .ht-pick { bottom: auto; top: 2.4rem; }
.fav-item .fav-open-original { margin-right: 1.5rem; }
.fav-open-original {
  display: inline-flex; margin-top: 0.15rem; height: auto; min-height: 0;
  padding: 0.2rem 0.6rem; font-size: var(--fs-2xs); font-weight: 650; line-height: 1.2;
  border-radius: var(--r-pill);
}

/* --- N. Research / Resources ------------------------------------------- */
#page-research .panel, #page-resources .panel { width: 100%; }
#research-summary, #research-diagnostics { margin: var(--s-4) 0; }
.research-evidence.is-degraded { border-left: 2px solid var(--gold-line); }
.research-evidence-degraded {
  margin: var(--s-2) 0 0; padding: var(--s-2) var(--s-3);
  font-size: var(--fs-2xs); color: var(--gold);
  background: var(--gold-soft); border: 1px solid var(--gold-line);
  border-radius: var(--r-1);
}
.research-evidence-meta { color: var(--paper-3); font-size: var(--fs-xs); }
.research-evidence {
  padding: var(--s-4); border: 1px solid var(--line-1);
  border-radius: var(--r-2); background: var(--ink-100);
}
.research-evidence h3 { margin: 0 0 0.35rem; font-size: var(--fs-sm); font-weight: 650; }
.research-evidence p { margin: 0.25rem 0; color: var(--paper-3); font-size: var(--fs-xs); line-height: var(--lh-snug); }
.research-diagnostic { color: var(--coral) !important; }
#settings-runtime-summary { margin: var(--s-4) 0 0; }
#page-resources .resource-toolbar { display: flex; gap: var(--s-2); flex-wrap: wrap; align-items: center; }
#page-resources .resource-toolbar input { flex: 1 1 22rem; }
#page-resources .resource-options { display: flex; gap: var(--s-4); flex-wrap: wrap; align-items: center; margin-top: var(--s-3); }
#page-resources .resource-options label { display: inline-flex; gap: 0.35rem; align-items: center; color: var(--paper-3); }
#page-resources #resource-pasted-text { width: 100%; margin-top: var(--s-3); min-height: 4.5rem; }
#resource-results { margin-top: var(--s-4); }
.resource-candidate, .resource-set-summary, .resource-diagnostics {
  border: 1px solid var(--line-1); border-radius: var(--r-2);
  background: var(--ink-100); padding: var(--s-4); margin-bottom: var(--s-2);
}
.resource-adapter { font-size: var(--fs-xs); }

/* --- O. Creation / Publishing ------------------------------------------ */
#page-creation, #page-publishing { display: block; }
#github-studio, #skills-studio { display: flex; flex-direction: column; gap: var(--s-4); min-height: 0; }
.studio-heading { margin: 0; font-size: var(--fs-lg); font-weight: 650; }
#page-creation > .panel { gap: var(--s-4); }
#poster-template-dock {
  margin: 0 0 var(--s-4); padding: var(--s-4);
  border: 1px solid var(--line-2); border-radius: var(--r-2); background: var(--ink-050);
}
.poster-template-cards { display: flex; gap: var(--s-3); }
.poster-template-card {
  flex: 1 1 0; min-width: 0; display: flex; flex-direction: column;
  gap: 0.45rem; align-items: center;
  padding: var(--s-3); border: 1px solid var(--line-2); border-radius: var(--r-2);
  background: var(--ink-100); color: var(--paper-3); cursor: pointer;
  font-size: var(--fs-xs); font-weight: 600;
  transition: border-color var(--t-fast) var(--ease), color var(--t-fast) var(--ease);
}
.poster-template-card.active { border-color: var(--amber-2); color: var(--paper-1); background: var(--amber-a1); }
.poster-template-preview { width: 100%; aspect-ratio: 3/4; border-radius: var(--r-1); overflow: hidden; position: relative; }
#skills-article {
  flex: 0 0 auto; min-height: 6rem; max-height: 24rem; overflow-y: auto;
  padding: var(--s-4); border: 1px solid var(--line-1);
  border-radius: var(--r-2); background: rgba(0, 0, 0, 0.2);
}
#skills-article h3 { margin: 0 0 0.4rem; font-family: var(--font-display); font-size: 1.3rem; }
#skills-body { white-space: pre-wrap; line-height: 1.75; color: var(--paper-1); font-size: var(--fs-sm); }
#skills-degraded-note {
  margin: 0; padding: 0.6rem 0.75rem; font-size: var(--fs-xs); line-height: var(--lh-snug);
  border: 1px solid var(--coral-line); border-radius: var(--r-1);
  background: var(--coral-soft); color: var(--coral); word-break: break-word;
}
#skills-degraded-note[hidden] { display: none; }
#skills-sources { display: flex; flex-direction: column; gap: 0.45rem; }
#skills-source-chips { display: flex; flex-wrap: wrap; gap: 0.4rem; margin: 0; padding: 0; list-style: none; }
#skills-source-chips li { display: inline-flex; }
#skills-source-chips button {
  font-size: var(--fs-2xs); padding: 0 0.6rem; min-height: 26px; border-radius: var(--r-pill);
  border: 1px solid var(--line-2); background: var(--ink-100); color: var(--paper-3); cursor: pointer;
}
#skills-source-chips button:hover { border-color: var(--amber-2); color: var(--paper-1); }
#skills-source-chips li.is-manual button { border-style: dashed; }
.image-carousel-nav { display: flex; align-items: center; justify-content: center; gap: var(--s-3); }
.image-carousel-nav button { height: 30px; min-height: 30px; padding: 0 0.85rem; font-size: var(--fs-2xs); }
.image-carousel-counter {
  min-width: 4.8rem; text-align: center; font-size: var(--fs-xs);
  font-variant-numeric: tabular-nums; color: var(--paper-3);
}
.image-card {
  display: flex; flex-direction: column; gap: 0.5rem; align-items: stretch;
  background: rgba(0, 0, 0, 0.2); border: 1px solid var(--line-1);
  border-radius: var(--r-1); padding: var(--s-3);
  transition: border-color var(--t-fast) var(--ease);
}
.image-card:hover { border-color: var(--amber-a3); transform: none; }
.image-card img { width: 100%; height: auto; max-height: 18rem; object-fit: contain; border-radius: var(--r-1); background: #0b0a08; display: block; cursor: zoom-in; }
.image-card.cover { border-color: var(--amber-2); box-shadow: 0 0 0 1px var(--amber-a3); }
.image-check { display: inline-flex; align-items: center; gap: 0.35rem; font-size: var(--fs-2xs); color: var(--paper-3); font-weight: 600; cursor: pointer; user-select: none; white-space: nowrap; }
.gh-toolbar, .gh-swatch { display: flex; align-items: center; gap: var(--s-2); flex-wrap: wrap; }
.gh-swatch { font-size: var(--fs-2xs); color: var(--paper-3); }
.gh-gallery { display: flex; justify-content: center; align-items: flex-start; min-height: 12rem; }
.gh-gallery .gh-tile { width: min(100%, 20rem); }
.gh-tile { position: relative; aspect-ratio: 4/3; border-radius: 0; overflow: hidden; background: #141210; border: 2px solid #2a261f; cursor: zoom-in; }
.gh-tile.selected { border-color: var(--amber-2); box-shadow: 0 0 0 1px var(--amber-2); }
.gh-tile img { width: 100%; height: 100%; object-fit: contain; display: block; background: #06155a; }
.gh-tile.gh-cover, .gh-tile.gh-poster { aspect-ratio: 3/4; }
.gh-tile .gh-pending { margin: 0; padding: var(--s-4); color: var(--paper-3); font-size: var(--fs-xs); }
.gh-check { position: absolute; top: 0.45rem; right: 0.45rem; z-index: 2; width: 1.5rem; height: 1.5rem; min-height: 1.5rem; padding: 0; border-radius: var(--r-pill); font-size: var(--fs-2xs); }
.xhs-toolbar { display: flex; gap: var(--s-2); flex-wrap: wrap; }
.xhs-cards { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: var(--s-3); }
#github-xhs-body { min-height: 10rem; }
#package-path { overflow-wrap: anywhere; word-break: normal; background: rgba(0, 0, 0, 0.3); border: 1px solid var(--line-2); border-radius: var(--r-1); padding: 4px 6px; font-size: var(--fs-2xs); }
#manual-box { background: var(--ink-100); border: 1px solid var(--line-2); border-radius: var(--r-2); padding: var(--s-4); margin-top: var(--s-4); }

#page-publishing .publishing-actions { display: flex; gap: var(--s-2); flex-wrap: wrap; }
#page-publishing .publishing-note { color: var(--paper-3); max-width: 48rem; font-size: var(--fs-xs); }
#page-publishing .publishing-draft-actions { display: flex; gap: var(--s-2); flex-wrap: wrap; margin-top: var(--s-3); }
#page-publishing .publishing-draft h3 { margin: 0 0 0.3rem; font-size: var(--fs-sm); font-weight: 650; overflow-wrap: anywhere; }
.publishing-draft {
  border: 1px solid var(--line-1); border-radius: var(--r-2);
  background: var(--ink-100); padding: var(--s-4);
}
.publishing-draft .meta { overflow-wrap: anywhere; }

/* --- P. Settings & dialogs -------------------------------------------- */
#settings-runtime-summary {
  padding: var(--s-4); border: 1px solid var(--line-2); border-radius: var(--r-2);
  background: var(--ink-100); font-size: var(--fs-xs); color: var(--paper-2);
}
.runtime-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(11rem, 1fr));
  gap: var(--s-2); margin: var(--s-3) 0 0;
}
.runtime-item {
  border: 1px solid var(--line-1); border-radius: var(--r-1);
  background: var(--ink-050); padding: 0.55rem 0.7rem;
  display: flex; flex-direction: column; gap: 0.15rem; min-width: 0;
}
.runtime-item .runtime-label {
  font-size: var(--fs-2xs); color: var(--paper-3); font-weight: 700;
  letter-spacing: 0.06em; text-transform: uppercase;
}
.runtime-item .runtime-state {
  font-size: var(--fs-xs); font-weight: 650; color: var(--paper-3);
  display: inline-flex; align-items: center; gap: 0.35rem;
}
.runtime-item.is-on .runtime-state { color: var(--jade); }
.runtime-item.is-off .runtime-state { color: var(--gold); }
.runtime-item.is-on { border-color: var(--jade-line); }
.runtime-item.is-off { border-color: var(--gold-line); }

.provider-section { margin-top: 0.4rem; display: flex; flex-direction: column; gap: var(--s-4); }
.provider-row, .endpoint-card {
  background: var(--ink-100); border: 1px solid var(--line-1);
  border-radius: var(--r-1); padding: var(--s-4);
}
.provider-row { display: flex; flex-wrap: wrap; align-items: center; gap: var(--s-3); }
.provider-row.active { border-color: var(--amber-2); background: var(--amber-a1); }
.provider-row-title { font-size: var(--fs-sm); font-weight: 650; word-break: break-all; }
.provider-row-meta { font-size: var(--fs-2xs); color: var(--paper-3); margin-top: 0.2rem; }
.provider-tag {
  font-size: var(--fs-2xs); border: 1px solid var(--line-2); border-radius: var(--r-pill);
  padding: 0.1rem 0.5rem; color: var(--paper-3); flex: 0 0 auto;
}
.provider-expand { width: 100%; display: flex; flex-wrap: wrap; gap: var(--s-3); align-items: end; padding-top: 0.35rem; }
.provider-expand label { flex: 1 1 14rem; min-width: 0; }
#provider-editor {
  background: var(--ink-200); border: 1px solid var(--line-2); border-radius: var(--r-2);
  padding: var(--s-4); display: flex; flex-direction: column; gap: var(--s-4); margin-top: 0.15rem;
}
.endpoint-card .endpoint-url { word-break: break-all; font-size: var(--fs-xs); margin-bottom: 0.5rem; color: var(--paper-2); }
.purpose-stack { display: flex; flex-direction: column; gap: var(--s-4); margin: var(--s-4) 0; }
.purpose-card {
  background: var(--ink-100); border: 1px solid var(--line-1); border-radius: var(--r-2);
  padding: var(--s-4); display: flex; flex-direction: column; gap: var(--s-3);
}
.purpose-card h3 { margin: 0; font-size: var(--fs-sm); font-weight: 650; color: var(--paper-1); display: flex; align-items: center; gap: 0.45rem; }
.purpose-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.15fr); gap: var(--s-3) var(--s-4); align-items: end; }
.purpose-actions { display: flex; gap: var(--s-3); align-items: end; flex-wrap: wrap; }
.purpose-actions button { height: 36px; min-width: 7rem; white-space: nowrap; }
.model-role-row { display: grid; grid-template-columns: minmax(0, 1fr) 7.5rem auto; align-items: end; gap: var(--s-3); margin-top: 0.7rem; }
.model-role-row.simple { grid-template-columns: minmax(0, 1fr) auto; }
.model-role-row button { height: 36px; min-width: 7rem; white-space: nowrap; }
#settings-platforms { display: flex; }
#settings-platforms button { flex: 1 1 0; }
.account-form[hidden], .account-delete[hidden] { display: none; }

#config-dialog {
  background: var(--ink-050); color: var(--paper-1);
  border: 1px solid var(--line-2); border-radius: var(--r-3); padding: var(--s-6);
  width: min(680px, calc(100% - 32px)); max-height: calc(100vh - 32px); overflow: hidden;
  box-shadow: var(--e-3);
}
#config-dialog::backdrop { background: rgba(8, 7, 6, 0.7); backdrop-filter: blur(6px); }
#config-dialog form { display: grid; grid-template-rows: auto minmax(0, 1fr) auto; gap: var(--s-4); max-height: calc(100vh - 80px); }
#config-dialog h2 { margin: 0; font-size: var(--fs-lg); font-weight: 650; color: var(--paper-1); font-family: var(--font-display); }
#config-dialog label { display: flex; flex-direction: column; gap: 0.4rem; font-size: var(--fs-xs); font-weight: 600; color: var(--paper-3); }
#config-dialog fieldset { border: 0; border-top: 1px solid var(--line-2); padding: var(--s-4) 0 0; margin: 0; min-width: 0; }
#config-dialog legend { font-size: var(--fs-xs); font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; padding: 0 8px 0 0; color: var(--paper-3); }
#config-dialog fieldset > .row { align-items: flex-end; flex-wrap: nowrap; }
#config-dialog fieldset > .row > label { min-width: 0; }
#config-dialog fieldset > .row > button { flex: 0 0 auto; }
.dialog-content { display: flex; flex-direction: column; gap: var(--s-3); min-height: 0; overflow-y: auto; overscroll-behavior: contain; padding-right: 2px; }

.original-dialog {
  border: 0; padding: 0; width: min(1000px, calc(100vw - 32px)); height: min(88vh, 920px);
  max-width: none; max-height: none; background: var(--ink-050); color: var(--paper-1);
  border-radius: var(--r-3); box-shadow: var(--e-3);
}
.original-dialog::backdrop { background: rgba(8, 7, 6, 0.74); backdrop-filter: blur(6px); }
.original-page { height: 100%; display: grid; grid-template-rows: auto auto minmax(0, 1fr); }
.original-head { display: flex; width: 100%; min-width: 0; align-items: center; justify-content: space-between; gap: var(--s-4); padding: var(--s-4) var(--s-5); border-bottom: 1px solid var(--line-2); }
.original-head h2 { margin: 0; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: var(--fs-lg); font-family: var(--font-display); }
.original-head-actions { display: flex; flex: 0 0 auto; flex-wrap: nowrap; align-items: center; gap: 0.35rem; }
#original-translate { height: 34px; width: max-content; flex: 0 0 auto; padding: 0 0.75rem; white-space: nowrap; border-color: var(--amber-a3); background: var(--amber-a1); color: var(--amber-1); font-size: var(--fs-xs); font-weight: 650; }
#original-close { width: 34px; height: 34px; min-height: 34px; flex: 0 0 34px; padding: 0; border-radius: var(--r-pill); font-size: 1.15rem; color: var(--paper-3); }
#original-sources { padding: 0 var(--s-5); flex-wrap: wrap; gap: var(--s-2); }
#original-sources a { color: var(--amber-2); font-size: var(--fs-2xs); word-break: break-all; }
#original-scroll { overflow: auto; padding: var(--s-4) var(--s-5) var(--s-8); }
#original-note { margin: 0 0 var(--s-3); font-size: var(--fs-2xs); color: var(--paper-3); }
.original-section h3 { margin: 0.25rem 0 var(--s-3); font-size: var(--fs-2xs); font-weight: 700; letter-spacing: 0.1em; text-transform: uppercase; color: var(--paper-3); }
#original-content, #original-translation { white-space: pre-wrap; line-height: 1.8; word-break: break-word; font-size: var(--fs-sm); }
#original-translation-wrap { margin-top: var(--s-6); padding-top: var(--s-5); border-top: 1px solid var(--line-2); }
#image-lightbox { border: 0; padding: 0; width: 100vw; height: 100vh; max-width: none; max-height: none; background: rgba(8, 7, 6, 0.92); color: var(--paper-1); }
.lightbox-stage { position: relative; display: flex; flex-direction: column; align-items: center; justify-content: center; width: 100%; height: 100%; padding: var(--s-5); gap: var(--s-3); }
.lightbox-toolbar { position: absolute; top: var(--s-4); right: var(--s-4); display: flex; gap: var(--s-2); z-index: 1; }
.lightbox-toolbar button { height: 32px; min-height: 32px; padding: 0 0.75rem; font-size: var(--fs-2xs); }
#lightbox-image { max-width: min(92vw, 1100px); max-height: min(78vh, 900px); object-fit: contain; border-radius: var(--r-1); background: #0b0a08; cursor: ew-resize; }
#lightbox-caption { margin: 0; font-size: var(--fs-xs); color: var(--paper-3); text-align: center; word-break: break-all; max-width: min(92vw, 1100px); }

/* --- Q. Wait banner ---------------------------------------------------- */
#wait-banner { width: 100%; max-width: var(--shell-max); margin: 0 auto var(--s-4); padding: 0 var(--s-6); }
#wait-banner { display: flex; flex-direction: column; gap: var(--s-2); pointer-events: none; }
#wait-jobs { display: flex; flex-direction: column; gap: 0.55rem; pointer-events: none; }
.wait-job {
  display: grid; grid-template-columns: auto minmax(0, 1fr) auto auto;
  gap: 0.35rem var(--s-3); align-items: center;
  padding: var(--s-3) var(--s-4);
  border-radius: var(--r-2); border: 1px solid var(--amber-a3);
  background: linear-gradient(180deg, var(--ink-200), var(--ink-050));
  box-shadow: var(--e-2), 0 0 0 4px var(--amber-a1);
}
.wait-job.is-done { border-color: var(--jade-line); box-shadow: var(--e-2), 0 0 0 4px var(--jade-soft); }
.wait-job.is-error { border-color: var(--coral-line); box-shadow: var(--e-2), 0 0 0 4px var(--coral-soft); }
.wait-spinner { width: 1.45rem; height: 1.45rem; border-radius: 50%; flex: 0 0 auto; border: 2px solid rgba(207, 169, 107, 0.24); border-top-color: var(--amber-2); animation: waitSpin 0.7s linear infinite; }
.wait-job.is-done .wait-spinner, .wait-job.is-error .wait-spinner { animation: none; border-color: currentColor; }
.wait-title { margin: 0; font-size: var(--fs-sm); font-weight: 700; color: var(--paper-1); }
.wait-detail { margin: 0.12rem 0 0; font-size: var(--fs-2xs); color: var(--paper-3); line-height: var(--lh-snug); }
.wait-time { margin: 0; font-variant-numeric: tabular-nums; font-weight: 700; color: var(--amber-1); white-space: nowrap; font-size: var(--fs-sm); }
.wait-stop { pointer-events: auto; height: 30px; min-height: 30px; padding: 0 0.8rem; border-radius: var(--r-pill); font-size: var(--fs-2xs); font-weight: 700; color: var(--coral); border-color: var(--coral-line); background: var(--coral-soft); }
.wait-stop:hover:not(:disabled) { background: rgba(226, 144, 124, 0.22); border-color: var(--coral); }
.wait-job.is-done .wait-stop, .wait-job.is-error .wait-stop { display: none; }
.wait-copy { font-size: var(--fs-2xs); color: var(--paper-3); }

[role=status] { color: var(--paper-3); font-size: var(--fs-xs); }
[role=status]:empty { display: none; }
.status-error { color: var(--coral); }
.status-ok { color: var(--jade); }
[role=alert] { color: var(--coral); font-size: var(--fs-xs); }

/* --- R. Responsive ----------------------------------------------------- */
@media (max-width: 1180px) {
  #page-news { grid-template-columns: minmax(0, 1fr); }
  #article-panel { position: static; }
}
@media (max-width: 900px) {
  body { padding: 0 0 var(--s-10); }
  header { padding: var(--s-4) var(--s-4) var(--s-3); gap: var(--s-3); }
  .header-meta { order: 3; margin-left: 0; width: 100%; }
  .header-actions { margin-left: auto; }
  #app-nav { padding: 0 var(--s-4) var(--s-3); gap: var(--s-2); }
  main { padding: 0 var(--s-4); }
  #wait-banner { padding: 0 var(--s-4); }
  #primary-nav { flex-wrap: nowrap; overflow-x: auto; padding-bottom: 2px; }
  .nav-divider { display: none; }
  #platform-switcher { margin-left: 0; }
  #hotspot-list { max-height: 46vh; }
  .home-shortcuts, .ia-grid { grid-template-columns: 1fr; }
  #page-home .home-hero { grid-template-columns: 1fr; }
  .home-hero-main, .home-hero-side, .ia-card { padding: var(--s-5); }
  #page-sources .source-summary, #page-runs .run-summary { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .registry-row, .run-source-row { grid-template-columns: minmax(0, 1fr) auto; }
  .registry-row .row-detail, .run-source-row .row-detail { grid-column: 1 / -1; }
  .filter-bar { width: 100%; }
  .source-controls { justify-content: flex-start; grid-column: 1 / -1; }
  .xhs-cards { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .model-role-row, .model-role-row.simple { grid-template-columns: minmax(0, 1fr); }
  .model-role-row button { width: 100%; }
  .purpose-grid { grid-template-columns: minmax(0, 1fr); }
  .article-actions { align-items: stretch; }
  .article-actions .model-picker, .article-actions input { width: 100%; min-width: 0; flex: 1 1 100%; }
  #picked-dock { left: var(--s-3); right: var(--s-3); bottom: var(--s-3); max-width: none; }
}
@media (max-width: 560px) {
  .page-heading { font-size: var(--fs-xl); }
  .poster-template-cards { flex-direction: column; }
  .xhs-cards { grid-template-columns: 1fr; }
}

@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
  }
}
</style>
</head>
<body>
<header>
  <div class="brand">
    <span class="brand-mark" aria-hidden="true">AP</span>
    <span class="brand-text">
      <h1 class="logo">AutoPosting</h1>
      <p class="brand-kicker">AI 信息生产工作台</p>
    </span>
  </div>
  <div class="header-meta">
    <span id="service-status">v3.2.2 · 本地服务</span>
  </div>
  <div class="header-actions">
    <button type="button" id="favorites-btn" aria-expanded="false" aria-controls="favorites-drawer">收藏</button>
    <button type="button" id="config-btn">设置</button>
  </div>
</header>
<aside id="favorites-drawer" hidden>
  <div class="favorites-drawer-head">
    <h2>收藏</h2>
    <button type="button" id="favorites-close">关闭</button>
  </div>
  <ul id="favorites-list" aria-label="收藏列表"></ul>
</aside>
<nav id="app-nav" aria-label="主导航">
  <div id="primary-nav" role="tablist" aria-label="AutoPosting 主导航">
    <button type="button" class="nav-item" data-app-page-target="home" aria-current="page">首页</button>
    <button type="button" class="nav-item" data-app-page-target="news">资讯</button>
    <button type="button" class="nav-item" data-app-page-target="resources">资源</button>
    <span class="nav-divider" aria-hidden="true"></span>
    <button type="button" class="nav-item" data-app-page-target="creation">创作</button>
    <button type="button" class="nav-item" data-app-page-target="publishing">发布</button>
    <span class="nav-divider" aria-hidden="true"></span>
    <button type="button" class="nav-item" data-app-page-target="sources">信息源</button>
    <button type="button" class="nav-item" data-app-page-target="runs">运行记录</button>
    <button type="button" class="nav-item" data-app-page-target="settings">设置</button>
  </div>
  <div id="platform-switcher" class="platform-tabs" role="group" aria-label="平台选择">
    <button type="button" id="seg-wechat" class="seg active" data-platform="wechat" aria-pressed="true">微信</button>
    <button type="button" id="seg-xhs" class="seg" data-platform="xiaohongshu" aria-pressed="false">小红书</button>
  </div>
</nav>
<aside id="wait-banner" hidden role="status" aria-live="assertive">
  <div id="wait-jobs"></div>
</aside>
<main>
  <section class="app-page" id="page-home" data-app-page="home">
    <div class="home-hero">
      <div class="home-hero-main">
        <p class="page-kicker">AutoPosting · 本地优先信息生产</p>
        <h2>今天想发现什么？</h2>
        <p>从长期订阅的资讯，到资源探索、研究、创作与发布，所有工作都从这里继续。</p>
        <div class="home-search">
          <input id="home-query" type="text" placeholder="搜最近值得关注的 AI 新闻，或找一个 Agent Skill / Repo" aria-label="首页探索输入">
          <button type="button" id="home-search-btn" class="primary">开始探索</button>
        </div>
      </div>
      <div class="home-hero-side">
        <p class="page-kicker">继续工作</p>
        <h3>最近活动</h3>
        <p id="home-recent-summary" class="page-description">加载状态后，这里会显示最近的资讯运行、研究和草稿。</p>
        <button type="button" id="home-open-runs">查看运行记录</button>
      </div>
    </div>
    <div class="home-shortcuts">
      <article class="ia-card"><h3>资讯探索</h3><p>统一浏览多来源资讯，选择事件并继续研究。</p><button type="button" data-app-page-target="news">打开资讯</button></article>
      <article class="ia-card"><h3>资源探索</h3><p>用自然语言查找 Repo、Skill、Tool 和 Website。</p><button type="button" data-app-page-target="resources">打开资源</button></article>
      <article class="ia-card"><h3>继续创作</h3><p>从已选资讯进入文章编辑、视觉与发布流程。</p><button type="button" data-app-page-target="creation">打开创作</button></article>
    </div>
  </section>
  <section class="app-page" id="page-news" data-app-page="news" hidden>
  <div class="page-head">
    <div class="page-head-body">
      <p class="page-kicker">News Radar</p>
      <h2 class="page-heading">资讯探索</h2>
      <p class="page-description">按综合分、时效与独立来源聚合长期订阅的资讯事件；选择事件后可继续研究，或直接生成文章。</p>
    </div>
  </div>
  <section class="panel" id="hotspots-panel" aria-labelledby="h-hotspots">
    <div class="panel-header">
      <div>
        <h2 id="h-hotspots" class="panel-title">资讯探索</h2>
        <p id="hotspot-sub" class="panel-sub">按综合分、时效与独立来源聚合资讯事件；选择一个或多个事件后可继续研究</p>
      </div>
      <div class="row">
        <label for="news-scope-select">来源范围</label>
        <select id="news-scope-select" aria-label="来源范围">
          <option value="all" selected>全部来源</option>
          <option value="official">仅订阅源</option>
          <option value="community">仅社区来源</option>
        </select>
        <label for="hours-select">时间范围</label>
        <select id="hours-select">
          <option value="4" selected>4 小时</option>
          <option value="6">6 小时</option>
          <option value="24">24 小时</option>
          <option value="72">72 小时</option>
          <option value="168">168 小时</option>
          <option value="720">720 小时</option>
          <option value="custom">自定义</option>
        </select>
        <input id="hours-custom" type="number" min="1" max="720" value="2" hidden aria-label="自定义小时数">
        <button type="button" id="collect-btn">刷新全部</button>
        <button type="button" id="translate-btn">翻译标题</button>
      </div>
    </div>
    <div class="rank-toolbar">
      <div id="rank-tabs" class="platform-tabs" role="tablist" aria-label="排序方式">
        <button type="button" class="seg active" data-rank="heat" aria-selected="true">热度优先</button>
        <button type="button" class="seg" data-rank="time" aria-selected="false">时间优先</button>
      </div>
      <button type="button" id="open-original-btn" disabled>打开原文</button>
      <button type="button" id="research-btn" class="primary" disabled>研究已选事件</button>
    </div>
    <div id="hotspot-pane">
      <ul id="hotspot-list"></ul>
      <p id="hotspot-status" role="status" aria-live="polite"></p>
    </div>
    <div id="source-health-wrap">
      <button type="button" id="source-health-toggle" aria-expanded="false" aria-controls="source-health">订阅源状态</button>
      <p id="source-health-legend" class="source-legend">这些不是新闻。抓取成功=这个订阅上次拉 RSS 没报错；「只刷这个」只重新抓这一路。</p>
      <ul id="source-health" hidden aria-label="来源健康"></ul>
    </div>
  </section>
  </section>
  <section class="app-page" id="page-research" data-app-page="research" hidden>
    <section class="panel" aria-labelledby="h-research">
      <div class="page-head">
        <div class="page-head-body">
          <p class="page-kicker">Topic Dossier</p>
          <h2 id="h-research" class="page-heading">事件研究</h2>
          <p class="page-description">从资讯页选择一个或多个事件，研究结果会保留事件来源、检索计划、证据分组与 Jev 诊断。</p>
        </div>
      </div>
      <div class="resource-toolbar">
        <input id="research-query" type="text" maxlength="200" placeholder="可选：限定研究问题" aria-label="研究问题">
        <button type="button" id="research-run-btn" class="primary" disabled>开始研究</button>
        <button type="button" id="research-to-creation-btn" disabled>进入创作</button>
        <button type="button" id="research-back-news">返回资讯</button>
      </div>
      <p id="research-status" class="page-description" role="status" aria-live="polite">尚未提交研究。</p>
      <div id="research-summary" class="ia-grid"></div>
      <div id="research-diagnostics"></div>
      <ul id="research-evidence-list" aria-label="研究证据"></ul>
    </section>
  </section>
  <section class="app-page" id="page-resources" data-app-page="resources" hidden>
  <section class="panel" aria-labelledby="h-resources">
      <div class="page-head">
        <div class="page-head-body">
          <p class="page-kicker">Resource Discovery</p>
          <h2 id="h-resources" class="page-heading">资源探索</h2>
          <p class="page-description">用一句话搜索 Repo、Skill、Web 资源。结果来自统一 ResourceSet，不会把旧 Skill / GitHub 工作台 JSON 冒充成统一候选，也不会自动进入创作或发布。</p>
        </div>
      </div>
      <div class="resource-toolbar">
        <input id="resource-query" type="text" maxlength="200" placeholder="例如：能自动制作 PPT 的 Agent Skill / Repo" aria-label="资源探索输入">
        <button type="button" id="resource-search-btn" class="primary">搜索资源</button>
        <button type="button" id="resource-weekly-btn">GitHub Weekly Preset</button>
        <button type="button" id="resource-studio-btn">送入 Skill 出图</button>
        <button type="button" id="resource-weekly-studio-btn">周榜出图工作台</button>
      </div>
      <div class="resource-options" role="group" aria-label="资源来源和类型">
        <span class="muted">检索源</span>
        <label><input type="checkbox" class="resource-adapter" value="github" checked> Repo</label>
        <label><input type="checkbox" class="resource-adapter" value="skills" checked> Skill</label>
        <label><input type="checkbox" class="resource-adapter" value="web" checked> Web</label>
        <label for="resource-kind-filter" class="muted">显示</label>
        <select id="resource-kind-filter" aria-label="资源类型筛选">
          <option value="all">全部类型</option>
          <option value="repo">Repo</option>
          <option value="skill">Skill</option>
          <option value="website">Web</option>
          <option value="tool">Tool</option>
        </select>
      </div>
      <textarea id="resource-pasted-text" rows="2" maxlength="20000" placeholder="可选：粘贴 GitHub / skills.sh Skill 链接，加入同一组候选" aria-label="粘贴 Skill 链接"></textarea>
      <p id="resource-status" class="page-description" role="status" aria-live="polite">选择 Repo、Skill、Web 后搜索；可用 GitHub Weekly Preset 直接查本周 AI 项目。</p>
      <div id="resource-results" class="ia-grid" data-resource-discovery-slot="unified">
        <article class="ia-card"><h3>还没有资源搜索</h3><p>输入自然语言需求，结果会显示统一 ResourceSet 的计划、来源状态、诊断和候选。</p></article>
      </div>
    </section>
  </section>
  <section class="app-page" id="page-creation" data-app-page="creation" hidden>
  <div class="page-head">
    <div class="page-head-body">
      <p class="page-kicker">Creation Studio</p>
      <h2 class="page-heading">创作</h2>
      <p class="page-description">文章生成与两张视觉工作台集中在这里：正文编辑、GitHub 周榜出图与 Skill 海报都在同一个页面完成。</p>
    </div>
    <div class="page-head-actions">
      <button type="button" data-app-page-target="publishing">去发布</button>
    </div>
  </div>
  <section class="panel" id="article-panel" aria-labelledby="h-article">
    <h2 id="h-article" class="sr-only">文章</h2>
    <article id="article-context" class="ia-card" hidden>
      <p class="page-kicker">ArticleGenerationContext</p>
      <h3 id="article-context-title"></h3>
      <p id="article-context-meta" class="page-description"></p>
      <button type="button" id="article-context-clear">清除研究上下文</button>
    </article>
    <div class="panel-header">
      <div class="row">
      <select id="prompt-style-select" aria-label="生成风格" title="生成风格">
        <option value="AI专家" selected>AI专家</option>
        <option value="经验分享">经验分享</option>
        <option value="klinkenborg_style">klinkenborg_style</option>
      </select>
      <button type="button" id="generate-btn" class="btn-magic" disabled>生成文章</button>
      <textarea id="article-paste" rows="1" placeholder="可粘贴原文页面，点生成文章时一并送进模型"></textarea>
      </div>
    </div>
    <p id="article-status" role="status" aria-live="polite"></p>
    <p id="studio-status" class="page-description" role="status" aria-live="polite"></p>
    <p id="article-empty">尚未生成文章</p>
    <div id="poster-template-dock">
      <p class="panel-sub">海报模版。切换只改背景和风格，文案模块不变。</p>
      <div class="poster-template-cards" role="list">
        <button type="button" class="poster-template-card active" data-template="1" aria-pressed="true">
          <span class="poster-template-preview preview-geometric" aria-hidden="true"></span>
          <span>模版一</span>
        </button>
        <button type="button" class="poster-template-card" data-template="2" aria-pressed="false">
          <span class="poster-template-preview preview-toc" aria-hidden="true"></span>
          <span>模版二</span>
        </button>
      </div>
    </div>
    <div id="github-studio">
      <h3 class="studio-heading">GitHub 周榜出图工作台</h3>
      <p class="page-description">周榜发现现在走资源探索的「GitHub Weekly Preset」；这里负责抓取本周 AI 项目、选主题配色并生成 3:4 图片。</p>
      <div class="gh-toolbar">
        <button type="button" id="github-fetch-btn">抓取本周周榜</button>
        <span id="github-render-controls">
          <button type="button" id="github-render-btn">生成图片</button>
          <span class="platform-tabs" role="group" aria-label="配色">
            <button type="button" class="seg active" id="gh-theme-dark" data-theme="dark">暗色</button>
            <button type="button" class="seg" id="gh-theme-light" data-theme="light">浅色</button>
          </span>
          <label class="gh-swatch">主色<input id="gh-color-main" type="color" value="#06155A"></label>
          <label class="gh-swatch">副色<input id="gh-color-secondary" type="color" value="#101F63"></label>
          <label class="gh-swatch">强调色<input id="gh-color-accent" type="color" value="#F3F0E8"></label>
        </span>
        <button type="button" id="github-select-all" disabled>全选</button>
        <button type="button" id="github-export-btn" disabled>导出选中图片</button>
        <button type="button" id="github-copy-btn" disabled>复制小红书文案</button>
        <button type="button" data-app-page-target="resources">去资源探索</button>
      </div>
      <div id="github-gallery" class="gh-gallery"></div>
      <div class="image-carousel-nav" id="github-gallery-nav" hidden>
        <span id="github-gallery-counter" class="image-carousel-counter">0 / 0</span>
      </div>
      <label class="article-field">小红书标题<input id="github-xhs-title" type="text" readonly value="本周值得关注的github ai项目前10"></label>
      <label class="article-field">小红书正文<textarea id="github-xhs-body" rows="12"></textarea></label>
    </div>
    <div id="skills-studio">
      <h3 class="studio-heading">Skill 海报出图工作台</h3>
      <p class="page-description">在资源探索里用自然语言找 Skill 或粘贴 Skill 链接，再送到这里生成封面与每个 skill 一张海报。</p>
      <div class="gh-toolbar">
        <button type="button" id="skills-fetch-btn">读取上次收录</button>
        <button type="button" id="skills-clear-btn">清空收录</button>
        <button type="button" data-app-page-target="resources">去资源探索</button>
      </div>
      <div id="skills-sources">
        <ul id="skills-source-chips" aria-label="已收录 skill 的来源"></ul>
        <p id="skills-degraded-note" role="status" hidden></p>
      </div>
      <label class="article-field" for="skill-title">标题
        <input id="skill-title" type="text" maxlength="120" placeholder="封面海报标题；同时作为介绍文标题">
      </label>
      <div class="gh-toolbar">
        <button type="button" id="skills-render-btn">生成图片</button>
        <button type="button" id="skills-cover-btn" disabled>只重新生成封面海报</button>
        <button type="button" id="skills-select-all" disabled>全选</button>
        <button type="button" id="skills-export-btn" disabled>导出选中图片</button>
      </div>
      <div id="skills-gallery" class="gh-gallery"></div>
      <div class="image-carousel-nav" id="skills-gallery-nav" hidden>
        <span id="skills-gallery-counter" class="image-carousel-counter">0 / 0</span>
      </div>
      <div id="skills-article">
        <h3 id="skills-title">介绍文</h3>
        <div id="skills-body">查找完成后会生成介绍文。按收录 skill 从左到右、从上到下说明。</div>
      </div>
    </div>
    <article id="article-view">
      <div id="article-scroll" class="article-scroll" hidden>
        <label class="article-field">标题<input id="article-title" type="text"></label>
        <div id="image-picker" hidden>
          <div class="image-picker-head">
            <p class="panel-sub">原文配图。点击预览可看大图；放大后点照片左侧上一张、右侧下一张，Esc 退出。勾选封面和正文。默认只勾选封面，正文图需另行勾选。</p>
            <button type="button" id="image-rescan">重新扫描</button>
          </div>
          <div id="image-picker-list"></div>
          <div class="image-carousel-nav" id="image-picker-nav" hidden>
            <span id="image-picker-counter" class="image-carousel-counter">0 / 0</span>
          </div>
          <p id="image-picker-status" role="status"></p>
        </div>
        <div id="xhs-studio" hidden>
          <div class="xhs-toolbar">
            <button type="button" id="xhs-export-btn" disabled>导出图片</button>
          </div>
          <div id="xhs-cards" class="xhs-cards"></div>
          <canvas id="xhs-canvas" hidden></canvas>
        </div>
        <div class="article-field">
          <div class="article-field-head">
            <span>正文</span>
          </div>
          <textarea id="article-body" rows="14"></textarea>
        </div>
        <p id="article-sources" class="article-meta muted"></p>
        <p id="draft-id" class="muted"></p>
      </div>
      <div class="article-actions" aria-label="文章操作">
        <div class="row">
          <button type="button" id="save-article-btn" disabled>保存修改</button>
          <select id="article-model" class="model-picker" aria-label="文章模型"></select>
        </div>
        <div class="row">
          <input id="schedule-at" type="datetime-local" aria-label="定时到草稿时间">
          <button type="button" id="schedule-btn" disabled>定时到草稿</button>
          <button type="button" id="publish-btn" disabled class="primary">到草稿</button>
        </div>
      </div>
      <p id="publish-status" role="status" aria-live="polite"></p>
      <div id="manual-box" hidden>
        <p>发布包已生成，仍需在官方创作中心人工提交。</p>
        <p>本地发布包：<code id="package-path"></code></p>
        <button type="button" id="creator-btn">打开官方创作中心</button>
      </div>
    </article>
  </section>
  </section>
  <section class="app-page" id="page-publishing" data-app-page="publishing" hidden>
    <section class="panel" aria-labelledby="h-publishing">
      <div class="page-head">
        <div class="page-head-body">
          <p class="page-kicker">Publishing</p>
          <h2 id="h-publishing" class="page-heading">发布</h2>
          <p class="publishing-note">审批、微信草稿、小红书本地发布包与调度的统一入口。发布动作复用现有安全边界：微信只写入草稿箱，小红书只生成本地包等待人工提交。</p>
        </div>
      </div>
      <div class="publishing-actions">
        <button type="button" id="publishing-refresh-btn">刷新草稿</button>
        <button type="button" id="publishing-open-creation" class="primary">继续创作</button>
        <button type="button" id="publishing-open-settings">配置发布账号</button>
      </div>
      <p id="publishing-summary" class="page-description" role="status" aria-live="polite">正在加载草稿…</p>
      <div id="publishing-draft-list" class="ia-grid"></div>
    </section>
  </section>
  <section class="app-page" id="page-sources" data-app-page="sources" hidden>
    <section class="panel" aria-labelledby="h-sources">
      <div class="page-head">
        <div class="page-head-body">
          <p class="page-kicker">Source Registry</p>
          <h2 id="h-sources" class="page-heading">信息源</h2>
          <p class="page-description">长期订阅源独立管理；临时研究证据不会自动写入这里。抓取失败会保留原因，成功会记录上次成功时间与连续失败次数。</p>
        </div>
        <div class="page-head-actions">
          <button type="button" id="source-open-runs">查看运行记录</button>
          <button type="button" id="source-refresh-all" class="primary">刷新全部信息源</button>
        </div>
      </div>
      <div id="source-summary" class="source-summary"></div>
      <div class="filter-bar" role="group" aria-label="筛选信息源">
        <button type="button" class="chip-filter is-on" data-source-filter="all" aria-pressed="true">全部</button>
        <button type="button" class="chip-filter" data-source-filter="enabled" aria-pressed="false">已启用</button>
        <button type="button" class="chip-filter" data-source-filter="error" aria-pressed="false">抓取异常</button>
        <button type="button" class="chip-filter" data-source-filter="disabled" aria-pressed="false">已停用</button>
        <input id="source-search" type="text" placeholder="按名称、来源或适配器筛选" aria-label="筛选信息源名称">
        <span id="source-filter-count" class="filter-count"></span>
      </div>
      <p id="source-status" class="page-description" role="status" aria-live="polite"></p>
      <ul id="source-registry-list" aria-label="信息源列表"></ul>
    </section>
  </section>
  <section class="app-page" id="page-runs" data-app-page="runs" hidden>
    <section class="panel" aria-labelledby="h-runs">
      <div class="page-head">
        <div class="page-head-body">
          <p class="page-kicker">Collection Runs</p>
          <h2 id="h-runs" class="page-heading">运行记录</h2>
          <p class="page-description">每次采集都会保留尝试、成功、失败、去重和聚类结果；点击一条运行可查看源级明细。</p>
        </div>
      </div>
      <div id="run-summary" class="run-summary"></div>
      <ul id="collection-run-sources" aria-label="本次运行的信息源明细"></ul>
    </section>
  </section>
  <section class="app-page" id="page-settings" data-app-page="settings" hidden>
    <section class="panel" aria-labelledby="h-settings">
      <div class="page-head">
        <div class="page-head-body">
          <p class="page-kicker">System</p>
          <h2 id="h-settings" class="page-heading">设置</h2>
          <p class="page-description">模型、搜索与发布账号逐步集中到这里。现有设置表单暂以兼容对话框承载。</p>
        </div>
      </div>
      <div class="ia-grid">
        <article class="ia-card"><h3>AI Providers</h3><p>文章、翻译、榜单模型与 Jev 配置。</p><button type="button" id="settings-open-model">打开模型设置</button></article>
        <article class="ia-card"><h3>Planner / Jev</h3><p id="settings-runtime-summary">研究规划和 Jev 使用当前已保存模型端点；状态加载中。</p><button type="button" id="settings-open-research">打开资讯研究</button></article>
        <article class="ia-card"><h3>Search / Data</h3><p>信息源和资源搜索配置集中管理；GitHub Token 只写入本机钥匙串，不回显。</p><button type="button" id="settings-open-sources">管理信息源</button></article>
        <article class="ia-card"><h3>GitHub Token</h3><label>新 Token（不会回显）<input id="settings-github-token" type="password" autocomplete="off" aria-label="设置页 GitHub Token"></label><div class="row"><button type="button" id="settings-save-github-token">保存</button><button type="button" id="settings-clear-github-token">清除</button></div><p id="settings-github-status" class="muted">未读取状态。</p></article>
        <article class="ia-card"><h3>Publishing</h3><p>微信草稿、小红书人工发布包与其他账号。</p><button type="button" id="settings-open-accounts">打开账号设置</button></article>
      </div>
    </section>
  </section>
</main>
<aside id="picked-dock" hidden></aside>

<dialog id="config-dialog" aria-labelledby="config-title">
  <form id="config-form" novalidate>
    <h2 id="config-title">设置</h2>
    <div class="dialog-content">
      <div id="settings-pages" class="row platform-tabs" role="tablist" aria-label="设置页面">
        <button type="button" class="seg active" data-settings-page="model" role="tab" aria-selected="true">模型</button>
        <button type="button" class="seg" data-settings-page="style" role="tab" aria-selected="false">写作风格</button>
        <button type="button" class="seg" data-settings-page="accounts" role="tab" aria-selected="false">账号</button>
      </div>
      <fieldset class="settings-page" data-page="model">
        <legend>模型</legend>
        <p class="muted">文章、翻译、榜单各自保存。先选提供方，再从下拉列表选模型。下方已保存的提供方可一键套到全部入口。</p>
        <div class="purpose-stack">
          <section class="purpose-card">
            <h3>文章模型</h3>
            <div class="purpose-grid">
              <label>提供方<select name="article_provider"></select></label>
              <label>模型<select name="model"></select></label>
            </div>
            <div class="purpose-actions">
              <label>Effort<select name="effort">
                <option value="none">none</option><option value="minimal">minimal</option>
                <option value="low">low</option><option value="medium">medium</option>
                <option value="high">high</option><option value="xhigh">xhigh</option>
                <option value="max">max</option><option value="ultra">ultra</option>
              </select></label>
              <button type="button" id="endpoint-use-article">用于文章</button>
            </div>
          </section>
          <section class="purpose-card">
            <h3>翻译模型</h3>
            <div class="purpose-grid">
              <label>提供方<select name="translation_provider"></select></label>
              <label>模型<select name="translation_model"></select></label>
            </div>
            <div class="purpose-actions">
              <button type="button" id="endpoint-use-translation">用于翻译</button>
            </div>
          </section>
          <section class="purpose-card">
            <h3>榜单模型</h3>
            <p class="muted">只改写周榜海报和 skill 海报里的简介、能做什么、适合什么场景、如何实现。名称、星标／装机量、页面模板不走模型。</p>
            <div class="purpose-grid">
              <label>提供方<select name="leaderboard_provider"></select></label>
              <label>模型<select name="leaderboard_model"></select></label>
            </div>
            <div class="purpose-actions">
              <button type="button" id="endpoint-use-leaderboard">用于榜单</button>
            </div>
          </section>
        </div>
        <div class="provider-section">
          <p class="muted">已保存提供方。点开后用下拉列表选模型，可一键配置全部入口。</p>
          <ul id="provider-list"></ul>
          <ul id="endpoint-list" hidden></ul>
          <button type="button" id="provider-add">添加自定义提供方</button>
          <div id="provider-editor" hidden>
            <strong id="provider-editor-title">自定义提供方</strong>
            <label>API Key<input name="api_key" type="password" autocomplete="off"></label>
            <p id="api-key-hint" class="muted">已保存的密钥不会回显，留空表示保持原值。</p>
            <label>接口地址<input name="base_url" type="text" autocomplete="off"></label>
            <div class="purpose-actions">
              <button type="button" id="endpoint-connect">获取可用模型</button>
              <button type="button" id="provider-apply" class="primary">保存提供方</button>
              <button type="button" id="provider-cancel">取消</button>
              <button type="button" id="provider-remove">删除</button>
            </div>
          </div>
        </div>
        <p id="model-endpoint-status" role="status" aria-live="polite"></p>
      </fieldset>
      <fieldset class="settings-page" data-page="style" hidden>
        <legend>文章生成风格</legend>
        <label>风格要求<textarea name="style" rows="12"></textarea></label>
        <label>导入文本文件<input id="style-file" type="file" accept="text/plain,.txt,.md"></label>
        <p class="muted">文件仅在浏览器本地读取，保存后内容会在生成文章前传给模型。</p>
      </fieldset>
      <fieldset class="settings-page" data-page="accounts" hidden>
        <legend>账号配置</legend>
        <div id="settings-platforms" class="row platform-tabs" role="group" aria-label="账号平台">
          <button type="button" class="seg active" data-settings-platform="wechat" aria-pressed="true">微信公众号</button>
          <button type="button" class="seg" data-settings-platform="xiaohongshu" aria-pressed="false">小红书</button>
          <button type="button" class="seg" data-settings-platform="x" aria-pressed="false">X</button>
        </div>
        <div id="settings-wechat" class="settings-panel">
          <p id="wechat-status" class="muted">微信公众号 API：未配置</p>
          <div id="wechat-form" class="account-form">
            <label>App ID<input name="app_id" type="text" autocomplete="off"></label>
            <label>App Secret<input name="app_secret" type="password" autocomplete="off"></label>
            <label>默认封面 Media ID（可选）<input name="thumb_media_id" type="text" autocomplete="off"></label>
            <p class="muted">一般不用填。点「到草稿」时会把已选原文配图上传成封面；没有已选配图时才生成标题大字报；再没有才用这里的备用 ID。</p>
            <label>作者（可选）<input name="author" type="text" autocomplete="off"></label>
          </div>
          <button type="button" id="wechat-delete" class="account-delete" hidden>删除配置</button>
        </div>
        <div id="settings-xiaohongshu" class="settings-panel" hidden>
          <p id="xhs-status" class="muted">小红书：未填写账号名称</p>
          <div id="xhs-form" class="account-form">
            <label>账号名称（可选）<input name="account_name" type="text" autocomplete="off"></label>
            <label>官方创作中心<input name="creator_url" type="text" autocomplete="off"></label>
          </div>
          <button type="button" id="xhs-delete" class="account-delete" hidden>删除配置</button>
        </div>
        <div id="settings-x" class="settings-panel" hidden>
          <p id="x-status" class="muted">X 官方读取令牌：未配置</p>
          <div id="x-form" class="account-form">
            <label>Bearer Token<input name="x_bearer_token" type="password" autocomplete="off"></label>
            <p class="muted">用于读取已启用的 X 账号时间线。不爬网页。S4 讨论号可直接开；S1/S2 仍要先人工确认身份。</p>
          </div>
          <button type="button" id="x-delete" class="account-delete" hidden>删除配置</button>
        </div>
      </fieldset>
      <p id="config-error" role="alert"></p>
    </div>
    <div class="row">
      <button type="submit" id="config-save" class="primary">保存</button>
      <button type="button" id="config-cancel">关闭</button>
    </div>
  </form>
</dialog>

<dialog id="original-dialog" class="original-dialog" aria-labelledby="original-title">
  <div class="original-page">
    <div class="original-head">
      <h2 id="original-title">候选原文</h2>
      <div class="original-head-actions">
        <button type="button" id="original-translate">翻译文章</button>
        <button type="button" id="original-close" aria-label="关闭" title="关闭">×</button>
      </div>
    </div>
    <div id="original-sources" class="row"></div>
    <div id="original-scroll" role="document">
      <p id="original-note"></p>
      <section class="original-section">
        <h3>原文</h3>
        <div id="original-content"></div>
      </section>
      <section id="original-translation-wrap" class="original-section" hidden>
        <h3>中文翻译</h3>
        <div id="original-translation"></div>
      </section>
    </div>
  </div>
</dialog>

<dialog id="image-lightbox" aria-label="图片放大预览">
  <div class="lightbox-stage">
    <div class="lightbox-toolbar">
      <button type="button" id="lightbox-prev">上一张</button>
      <button type="button" id="lightbox-next">下一张</button>
      <button type="button" id="lightbox-close">关闭</button>
    </div>
    <img id="lightbox-image" alt="放大预览">
    <p id="lightbox-caption"></p>
  </div>
</dialog>

<script>
"use strict";
var state = {
  page: "home",
  hours: 4,
  rank: "heat",
  newsScope: "all",
  newsStyle: "AI专家",
  rankings: {heat: [], time: []},
  radarEvents: {},
  sources: [],
  platform: "wechat",
  item: null,
  pickedIds: {},
  favoriteIds: {},
  draft: null,
  lightboxIndex: 0,
  imagePickerIndex: 0,
  githubGalleryIndex: 0,
  skillsGalleryIndex: 0,
  posterTemplate: "1",
  creatorUrl: "",
  pendingPublish: false,
  articleDirty: false,
  settingsPlatform: "wechat",
  settingsPage: "model",
  style: "",
  xiaohongshu: {
    accountName: "",
    creatorUrl: "https://creator.xiaohongshu.com/publish/publish",
  },
  llm: {model: "", base_url: "", effort: "none", configured: false},
  translation: {model: "", base_url: "", configured: false, enabled: false},
  leaderboard: {model: "", base_url: "", configured: false, enabled: false},
  articleRequestGen: 0,
  articleContext: null,
  githubWeekly: {repos: [], xhs_title: "本周值得关注的github ai项目前10", xhs_body: ""},
  githubTheme: "dark",
  skillsPackage: {query: "", title: "", user_title: "", theme: "", stages: [], skills: [], article: {title: "", body: ""}, images: []},
  githubTokenConfigured: false,
  modelEndpoints: [],
  platforms: {wechat: {configured: false}, xiaohongshu: {account_configured: false}, x: {configured: false}},
  providerDraft: {open: false, mode: "add", endpointId: "", models: []},
  originalGen: 0,
  originalItemId: "",
  originalTranslatable: false,
  hotspotGen: 0,
  collectGen: 0,
  collectionRun: null,
  resourceSet: null,
  research: {dossier: null, eventIds: [], loading: false},
  researchRuntime: {planner_configured: false, jev_configured: false},
  sourceFilter: "all",
  sourceQuery: "",
};

function $(id) { return document.getElementById(id); }

function el(tag, className, text) {
  var node = document.createElement(tag);
  if (className) { node.className = className; }
  if (text !== undefined) { node.textContent = text; }
  return node;
}

function busy(button, on) {
  if (!button) { return; }
  if (button._busyTimer) { clearInterval(button._busyTimer); button._busyTimer = null; }
  if (button.dataset.originalLabel === undefined) {
    button.dataset.originalLabel = button.textContent;
  }
  button.disabled = on;
  if (!on) {
    button.textContent = button.dataset.originalLabel;
    delete button.dataset.originalLabel;
    return;
  }
  var started = Date.now();
  function tick() {
    var s = Math.max(0, Math.floor((Date.now() - started) / 1000));
    button.textContent = "处理中…" + (s ? " " + s + "s" : "");
  }
  tick();
  button._busyTimer = setInterval(tick, 1000);
}

function status(id, text, kind) {
  var node = $(id);
  if (!node) { return; }
  node.textContent = text;
  node.className = kind ? "status-" + kind : "";
}

var APP_PAGES = ["home", "news", "research", "resources", "creation", "publishing", "sources", "runs", "settings"];

function renderSummaryStats(target, stats) {
  if (!target) { return; }
  target.textContent = "";
  (stats || []).forEach(function (item) {
    var card = el("div", "summary-stat" + (item.tone ? " is-" + item.tone : ""));
    card.appendChild(el("strong", "", String(item.value == null ? "—" : item.value)));
    card.appendChild(el("span", "", item.label));
    if (item.title) { card.title = item.title; }
    target.appendChild(card);
  });
}

var SOURCE_TYPE_LABELS = {
  rss: "RSS", sitemap: "Sitemap", reddit: "Reddit", github: "GitHub",
  api: "API", account: "账号", html: "网页", atom: "Atom",
};
var RUN_STATUS_LABELS = {
  ok: "成功", success: "成功", error: "失败", failed: "失败",
  skipped: "跳过", running: "进行中", partial: "部分成功",
};
function sourceTypeLabel(source) {
  var raw = String((source && (source.adapter_type || source.source_type || source.type)) || "").trim();
  if (!raw) { return "未知"; }
  return SOURCE_TYPE_LABELS[raw.toLowerCase()] || raw.toUpperCase();
}
function sourceIntervalText(minutes) {
  var n = Number(minutes || 0);
  if (!n) { return "未设置间隔"; }
  if (n % 60 === 0) { return "每 " + (n / 60) + " 小时"; }
  return "每 " + n + " 分钟";
}
function sourceEnabled(source) {
  // SQLite stores enabled as 0/1, so a raw row would slip past ``=== false``.
  if (!source) { return false; }
  var raw = source.enabled;
  if (raw === undefined || raw === null || raw === "") { return true; }
  if (typeof raw === "string") { return !["0", "false", "no", "off"].includes(raw.trim().toLowerCase()); }
  return Boolean(raw);
}
function sourceStatusOf(source) {
  if (!source) { return "never"; }
  if (!sourceEnabled(source)) { return "disabled"; }
  // ``health`` is the backend's derived value (disabled > error > never >
  // stale > ok); ``status`` is the stored legacy column. Prefer the derived
  // one so the pill, the tiles and the filter cannot disagree.
  var derived = String(source.health || "").trim();
  if (derived) { return derived; }
  return String(source.status || "never");
}
function sourceMatchesFilter(source) {
  var filter = state.sourceFilter || "all";
  if (filter === "enabled" && !sourceEnabled(source)) { return false; }
  if (filter === "disabled" && sourceEnabled(source)) { return false; }
  if (filter === "error" && sourceStatusOf(source) !== "error") { return false; }
  var query = String(state.sourceQuery || "").trim().toLowerCase();
  if (!query) { return true; }
  return [source.name, source.source_id, source.url, source.source_type,
          source.type, source.adapter_type, source.topic_scope, source.language]
    .filter(Boolean).join(" ").toLowerCase().indexOf(query) >= 0;
}
function metaLine(bits) {
  var node = el("div", "row-meta");
  bits.filter(function (bit) { return bit !== "" && bit != null; }).forEach(function (bit, pos) {
    if (pos) { node.appendChild(el("span", "sep", "\u00b7")); }
    node.appendChild(el("span", "", String(bit)));
  });
  return node;
}
function runStatusOf(status) {
  var key = String(status || "").toLowerCase();
  if (key === "ok" || key === "success") { return "ok"; }
  if (key === "error" || key === "failed") { return "error"; }
  if (key === "skipped") { return "never"; }
  if (key === "partial") { return "stale"; }
  return "never";
}
function runStatusLabel(status) {
  var key = String(status || "").toLowerCase();
  return RUN_STATUS_LABELS[key] || (key ? key.toUpperCase() : "未知");
}
function diagnosticText(diagnostic) {
  if (diagnostic && typeof diagnostic === "object") {
    return String(diagnostic.detail || diagnostic.reason || diagnostic.code || "");
  }
  return String(diagnostic || "");
}

function renderSourceRegistry(sources) {
  var all = Array.isArray(sources) ? sources : [];
  state.sources = all;
  var counts = {total: all.length, enabled: 0, ok: 0, error: 0, stale: 0, never: 0, disabled: 0};
  all.forEach(function (source) {
    if (source.enabled) { counts.enabled += 1; }
    var key = sourceStatusOf(source);
    if (counts[key] != null) { counts[key] += 1; }
  });
  renderSummaryStats($("source-summary"), [
    {label: "总源", value: counts.total},
    {label: "启用", value: counts.enabled},
    {label: "正常", value: counts.ok, tone: counts.ok ? "ok" : "",
     title: "最近一次抓取成功，且未超过刷新间隔的两倍"},
    {label: "待刷新", value: counts.stale, tone: counts.stale ? "warn" : "",
     title: "距最近一次抓取已超过刷新间隔的两倍"},
    {label: "异常", value: counts.error, tone: counts.error ? "err" : "",
     title: "最近一次抓取失败，展开该行可见具体错误"},
    {label: "停用", value: counts.disabled,
     title: "已停用；启用前不会被调度"},
  ]);
  var rows = all.filter(sourceMatchesFilter);
  var counter = $("source-filter-count");
  if (counter) {
    counter.textContent = rows.length === all.length
      ? (all.length + " 路订阅")
      : (rows.length + " / " + all.length + " 路订阅");
  }
  var list = $("source-registry-list");
  if (!list) { return; }
  list.textContent = "";
  if (!all.length) {
    list.appendChild(el("li", "run-empty", "暂无信息源"));
    return;
  }
  if (!rows.length) {
    list.appendChild(el("li", "run-empty", "当前筛选没有匹配的信息源"));
    return;
  }
  rows.forEach(function (source) {
    var statusKey = sourceStatusOf(source);
    var row = el("li", "registry-row");
    if (!sourceEnabled(source)) { row.classList.add("is-off"); }
    if (statusKey === "error") { row.classList.add("is-bad"); }

    var main = el("div", "row-main");
    var title = el("div", "row-title");
    var name = el("span", "row-name", source.name || source.source_id || "未命名信息源");
    name.title = source.url || source.name || source.source_id || "";
    title.appendChild(name);
    var tier = String(source.tier || "").trim();
    if (tier) { title.appendChild(el("span", "chip chip-tier", tier.toUpperCase())); }
    title.appendChild(el("span", "chip chip-type", sourceTypeLabel(source)));
    if (source.topic_scope) { title.appendChild(el("span", "chip", String(source.topic_scope))); }
    if (source.language) { title.appendChild(el("span", "chip", String(source.language))); }
    main.appendChild(title);

    var bits = [sourceIntervalText(source.fetch_interval_minutes)];
    if (source.last_success_at) { bits.push("上次成功 " + formatTime(source.last_success_at)); }
    else if (source.last_fetched_at) { bits.push("上次抓取 " + formatTime(source.last_fetched_at)); }
    else { bits.push("还没抓过"); }
    var failures = Number(source.consecutive_failures || 0);
    if (failures > 0) { bits.push("连续失败 " + failures + " 次"); }
    main.appendChild(metaLine(bits));

    if (statusKey === "error" && source.last_error) {
      // Readable reason on the row; the raw diagnostic stays in the tooltip.
      var detail = el("div", "row-detail is-err",
        "抓取失败：" + (sourceFailureText(source.last_error) || "原因不明"));
      detail.title = String(source.last_error);
      main.appendChild(detail);
    } else if (source.url) {
      var urlLine = el("div", "row-detail", String(source.url));
      urlLine.title = String(source.url);
      main.appendChild(urlLine);
    }
    row.appendChild(main);
    row.appendChild(el("span", "status-pill " + statusKey, sourceStatusLabel(statusKey)));

    var controls = el("div", "source-controls");
    var enabledLabel = document.createElement("label");
    enabledLabel.className = "source-toggle";
    var enabled = document.createElement("input");
    enabled.type = "checkbox";
    enabled.checked = sourceEnabled(source);
    enabled.setAttribute("aria-label", "启用 " + (source.name || source.source_id));
    enabled.addEventListener("change", function () {
      var next = enabled.checked;
      enabled.disabled = true;
      api("/api/sources/enabled", {source_id: source.source_id, enabled: next}).then(function (data) {
        renderSourceRegistry(data.sources || []);
        status("source-status", next ? "信息源已启用" : "信息源已停用", "ok");
      }).catch(function (error) {
        enabled.checked = !next;
        enabled.disabled = false;
        status("source-status", "保存信息源状态失败：" + error.message, "error");
      });
    });
    enabledLabel.appendChild(enabled);
    enabledLabel.appendChild(document.createTextNode("启用"));
    controls.appendChild(enabledLabel);
    var interval = document.createElement("input");
    interval.type = "number";
    interval.min = "1";
    interval.step = "1";
    interval.value = String(source.fetch_interval_minutes || 15);
    interval.title = "抓取间隔（分钟）";
    interval.setAttribute("aria-label", (source.name || source.source_id) + " 抓取间隔（分钟）");
    interval.addEventListener("change", function () {
      var minutes = Number(interval.value);
      if (!Number.isInteger(minutes) || minutes < 1) {
        interval.value = String(source.fetch_interval_minutes || 15);
        status("source-status", "抓取间隔必须是正整数分钟", "error");
        return;
      }
      interval.disabled = true;
      api("/api/sources/fetch-interval", {source_id: source.source_id, minutes: minutes}).then(function (data) {
        renderSourceRegistry(data.sources || []);
        status("source-status", "抓取间隔已保存", "ok");
      }).catch(function (error) {
        interval.disabled = false;
        status("source-status", "保存抓取间隔失败：" + error.message, "error");
      });
    });
    controls.appendChild(interval);
    var action = document.createElement("button");
    action.type = "button";
    action.textContent = source.enabled ? "只刷这个" : "已停用";
    action.disabled = !source.enabled;
    action.title = "只重新抓取这一路订阅，并跳到资讯页看结果";
    action.addEventListener("click", function () {
      var scope = String(source.adapter_type || source.type || "") === "reddit" ? "community" : "official";
      setAppPage("news");
      setNewsScope(scope);
      collect(source.source_id);
    });
    controls.appendChild(action);
    row.appendChild(controls);
    list.appendChild(row);
  });
}

function renderCollectionRun(payload) {
  var run = (payload && (payload.collection_run || payload.run)) || payload || {};
  if (!run || typeof run !== "object") { run = {}; }
  state.collectionRun = run;
  var requested = Array.isArray(run.requested_sources) ? run.requested_sources.length : Number(run.requested_sources || run.requested || 0);
  var attempted = Array.isArray(run.attempted_sources) ? run.attempted_sources.length : Number(run.attempted_sources || 0);
  var successful = Array.isArray(run.successful_sources) ? run.successful_sources.length : Number(run.successful_sources || 0);
  var failed = Array.isArray(run.failed_sources) ? run.failed_sources.length : Number(run.failed_sources || run.failed || 0);
  var fetched = Number(run.items_fetched || run.fetched || 0);
  var inserted = Number(run.items_inserted || run.inserted || 0);
  var deduped = Number(run.items_after_dedupe || run.deduped || 0);
  var clustered = Number(run.clusters_created_or_updated || run.clusters || 0);
  var homeSummary = $("home-recent-summary");
  if (homeSummary) {
    // The home card reports the latest run even when it happened in an earlier
    // session: a loading placeholder that never resolves is worse than a line.
    homeSummary.textContent = run.run_id
      ? ("最近一次采集：" + (failed ? ("有 " + failed + " 个源失败") : "全部成功")
         + " · 新增 " + inserted + " 条" + (clustered ? (" · 聚类 " + clustered + " 个事件") : "")
         + (run.started_at ? (" · " + formatTime(run.started_at)) : ""))
      : "还没有采集运行记录，去「资讯探索」刷新一次来源。";
  }
  renderSummaryStats($("run-summary"), [
    {label: "尝试源", value: requested || attempted},
    {label: "成功", value: successful, tone: successful ? "ok" : ""},
    {label: "失败", value: failed, tone: failed ? "err" : ""},
    {label: "获得内容", value: fetched},
    {label: "新增 / 去重后", value: (inserted || 0) + " / " + (deduped || 0)},
  ]);
  var list = $("collection-run-sources");
  if (!list) { return; }
  list.textContent = "";
  var attempts = Array.isArray(run.source_attempts) ? run.source_attempts : [];
  if (!attempts.length) {
    list.appendChild(el("li", "run-empty", run.run_id ? "本次运行已记录，暂无源级明细" : "还没有采集运行记录"));
    return;
  }
  attempts.forEach(function (attempt) {
    if (!attempt || typeof attempt !== "object") { return; }
    var tone = runStatusOf(attempt.status);
    var row = el("li", "run-source-row");
    if (tone === "ok") { row.classList.add("is-ok"); }
    else if (tone === "error") { row.classList.add("is-bad"); }
    else if (tone === "never") { row.classList.add("is-skipped"); }

    var sourceName = attempt.name || attempt.source_id || "未命名信息源";
    (state.sources || []).some(function (source) {
      if (source && source.source_id === attempt.source_id) {
        sourceName = source.name || sourceName;
        return true;
      }
      return false;
    });
    var main = el("div", "row-main");
    var title = el("div", "row-title");
    var nameNode = el("span", "row-name", sourceName);
    nameNode.title = sourceName;
    title.appendChild(nameNode);
    if (attempt.adapter_type || attempt.source_type) {
      title.appendChild(el("span", "chip chip-type", sourceTypeLabel(attempt)));
    }
    main.appendChild(title);

    var fetched = Number(attempt.items_fetched == null ? 0 : attempt.items_fetched);
    var inserted = Number(attempt.items_inserted || 0);
    var skipped = Math.max(fetched - inserted, 0);
    main.appendChild(metaLine([
      fetched + " 条获得",
      inserted + " 条新增",
      skipped ? (skipped + " 条已存在") : "",
      attempt.duration_ms ? (Math.round(Number(attempt.duration_ms) / 100) / 10 + "s") : "",
    ]));
    var diagnostic = diagnosticText(attempt.diagnostic);
    var detail = diagnostic || attempt.error || "";
    if (detail) {
      var detailNode = el("div", "row-detail" + (tone === "error" ? " is-err" : ""), detail);
      detailNode.title = detail;
      main.appendChild(detailNode);
    }
    row.appendChild(main);
    row.appendChild(el("span", "status-pill " + tone, runStatusLabel(attempt.status)));

    if (fetched > 0) {
      var bar = el("div", "outcome-bar");
      var pct = function (n) { return Math.round((n / fetched) * 100) + "%"; };
      var newPart = el("i", "o-new");
      newPart.style.width = pct(Math.min(inserted, fetched));
      bar.appendChild(newPart);
      var dupePart = el("i", "o-dupe");
      dupePart.style.width = pct(skipped);
      bar.appendChild(dupePart);
      bar.title = "获得 " + fetched + " 条 · 新增 " + inserted + " 条 · 已存在 " + skipped + " 条";
      row.appendChild(bar);
    } else {
      row.appendChild(el("span", "chip", "0 条"));
    }
    list.appendChild(row);
  });
}

function renderCollectionRuns(payload) {
  var runs = Array.isArray(payload) ? payload : (payload && Array.isArray(payload.runs) ? payload.runs : []);
  var list = $("collection-run-sources");
  if (!list) { return; }
  list.textContent = "";
  if (!runs.length) {
    renderCollectionRun(state.collectionRun || {});
    return;
  }
  var latest = runs[0] || {};
  var homeSummary = $("home-recent-summary");
  if (homeSummary) {
    // Loading the page must not leave the home card on its placeholder: the
    // latest persisted run is the answer, whether or not it ran in this tab.
    var failedCount = Array.isArray(latest.failed_sources) ? latest.failed_sources.length : 0;
    homeSummary.textContent = "最近一次采集：" + (failedCount ? ("有 " + failedCount + " 个源失败") : "全部成功")
      + " · 新增 " + Number(latest.items_inserted || 0) + " 条"
      + (latest.started_at ? (" · " + formatTime(latest.started_at)) : "");
  }
  var latestFailed = Array.isArray(latest.failed_sources) ? latest.failed_sources.length : 0;
  renderSummaryStats($("run-summary"), [
    {label: "运行记录", value: runs.length},
    {label: "最近状态", value: runStatusLabel(latest.status), tone: latestFailed ? "warn" : "ok"},
    {label: "成功源", value: Array.isArray(latest.successful_sources) ? latest.successful_sources.length : 0},
    {label: "失败源", value: latestFailed, tone: latestFailed ? "err" : ""},
    {label: "去重后", value: Number(latest.items_after_dedupe || 0)},
  ]);
  runs.forEach(function (run) {
    if (!run || typeof run !== "object") { return; }
    var failedCount = Array.isArray(run.failed_sources) ? run.failed_sources.length : Number(run.failed || 0);
    var row = el("li", "run-source-row" + (failedCount ? " is-bad" : " is-ok"));
    row.tabIndex = 0;
    row.setAttribute("role", "button");
    row.setAttribute("aria-label", "查看运行 " + String(run.run_id || "") + " 的源级明细");

    var main = el("div", "row-main");
    var title = el("div", "row-title");
    title.appendChild(el("span", "row-name run-card-id", String(run.run_id || "未命名运行")));
    title.appendChild(el("span", "chip " + (failedCount ? "chip-media" : "chip-community"), failedCount ? (failedCount + " 个源失败") : "全部成功"));
    main.appendChild(title);
    var clustered = Number(run.clusters_created_or_updated || 0);
    main.appendChild(metaLine([
      run.started_at ? formatTime(run.started_at) : "",
      (Array.isArray(run.requested_sources) ? run.requested_sources.length : Number(run.requested_sources || 0)) + " 路订阅",
      "新增 " + Number(run.items_inserted || 0) + " 条",
      clustered ? ("聚类 " + clustered + " 个事件") : "",
    ]));
    row.appendChild(main);
    row.appendChild(el("span", "status-pill " + (failedCount ? "error" : "ok"), runStatusLabel(run.status)));

    var open = function () {
      api("/api/collection-runs/" + encodeURIComponent(run.run_id), null, {method: "GET"}).then(function (data) {
        renderCollectionRun(data.run || data);
      }).catch(function (error) {
        status("source-status", "读取运行记录失败：" + error.message, "error");
      });
    };
    row.addEventListener("click", open);
    row.addEventListener("keydown", function (event) { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(); } });
    var openBtn = el("button", "btn-ghost btn-sm", "查看明细");
    openBtn.type = "button";
    openBtn.addEventListener("click", function (event) { event.stopPropagation(); open(); });
    row.appendChild(openBtn);
    list.appendChild(row);
  });
}

function loadSourceRegistry() {
  return api("/api/sources", null, {method: "GET"}).then(function (data) {
    renderSourceRegistry(data.sources || []);
    return data;
  });
}

function loadCollectionRuns() {
  return api("/api/collection-runs?limit=20", null, {method: "GET"}).then(function (data) {
    renderCollectionRuns(data);
    return data;
  });
}

var PUBLISHING_STATUS_LABELS = {
  DRAFTED: "待审批",
  APPROVED: "已审批",
  REJECTED: "已拒绝",
  PUBLISHED: "已发布",
  FAILED: "失败",
};
function publishingStatusLabel(status) {
  return PUBLISHING_STATUS_LABELS[String(status || "")] || String(status || "未知");
}
function publishingPlatformLabel(platform) {
  return String(platform || "") === "xiaohongshu" ? "小红书" : "微信";
}
function renderPublishingDrafts(drafts) {
  var list = $("publishing-draft-list");
  if (!list) { return; }
  list.textContent = "";
  if (!drafts.length) {
    list.appendChild(el("article", "ia-card", "还没有草稿。从资讯或资源进入创作后，草稿会出现在这里。"));
  }
  drafts.forEach(function (draft) {
    if (!draft || !draft.draft_id) { return; }
    var card = el("article", "ia-card publishing-draft");
    card.dataset.draftId = String(draft.draft_id);
    card.appendChild(el("h3", "", draft.title || "未命名草稿"));
    card.appendChild(el("p", "meta", publishingPlatformLabel(draft.platform) + " · " + publishingStatusLabel(draft.status) + " · 更新 " + formatTime(draft.updated_at || "")));
    if (draft.approved_at) {
      card.appendChild(el("p", "meta", "审批：" + (draft.approved_by || "—") + " · " + formatTime(draft.approved_at)));
    }
    if (draft.attempt_status) {
      card.appendChild(el("p", "meta", "最近发布：" + String(draft.attempt_status) + " · " + formatTime(draft.attempt_at || "")));
    }
    var actions = el("div", "publishing-draft-actions");
    var editBtn = el("button", "", "继续编辑");
    editBtn.type = "button";
    editBtn.addEventListener("click", function () { openDraftForPublishing(draft.draft_id, "edit"); });
    actions.appendChild(editBtn);
    var publishBtn = el("button", "primary", publishingPlatformLabel(draft.platform) === "微信" ? "到草稿箱" : "生成发布包");
    publishBtn.type = "button";
    publishBtn.addEventListener("click", function () { openDraftForPublishing(draft.draft_id, "publish"); });
    actions.appendChild(publishBtn);
    var scheduleBtn = el("button", "", "定时到草稿");
    scheduleBtn.type = "button";
    scheduleBtn.addEventListener("click", function () { openDraftForPublishing(draft.draft_id, "schedule"); });
    actions.appendChild(scheduleBtn);
    card.appendChild(actions);
    list.appendChild(card);
  });
  var summary = $("publishing-summary");
  if (summary) {
    var pending = drafts.filter(function (item) { return String(item.status || "") === "DRAFTED"; }).length;
    summary.textContent = "共 " + drafts.length + " 份草稿 · " + pending + " 份待审批";
  }
}
function loadPublishingDrafts() {
  return api("/api/drafts?limit=20", null, {method: "GET"}).then(function (data) {
    renderPublishingDrafts(Array.isArray(data.drafts) ? data.drafts : []);
    return data;
  });
}
function openDraftForPublishing(draftId, action) {
  if (!draftId) { return Promise.resolve(); }
  return api("/api/drafts/" + encodeURIComponent(draftId), null, {method: "GET"}).then(function (data) {
    var draft = data.draft || data;
    if (!draft || !draft.draft_id) { throw new Error("草稿数据不完整"); }
    state.draft = draft;
    state.item = null;
    state.creatorUrl = "";
    state.articleDirty = false;
    setPlatform(String(draft.platform || "wechat") === "xiaohongshu" ? "xiaohongshu" : "wechat");
    setAppPage("creation");
    renderArticle(draft);
    $("publish-btn").disabled = false;
    $("schedule-btn").disabled = false;
    if (action === "publish") {
      publish();
    } else if (action === "schedule") {
      status("publish-status", "请选择定时时间后点「定时到草稿」", "");
      if ($("schedule-at")) { $("schedule-at").focus(); }
    } else {
      status("publish-status", "已载入草稿：" + (draft.title || draft.draft_id), "ok");
    }
    return draft;
  }).catch(function (error) {
    status("publishing-summary", "打开草稿失败：" + error.message, "error");
  });
}

function selectedResearchEventIds() {
  var ids = [];
  pickedIdList().forEach(function (itemId) {
    var event = state.radarEvents[itemId];
    var eventId = event && event.event_id ? String(event.event_id) : "";
    if (!eventId) {
      var candidates = ((state.rankings || {}).heat || []).concat((state.rankings || {}).time || []);
      candidates.some(function (item) {
        if (item && String(item.item_id || "") === String(itemId) && item.event_id) {
          eventId = String(item.event_id);
          return true;
        }
        return false;
      });
    }
    if (eventId && ids.indexOf(eventId) < 0) { ids.push(eventId); }
  });
  return ids;
}

function syncResearchControls() {
  var ids = selectedResearchEventIds();
  state.research.eventIds = ids;
  var enabled = ids.length > 0 && !state.research.loading;
  var button = $("research-btn");
  var runButton = $("research-run-btn");
  var toCreation = $("research-to-creation-btn");
  if (button) { button.disabled = !enabled; }
  if (runButton) { runButton.disabled = !enabled; }
  // 只有拿到 dossier 才能进创作：这是「Topic Dossier → 文章」的唯一入口。
  if (toCreation) { toCreation.disabled = !state.research.dossier; }
}

function articleContextFromDossier(dossier) {
  if (!dossier || typeof dossier !== "object") { return null; }
  var dossierId = String(dossier.dossier_id || "").trim();
  if (!dossierId) { return null; }
  var evidence = Array.isArray(dossier.evidence) ? dossier.evidence : [];
  return {
    source_type: "topic_dossier",
    dossier_id: dossierId,
    title: String(dossier.title || dossier.query || "未命名研究"),
    event_count: Array.isArray(dossier.event_ids) ? dossier.event_ids.length : 0,
    evidence_count: evidence.length,
    status: String(dossier.status || ""),
    degraded: !!dossier.degraded,
  };
}

function renderArticleContext() {
  var box = $("article-context");
  if (!box) { return; }
  var context = state.articleContext;
  if (!context) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  $("article-context-title").textContent = context.title || "未命名研究";
  var meta = [
    context.source_type === "topic_dossier" ? "Topic Dossier" : "手动选材",
    context.dossier_id || "",
    context.event_count ? context.event_count + " 个事件" : "",
    context.evidence_count ? context.evidence_count + " 条证据" : "",
    context.status || "",
    context.degraded ? "部分能力降级" : "",
  ].filter(Boolean);
  $("article-context-meta").textContent = meta.join(" · ");
  syncGenerateEnabled();
}

function setArticleContextFromDossier(dossier) {
  var context = articleContextFromDossier(dossier);
  if (!context) {
    status("research-status", "这份研究还没有 dossier_id，不能作为创作输入", "error");
    return false;
  }
  state.articleContext = context;
  renderArticleContext();
  setAppPage("creation");
  status("article-status", "已带入研究：" + context.title + "。直接点「生成文章」即可。", "ok");
  return true;
}

function clearArticleContext() {
  state.articleContext = null;
  renderArticleContext();
  status("article-status", "已清除研究上下文，回到手动选材。", "");
}

var READ_CAUSE_LABELS = {
  blocked_http: "站点拒绝访问",
  auth_required: "需要登录",
  rate_limited: "被限流",
  forbidden: "被拒绝",
  http_error: "HTTP 错误",
  timeout: "读取超时",
  connection_reset: "连接中断",
  dns_failure: "域名解析失败",
  tls_failure: "证书校验失败",
  too_large: "内容过大",
  unsupported_content: "格式不支持",
  invalid_content: "内容无法解析",
  invalid_response: "响应格式异常",
  provider_error: "模型侧错误",
  reader_internal_error: "读取器内部错误",
  fetch_failed: "抓取失败",
  not_configured: "未配置",
  unreachable: "连接不上",
  low_confidence: "置信度不足",
  auth_failed: "鉴权失败",
  credential_required: "缺少凭据",
  unsupported_source: "来源类型不支持",
  unsupported_adapter: "适配器不支持",
  storage_error: "存储错误",
  server_error: "服务端错误",
  forbidden: "被拒绝",
  unknown_error: "原因不明",
  bad_json: "请求格式错误",
  invalid_json: "响应格式错误",
  not_found: "找不到资源",
  collector_reported_failure: "采集器报告失败",
};
function readCauseLabel(cause) {
  var key = String(cause || "").trim();
  if (!key) { return ""; }
  return READ_CAUSE_LABELS[key] || key;
}
var UNKNOWN_CAUSE_LABELS = { unknown_error: "原因不明", source_fetch_failed: "抓取失败" };

function parseDiagnosticString(raw) {
  // Stored diagnostics are ``code=..;cause_code=..;stage=..;detail=..``.
  var out = {};
  String(raw || "").split(";").forEach(function (part) {
    var at = part.indexOf("=");
    if (at > 0) { out[part.slice(0, at).trim()] = part.slice(at + 1).trim(); }
  });
  return out;
}

function sourceFailureText(raw) {
  var text = String(raw || "").trim();
  if (!text) { return ""; }
  if (text.indexOf("=") < 0) { return text; }
  var fields = parseDiagnosticString(text);
  var cause = fields.cause_code || "";
  var label = READ_CAUSE_LABELS[cause] || UNKNOWN_CAUSE_LABELS[cause] || cause;
  var detail = fields.detail || "";
  // ``unknown_error`` alone says nothing, so prefer the upstream message.
  if (detail && (cause === "unknown_error" || !cause)) { return detail; }
  return detail ? (label + "：" + detail) : (label || text);
}
var FALLBACK_SOURCE_LABELS = { feed: "已采集正文", rss: "已采集正文", collected: "已采集正文" };

function diagnosticLabel(diagnostic) {
  if (!diagnostic || typeof diagnostic !== "object") { return ""; }
  return [diagnostic.source, diagnostic.stage, diagnostic.code, diagnostic.cause_code]
    .filter(function (value) { return value !== undefined && value !== null && String(value); })
    .map(function (value) { return String(value); }).join(" · ");
}

function renderResearchDossier(dossier) {
  state.research.dossier = dossier || null;
  var summary = $("research-summary");
  var diagnostics = $("research-diagnostics");
  var evidenceList = $("research-evidence-list");
  if (!summary || !diagnostics || !evidenceList) { return; }
  summary.textContent = "";
  diagnostics.textContent = "";
  evidenceList.textContent = "";
  if (!dossier || typeof dossier !== "object") {
    summary.appendChild(el("article", "ia-card", "尚未生成 Topic Dossier"));
    syncResearchControls();
    return;
  }
  var topics = Array.isArray(dossier.topics) ? dossier.topics.length : 0;
  var evidence = Array.isArray(dossier.evidence) ? dossier.evidence : [];
  renderSummaryStats(summary, [
    {label: "状态", value: dossier.status || "—"},
    {label: "事件", value: Array.isArray(dossier.event_ids) ? dossier.event_ids.length : 0},
    {label: "主题", value: topics},
    {label: "证据", value: evidence.length},
    {label: "降级", value: dossier.degraded ? "是" : "否"},
  ]);
  var allDiagnostics = Array.isArray(dossier.diagnostics) ? dossier.diagnostics.slice() : [];
  evidence.forEach(function (item) {
    (item && item.assessment && Array.isArray(item.assessment.diagnostics) ? item.assessment.diagnostics : []).forEach(function (itemDiagnostic) {
      allDiagnostics.push(itemDiagnostic);
    });
  });
  var seenDiagnostics = {};
  allDiagnostics.forEach(function (item) {
    var label = diagnosticLabel(item);
    if (!label || seenDiagnostics[label]) { return; }
    seenDiagnostics[label] = true;
    diagnostics.appendChild(el("p", "page-description research-diagnostic", "诊断：" + label));
  });
  if (!evidence.length) {
    evidenceList.appendChild(el("li", "run-empty", "没有可展示的证据；请查看上方诊断。"));
    syncResearchControls();
    return;
  }
  evidence.forEach(function (item) {
    if (!item || typeof item !== "object") { return; }
    var row = el("li", "research-evidence");
    var heading = el("h3", "", item.title || item.url || "未命名证据");
    var url = String(item.url || "");
    if (url) {
      heading.textContent = "";
      var link = document.createElement("a");
      link.href = url;
      link.target = "_blank";
      link.rel = "noreferrer noopener";
      link.textContent = item.title || url;
      heading.appendChild(link);
    }
    row.appendChild(heading);
    var readMethod = String(item.read_method || "").trim();
    var metaBits = [item.relation || "topic_only"];
    metaBits.push(readMethod && readMethod !== "none" ? readMethod : (item.reader_status || "unread"));
    if (item.assessment && item.assessment.decision) { metaBits.push(item.assessment.decision); }
    row.appendChild(el("p", "research-evidence-meta", metaBits.join(" · ")));
    if (item.degraded) {
      row.classList.add("is-degraded");
      var why = ["降级读取"];
      if (item.cause_code) { why.push(readCauseLabel(item.cause_code) + "（" + item.cause_code + "）"); }
      var source = String(item.fallback_source || "").trim();
      if (source) { why.push((FALLBACK_SOURCE_LABELS[source] || source) + " 作为正文"); }
      if (item.blocked_kind) { why.push("拦截类型 " + item.blocked_kind); }
      var notice = el("p", "research-evidence-degraded", why.join(" · "));
      notice.title = "页面正文不可用，已回退到本工作台已采集的内容；证据未丢失，但完整度低于直接读取。";
      row.appendChild(notice);
    }
    if (item.snippet) { row.appendChild(el("p", "", String(item.snippet).slice(0, 500))); }
    evidenceList.appendChild(row);
  });
  syncResearchControls();
}

function runResearch() {
  var ids = selectedResearchEventIds();
  if (!ids.length || state.research.loading) {
    status("hotspot-status", "请先在资讯页选择至少一个事件", "error");
    return;
  }
  var queryNode = $("research-query");
  var query = queryNode ? String(queryNode.value || "").trim() : "";
  state.research.eventIds = ids;
  state.research.loading = true;
  syncResearchControls();
  setAppPage("research");
  status("research-status", "正在生成 Topic Dossier…");
  api("/api/research", {event_ids: ids, query: query || undefined}).then(function (data) {
    renderResearchDossier(data.dossier || data);
    var dossier = data.dossier || {};
    status("research-status", "研究完成 · " + (dossier.status || "已返回") + (dossier.degraded ? " · 部分能力降级" : ""), dossier.degraded ? "error" : "ok");
  }).catch(function (error) {
    status("research-status", "研究失败：" + error.message, "error");
  }).then(function () {
    state.research.loading = false;
    syncResearchControls();
  });
}

function setAppPage(page) {
  if (APP_PAGES.indexOf(page) < 0) { return; }
  var previousPage = state.page;
  state.page = page;
  document.querySelectorAll("[data-app-page]").forEach(function (panel) {
    panel.hidden = panel.dataset.appPage !== page;
  });
  document.querySelectorAll("[data-app-page-target]").forEach(function (button) {
    var active = button.dataset.appPageTarget === page;
    button.setAttribute("aria-current", active ? "page" : "false");
    button.classList.toggle("active", active);
  });
  if (page === "sources") { loadSourceRegistry().catch(function (error) { status("source-status", "读取信息源失败：" + error.message, "error"); }); }
  if (page === "runs") { loadCollectionRuns().catch(function (error) { status("source-status", "读取运行记录失败：" + error.message, "error"); }); }
  if (page === "publishing") { loadPublishingDrafts().catch(function (error) { status("publishing-summary", "读取草稿失败：" + error.message, "error"); }); }
  if (page === "creation") {
    // Restore the retained poster studios from their persisted snapshots once.
    if (!state.githubStudioLoaded) {
      state.githubStudioLoaded = true;
      loadGithubWeekly();
    }
    if (!state.skillsStudioLoaded) {
      state.skillsStudioLoaded = true;
      loadSkills();
    }
  }
}

var WAIT_HINTS = {
  collect: {title: "正在抓取订阅源", hint: "通常 10–30 秒，源越多越久"},
  collect_one: {title: "正在刷新这一路订阅", hint: "通常几秒到十几秒"},
  viewpoints: {title: "正在抓取 Reddit 热帖", hint: "通常 10–30 秒"},
  translate: {title: "正在翻译标题", hint: "通常 5–20 秒，看条数和模型"},
  github_fetch: {title: "正在抓取 GitHub 周榜", hint: "通常 5–20 秒；配了榜单模型会再改写简介"},
  github_render: {title: "正在生成周榜图片", hint: "每张大约 8–25 秒；11 张大约 2–5 分钟", unit: "张"},
  generate: {title: "正在生成文章", hint: "通常 15–90 秒，看模型和篇幅"},
  original: {title: "正在读取原文", hint: "通常几秒"},
  original_tr: {title: "正在翻译原文", hint: "通常 10–40 秒"},
  models: {title: "正在获取可用模型", hint: "通常几秒到十几秒"},
  publish: {title: "正在写入草稿", hint: "通常 5–20 秒", unit: "步"},
  schedule: {title: "正在预约发布", hint: "通常几秒"},
  rescan: {title: "正在扫描配图", hint: "通常几秒到十几秒"},
  export: {title: "正在导出图片", hint: "按张下载，通常几秒"},
  save: {title: "正在保存", hint: "通常一两秒"},
  assign: {title: "正在保存模型配置", hint: "通常一两秒"},
  skill_search: {title: "正在查找 Skill", hint: "通常 20–60 秒；超时会先交已找到的结果"},
  skill_paste: {title: "正在读取粘贴的 Skill", hint: "每个链接抓一次 SKILL.md，通常几秒", unit: "个"},
  skill_render: {title: "正在生成技能包图片", hint: "封面大字报加项目卡，大约半分钟到几分钟", unit: "张"}
};
var waitJobs = {};
var waitState = {timer: null};

function formatElapsed(ms) {
  var s = Math.max(0, Math.floor(ms / 1000));
  var m = Math.floor(s / 60);
  s = s % 60;
  return m ? (m + " 分 " + s + " 秒") : (s + " 秒");
}

function abortController() {
  if (typeof AbortController === "function") { return new AbortController(); }
  return {signal: undefined, abort: function () {}};
}

function waitRunning(kind) {
  return !!(waitJobs[kind] && waitJobs[kind].status === "running");
}

function waitCancelled(kind) {
  return !!(waitJobs[kind] && waitJobs[kind].cancelled);
}

function waitSignal(kind) {
  var job = waitJobs[kind];
  return job && job.abort ? job.abort.signal : undefined;
}

function isWaitAbort(error, kind) {
  if (waitCancelled(kind)) { return true; }
  return !!(error && (error.name === "AbortError" || error.code === 20));
}

function resolveWaitJob(kind) {
  if (kind && waitJobs[kind]) { return waitJobs[kind]; }
  var running = [];
  Object.keys(waitJobs).forEach(function (key) {
    if (waitJobs[key].status === "running") { running.push(waitJobs[key]); }
  });
  return running.length === 1 ? running[0] : null;
}

function waitStart(kind, opts) {
  opts = opts || {};
  var spec = WAIT_HINTS[kind] || {title: "处理中", hint: "请稍候"};
  if (waitRunning(kind)) { return waitJobs[kind]; }
  waitJobs[kind] = {
    kind: kind,
    status: "running",
    started: Date.now(),
    current: opts.current || 0,
    total: opts.total || 0,
    step: opts.step || "",
    unit: opts.unit || spec.unit || "步",
    title: opts.title || spec.title,
    hint: spec.hint,
    cancelled: false,
    abort: abortController()
  };
  paintWait();
  if (!waitState.timer) { waitState.timer = setInterval(paintWait, 250); }
  return waitJobs[kind];
}

function waitProgress(current, total, step, kind) {
  var job = resolveWaitJob(kind);
  if (!job || job.status !== "running") { return; }
  job.current = current;
  if (total) { job.total = total; }
  job.step = step || "";
  paintWait();
}

function waitDone(message, kind) {
  var job = resolveWaitJob(kind);
  if (!job) { return; }
  job.status = "done";
  job.title = message || "全部完成";
  job.finished = Date.now();
  paintWait();
  scheduleWaitHide(job.kind);
}

function waitFail(message, kind) {
  var job = resolveWaitJob(kind);
  if (!job) { return; }
  job.status = "error";
  job.title = job.cancelled ? "已停止" : "没有完成";
  job.hint = message || "请求失败";
  job.finished = Date.now();
  paintWait();
  scheduleWaitHide(job.kind);
}

function waitStop(kind) {
  var job = waitJobs[kind];
  if (!job || job.status !== "running") { return; }
  job.cancelled = true;
  try { job.abort.abort(); } catch (err) {}
  waitFail("已停止", kind);
}

function scheduleWaitHide(kind) {
  window.setTimeout(function () {
    var job = waitJobs[kind];
    if (!job || job.status === "running") { return; }
    delete waitJobs[kind];
    paintWait();
  }, 2400);
}

function paintWait() {
  var banner = $("wait-banner");
  var dock = $("wait-jobs");
  if (!banner || !dock) { return; }
  var kinds = Object.keys(waitJobs);
  if (!kinds.length) {
    banner.hidden = true;
    if (waitState.timer) { clearInterval(waitState.timer); waitState.timer = null; }
    return;
  }
  banner.hidden = false;
  kinds.forEach(function (kind) {
    var job = waitJobs[kind];
    var card = dock.querySelector('[data-kind="' + kind + '"]');
    if (!card) {
      card = el("article", "wait-job");
      card.setAttribute("data-kind", kind);
      card.innerHTML = '<div class="wait-spinner" aria-hidden="true"></div>'
        + '<div class="wait-copy"><p class="wait-title"></p><p class="wait-detail"></p></div>'
        + '<p class="wait-time"></p>'
        + '<button type="button" class="wait-stop">停止</button>';
      card.querySelector(".wait-stop").addEventListener("click", function () { waitStop(kind); });
      dock.appendChild(card);
    }
    card.className = "wait-job" + (job.status === "done" ? " is-done" : "") + (job.status === "error" ? " is-error" : "");
    card.querySelector(".wait-title").textContent = job.title;
    var detail = job.hint;
    if (job.status === "running" && kind === "skill_search") {
      var progress = (state.skillsPackage && state.skillsPackage.progress) || {};
      if (progress.step_index || progress.label) {
        detail = "第 " + (progress.step_index || 1) + "/" + (progress.step_total || 7) + " 步 · " + (progress.label || "查找中") + " · " + (progress.detail || job.hint);
      } else {
        detail = "查找中";
      }
    } else if (job.status === "running" && job.total) {
      var current = Math.min(job.current, job.total);
      detail = "第 " + current + " / " + job.total + " " + (job.unit || "步") + (job.step ? " · " + job.step : "") + " · " + job.hint;
    } else if (job.status === "done") {
      detail = "共用 " + formatElapsed((job.finished || Date.now()) - job.started);
    }
    card.querySelector(".wait-detail").textContent = detail;
    card.querySelector(".wait-time").textContent = job.status === "done" ? "" : ("已用 " + formatElapsed(Date.now() - job.started));
    card.querySelector(".wait-stop").hidden = job.status !== "running";
  });
  Array.prototype.slice.call(dock.children).forEach(function (card) {
    if (!waitJobs[card.getAttribute("data-kind")]) { card.remove(); }
  });
}

function formatTime(iso) {
  var date = new Date(iso);
  if (isNaN(date.getTime())) { return iso || ""; }
  return date.toLocaleString();
}

function api(path, payload, opts) {
  opts = opts || {};
  var method = (opts.method || "POST").toUpperCase();
  var init = {
    method: method,
    headers: {},
    signal: opts.signal
  };
  if (method !== "GET") {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(payload || {});
  }
  return fetch(path, init).then(function (response) {
    return response.json().catch(function () { return {}; }).then(function (data) {
      if (!response.ok || data.ok === false) {
        var error = new Error(data.error_reason || data.error || ("HTTP " + response.status));
        error.apiError = true;
        error.code = data.error_code || data.error || ("HTTP " + response.status);
        error.reason = data.error_reason || data.error || ("HTTP " + response.status);
        throw error;
      }
      return data;
    });
  }).catch(function (error) {
    if (error && error.name === "AbortError") { throw error; }
    if (error instanceof TypeError && !error.apiError) { throw new Error("网络错误"); }
    throw error;
  });
}

function waitApi(kind, path, payload) {
  return api(path, payload, {signal: waitSignal(kind)});
}

function sourceStatusLabel(status) {
  return {
    ok: "抓取成功",
    error: "抓取失败",
    stale: "该刷新了",
    never: "还没抓过",
    disabled: "已停用"
  }[status] || status;
}

function renderSourceHealth(sources) {
  sources = sources || [];
  // Several endpoints carry a source list and they arrive in an unpredictable
  // order.  The legacy ``sources`` shape is the collectable-subscription subset
  // (it has no ``health``) and would hide community sources and every failing
  // row, so once the DB-backed registry has been seen it stays authoritative.
  var hasHealth = sources.some(function (source) { return source && ("health" in source); });
  if (!hasHealth && state.sourceHealthFromRegistry) { return; }
  state.sourceHealthFromRegistry = hasHealth;
  state.sources = sources;
  renderSourceRegistry(state.sources);
  var list = $("source-health");
  if (!list) { return; }
  list.textContent = "";
  var counts = {ok: 0, error: 0, stale: 0, never: 0, disabled: 0};
  state.sources.forEach(function (source) {
    var statusKey = sourceStatusOf(source);
    counts[statusKey] = (counts[statusKey] || 0) + 1;
    var row = el("li", "source-health");
    var dot = el("span", "sh-dot " + statusKey);
    row.appendChild(dot);
    row.appendChild(el("span", "sh-name", source.name || source.source_id));
    var detail = sourceStatusLabel(statusKey);
    if (statusKey === "error" && source.last_error) {
      detail = "抓取失败：" + (sourceFailureText(source.last_error) || "原因不明");
    } else if (source.last_fetched_at) {
      detail += " · " + formatTime(source.last_fetched_at);
    }
    var meta = el("span", "sh-meta", detail);
    meta.title = detail;
    row.appendChild(meta);
    if (source.enabled) {
      var button = el("button", "", "只刷这个");
      button.type = "button";
      button.title = "只重新抓取这一路订阅，不是打开新闻";
      button.setAttribute("data-source-id", source.source_id);
      button.addEventListener("click", function () { collect(source.source_id); });
      row.appendChild(button);
    }
    list.appendChild(row);
  });
  var bits = [];
  if (counts.ok) { bits.push(counts.ok + " 路抓取成功"); }
  if (counts.error) { bits.push(counts.error + " 路失败"); }
  if (counts.stale) { bits.push(counts.stale + " 路该刷新"); }
  if (counts.never) { bits.push(counts.never + " 路还没抓过"); }
  if (counts.disabled) { bits.push(counts.disabled + " 路停用"); }
  var toggle = $("source-health-toggle");
  if (toggle) {
    var open = toggle.getAttribute("aria-expanded") === "true";
    toggle.textContent = (bits.length ? bits.join(" · ") : "暂无订阅源") + (open ? " · 收起" : " · 点开看来源");
  }
}

function setRank(rank) {
  state.rank = rank === "time" ? "time" : "heat";
  document.querySelectorAll("[data-rank]").forEach(function (tab) {
    var active = tab.dataset.rank === state.rank;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
  });
  renderHotspots(state.rankings[state.rank] || []);
}

function renderHotspots(items) {
  var list = $("hotspot-list");
  var selectedId = state.item ? state.item.item_id : "";
  var selectedItem = null;
  list.textContent = "";
  if (!items.length) {
    if (selectedId) { clearHotspotSelection(true); }
    var empty = el("li", "hotspot-empty");
    empty.appendChild(el("p", "ht-title", "当前时间范围内没有话题或新闻"));
    empty.appendChild(el("p", "ht-summary", "可以拉长上方时间范围，或点「刷新全部」去各订阅源重新抓取。底部「抓取成功 / 只刷这个」只表示订阅健康，不是新闻标题。"));
    list.appendChild(empty);
    scheduleFitHotspotList();
    if (typeof renderPickedDock === "function") renderPickedDock();
    return;
  }
  items.forEach(function (item, index) {
    var button = el("button", "hotspot-item");
    var selected = item.item_id === selectedId;
    if (selected) { selectedItem = item; }
    button.type = "button";
    button.setAttribute("data-item-id", item.item_id);
    button.setAttribute("aria-pressed", String(selected));
    button.classList.toggle("selected", selected);
    button.classList.toggle("is-picked", !!state.pickedIds[item.item_id]);
    button.appendChild(el("span", "idx", (index + 1) + "."));
    button.appendChild(el("span", "ht-label", "原文"));
    var title = el("span", "ht-title", item.title);
    button.appendChild(title);
    button.appendChild(el("span", "ht-summary", item.summary));
    var originalCjk = /[\u3400-\u9fff]/.test(String(item.title || ""));
    if (!originalCjk) {
      button.appendChild(el("span", "ht-label", "中文（AI翻译，非官方表述）"));
      if (item.title_zh || item.summary_zh) {
        button.appendChild(el("span", "ht-title", item.title_zh));
        button.appendChild(el("span", "ht-summary", item.summary_zh));
      } else {
        var unavailable = state.translation.enabled ? "未翻译，可点上方「翻译标题」" : "未翻译（未配置翻译模型）";
        if (item.translation_error_reason || item.translation_error_code) {
          unavailable += "（" + (item.translation_error_reason || item.translation_error_code);
          if (item.translation_error_reason && item.translation_error_code) {
            unavailable += "；错误码：" + item.translation_error_code;
          }
          unavailable += "）";
        }
        button.appendChild(el("span", "ht-zh-unavailable", unavailable));
      }
    }
    var sourceCount = item.source_count || (item.source_ids || []).length;
    var meta = formatTime(item.observed_at) + " · " + sourceCount + " 个来源";
    if (item.effective_tier) {
      meta += " · " + (item.promoted ? (item.base_tier + "→" + item.effective_tier) : item.effective_tier);
    }
    var heat = item.engagement || {};
    var heatBits = [];
    if (heat.score) { heatBits.push("分 " + heat.score); }
    if (heat.comments) { heatBits.push("评 " + heat.comments); }
    if (heat.likes) { heatBits.push("赞 " + heat.likes); }
    if (heat.reposts) { heatBits.push("转 " + heat.reposts); }
    if (heatBits.length) { meta += " · " + heatBits.join(" "); }
    var radar = state.radarEvents[item.item_id];
    if (radar) {
      meta += " · 重要 " + Number(radar.importance_score || 0).toFixed(1)
        + " · 关注 " + Number(radar.attention_score || 0).toFixed(1)
        + " · 优先 " + Number(radar.priority_score || 0).toFixed(1);
      var decision = radar.decision || {};
      var decisionDiagnostics = Array.isArray(decision.diagnostics) ? decision.diagnostics : [];
      if (decisionDiagnostics.length) {
        var decisionLabel = diagnosticLabel(decisionDiagnostics[0]);
        if (decisionLabel) { meta += " · Jev " + decisionLabel; }
      }
    }
    if (item.discussion) { meta += " · 讨论观察"; }
    button.appendChild(el("span", "ht-meta", meta));
    button.addEventListener("click", function () { selectHotspot(item, button); });
    var li = document.createElement("li");
    li.className = "hotspot-row";
    li.appendChild(button);
    li.appendChild(starButton(item));
    li.appendChild(pickToggle(item));
    list.appendChild(li);
  });
  if (selectedItem) {
    state.item = selectedItem;
  } else if (selectedId) {
    var favorite = state.favoriteIds && state.favoriteIds[selectedId];
    if (favorite) {
      state.item = favorite;
    } else {
      clearHotspotSelection(true);
    }
  }
  scheduleFitHotspotList();
  if (typeof renderPickedDock === "function") renderPickedDock();
}

function scheduleFitHotspotList() {
  window.requestAnimationFrame(function () {
    window.requestAnimationFrame(fitHotspotList);
  });
  window.setTimeout(fitHotspotList, 60);
}

function fitHotspotList() {
  var list = $("hotspot-list");
  if (!list) { return; }
  var items = list.children;
  if (!items.length) {
    list.style.height = "";
    return;
  }
  var count = Math.min(2, items.length);
  var first = items[0];
  var last = items[count - 1];
  var height = last.offsetTop + last.offsetHeight - first.offsetTop;
  var cap = window.matchMedia("(max-width: 900px)").matches
    ? Math.round(window.innerHeight * 0.5)
    : Infinity;
  if (height > 0) {
    var next = Math.ceil(Math.min(height, cap)) + "px";
    list.style.height = next;
    list.style.maxHeight = next;
  }
}

function publishButtonLabel() {
  return state.platform === "wechat" ? "到草稿" : "发布";
}

function scheduleButtonLabel() {
  return state.platform === "wechat" ? "定时到草稿" : "定时发布";
}

function applyPublishLabels() {
  $("publish-btn").dataset.originalLabel = publishButtonLabel();
  $("publish-btn").textContent = publishButtonLabel();
  $("schedule-btn").dataset.originalLabel = scheduleButtonLabel();
  $("schedule-btn").textContent = scheduleButtonLabel();
}

function resetArticle() {
  state.draft = null;
  state.creatorUrl = "";
  state.pendingPublish = false;
  state.articleDirty = false;
  $("article-empty").hidden = false;
  $("article-scroll").hidden = true;
  closeImageLightbox();
  $("image-picker").hidden = true;
  $("image-picker-list").textContent = "";
  $("article-title").value = "";
  $("article-body").value = "";
  $("xhs-studio").hidden = true;
  $("xhs-cards").textContent = "";
  $("xhs-export-btn").disabled = true;
  applyPublishLabels();
  $("publish-btn").disabled = true;
  $("schedule-btn").disabled = true;
  $("manual-box").hidden = true;
  $("save-article-btn").disabled = true;
  status("article-status", "");
  status("publish-status", "");
}






function normalizeBaseUrl(value) {
  return String(value || "").trim().replace(/\/+$/, "");
}

function endpointForState(settings) {
  var baseUrl = normalizeBaseUrl(settings && settings.base_url);
  return state.modelEndpoints.find(function (endpoint) {
    return normalizeBaseUrl(endpoint.base_url) === baseUrl;
  }) || null;
}

function modelChoice(endpointId, model) {
  return endpointId + "\t" + model;
}

function parseModelChoice(value) {
  var separator = String(value || "").indexOf("\t");
  if (separator < 1) { return null; }
  return {
    endpointId: value.slice(0, separator),
    model: value.slice(separator + 1),
  };
}

function renderMainModelPicker(id, settings) {
  var select = $(id);
  var currentEndpoint = endpointForState(settings);
  var currentValue = currentEndpoint && settings.model
    ? modelChoice(currentEndpoint.id, settings.model)
    : "";
  select.textContent = "";
  var hasModels = false;
  state.modelEndpoints.forEach(function (endpoint) {
    (endpoint.models || []).forEach(function (model) {
      hasModels = true;
      var option = el("option", "", model);
      option.value = modelChoice(endpoint.id, model);
      option.selected = option.value === currentValue;
      select.appendChild(option);
    });
  });
  if (!hasModels) {
    var empty = el("option", "", "未配置模型");
    empty.value = "";
    empty.disabled = true;
    empty.selected = true;
    select.appendChild(empty);
  }
  var edit = el("option", "", "编辑模型");
  edit.value = "edit";
  select.appendChild(edit);
}

function renderModelPicker() {
  renderMainModelPicker("article-model", state.llm);
}

function fillSelect(select, items, selected, emptyLabel) {
  select.textContent = "";
  if (!items.length) {
    var empty = el("option", "", emptyLabel || "未配置");
    empty.value = "";
    empty.disabled = true;
    empty.selected = true;
    select.appendChild(empty);
    return;
  }
  items.forEach(function (item) {
    var option = el("option", "", item.label);
    option.value = item.value;
    option.selected = item.value === selected;
    select.appendChild(option);
  });
}

function providerOptions() {
  return state.modelEndpoints.map(function (endpoint) {
    return {value: endpoint.id, label: providerHost(endpoint)};
  });
}

function modelOptions(endpoint) {
  return ((endpoint && endpoint.models) || []).map(function (model) {
    return {value: model, label: model};
  });
}

function fillPurposePickers() {
  var form = $("config-form");
  if (!form) { return; }
  var pairs = [
    ["article", "article_provider", "model", state.llm],
    ["translation", "translation_provider", "translation_model", state.translation],
    ["leaderboard", "leaderboard_provider", "leaderboard_model", state.leaderboard],
  ];
  pairs.forEach(function (pair) {
    var providerSelect = form.elements[pair[1]];
    var modelSelect = form.elements[pair[2]];
    var settings = pair[3];
    var current = endpointForState(settings);
    fillSelect(providerSelect, providerOptions(), current ? current.id : "", "先添加提供方");
    fillSelect(
      modelSelect,
      modelOptions(current),
      pair[0] === "translation" ? (state.translation.model || state.llm.model) : (settings.model || ""),
      "先选择提供方"
    );
  });
}

function onPurposeProviderChange(purpose) {
  var form = $("config-form");
  var names = {article: "article_provider", translation: "translation_provider", leaderboard: "leaderboard_provider"};
  var models = {article: "model", translation: "translation_model", leaderboard: "leaderboard_model"};
  var endpoint = state.modelEndpoints.find(function (item) {
    return item.id === form.elements[names[purpose]].value;
  });
  fillSelect(form.elements[models[purpose]], modelOptions(endpoint), "", "选择模型");
}

function providerHost(endpoint) {
  try {
    return new URL(endpoint.base_url).host || endpoint.base_url;
  } catch (error) {
    return endpoint.base_url;
  }
}

function endpointPurposeTags(endpoint) {
  var tags = [];
  var article = endpointForState(state.llm);
  var translation = endpointForState(state.translation);
  if (article && article.id === endpoint.id) { tags.push("文章"); }
  if (translation && translation.id === endpoint.id) { tags.push("翻译"); }
  var leaderboard = endpointForState(state.leaderboard);
  if (leaderboard && leaderboard.id === endpoint.id) { tags.push("榜单"); }
  return tags;
}

function closeProviderEditor() {
  state.providerDraft = {open: false, mode: "add", endpointId: "", models: []};
  var form = $("config-form");
  form.dataset.endpointId = "";
  form.base_url.value = "";
  form.api_key.value = "";
  $("provider-editor").hidden = true;
  renderEndpointLists();
}

function openProviderEditor(mode, endpoint) {
  state.providerDraft = {
    open: true,
    mode: mode,
    endpointId: endpoint ? endpoint.id : "",
    models: endpoint ? (endpoint.models || []).slice() : [],
  };
  var form = $("config-form");
  form.dataset.endpointId = state.providerDraft.endpointId;
  form.base_url.value = endpoint ? endpoint.base_url : "";
  form.api_key.value = "";
  $("api-key-hint").textContent = endpoint && endpoint.credential_available
    ? "已保存的密钥不会回显，留空表示保持原值。"
    : "输入 API Key。";
  $("provider-editor-title").textContent = endpoint ? ("编辑 " + providerHost(endpoint)) : "自定义提供方";
  $("provider-remove").hidden = !endpoint;
  $("provider-editor").hidden = false;
  renderEndpointLists();
}

function renderEndpointLists() {
  var list = $("provider-list");
  if (!list) { return; }
  list.textContent = "";
  if (!state.modelEndpoints.length) {
    list.appendChild(el("li", "muted", "还没有保存的接口。点下方添加自定义提供方。"));
    return;
  }
  state.modelEndpoints.forEach(function (endpoint) {
    var expanded = state.providerDraft.endpointId === endpoint.id && state.providerDraft.mode === "pick";
    var row = el("li", "provider-row" + (expanded ? " active" : ""));
    var dot = el("span", "provider-dot" + (endpoint.credential_available ? " ok" : ""));
    dot.title = endpoint.credential_available ? "API Key 已保存" : "缺少 API Key";
    row.appendChild(dot);
    var body = el("div", "provider-row-body");
    body.appendChild(el("div", "provider-row-title", providerHost(endpoint)));
    var tags = endpointPurposeTags(endpoint);
    body.appendChild(el("div", "provider-row-meta", endpoint.models.length + " 个模型" + (tags.length ? " · " + tags.join(" / ") : "")));
    body.addEventListener("click", function () { toggleProviderPick(endpoint); });
    row.appendChild(body);
    row.appendChild(el("span", "provider-tag", "自定义"));
    if (expanded) {
      var expand = el("div", "provider-expand");
      var label = document.createElement("label");
      label.appendChild(document.createTextNode("模型"));
      var select = document.createElement("select");
      select.id = "bulk-model";
      fillSelect(select, modelOptions(endpoint), "", "选择模型");
      label.appendChild(select);
      expand.appendChild(label);
      var applyAll = el("button", "primary", "一键配置全部入口");
      applyAll.type = "button";
      applyAll.addEventListener("click", function (event) {
        event.stopPropagation();
        bulkAssignEndpoint(endpoint.id, select.value);
      });
      var edit = el("button", "", "编辑接口");
      edit.type = "button";
      edit.addEventListener("click", function (event) {
        event.stopPropagation();
        openProviderEditor("edit", endpoint);
      });
      expand.appendChild(applyAll);
      expand.appendChild(edit);
      row.appendChild(expand);
    }
    list.appendChild(row);
  });
}

function toggleProviderPick(endpoint) {
  if (state.providerDraft.mode === "pick" && state.providerDraft.endpointId === endpoint.id) {
    closeProviderEditor();
    return;
  }
  $("provider-editor").hidden = true;
  state.providerDraft = {open: true, mode: "pick", endpointId: endpoint.id, models: endpoint.models || []};
  renderEndpointLists();
}

function connectEndpoint() {
  var form = $("config-form");
  var button = $("endpoint-connect");
  var baseUrl = form.base_url.value.trim();
  var apiKey = form.api_key.value;
  var endpointId = form.dataset.endpointId || "";
  if (!baseUrl && !endpointId) {
    status("config-error", "请先填写接口地址", "error");
    return;
  }
  busy(button, true);
  waitStart("models");
  status("config-error", "正在获取可用模型…");
  var payload = {purpose: "article", base_url: baseUrl, api_key: apiKey, persist: true};
  if (endpointId && !apiKey) { payload.endpoint_id = endpointId; }
  waitApi("models", "/api/models", payload).then(function (data) {
    applyModelResponse(data);
    form.dataset.endpointId = data.endpoint_id || endpointId || "";
    state.providerDraft.endpointId = form.dataset.endpointId;
    state.providerDraft.models = data.models || [];
    $("provider-remove").hidden = false;
    status("config-error", "已保存 " + (data.models || []).length + " 个模型到下拉列表", "ok");
    waitDone("已获取模型列表", "models");
  }).catch(function (error) {
    if (isWaitAbort(error, "models")) { return; }
    status("config-error", showApiError("获取失败：", error), "error");
    waitFail(error.message || "获取失败", "models");
  }).then(function () { busy(button, false); });
}

function applyProvider() {
  var form = $("config-form");
  var button = $("provider-apply");
  var baseUrl = form.base_url.value.trim();
  var apiKey = form.api_key.value;
  var endpointId = form.dataset.endpointId || "";
  if (!baseUrl && !endpointId) {
    status("config-error", "请先填写接口地址", "error");
    return;
  }
  busy(button, true);
  waitStart("save");
  var payload = {purpose: "article", base_url: baseUrl, api_key: apiKey, persist: true};
  if (endpointId && !apiKey) { payload.endpoint_id = endpointId; }
  if (state.providerDraft.models && state.providerDraft.models.length) {
    payload.models = state.providerDraft.models;
  }
  waitApi("save", "/api/models", payload).then(function (data) {
    applyModelResponse(data);
    form.dataset.endpointId = data.endpoint_id || endpointId || "";
    state.providerDraft.endpointId = form.dataset.endpointId;
    $("provider-remove").hidden = false;
    status("config-error", "提供方已保存", "ok");
    waitDone("提供方已保存", "save");
  }).catch(function (error) {
    if (isWaitAbort(error, "save")) { return; }
    status("config-error", showApiError("保存失败：", error), "error");
    waitFail(error.message || "保存失败", "save");
  }).then(function () { busy(button, false); });
}

function assignEndpoint(purpose) {
  var form = $("config-form");
  var providers = {article: "article_provider", translation: "translation_provider", leaderboard: "leaderboard_provider"};
  var names = {article: "model", translation: "translation_model", leaderboard: "leaderboard_model"};
  var paths = {article: "/api/model", translation: "/api/translation/model", leaderboard: "/api/leaderboard/model"};
  var buttons = {article: "endpoint-use-article", translation: "endpoint-use-translation", leaderboard: "endpoint-use-leaderboard"};
  var labels = {article: "文章", translation: "翻译", leaderboard: "榜单"};
  var endpointId = form.elements[providers[purpose]].value.trim();
  var model = form.elements[names[purpose]].value.trim();
  if (!endpointId || !model) {
    status("config-error", "请先选择提供方和模型", "error");
    return;
  }
  var payload = {endpoint_id: endpointId, model: model};
  if (purpose === "article") { payload.effort = form.effort.value; }
  var button = $(buttons[purpose]);
  busy(button, true);
  waitStart("assign");
  waitApi("assign", paths[purpose], payload).then(function (data) {
    applyModelResponse(data);
    status("config-error", "已保存" + labels[purpose] + "模型", "ok");
    waitDone("已保存" + labels[purpose] + "模型", "assign");
  }).catch(function (error) {
    if (isWaitAbort(error, "assign")) { return; }
    status("config-error", showApiError("保存失败：", error), "error");
    waitFail(error.message || "保存失败", "assign");
  }).then(function () { busy(button, false); });
}

function bulkAssignEndpoint(endpointId, model) {
  if (!endpointId || !model) {
    status("model-endpoint-status", "请先在下拉列表中选择模型", "error");
    return;
  }
  var effort = $("config-form").effort.value || "none";
  status("model-endpoint-status", "正在配置全部入口…");
  waitStart("assign");
  waitApi("assign", "/api/model", {endpoint_id: endpointId, model: model, effort: effort}).then(function (data) {
    applyModelResponse(data);
    return waitApi("assign", "/api/translation/model", {endpoint_id: endpointId, model: model});
  }).then(function (data) {
    applyModelResponse(data);
    return waitApi("assign", "/api/leaderboard/model", {endpoint_id: endpointId, model: model});
  }).then(function (data) {
    applyModelResponse(data);
    status("model-endpoint-status", "已将该模型配置到文章、翻译和榜单", "ok");
    waitDone("已配置全部入口", "assign");
  }).catch(function (error) {
    if (isWaitAbort(error, "assign")) { return; }
    status("model-endpoint-status", showApiError("配置失败：", error), "error");
    waitFail(error.message || "配置失败", "assign");
  });
}

function deleteEndpoint(endpointId) {
  if (!endpointId) { return; }
  if (!window.confirm("删除该接口及其 API Key？")) { return; }
  api("/api/model-endpoints/delete", {endpoint_id: endpointId}).then(function (data) {
    applyModelResponse(data);
    closeProviderEditor();
    status("model-endpoint-status", "接口已删除", "ok");
  }).catch(function (error) {
    status("model-endpoint-status", showApiError("删除失败：", error), "error");
  });
}

function selectedLLM() {
  return {model: state.llm.model, effort: state.llm.effort || "none"};
}

function applyModelResponse(data) {
  if (data.model_endpoints) { state.modelEndpoints = data.model_endpoints; }
  if (data.llm) { state.llm = data.llm; }
  if (data.translation_llm) { state.translation = data.translation_llm; }
  if (data.leaderboard_llm) { state.leaderboard = data.leaderboard_llm; }
  renderModelPicker();
  fillPurposePickers();
  renderEndpointLists();
}

function activateModel(purpose, endpointId, model) {
  var payload = {endpoint_id: endpointId, model: model};
  if (purpose === "article") { payload.effort = "none"; }
  var path = {article: "/api/model", translation: "/api/translation/model", leaderboard: "/api/leaderboard/model"}[purpose];
  return api(path, payload).then(function (data) {
    applyModelResponse(data);
    status(purpose === "article" ? "article-status" : "model-endpoint-status", "模型已切换", "ok");
    return data;
  });
}

function changeMainModel(purpose, event) {
  var select = event.target;
  if (select.value === "edit") {
    select.value = "";
    setSettingsPage("model");
    openSettings(false);
    return;
  }
  var choice = parseModelChoice(select.value);
  if (!choice) { return; }
  select.disabled = true;
  activateModel(purpose, choice.endpointId, choice.model).catch(function (error) {
    status(purpose === "article" ? "article-status" : "model-endpoint-status", showApiError("切换失败：", error), "error");
    renderModelPicker();
  }).then(function () { select.disabled = false; });
}

function showApiError(prefix, error) {
  var message = error.reason || error.message;
  if (error.code) { message += "（错误码：" + error.code + "）"; }
  return prefix + message;
}

function selectHotspot(item, button) {
  if (state.item && item && state.item.item_id === item.item_id) {
    var nodes = $("hotspot-list").querySelectorAll(".hotspot-item");
    nodes.forEach(function (node) {
      var active = node === button;
      node.setAttribute("aria-pressed", String(active));
      node.classList.toggle("selected", active);
    });
    $("open-original-btn").disabled = false;
    syncGenerateEnabled();
    if (typeof renderPickedDock === "function") renderPickedDock();
    return;
  }
  state.item = item;
  var nodes = $("hotspot-list").querySelectorAll(".hotspot-item");
  nodes.forEach(function (node) {
    var active = node === button;
    node.setAttribute("aria-pressed", String(active));
    node.classList.toggle("selected", active);
  });
  $("open-original-btn").disabled = false;
  syncGenerateEnabled();
  if (typeof renderPickedDock === "function") renderPickedDock();
}

function clearHotspotSelection(keepArticle) {
  state.item = null;
  if (!keepArticle) {
    state.articleRequestGen += 1;
    resetArticle();
  }
  closeOriginal();
  $("open-original-btn").disabled = true;
  var list = $("hotspot-list");
  if (list) {
    list.querySelectorAll(".hotspot-item").forEach(function (node) {
      node.setAttribute("aria-pressed", "false");
      node.classList.remove("selected");
    });
  }
  syncGenerateEnabled();
  if (typeof renderPickedDock === "function") renderPickedDock();
}

function clearFocusAfterGenerate() {
  // A1: 只在生成成功后清空「焦点」分组。正文、草稿、钩子与「已选 N」多选集合都不动。
  state.item = null;
  var list = $("hotspot-list");
  if (list) {
    list.querySelectorAll(".hotspot-item").forEach(function (node) {
      node.setAttribute("aria-pressed", "false");
      node.classList.remove("selected");
    });
  }
  $("open-original-btn").disabled = true;
  syncGenerateEnabled();
  if (typeof renderPickedDock === "function") renderPickedDock();
}

function pickedIdList() {
  return Object.keys(state.pickedIds || {}).filter(function (id) {
    return state.pickedIds[id];
  }).sort();
}

function syncGenerateEnabled() {
  var button = $("generate-btn");
  if (!button) { return; }
  var context = state.articleContext;
  var fromDossier = !!(context && context.source_type === "topic_dossier" && context.dossier_id);
  button.disabled = !(fromDossier || state.item || pickedIdList().length);
}

function togglePicked(itemId) {
  if (!itemId) { return; }
  if (state.pickedIds[itemId]) {
    delete state.pickedIds[itemId];
  } else {
    state.pickedIds[itemId] = true;
  }
  syncPickedHighlights();
  syncGenerateEnabled();
  syncResearchControls();
  if (typeof renderPickedDock === "function") renderPickedDock();
}

function renderPickedDock() {
  var dock = $("picked-dock");
  if (!dock) { return; }
  var ids = pickedIdList();
  dock.textContent = "";
  // 焦点与「已选 N」是两个互不覆盖的分组：多选非空时焦点仍然可见。
  if (state.item) {
    dock.hidden = false;
    dock.appendChild(el("span", "picked-dock-label", "焦点"));
    var focusChip = el("span", "picked-dock-chip is-static");
    focusChip.appendChild(el("span", "picked-dock-chip-label", state.item.title || ""));
    dock.appendChild(focusChip);
  }
  if (ids.length) {
    dock.hidden = false;
    dock.appendChild(el("span", "picked-dock-label", "已选 " + ids.length));
    ids.forEach(function (id) {
      var title = id;
      var fav = state.favoriteIds[id];
      if (fav && fav.title) {
        title = fav.title;
      } else {
        var lists = [(state.rankings || {}).heat || [], (state.rankings || {}).time || []];
        var found = false;
        lists.forEach(function (items) {
          if (found) { return; }
          items.forEach(function (item) {
            if (!found && item && item.item_id === id) {
              title = item.title || id;
              found = true;
            }
          });
        });
      }
      var chip = el("button", "picked-dock-chip");
      chip.type = "button";
      chip.setAttribute("data-item-id", id);
      chip.appendChild(el("span", "picked-dock-chip-label", title));
      chip.addEventListener("click", function (event) {
        event.preventDefault();
        event.stopPropagation();
        togglePicked(id);
      });
      dock.appendChild(chip);
    });
    return;
  }
  if (!state.item) {
    dock.hidden = true;
  }
}

function syncPickedHighlights() {
  document.querySelectorAll("#hotspot-list .hotspot-item, #favorites-list .fav-item").forEach(function (node) {
    var id = node.getAttribute("data-item-id");
    node.classList.toggle("is-picked", !!state.pickedIds[id]);
  });
  document.querySelectorAll("#hotspot-list .ht-pick, #favorites-list .ht-pick").forEach(function (node) {
    var on = !!state.pickedIds[node.getAttribute("data-item-id")];
    node.classList.toggle("is-on", on);
    node.setAttribute("aria-checked", String(on));
    node.setAttribute("aria-label", on ? "移出已选" : "加入已选（可多篇融合生成）");
    node.querySelectorAll(".ht-pick-box").forEach(function (box) {
      box.textContent = on ? "✓" : "";
    });
  });
}

function pickToggle(item) {
  var picked = !!state.pickedIds[item.item_id];
  var toggle = el("button", "ht-pick" + (picked ? " is-on" : ""));
  toggle.type = "button";
  toggle.setAttribute("role", "checkbox");
  toggle.setAttribute("aria-checked", String(picked));
  toggle.setAttribute("aria-label", picked ? "移出已选" : "加入已选（可多篇融合生成）");
  toggle.setAttribute("data-item-id", item.item_id);
  toggle.appendChild(el("span", "ht-pick-box", picked ? "✓" : ""));
  toggle.addEventListener("click", function (event) {
    event.preventDefault();
    event.stopPropagation();
    togglePicked(item.item_id);
  });
  return toggle;
}

function starButton(item) {
  var favorited = !!state.favoriteIds[item.item_id];
  var star = el("button", "ht-star", favorited ? "★" : "☆");
  star.type = "button";
  star.setAttribute("aria-label", "收藏");
  star.setAttribute("aria-pressed", String(favorited));
  star.addEventListener("click", function (event) {
    event.preventDefault();
    event.stopPropagation();
    toggleFavorite(item);
  });
  return star;
}

function toggleFavorite(item) {
  if (!item || !item.item_id) { return; }
  var favorited = !!state.favoriteIds[item.item_id];
  api("/api/favorites", {item_id: item.item_id}, {method: favorited ? "DELETE" : "POST"}).then(function () {
    return loadFavorites();
  }).catch(function (error) {
    status("hotspot-status", showApiError("收藏失败：", error), "error");
  });
}

function loadFavorites() {
  return fetch("/api/favorites").then(function (response) { return response.json(); }).then(function (data) {
    var nextFavorites = {};
    (data.items || []).forEach(function (item) {
      if (item && item.item_id) { nextFavorites[item.item_id] = item; }
    });
    state.favoriteIds = nextFavorites;
    renderFavorites();
    syncStars();
    return data;
  });
}

function renderFavorites() {
  var list = $("favorites-list");
  if (!list) { return; }
  list.textContent = "";
  var items = Object.keys(state.favoriteIds).map(function (id) { return state.favoriteIds[id]; });
  items.sort(function (a, b) {
    return String(b.favorited_at || "").localeCompare(String(a.favorited_at || ""));
  });
  items.forEach(function (item) {
    var li = el("li", "fav-item");
    li.setAttribute("data-item-id", item.item_id);
    li.classList.toggle("is-picked", !!state.pickedIds[item.item_id]);
    var title = el("span", "ht-title", item.title || "");
    var openBtn = el("button", "fav-open-original", "打开原文");
    openBtn.type = "button";
    openBtn.addEventListener("click", function (event) {
      event.preventDefault();
      event.stopPropagation();
      openOriginal(item);
    });
    li.appendChild(starButton(item));
    li.appendChild(title);
    li.appendChild(el("span", "ht-summary", item.summary || ""));
    li.appendChild(openBtn);
    // A2: 已选 is driven by a visible checkbox on every surface that feeds it.
    li.appendChild(pickToggle(item));
    list.appendChild(li);
  });
  if (typeof renderPickedDock === "function") renderPickedDock();
}

function syncStars() {
  document.querySelectorAll("#hotspot-list .hotspot-row").forEach(function (row) {
    var card = row.querySelector(".hotspot-item");
    var star = row.querySelector(".ht-star");
    if (!card || !star) { return; }
    var favorited = !!state.favoriteIds[card.getAttribute("data-item-id")];
    star.textContent = favorited ? "★" : "☆";
    star.setAttribute("aria-pressed", String(favorited));
  });
}

function toggleFavoritesDrawer(open) {
  var next = open === undefined ? !document.body.classList.contains("favorites-open") : !!open;
  document.body.classList.toggle("favorites-open", next);
  var drawer = $("favorites-drawer");
  var button = $("favorites-btn");
  if (drawer) { drawer.hidden = !next; }
  if (button) { button.setAttribute("aria-expanded", String(next)); }
  renderPickedDock();
  if (next) { loadFavorites(); }
}

function rankedCount(data) {
  var rankings = (data && data.rankings) || {time: (data && data.candidates) || [], heat: (data && data.candidates) || []};
  return (rankings[state.rank] || []).length;
}

function nextWiderHours(hours) {
  var presets = [4, 6, 24, 72, 168, 720];
  for (var i = 0; i < presets.length; i++) {
    if (presets[i] > hours) { return presets[i]; }
  }
  return 0;
}

function applyHotspots(data, extraStatus) {
  state.rankings = data.rankings || {time: data.candidates || [], heat: data.candidates || []};
  state.radarEvents = {};
  var radarEvents = data.radar && Array.isArray(data.radar.events) ? data.radar.events : [];
  radarEvents.forEach(function (event) {
    var ids = [event.event_id, event.item_id, event.representative_item_id, event.canonical_item_id]
      .concat(Array.isArray(event.item_ids) ? event.item_ids : []);
    ids.forEach(function (itemId) {
      if (itemId) { state.radarEvents[itemId] = event; }
    });
  });
  if (data.source_registry) { renderSourceHealth(data.source_registry); }
  else if (data.sources) { renderSourceHealth(data.sources); }
  setRank(state.rank);
  syncResearchControls();
  var shown = (state.rankings[state.rank] || []).length;
  var text = shown ? "共 " + shown + " 条候选" : "暂无候选";
  if (extraStatus) { text += extraStatus; }
  status("hotspot-status", text, shown ? "ok" : "");
}

function loadHotspots(translate, signal) {
  var gen = ++state.hotspotGen;
  var url = "/api/hotspots?hours=" + encodeURIComponent(currentHours())
    + "&scope=" + encodeURIComponent(state.newsScope || "all");
  if (translate) { url += "&translate=1"; }
  return fetch(url, signal ? {signal: signal} : {}).then(function (response) {
    return response.json().catch(function () { return {}; }).then(function (data) {
      if (!response.ok || data.ok === false) {
        throw new Error(data.error_reason || data.error || ("HTTP " + response.status));
      }
      if (gen !== state.hotspotGen) { return null; }
      data.hotspotGen = gen;
      applyHotspots(data);
      return data;
    });
  });
}

function widenIfEmpty(data, previousHours) {
  if (!data) { return Promise.resolve(null); }
  if (rankedCount(data) > 0) { return Promise.resolve(data); }
  var wider = nextWiderHours(previousHours || currentHours());
  if (!wider) { return Promise.resolve(data); }
  var select = $("hours-select");
  if (select) {
    select.value = String(wider);
    $("hours-custom").hidden = true;
  }
  state.hours = wider;
  return loadHotspots().then(function (next) {
    if (!next || next.hotspotGen !== state.hotspotGen) { return next; }
    applyHotspots(next, " · 近 " + (previousHours || 4) + " 小时没有候选，已改看 " + wider + " 小时");
    return next;
  });
}

function translateTitles() {
  var button = $("translate-btn");
  if (!state.translation.enabled) {
    status("hotspot-status", "请先在设置中选择“用于翻译”的模型", "error");
    return;
  }
  if (waitRunning("translate")) { return; }
  if (button) { busy(button, true); }
  waitStart("translate");
  status("hotspot-status", "正在翻译当前列表标题…");
  loadHotspots(true, waitSignal("translate")).then(function (data) {
    if (waitCancelled("translate")) { return; }
    var shown = rankedCount(data || {});
    status("hotspot-status", shown ? "共 " + shown + " 条候选 · 已尝试翻译标题" : "暂无候选", shown ? "ok" : "");
    waitDone(shown ? "标题翻译完成" : "没有可翻译的标题", "translate");
  }).catch(function (error) {
    if (isWaitAbort(error, "translate")) { return; }
    status("hotspot-status", "翻译失败：" + error.message, "error");
    waitFail(error.message || "翻译失败", "translate");
  }).then(function () {
    if (button) { busy(button, false); }
  });
}

function collect(sourceId) {
  if (waitRunning("collect")) { return; }
  var scope = state.newsScope || "all";
  var requestGen = ++state.collectGen;
  var hours = currentHours();
  if (sourceId) {
    waitStart("collect", {title: "正在刷新这一路订阅"});
  } else if (scope === "community") {
    waitStart("collect", {title: "正在抓取社区来源"});
  } else {
    waitStart("collect");
  }
  status("hotspot-status", sourceId ? "正在刷新该源，已有候选会留在上面…" : (scope === "community" ? "正在从社区来源抓取，已有候选会留在上面…" : "正在从各订阅源抓取，已有候选会留在上面…"));
  var payload = {hours: hours, force: true, scope: scope};
  if (sourceId) { payload.source_id = sourceId; }
  waitApi("collect", "/api/collect", payload).then(function (data) {
    if (waitCancelled("collect")) { return; }
    renderCollectionRun(data && (data.collection_run || data.collection) || data);
    if (requestGen !== state.collectGen) {
      waitDone("已切换栏目", "collect");
      return;
    }
    var inserted = data.collection && data.collection.inserted ? data.collection.inserted : 0;
    var failed = data.collection && data.collection.failed ? data.collection.failed : 0;
    var extra = "";
    if (inserted) { extra += " · 新入库 " + inserted + " 条"; }
    if (failed) { extra += " · " + failed + " 个源失败"; }
    var doneText = inserted ? ("抓取完成 · 新入库 " + inserted + " 条") : "抓取完成";
    if ((state.newsScope || "all") !== scope) {
      waitDone(doneText, "collect");
      return;
    }
    applyHotspots(data, extra);
    if (!rankedCount(data)) {
      return widenIfEmpty(data, hours).then(function (next) {
        if (next && failed) {
          $("hotspot-status").className = "status-error";
        }
        waitDone(doneText, "collect");
      });
    }
    if (failed) { $("hotspot-status").className = "status-error"; }
    waitDone(doneText, "collect");
  }).catch(function (error) {
    if (isWaitAbort(error, "collect")) { return; }
    if (requestGen !== state.collectGen) {
      waitDone("已切换栏目", "collect");
      return;
    }
    status("hotspot-status", "抓取失败：" + error.message, "error");
    waitFail(error.message || "抓取失败", "collect");
    if ((state.newsScope || "all") === scope) { loadHotspots(); }
  });
}

function normalizeClientAssets(raw) {
  if (raw && raw.schema === "article-assets-v2") { return raw; }
  var list = Array.isArray(raw) ? raw : [];
  var candidates = list.map(function (item) {
    return {
      id: item.id || item.url,
      url: item.url,
      source_id: item.source_id || "",
      source_url: item.source_url || item.url,
      width: item.width,
      height: item.height,
    };
  });
  return {
    schema: "article-assets-v2",
    candidates: candidates,
    cover_id: candidates[0] ? candidates[0].id : "",
    body_ids: candidates.map(function (item) { return item.id; }),
  };
}

function persistImages() {
  if (!state.draft) { return Promise.resolve(); }
  var assets = normalizeClientAssets(state.draft.assets);
  return api("/api/articles/images", {
    draft_id: state.draft.draft_id,
    cover_id: assets.cover_id || "",
    body_ids: assets.body_ids || [],
  }).then(function (data) {
    state.draft = Object.assign({}, state.draft, data);
    $("article-title").value = data.title || $("article-title").value;
    $("article-body").value = data.body || $("article-body").value;
    state.draft.assets = data.assets;
    renderImagePicker(data.assets);
    renderXhsStudio(data);
    status("image-picker-status", "配图选择已保存", "ok");
    return data;
  });
}

function changeImageSelection(mutator) {
  if (!state.draft) { return; }
  state.draft.assets = normalizeClientAssets(state.draft.assets);
  mutator(state.draft.assets);
  renderImagePicker(state.draft.assets);
  persistImages().catch(function (error) {
    status("image-picker-status", showApiError("配图保存失败：", error), "error");
  });
}

function rescanImages() {
  if (!state.draft) { return; }
  if (waitRunning("rescan")) { return; }
  var button = $("image-rescan");
  busy(button, true);
  waitStart("rescan");
  status("image-picker-status", "正在从原文扫描配图…");
  waitApi("rescan", "/api/articles/images/rescan", {draft_id: state.draft.draft_id}).then(function (data) {
    if (waitCancelled("rescan")) { return; }
    state.draft = Object.assign({}, state.draft, data);
    $("article-body").value = data.body || $("article-body").value;
    renderImagePicker(data.assets);
    renderXhsStudio(data);
    var count = (data.assets && data.assets.candidates) ? data.assets.candidates.length : 0;
    status("image-picker-status", count ? "已扫描到 " + count + " 张原图" : "原文里没有可用配图", count ? "ok" : "error");
    waitDone(count ? ("已扫描到 " + count + " 张原图") : "原文里没有可用配图", "rescan");
  }).catch(function (error) {
    if (isWaitAbort(error, "rescan")) { return; }
    status("image-picker-status", showApiError("扫描失败：", error), "error");
    waitFail(error.message || "扫描失败", "rescan");
  }).then(function () { busy(button, false); });
}

function clampCarouselIndex(index, total) {
  if (total <= 0) { return 0; }
  return ((index % total) + total) % total;
}
function paintCarouselNav(navId, counterId, index, total) {
  // The prev/next buttons were removed in 3.2.1; only the counter strip is
  // real, so there are no button ids to take here.
  var nav = $(navId);
  if (nav) { nav.hidden = total < 2; }
  var counter = $(counterId);
  if (counter) { counter.textContent = total ? ((index + 1) + " / " + total) : "0 / 0"; }
}

function renderImagePicker(raw) {
  var picker = $("image-picker");
  var list = $("image-picker-list");
  var assets = normalizeClientAssets(raw);
  if (state.draft) { state.draft.assets = assets; }
  list.textContent = "";
  if (!state.draft) {
    picker.hidden = true;
    paintCarouselNav("image-picker-nav", "image-picker-counter", 0, 0);
    return;
  }
  picker.hidden = false;
  if (!assets.candidates.length) {
    list.appendChild(el("p", "muted", "未从原文找到配图。可点重新扫描。"));
    var sourceLine = "来源：" + (state.draft.source_ids || []).join(", ");
    $("article-sources").textContent = sourceLine + " · 无配图";
    paintCarouselNav("image-picker-nav", "image-picker-counter", 0, 0);
    return;
  }
  var coverCount = assets.cover_id ? 1 : 0;
  var sourceLine = "来源：" + ((state.draft && state.draft.source_ids) || []).join(", ");
  $("article-sources").textContent = sourceLine + " · 封面 " + coverCount + " · 正文图 " + assets.body_ids.length + " / " + assets.candidates.length;
  state.imagePickerIndex = clampCarouselIndex(state.imagePickerIndex || 0, assets.candidates.length);
  var index = state.imagePickerIndex;
  var image = assets.candidates[index];
  if (image) {
    var card = el("div", "image-card" + (image.id === assets.cover_id ? " cover" : ""));
    var thumb = document.createElement("img");
    thumb.alt = "点击查看大图";
    thumb.title = "点击查看大图";
    thumb.src = "/api/image-proxy?url=" + encodeURIComponent(image.url);
    thumb.addEventListener("click", function () { openImageLightbox(index); });
    card.appendChild(thumb);
    var meta = el("div", "image-meta");
    meta.appendChild(el("div", "", image.id === assets.cover_id ? "封面候选" : "正文候选"));
    var size = (image.width && image.height) ? (image.width + "×" + image.height) : "尺寸未知";
    meta.appendChild(el("div", "", size + (image.source_id ? " · " + image.source_id : "")));
    meta.appendChild(el("div", "image-url", image.url));
    card.appendChild(meta);
    var actions = el("div", "image-actions");
    var coverLabel = el("label", "image-check");
    coverLabel.innerHTML = '<input type="checkbox" class="cover-check">封面';
    var coverBox = coverLabel.querySelector("input");
    coverBox.checked = image.id === assets.cover_id;
    coverBox.addEventListener("change", function () {
      var on = coverBox.checked;
      changeImageSelection(function (next) {
        next.cover_id = on ? image.id : (next.cover_id === image.id ? "" : next.cover_id);
      });
    });
    var bodyOn = assets.body_ids.indexOf(image.id) >= 0;
    var bodyLabel = el("label", "image-check");
    bodyLabel.innerHTML = '<input type="checkbox" class="body-check">正文';
    var bodyBox = bodyLabel.querySelector("input");
    bodyBox.checked = bodyOn;
    bodyBox.addEventListener("change", function () {
      var on = bodyBox.checked;
      changeImageSelection(function (next) {
        var ids = next.body_ids.slice();
        var pos = ids.indexOf(image.id);
        if (on && pos < 0) { ids.push(image.id); }
        if (!on && pos >= 0) { ids.splice(pos, 1); }
        next.body_ids = ids;
      });
    });
    actions.appendChild(coverLabel);
    actions.appendChild(bodyLabel);
    if (bodyOn) {
      var up = el("button", "", "上移");
      up.type = "button";
      up.disabled = assets.body_ids.indexOf(image.id) === 0;
      up.addEventListener("click", function () {
        changeImageSelection(function (next) {
          var ids = next.body_ids.slice();
          var pos = ids.indexOf(image.id);
          if (pos > 0) { ids.splice(pos - 1, 0, ids.splice(pos, 1)[0]); }
          next.body_ids = ids;
        });
      });
      var down = el("button", "", "下移");
      down.type = "button";
      down.disabled = assets.body_ids.indexOf(image.id) === assets.body_ids.length - 1;
      down.addEventListener("click", function () {
        changeImageSelection(function (next) {
          var ids = next.body_ids.slice();
          var pos = ids.indexOf(image.id);
          if (pos >= 0 && pos < ids.length - 1) { ids.splice(pos + 1, 0, ids.splice(pos, 1)[0]); }
          next.body_ids = ids;
        });
      });
      actions.appendChild(up);
      actions.appendChild(down);
    }
    card.appendChild(actions);
    list.appendChild(card);
  }
  paintCarouselNav("image-picker-nav", "image-picker-counter", index, assets.candidates.length);
}

function proxiedImage(url) {
  if (!url) { return ""; }
  if (url.charAt(0) === "/" && url.charAt(1) !== "/") { return url; }
  if (url.indexOf("/api/generated/") === 0) { return url; }
  return "/api/image-proxy?url=" + encodeURIComponent(url);
}

function currentLightboxImages() {
  var assets = normalizeClientAssets(state.draft && state.draft.assets);
  return assets.candidates || [];
}

function openImageLightbox(index) {
  var images = currentLightboxImages();
  if (!images.length) { return; }
  var total = images.length;
  state.lightboxIndex = ((index % total) + total) % total;
  var image = images[state.lightboxIndex];
  var preview = $("lightbox-image");
  preview.alt = image.credit || "放大预览";
  preview.src = proxiedImage(image.url);
  var size = (image.width && image.height) ? (image.width + "×" + image.height + " · ") : "";
  $("lightbox-caption").textContent = (state.lightboxIndex + 1) + " / " + total + " · " + size + image.url;
  $("lightbox-prev").hidden = total < 2;
  $("lightbox-next").hidden = total < 2;
  var dialog = $("image-lightbox");
  if (dialog && !dialog.open) { dialog.showModal(); }
}

function stepImageLightbox(delta) {
  openImageLightbox(state.lightboxIndex + delta);
}

function closeImageLightbox() {
  var dialog = $("image-lightbox");
  if (dialog && dialog.open) { dialog.close(); }
  var preview = $("lightbox-image");
  if (preview) { preview.removeAttribute("src"); }
}
function hitLightboxSide(event) {
  var node = event.currentTarget;
  var rect = node.getBoundingClientRect();
  return (event.clientX - rect.left) < rect.width / 2 ? -1 : 1;
}
function onLightboxImageClick(event) {
  event.stopPropagation();
  if (currentLightboxImages().length < 2) { return; }
  stepImageLightbox(hitLightboxSide(event));
}

var articleInputAnchor = null;

function captureArticleInputAnchor() {
  var body = $("article-body");
  if (!body || body.closest("[hidden]")) { return; }
  articleInputAnchor = {
    pageY: window.scrollY,
  };
}

function fitArticleBody() {
  var body = $("article-body");
  if (!body || body.closest("[hidden]")) { return; }
  var anchor = articleInputAnchor;
  articleInputAnchor = null;
  var pageY = anchor ? anchor.pageY : window.scrollY;
  body.style.height = "auto";
  var next = Math.max(260, body.scrollHeight + 2);
  body.style.height = next + "px";
  var stableY = pageY;
  window.scrollTo(0, stableY);
  window.requestAnimationFrame(function () {
    if (document.activeElement !== body) { return; }
    window.scrollTo(0, stableY);
    window.setTimeout(function () {
      if (document.activeElement === body) { window.scrollTo(0, stableY); }
    }, 50);
  });
}

function renderArticle(data) {
  $("article-empty").hidden = true;
  $("article-scroll").hidden = false;
  $("article-title").value = data.title || "";
  $("article-body").value = data.body || "";

  $("article-sources").textContent = "来源：" + (data.source_ids || []).join(", ");
  renderImagePicker(data.assets);
  renderXhsStudio(data);
  $("draft-id").textContent = "draft_id: " + data.draft_id;
  $("article-status").textContent = "";
  $("save-article-btn").disabled = false;
  state.articleDirty = false;
  fitArticleBody();
}

function renderXhsStudio(data) {
  var studio = $("xhs-studio");
  var cards = (data && data.cards) || [];
  var show = state.platform === "xiaohongshu" && cards.length;
  studio.hidden = !show;
  $("xhs-export-btn").disabled = !show;
  var root = $("xhs-cards");
  root.textContent = "";
  if (!show) { return; }
  cards.forEach(function (card) {
    var node = el("div", "xhs-paper xhs-" + (card.kind || "page"));
    var head = el("div", "xhs-head");
    var handle = card.handle || "@小红书号";
    var avatar = el("div", "xhs-avatar", handle.replace(/^@/, "").slice(0, 1) || "A");
    head.appendChild(avatar);
    head.appendChild(el("span", "xhs-id", handle));
    node.appendChild(head);
    if (card.kind === "cover") {
      node.appendChild(el("h3", "", card.title || ""));
      if (card.image_url) {
        var image = document.createElement("img");
        image.alt = card.title || "封面";
        image.src = proxiedImage(card.image_url);
        node.appendChild(image);
      } else if (card.lede) {
        node.appendChild(el("p", "xhs-lede", card.lede));
      }
    } else {
      node.appendChild(el("p", "xhs-copy", card.text || ""));
    }
    root.appendChild(node);
  });
}

function closeOriginal() {
  state.originalGen += 1;
  state.originalItemId = "";
  state.originalTranslatable = false;
  if ($("original-dialog").open) { $("original-dialog").close(); }
  busy($("open-original-btn"), false);
  $("open-original-btn").disabled = !state.item;
}

function openOriginal(item) {
  item = item || state.item;
  if (!item || !item.item_id) { return; }
  var itemId = item.item_id;
  var gen = ++state.originalGen;
  var button = $("open-original-btn");
  var fromFocus = !!(state.item && state.item.item_id === itemId);
  state.originalItemId = itemId;
  state.originalTranslatable = false;
  $("original-title").textContent = item.title_zh || item.title || "候选原文";
  $("original-sources").textContent = "";
  $("original-note").textContent = "";
  $("original-content").textContent = "正在读取已收录的原文内容…";
  $("original-translation").textContent = "";
  $("original-translation-wrap").hidden = true;
  $("original-translate").hidden = false;
  $("original-translate").disabled = true;
  $("original-translate").textContent = "翻译文章";
  delete $("original-translate").dataset.originalLabel;
  if (!$("original-dialog").open) { $("original-dialog").showModal(); }
  if (fromFocus) { busy(button, true); }
  waitStart("original");
  waitApi("original", "/api/original", {item_id: itemId}).then(function (data) {
    if (waitCancelled("original")) { return; }
    if (gen !== state.originalGen) { return; }
    $("original-title").textContent = data.title || "候选原文";
    $("original-content").textContent = data.content || "该候选没有可显示的正文内容。";
    $("original-note").textContent = data.content_note || "";
    state.originalTranslatable = !!data.translatable;
    $("original-translate").hidden = !state.originalTranslatable;
    $("original-translate").disabled = !state.originalTranslatable;
    (data.sources || []).forEach(function (source) {
      var link = el("a", "", source.source_id || "打开原站");
      link.href = source.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.title = source.url;
      $("original-sources").appendChild(link);
    });
    waitDone("原文已打开", "original");
  }).catch(function (error) {
    if (isWaitAbort(error, "original")) { return; }
    if (gen === state.originalGen) {
      $("original-content").textContent = showApiError("读取原文失败：", error);
      waitFail(error.message || "读取原文失败", "original");
    }
  }).then(function () {
    if (gen === state.originalGen && fromFocus) { busy(button, false); }
  });
}

function translateOriginal() {
  if (!state.originalItemId || !state.originalTranslatable) { return; }
  var itemId = state.originalItemId;
  var gen = state.originalGen;
  var button = $("original-translate");
  var output = $("original-translation");
  $("original-translation-wrap").hidden = false;
  if (!state.translation.enabled) {
    output.textContent = "请先在设置中选择“用于翻译”的模型。";
    return;
  }
  output.textContent = "正在翻译…";
  busy(button, true);
  waitStart("original_tr");
  waitApi("original_tr", "/api/original/translate", {item_id: itemId}).then(function (data) {
    if (waitCancelled("original_tr")) { return; }
    if (gen !== state.originalGen || itemId !== state.originalItemId) { return; }
    output.textContent = data.translation || "翻译结果为空。";
    button.dataset.originalLabel = "已翻译";
    waitDone("原文翻译完成", "original_tr");
  }).catch(function (error) {
    if (isWaitAbort(error, "original_tr")) { return; }
    if (gen === state.originalGen) {
      output.textContent = showApiError("翻译失败：", error);
      waitFail(error.message || "翻译失败", "original_tr");
    }
  }).then(function () {
    if (gen === state.originalGen) { busy(button, false); }
  });
}

function generate() {
  var context = state.articleContext;
  var dossierId = context && context.source_type === "topic_dossier" ? String(context.dossier_id || "") : "";
  var picked = dossierId ? [] : pickedIdList();
  if (!dossierId && !state.item && !picked.length) { return; }
  var requestPlatform = state.platform;
  var requestItem = state.item;
  var requestIds = dossierId ? [] : (picked.length ? picked.slice() : [state.item.item_id]);
  var requestGen = ++state.articleRequestGen;
  var button = $("generate-btn");
  var llm = selectedLLM();
  if (!llm.model) {
    status("article-status", "请先在设置中填写模型", "error");
    return;
  }
  var payload = {
    platform: state.platform,
    model: llm.model,
    effort: llm.effort,
    prompt_style: $("prompt-style-select").value,
  };
  if (dossierId) {
    // The Topic Dossier is the single source of truth: no hooks, no re-search.
    payload.dossier_id = dossierId;
  } else if (picked.length >= 1) {
    payload.item_ids = picked;
  } else {
    payload.item_id = state.item.item_id;
  }
  var pasted = String((($("article-paste") || {}).value) || "").trim();
  if (pasted) { payload.pasted_text = pasted.slice(0, 20000); }
  if (waitRunning("generate")) { return; }
  busy(button, true);
  waitStart("generate");
  status("article-status", "正在生成文章…");
  waitApi("generate", "/api/articles", payload).then(function (data) {
    if (waitCancelled("generate") || requestGen !== state.articleRequestGen) { return; }
    if (state.platform !== requestPlatform) { return; }
    state.draft = data;
    renderArticle(data);
    applyPublishLabels();
    $("publish-btn").disabled = false;
    $("schedule-btn").disabled = false;
    clearFocusAfterGenerate();
    waitDone("文章已生成", "generate");
    status("article-status", "文章已生成", "ok");
  }).catch(function (error) {
    if (isWaitAbort(error, "generate")) { return; }
    status("article-status", showApiError("生成失败：", error), "error");
    waitFail(error.message || "生成失败", "generate");
  }).then(function () {
    busy(button, false);
  });
}

function persistArticle(kind) {
  if (!state.draft) { return Promise.resolve(); }
  return api("/api/articles/edit", {
    draft_id: state.draft.draft_id,
    title: $("article-title").value,
    body: $("article-body").value,
  }, kind ? {signal: waitSignal(kind)} : {}).then(function (data) {
    state.draft = Object.assign({}, state.draft, data);
    renderArticle(state.draft);
    return data;
  });
}

function saveArticle() {
  if (!state.draft) { return; }
  var button = $("save-article-btn");
  busy(button, true);
  waitStart("save");
  persistArticle("save").then(function () {
    if (waitCancelled("save")) { return; }
    $("publish-btn").disabled = false;
    $("schedule-btn").disabled = false;
    status("article-status", "修改已保存，需重新审批", "ok");
    waitDone("修改已保存", "save");
  }).catch(function (error) {
    if (isWaitAbort(error, "save")) { return; }
    status("article-status", showApiError("保存失败：", error), "error");
    waitFail(error.message || "保存失败", "save");
  }).then(function () {
    busy(button, false);
  });
}

function setPlatform(platform) {
  if (state.platform !== platform) {
    state.articleRequestGen += 1;
    state.collectGen += 1;
  }
  state.platform = platform;
  document.body.classList.toggle("platform-xhs", platform === "xiaohongshu");
  resetArticle();
  var nodes = document.querySelectorAll("button[data-platform]");
  nodes.forEach(function (node) {
    var active = node.dataset.platform === platform;
    node.setAttribute("aria-pressed", String(active));
    node.classList.toggle("active", active);
  });
}

function setSettingsPlatform(platform) {
  state.settingsPlatform = platform;
  document.querySelectorAll("[data-settings-platform]").forEach(function (tab) {
    var active = tab.dataset.settingsPlatform === platform;
    tab.setAttribute("aria-pressed", String(active));
    tab.classList.toggle("active", active);
  });
  $("settings-wechat").hidden = platform !== "wechat";
  $("settings-xiaohongshu").hidden = platform !== "xiaohongshu";
  $("settings-x").hidden = platform !== "x";
}

function setSettingsPage(page) {
  state.settingsPage = page;
  document.querySelectorAll("[data-settings-page]").forEach(function (tab) {
    var active = tab.dataset.settingsPage === page;
    tab.setAttribute("aria-selected", String(active));
    tab.classList.toggle("active", active);
  });
  document.querySelectorAll("[data-page]").forEach(function (panel) {
    panel.hidden = panel.dataset.page !== page;
  });
}

function renderAccountPanels() {
  var wechatOn = !!(state.platforms.wechat && state.platforms.wechat.configured);
  $("wechat-form").hidden = wechatOn;
  $("wechat-delete").hidden = !wechatOn;
  status("wechat-status", wechatOn ? "微信公众号 API：已配置" : "微信公众号 API：未配置", wechatOn ? "ok" : "");
  var xhsOn = !!(state.platforms.xiaohongshu && state.platforms.xiaohongshu.account_configured);
  $("xhs-form").hidden = xhsOn;
  $("xhs-delete").hidden = !xhsOn;
  status("xhs-status", xhsOn ? "小红书：已配置" : "小红书：未填写账号名称", xhsOn ? "ok" : "");
  var xOn = !!(state.platforms.x && state.platforms.x.configured);
  $("x-form").hidden = xOn;
  $("x-delete").hidden = !xOn;
  status("x-status", xOn ? "X 官方读取令牌：已配置" : "X 官方读取令牌：未配置", xOn ? "ok" : "");
}

function deleteAccount(platform) {
  if (!window.confirm("删除该账号配置？删除后需重新填写。")) { return; }
  api("/api/config/delete", {platform: platform}).then(function (data) {
    if (platform === "wechat") { state.platforms.wechat = {configured: false}; }
    if (platform === "x") { state.platforms.x = {configured: false}; }
    if (platform === "xiaohongshu") {
      state.xiaohongshu.accountName = "";
      state.xiaohongshu.creatorUrl = "https://creator.xiaohongshu.com/publish/publish";
    }
    renderAccountPanels();
    status("config-error", "已删除配置", "ok");
  }).catch(function (error) {
    status("config-error", showApiError("删除失败：", error), "error");
  });
}

function openSettings(pendingPublish) {
  state.pendingPublish = pendingPublish;
  var form = $("config-form");
  form.effort.value = state.llm.effort || "none";
  form.base_url.value = "";
  form.api_key.value = "";
  form.dataset.endpointId = "";
  form.elements.style.value = state.style;
  form.app_id.value = "";
  form.app_secret.value = "";
  form.thumb_media_id.value = "";
  form.author.value = "";
  form.account_name.value = "";
  form.creator_url.value = "";
  form.x_bearer_token.value = "";
  closeProviderEditor();
  fillPurposePickers();
  if (!state.modelEndpoints.length) { openProviderEditor("add", null); }
  renderAccountPanels();
  if (pendingPublish) { setSettingsPlatform("wechat"); }
  setSettingsPage(pendingPublish ? "accounts" : state.settingsPage);
  $("config-error").textContent = "";
  if (!$("config-dialog").open) { $("config-dialog").showModal(); }
}

function currentHours() {
  if ($("hours-select").value === "custom") {
    return parseInt($("hours-custom").value, 10);
  }
  return parseInt($("hours-select").value, 10) || 4;
}

function schedulePublish() {
  var draft = state.draft;
  if (!draft) { return; }
  var when = $("schedule-at").value;
  if (!when) {
    status("publish-status", "请选择定时发布时间", "error");
    return;
  }
  var button = $("schedule-btn");
  busy(button, true);
  waitStart("schedule");
  (state.articleDirty ? persistArticle("schedule") : Promise.resolve()).then(function () {
    return persistImages();
  }).then(function () {
    if (waitCancelled("schedule")) { return; }
    draft = state.draft;
    return waitApi("schedule", "/api/approve", {draft_id: draft.draft_id, reviewer: "web-user"});
  }).then(function () {
    if (waitCancelled("schedule")) { return; }
    return waitApi("schedule", "/api/schedule", {
      draft_id: draft.draft_id,
      platform: state.platform,
      live: true,
      run_at: when,
    });
  }).then(function (result) {
    if (waitCancelled("schedule") || !result) { return; }
    status("publish-status", "已预约 " + (result.run_at || when), "ok");
    waitDone("已预约发布", "schedule");
  }).catch(function (error) {
    if (isWaitAbort(error, "schedule")) { return; }
    if (error.code === "wechat_configuration_required") {
      status("publish-status", "请先配置微信", "error");
      waitFail("请先配置微信", "schedule");
      openSettings(true);
      return;
    }
    status("publish-status", showApiError("定时失败：", error), "error");
    waitFail(error.message || "定时失败", "schedule");
  }).then(function () { busy(button, false); });
}

function publish() {
  var draft = state.draft;
  if (!draft) { return; }
  var button = $("publish-btn");
  var completed = false;
  busy(button, true);
  $("manual-box").hidden = true;
  waitStart("publish", {current: 1, total: 3, step: "保存文章"});
  status("publish-status", "保存文章…");
  (state.articleDirty ? persistArticle("publish") : Promise.resolve()).then(function () {
    return persistImages();
  }).then(function () {
    if (waitCancelled("publish")) { return; }
    draft = state.draft;
    waitProgress(2, 3, "提交审批", "publish");
    status("publish-status", "提交审批…");
    return waitApi("publish", "/api/approve", {draft_id: draft.draft_id, reviewer: "web-user"});
  }).then(function () {
    if (waitCancelled("publish")) { return; }
    waitProgress(3, 3, state.platform === "wechat" ? "写入草稿箱" : "发布", "publish");
    status("publish-status", state.platform === "wechat" ? "已审批，正在写入草稿箱…" : "已审批，正在发布…");
    return waitApi("publish", "/api/publish", {draft_id: draft.draft_id, platform: state.platform, live: true});
  }).then(function (result) {
    if (waitCancelled("publish") || !result) { return; }
    if (result.manual_required) {
      state.creatorUrl = result.creator_url || "";
      $("package-path").textContent = result.package_path || "";
      $("manual-box").hidden = false;
      status("publish-status", "发布包已生成", "ok");
      waitDone("发布包已生成", "publish");
    } else {
      status("publish-status", "已写入公众号草稿箱：" + (result.reference || result.status), "ok");
      waitDone("已写入公众号草稿箱", "publish");
    }
    completed = true;
    button.dataset.originalLabel = result.manual_required ? "已生成发布包" : "已到草稿";
  }).catch(function (error) {
    if (isWaitAbort(error, "publish")) { return; }
    if (error.code === "wechat_configuration_required") {
      status("publish-status", "请先配置微信", "error");
      waitFail("请先配置微信", "publish");
      openSettings(true);
    } else {
      status("publish-status", (state.platform === "wechat" ? "写入草稿失败：" : "发布失败：") + error.message, "error");
      waitFail(error.message || "发布失败", "publish");
    }
  }).then(function () {
    busy(button, false);
    if (completed) { button.disabled = true; }
  });
}

window.addEventListener("resize", function () { fitHotspotList(); });
$("hours-select").addEventListener("change", function (event) {
  var custom = event.target.value === "custom";
  $("hours-custom").hidden = !custom;
  state.hours = currentHours();
  loadHotspots();
});
$("hours-custom").addEventListener("change", function () {
  state.hours = currentHours();
  loadHotspots();
});
$("collect-btn").addEventListener("click", function () { collect(); });
$("translate-btn").addEventListener("click", function () { translateTitles(); });
function setSourcesOpen(open) {
  var list = $("source-health");
  var panel = $("hotspots-panel");
  var toggle = $("source-health-toggle");
  if (list) { list.hidden = !open; }
  if (panel) { panel.classList.toggle("sources-open", !!open); }
  if (toggle) { toggle.setAttribute("aria-expanded", String(!!open)); }
  renderSourceHealth(state.sources);
}

$("source-health-toggle").addEventListener("click", function () {
  setSourcesOpen($("source-health").hidden);
});
document.querySelectorAll("[data-rank]").forEach(function (tab) {
  tab.addEventListener("click", function () { setRank(tab.dataset.rank); });
});
function githubIcon(url) {
  return "/api/image-proxy?url=" + encodeURIComponent(url || "");
}
function renderGithubWeekly(payload) {
  state.githubWeekly = payload || state.githubWeekly;
  var visual = state.githubWeekly.visual || {};
  if (visual.theme) { setGithubTheme(visual.theme); }
  var savedPalette = visual.palette || {};
  if (savedPalette.main && $("gh-color-main")) { $("gh-color-main").value = savedPalette.main; }
  if (savedPalette.secondary && $("gh-color-secondary")) { $("gh-color-secondary").value = savedPalette.secondary; }
  if (savedPalette.accent && $("gh-color-accent")) { $("gh-color-accent").value = savedPalette.accent; }
  if (visual.poster_template) { setPosterTemplate(visual.poster_template, true); }
  var repos = state.githubWeekly.repos || [];
  // The retired hidden list is gone: the visible gallery below is the one
  // surface that shows this week's repos, so a duplicate cannot drift from it.
  renderGithubGallery();
  $("github-xhs-title").value = state.githubWeekly.xhs_title || "本周值得关注的github ai项目前10";
  $("github-xhs-body").value = state.githubWeekly.xhs_body || "";
  var images = githubImages();
  $("github-export-btn").disabled = !images.filter(function (image) { return image.selected !== false && image.url; }).length;
  $("github-copy-btn").disabled = !repos.length;
  $("github-select-all").disabled = !images.length;
}
function githubFreshUrl(url) {
  if (!url) { return ""; }
  var stamp = state.githubImageBust;
  if (!stamp) { return url; }
  return url + (url.indexOf("?") >= 0 ? "&" : "?") + "t=" + stamp;
}
function githubImages() {
  return (state.githubWeekly && state.githubWeekly.images) || [];
}
function renderGithubGallery() {
  var gallery = $("github-gallery");
  gallery.textContent = "";
  var images = githubImages();
  var total = images.length;
  if (!total) {
    paintCarouselNav("github-gallery-nav", "github-gallery-counter", 0, 0);
    return;
  }
  state.githubGalleryIndex = clampCarouselIndex(state.githubGalleryIndex || 0, total);
  var index = state.githubGalleryIndex;
  var image = images[index];
  var extras = "";
  if (image.busy) { extras += " is-running"; }
  else if (image.queued) { extras += " is-queued"; }
  if (image.error) { extras += " is-error"; }
  if (image.selected !== false) { extras += " selected"; }
  var kindClass = image.kind === "cover" ? " gh-cover" : (image.kind === "poster" ? " gh-poster" : "");
  var tile = el("div", "gh-tile" + kindClass + extras);
  tile.dataset.imageId = image.id;
  var check = document.createElement("button");
  check.type = "button";
  check.className = "gh-check";
  check.textContent = image.selected === false ? "" : "✓";
  check.addEventListener("click", function (event) {
    event.stopPropagation();
    toggleGithubImage(image.id);
  });
  tile.appendChild(check);
  if (image.url) {
    var img = document.createElement("img");
    img.alt = image.title || image.id;
    img.src = githubFreshUrl(image.url);
    img.addEventListener("click", function () { openGithubLightbox(index); });
    tile.appendChild(img);
  }
  if (image.busy) {
    tile.appendChild(el("p", "gh-pending gh-overlay", "正在生成…"));
  } else if (image.queued) {
    tile.appendChild(el("p", "gh-pending gh-overlay", "排队中"));
  } else if (image.error) {
    tile.appendChild(el("p", "gh-pending gh-overlay", "失败 · " + image.error));
  } else if (!image.url) {
    tile.appendChild(el("p", "gh-pending", "待出图 · " + (image.title || image.id)));
  }
  gallery.appendChild(tile);
  paintCarouselNav("github-gallery-nav", "github-gallery-counter", index, total);
}
function toggleGithubImage(imageId) {
  githubImages().forEach(function (image) {
    if (image.id === imageId) {
      image.selected = image.selected === false;
    }
  });
  renderGithubWeekly(state.githubWeekly);
}
function selectAllGithubImages() {
  var images = githubImages();
  var allOn = images.length && images.every(function (image) { return image.selected !== false; });
  images.forEach(function (image) { image.selected = !allOn; });
  renderGithubWeekly(state.githubWeekly);
}
function openGithubLightbox(index) {
  var images = githubImages().filter(function (image) { return image.url; });
  if (!images.length) { return; }
  state.draft = state.draft || {assets: {candidates: []}};
  state.draft.assets = {
    schema: "article-assets-v2",
    candidates: images.map(function (image) {
      return {id: image.id, url: image.url, source_id: "github-weekly", source_url: image.url};
    }),
    cover_id: "",
    body_ids: [],
  };
  var start = 0;
  images.forEach(function (image, pos) {
    if (githubImages()[index] && image.id === githubImages()[index].id) { start = pos; }
  });
  openImageLightbox(start);
}
function loadGithubWeekly() {
  status("studio-status", "正在读取周榜…");
  return fetch("/api/github-weekly").then(function (response) { return response.json(); }).then(function (data) {
    renderGithubWeekly(data);
    renderResourceGithubResults(data.repos || []);
    var count = (data.repos || []).length;
    status("studio-status", count ? "已显示 " + count + " 个仓库，可再点抓取周榜刷新" : "还没有周榜。点「抓取周榜」拉取本周新建的 AI 仓库，不够再用本周涨星补齐。", count ? "ok" : "");
    return data;
  }).catch(function (error) {
    status("studio-status", "读取周榜失败：" + error.message, "error");
  });
}
function githubThemeDefaults(theme) {
  return theme === "light"
    ? {main: "#F3EFE4", secondary: "#FFFCF6", accent: "#5C4FD6"}
    : {main: "#06155A", secondary: "#101F63", accent: "#F3F0E8"};
}
function templatePaletteDefaults(template, theme) {
  if (String(template) === "2") {
    return {main: "#DED3C0", secondary: "#EBE3D3", accent: "#9F7D4F"};
  }
  return githubThemeDefaults(theme);
}
function setPosterTemplate(id, keepPalette) {
  state.posterTemplate = String(id) === "2" ? "2" : "1";
  document.querySelectorAll("[data-template]").forEach(function (node) {
    var on = node.getAttribute("data-template") === state.posterTemplate;
    node.classList.toggle("active", on);
    node.setAttribute("aria-pressed", String(on));
  });
  if (!keepPalette) {
    var defaults = templatePaletteDefaults(state.posterTemplate, state.githubTheme || "dark");
    if ($("gh-color-main")) { $("gh-color-main").value = defaults.main; }
    if ($("gh-color-secondary")) { $("gh-color-secondary").value = defaults.secondary; }
    if ($("gh-color-accent")) { $("gh-color-accent").value = defaults.accent; }
  }
}
function visualPalette() {
  return {
    main: $("gh-color-main").value,
    secondary: $("gh-color-secondary").value,
    accent: $("gh-color-accent").value
  };
}
function setGithubTheme(theme) {
  state.githubTheme = theme === "light" ? "light" : "dark";
  if ($("gh-theme-dark")) { $("gh-theme-dark").classList.toggle("active", state.githubTheme === "dark"); }
  if ($("gh-theme-light")) { $("gh-theme-light").classList.toggle("active", state.githubTheme === "light"); }
  var defaults = templatePaletteDefaults(state.posterTemplate || "1", state.githubTheme);
  if ($("gh-color-main")) { $("gh-color-main").value = defaults.main; }
  if ($("gh-color-secondary")) { $("gh-color-secondary").value = defaults.secondary; }
  if ($("gh-color-accent")) { $("gh-color-accent").value = defaults.accent; }
}
function renderGithubWeeklyImages() {
  var button = $("github-render-btn");
  if (!(state.githubWeekly.repos || []).length) {
    status("studio-status", "请先点「抓取周榜」，再生成图片", "error");
    return;
  }
  var images = githubImages();
  if (!images.length) {
    status("studio-status", "没有可生成的图片任务", "error");
    return;
  }
  if (waitRunning("github_render")) { return; }
  if (button) { busy(button, true); }
  var palette = visualPalette();
  var theme = state.githubTheme || "dark";
  var total = images.length;
  var index = 0;
  var failed = 0;
  state.githubImageBust = Date.now();
  images.forEach(function (image) {
    image.queued = true;
    image.busy = false;
    image.error = "";
  });
  renderGithubGallery();
  waitStart("github_render", {current: 0, total: total, step: "准备开始"});
  status("studio-status", "开始生成 1 / " + total + " 张周榜图…");
  function finishRender() {
    images.forEach(function (image) { image.queued = false; image.busy = false; });
    renderGithubGallery();
    if (button) { busy(button, false); }
    $("github-export-btn").disabled = !images.filter(function (image) { return image.selected !== false && image.url; }).length;
  }
  function next() {
    if (waitCancelled("github_render")) {
      finishRender();
      return;
    }
    if (index >= total) {
      var done = total - failed;
      if (failed && !done) {
        waitFail("全部 " + total + " 张都没有生成", "github_render");
        status("studio-status", "出图失败：全部 " + total + " 张都没有生成", "error");
      } else {
        waitDone(failed ? ("已生成 " + done + " / " + total + " 张，失败 " + failed + " 张") : ("已全部生成 " + done + " 张"), "github_render");
        status("studio-status", failed ? ("已生成 " + done + " 张，失败 " + failed + " 张") : ("已全部生成 " + done + " 张周榜图，勾选后可导出"), failed ? "error" : "ok");
      }
      finishRender();
      return;
    }
    var job = images[index];
    job.queued = false;
    job.busy = true;
    job.error = "";
    state.githubGalleryIndex = index;
    waitProgress(index + 1, total, job.title || job.id, "github_render");
    status("studio-status", "正在生成第 " + (index + 1) + " / " + total + " 张 · " + (job.title || job.id));
    renderGithubGallery();
    // 逐张调用 /api/github-weekly/image；全量 /api/github-weekly/render 仍给脚本用
    waitApi("github_render", "/api/github-weekly/image", {id: job.id, theme: theme, palette: palette, poster_template: state.posterTemplate || "1"}).then(function (data) {
      if (waitCancelled("github_render")) {
        job.busy = false;
        finishRender();
        return;
      }
      Object.assign(job, data, {busy: false, queued: false, error: ""});
      index += 1;
      next();
    }).catch(function (error) {
      if (isWaitAbort(error, "github_render")) {
        job.busy = false;
        finishRender();
        return;
      }
      job.busy = false;
      job.queued = false;
      job.error = error.reason || error.message || "失败";
      failed += 1;
      index += 1;
      next();
    });
  }
  next();
}
function copyGithubXhs() {
  var text = ($("github-xhs-title").value || "") + "\\n\\n" + ($("github-xhs-body").value || "");
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(function () {
      status("studio-status", "小红书文案已复制", "ok");
    }).catch(function () {
      $("github-xhs-body").select();
      status("studio-status", "请手动复制文案", "");
    });
  } else {
    $("github-xhs-body").select();
  }
}
function loadGithubImage(url) {
  return new Promise(function (resolve) {
    if (!url) { resolve(null); return; }
    var image = new Image();
    image.crossOrigin = "anonymous";
    image.onload = function () { resolve(image); };
    image.onerror = function () { resolve(null); };
    image.src = githubIcon(url);
  });
}
function roundedRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}
function wrapText(ctx, text, x, y, maxWidth, lineHeight, color, font) {
  ctx.fillStyle = color;
  ctx.font = font;
  var line = "";
  String(text || "").split("").forEach(function (ch) {
    var next = line + ch;
    if (ctx.measureText(next).width > maxWidth && line) {
      ctx.fillText(line, x, y);
      line = ch;
      y += lineHeight;
    } else {
      line = next;
    }
  });
  if (line) { ctx.fillText(line, x, y); }
}
function downloadCanvas(canvas, filename) {
  var link = document.createElement("a");
  link.href = canvas.toDataURL("image/png");
  link.download = filename;
  link.click();
}
function exportGithubImages() {
  var selected = githubImages().filter(function (image) { return image.selected !== false && image.url; });
  if (!selected.length) { return; }
  if (waitRunning("export")) { return; }
  var button = $("github-export-btn");
  if (button) { busy(button, true); }
  waitStart("export", {current: 0, total: selected.length, unit: "张", step: "准备导出"});
  var chain = Promise.resolve();
  selected.forEach(function (image, index) {
    chain = chain.then(function () {
      if (waitCancelled("export")) { return; }
      waitProgress(index + 1, selected.length, image.title || image.id, "export");
      return fetch(image.url, {signal: waitSignal("export")}).then(function (response) { return response.blob(); }).then(function (blob) {
        var link = document.createElement("a");
        link.href = URL.createObjectURL(blob);
        link.download = (image.file || (image.id + ".png"));
        window.setTimeout(function () { link.click(); }, index * 220);
      });
    });
  });
  chain.then(function () {
    if (waitCancelled("export")) { return; }
    status("studio-status", "已按当前顺序导出 " + selected.length + " 张选中图片", "ok");
    waitDone("已导出 " + selected.length + " 张", "export");
  }).catch(function (error) {
    if (isWaitAbort(error, "export")) { return; }
    status("studio-status", "导出失败：" + error.message, "error");
    waitFail(error.message || "导出失败", "export");
  }).then(function () {
    if (button) { busy(button, false); }
  });
}
function skillImages() {
  return (state.skillsPackage && state.skillsPackage.images) || [];
}
// 降级原因来自后端，是 `来源:kind` 形式的记号。给用户看中文，原始记号留在 title 里。
var DEGRADE_LABELS = {
  "github:rate_limited": "GitHub 接口限流（未配置 Token 时每小时只给 60 次）",
  "github:timeout": "GitHub 响应超时",
  "github:unreachable": "GitHub 连不上",
  "github:server_error": "GitHub 服务端错误",
  "github:forbidden": "GitHub 拒绝访问",
  "github:not_found": "GitHub 找不到资源",
  "github:UnicodeEncodeError": "仓库路径里的非 ASCII 字符没能拼成合法 URL（1.4.0 起已按百分号编码，旧快照里可能还有这条）",
  "github:InvalidURL": "GitHub 上有一个地址无法请求（已跳过）",
  // 1.3.0 只存了异常类名，旧快照里的这条也要说人话。
  "github:RuntimeError": "GitHub 接口本次没返回（多半是限流）",
  "budget_exhausted": "本次检索预算用完",
  "skills.sh:unparseable": "skills.sh 返回无法解析",
  "skills.sh:timeout": "skills.sh 响应超时",
  "skills.sh:unreachable": "skills.sh 连不上",
  "web_search:timeout": "网页检索超时"
};
function degradeLabel(reason) {
  var key = String(reason || "");
  if (!key) { return ""; }
  if (DEGRADE_LABELS[key]) { return DEGRADE_LABELS[key]; }
  if (key.indexOf("skills.sh:") === 0) { return "skills.sh " + key.slice(10); }
  if (key.indexOf("web_search:") === 0) { return "网页检索 " + key.slice(11); }
  if (key.indexOf("github:") === 0) { return "GitHub 返回了异常：" + key.slice(7); }
  // Never print a raw identifier at the user: an unmapped reason still says which
  // source failed and what it was, in one readable line.
  return "来源" + key + " 返回了异常";
}
function degradedSummary(reasons) {
  var labels = reasons.map(degradeLabel).filter(function (item) { return !!item; });
  var text = "部分来源本次没有返回：" + (labels.length ? labels.join("；") : "已降级") + "，结果可能偏少。";
  if (reasons.indexOf("github:rate_limited") >= 0 && !state.githubTokenConfigured) {
    text += "去「设置」配好 GitHub Token 后，GitHub 每小时额度从 60 次升到 5000 次，收录会明显变多。";
  }
  return text;
}
var SKILL_SOURCE_LABELS = {
  "github": "GitHub",
  "GitHub": "GitHub",
  "skills.sh": "skills.sh",
  "web": "网页检索"
};
function skillSourceLabel(skill) {
  if (skill && skill.manual) { return "手动粘贴"; }
  var raw = String((skill && skill.source) || "").trim();
  if (!raw) { return "未知来源"; }
  return SKILL_SOURCE_LABELS[raw.toLowerCase()] || SKILL_SOURCE_LABELS[raw] || raw;
}
// One chip per source, with how many collected skills came from it. This is the
// whole of the old stage-chip + gap-note area: the user asked where the skills
// came from, not which coverage stage the search plan happened to define.
function renderSkillSourceChips(skills) {
  var chips = $("skills-source-chips");
  if (!chips) { return; }
  chips.textContent = "";
  var counts = {};
  var order = [];
  (skills || []).forEach(function (skill) {
    if (!skill || skill.gap) { return; }
    var label = skillSourceLabel(skill);
    if (counts[label] === undefined) { counts[label] = 0; order.push(label); }
    counts[label] += 1;
  });
  if (!order.length) {
    chips.appendChild(el("li", "", "还没有收录 skill"));
    return;
  }
  order.forEach(function (label) {
    var chip = el("li", label === "手动粘贴" ? "is-manual" : "");
    var button = document.createElement("button");
    button.type = "button";
    button.textContent = label + " " + counts[label];
    button.title = "点击全选或取消来自" + label + "的 skill";
    button.addEventListener("click", function () { toggleSkillsBySource(label); });
    chip.appendChild(button);
    chips.appendChild(chip);
  });
}
function skillIdentity(skill) {
  var key = String((skill && (skill.source_id || skill.full_name || skill.name)) || "").toLowerCase();
  var slot = String((skill && (skill.skill_path || skill.name)) || "").toLowerCase();
  return key + "|" + slot;
}
function skillPageUrl(skill) {
  var explicit = String((skill && (skill.url || skill.html_url)) || "").trim();
  if (/^https?:\/\//.test(explicit)) { return explicit; }
  var sourceId = String((skill && (skill.source_id || skill.full_name || skill.name)) || "");
  if (String((skill && skill.source) || "") === "skills.sh" && sourceId) {
    return "https://skills.sh/" + sourceId;
  }
  if (sourceId.indexOf("/") >= 0) { return "https://github.com/" + sourceId; }
  return "";
}
function collectedSkills(pack) {
  var rows = [];
  var seen = {};
  function add(skill) {
    if (!skill || skill.gap) { return; }
    var id = skillIdentity(skill);
    if (!id || seen[id]) { return; }
    seen[id] = true;
    rows.push(skill);
  }
  ((pack && pack.skills) || []).forEach(add);
  ((pack && pack.manual_skills) || []).forEach(add);
  return rows;
}
function selectedSkills(pack) {
  return collectedSkills(pack).filter(function (skill) {
    return skill && skill.selected !== false;
  });
}
function toggleSkillsBySource(label) {
  var pack = state.skillsPackage || {};
  var rows = collectedSkills(pack).filter(function (skill) {
    return skillSourceLabel(skill) === label;
  });
  if (!rows.length) { return; }
  var allOn = rows.every(function (skill) { return skill.selected !== false; });
  rows.forEach(function (skill) { skill.selected = allOn ? false : true; });
  if (!(pack.images || []).some(function (image) { return image && image.url; })) {
    pack.images = queuedSkillImages(pack);
  }
  renderSkillPackage(pack);
}
function resourceKindLabel(kind) {
  return {repo: "Repo", skill: "Skill", website: "Web", tool: "Tool", other: "Other"}[String(kind || "")] || "资源";
}
function resourceDiagnosticLabel(diagnostic) {
  if (!diagnostic || typeof diagnostic !== "object") { return ""; }
  return [diagnostic.source, diagnostic.stage, diagnostic.code, diagnostic.cause_code]
    .filter(function (value) { return value !== undefined && value !== null && String(value); })
    .map(function (value) { return String(value); }).join(" · ");
}
function renderResourceSet(resourceSet) {
  state.resourceSet = resourceSet || null;
  var target = $("resource-results");
  if (!target) { return; }
  target.textContent = "";
  if (!resourceSet || typeof resourceSet !== "object") {
    target.appendChild(el("article", "ia-card", "没有可展示的 ResourceSet。"));
    return;
  }
  var plan = resourceSet.plan && resourceSet.plan.plan ? resourceSet.plan.plan : {};
  var counts = resourceSet.counts && typeof resourceSet.counts === "object" ? resourceSet.counts : {};
  var summary = el("article", "ia-card resource-set-summary");
  summary.appendChild(el("h3", "", "ResourceSet · " + (resourceSet.status || "unknown")));
  summary.appendChild(el("p", "", "查询：" + (resourceSet.query || plan.query || "—") + (resourceSet.preset ? " · preset: " + resourceSet.preset : "")));
  summary.appendChild(el("p", "meta", "Repo " + (counts.repo || 0) + " · Skill " + (counts.skill || 0) + " · Web " + (counts.website || 0) + " · Tool " + (counts.tool || 0)));
  if (resourceSet.degraded) { summary.appendChild(el("p", "status-error", "部分来源降级，仍保留已返回候选。")); }
  var adapterStatus = resourceSet.adapter_status && typeof resourceSet.adapter_status === "object" ? resourceSet.adapter_status : {};
  var sourceLabels = [];
  Object.keys(adapterStatus).forEach(function (name) {
    var item = adapterStatus[name] || {};
    sourceLabels.push(name + " · " + (item.status || "unknown") + " · " + (item.count || 0));
  });
  if (sourceLabels.length) { summary.appendChild(el("p", "meta", "来源：" + sourceLabels.join(" ｜ "))); }
  var diagnostics = Array.isArray(resourceSet.diagnostics) ? resourceSet.diagnostics : [];
  var diagnosticLabels = [];
  diagnostics.forEach(function (item) {
    var label = resourceDiagnosticLabel(item);
    if (label && diagnosticLabels.indexOf(label) < 0) { diagnosticLabels.push(label); }
  });
  if (diagnosticLabels.length) { summary.appendChild(el("p", "resource-diagnostics", "诊断：" + diagnosticLabels.slice(0, 6).join(" ｜ "))); }
  target.appendChild(summary);
  var filter = $("resource-kind-filter");
  var kindFilter = filter ? String(filter.value || "all") : "all";
  var rows = Array.isArray(resourceSet.candidates) ? resourceSet.candidates.filter(function (candidate) {
    return candidate && (kindFilter === "all" || String(candidate.kind || "") === kindFilter);
  }) : [];
  if (!rows.length) {
    target.appendChild(el("article", "ia-card", kindFilter === "all" ? "暂无候选；请查看来源状态或换一句需求。" : "当前筛选没有候选。"));
    return;
  }
  rows.forEach(function (candidate) {
    var card = el("article", "ia-card resource-candidate");
    card.dataset.resourceId = String(candidate.resource_id || "");
    var heading = el("h3", "", candidate.name || candidate.url || "未命名资源");
    var url = String(candidate.url || "");
    if (url) {
      heading.textContent = "";
      var link = document.createElement("a");
      link.href = url;
      link.target = "_blank";
      link.rel = "noreferrer noopener";
      link.textContent = candidate.name || url;
      heading.appendChild(link);
    }
    card.appendChild(heading);
    card.appendChild(el("p", "meta", resourceKindLabel(candidate.kind) + " · " + (candidate.source || "unknown") + (candidate.selected ? " · 推荐" : "")));
    if (candidate.description) { card.appendChild(el("p", "", String(candidate.description).slice(0, 280))); }
    card.appendChild(el("p", "meta", "客观分 " + Number(candidate.objective_score || 0).toFixed(1)));
    target.appendChild(card);
  });
}
function selectedResourceAdapters() {
  var values = [];
  document.querySelectorAll(".resource-adapter").forEach(function (input) {
    if (input.checked) { values.push(String(input.value)); }
  });
  return values;
}
function discoverResources(preset) {
  var query = String($("resource-query").value || "").trim();
  var adapters = selectedResourceAdapters();
  if (!preset && !query) {
    status("resource-status", "请先输入自然语言资源需求", "error");
    return;
  }
  if (!adapters.length) {
    status("resource-status", "至少选择一个 Repo、Skill 或 Web 检索源", "error");
    return;
  }
  var button = preset ? $("resource-weekly-btn") : $("resource-search-btn");
  busy(button, true);
  status("resource-status", preset ? "正在查找 GitHub Weekly Preset…" : "正在搜索统一 ResourceSet…", "");
  var body = {
    query: query,
    pasted_text: String($("resource-pasted-text").value || ""),
    adapters: adapters,
    limit: 50,
    persist: true
  };
  if (preset) { body.preset = "github_weekly"; }
  api("/api/resources/discover", body).then(function (data) {
    var resourceSet = data.resource_set || data;
    renderResourceSet(resourceSet);
    status("resource-status", "搜索完成 · " + (resourceSet.status || "已返回") + (resourceSet.degraded ? " · 部分来源降级" : ""), resourceSet.degraded ? "error" : "ok");
  }).catch(function (error) {
    status("resource-status", "资源搜索失败：" + error.message, "error");
  }).then(function () {
    busy(button, false);
  });
}
function sendResourcesToStudio() {
  // Iteration 2 handoff: Skill stays a resource type, but the poster studio
  // keeps living in the creation page — this reuses the single skills pipeline.
  var query = String(($("resource-query") || {}).value || "").trim();
  var links = String(($("resource-pasted-text") || {}).value || "").trim();
  if (!links && !query) {
    status("resource-status", "先输入资源需求或粘贴 Skill 链接", "error");
    return;
  }
  setAppPage("creation");
  if (links) {
    status("studio-status", "正在读取粘贴的 Skill…", "");
    pasteSkills(links);
  } else {
    status("studio-status", "正在按资源需求查找 Skill…", "");
    searchSkills(query);
  }
}
function openWeeklyStudio() {
  setAppPage("creation");
  status("studio-status", "正在读取周榜快照…", "");
  return loadGithubWeekly();
}
function fetchGithubWeekly() {
  var button = $("github-fetch-btn");
  if (button) { busy(button, true); }
  waitStart("github_fetch");
  status("studio-status", "正在抓取本周周榜…", "");
  return waitApi("github_fetch", "/api/github-weekly", {}).then(function (data) {
    renderGithubWeekly(data);
    renderResourceGithubResults(data.repos || []);
    var count = (data.repos || []).length;
    status("studio-status", count ? ("已抓取 " + count + " 个仓库，可生成图片") : "本周没有抓到合适的仓库", count ? "ok" : "error");
    waitDone(count ? ("已抓取 " + count + " 个仓库") : "没有抓到仓库", "github_fetch");
    return data;
  }).catch(function (error) {
    status("studio-status", "抓取周榜失败：" + error.message, "error");
    waitFail(error.message || "抓取周榜失败", "github_fetch");
  }).then(function () {
    if (button) { busy(button, false); }
  });
}
function renderResourceResults(skills) {
  var target = $("resource-results");
  if (!target) { return; }
  if (target.dataset.resourceDiscoverySlot !== "legacy") { return; }
  target.textContent = "";
  var rows = (skills || []).filter(function (skill) { return skill && !skill.gap; });
  if (!rows.length) {
    target.appendChild(el("article", "ia-card", "暂无资源结果。输入关键词后，这里会复用现有 Skill 搜索结果。"));
    return;
  }
  rows.forEach(function (skill) {
    var card = el("article", "ia-card");
    var name = String(skill.name || skill.source_id || "未命名资源");
    var heading = el("h3", "", name);
    var url = skillPageUrl(skill);
    if (url) {
      heading.textContent = "";
      var link = document.createElement("a");
      link.href = url;
      link.target = "_blank";
      link.rel = "noreferrer noopener";
      link.textContent = name;
      heading.appendChild(link);
    }
    card.appendChild(heading);
    var detail = String(skill.description || skill.summary || skill.readme || "").trim();
    card.appendChild(el("p", "", detail ? detail.slice(0, 180) : (skillSourceLabel(skill) + " · 可继续进入创作页")));
    target.appendChild(card);
  });
}
function renderResourceGithubResults(repos) {
  var target = $("resource-results");
  if (!target) { return; }
  if (target.dataset.resourceDiscoverySlot !== "legacy") { return; }
  var rows = (repos || []).filter(function (repo) { return repo && (repo.full_name || repo.name); });
  if (!rows.length) { return; }
  target.textContent = "";
  rows.forEach(function (repo) {
    var card = el("article", "ia-card");
    var name = String(repo.full_name || repo.name || "未命名 Repo");
    var heading = el("h3", "", name);
    var url = String(repo.html_url || repo.url || "");
    if (url) {
      heading.textContent = "";
      var link = document.createElement("a");
      link.href = url;
      link.target = "_blank";
      link.rel = "noreferrer noopener";
      link.textContent = name;
      heading.appendChild(link);
    }
    card.appendChild(heading);
    card.appendChild(el("p", "", String(repo.summary || repo.description || "GitHub AI 项目")));
    target.appendChild(card);
  });
}
function escapeSkillHtml(text) {
  return String(text || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}
function replacePlainToken(html, token, replacement) {
  var out = "";
  var rest = html;
  while (rest) {
    var at = rest.indexOf(token);
    if (at < 0) { return out + rest; }
    var before = rest.slice(0, at);
    var lastOpen = before.lastIndexOf("<a ");
    var lastClose = before.lastIndexOf("</a>");
    if (lastOpen > lastClose) {
      var end = rest.indexOf("</a>", at);
      if (end < 0) { return out + rest; }
      out += rest.slice(0, end + 4);
      rest = rest.slice(end + 4);
      continue;
    }
    out += before + replacement;
    rest = rest.slice(at + token.length);
  }
  return out;
}
function linkifySkillArticle(body, skills) {
  var html = escapeSkillHtml(body).replace(/\\n/g, "<br>");
  var rows = (skills || []).slice().sort(function (left, right) {
    return String((right && right.name) || "").length - String((left && left.name) || "").length;
  });
  var seen = {};
  rows.forEach(function (skill) {
    var name = String((skill && skill.name) || "");
    var url = skillPageUrl(skill);
    if (!name || !url || seen[name]) { return; }
    seen[name] = true;
    var token = escapeSkillHtml(name);
    html = replacePlainToken(
      html,
      token,
      '<a href="' + String(url).replace(/"/g, "") + '" target="_blank" rel="noreferrer noopener">' + token + "</a>"
    );
  });
  return html;
}
function renderSkillPackage(payload) {
  state.skillsPackage = payload || state.skillsPackage || {};
  var pack = state.skillsPackage;
  var visual = pack.visual || {};
  if (visual.theme) { setGithubTheme(visual.theme); }
  var savedPalette = visual.palette || {};
  if (savedPalette.main && $("gh-color-main")) { $("gh-color-main").value = savedPalette.main; }
  if (savedPalette.secondary && $("gh-color-secondary")) { $("gh-color-secondary").value = savedPalette.secondary; }
  if (savedPalette.accent && $("gh-color-accent")) { $("gh-color-accent").value = savedPalette.accent; }
  var skills = collectedSkills(pack);
  pack.stages = (pack.stages || []).filter(function (stage) { return !!stage; });
  pack.skills = skills;
  if (pack.visual && pack.visual.poster_template) { setPosterTemplate(pack.visual.poster_template, true); }
  renderSkillSourceChips(skills);
  // B1: a degraded source must be visible, not silent — GitHub results still
  // count, but the user should know one source did not answer.
  var degradedNote = $("skills-degraded-note");
  if (degradedNote) {
    var reasons = (pack.degraded_reasons || []).map(function (item) {
      return typeof item === "string" ? item : (item && (item.reason || item.source)) || "";
    }).filter(function (item) { return !!item; });
    var degraded = !!pack.degraded || reasons.length > 0;
    degradedNote.hidden = !degraded;
    degradedNote.textContent = degraded ? degradedSummary(reasons) : "";
    degradedNote.title = reasons.join("、");
  }
  var userTitle = String(pack.user_title || "");
  var titleInput = $("skill-title");
  if (titleInput && document.activeElement !== titleInput) { titleInput.value = userTitle; }
  var articleTitle = (pack.article && pack.article.title) || pack.title || "";
  var articleBody = (pack.article && pack.article.body) || "";
  // 标题框 is the title of the whole package: it titles the cover poster and it
  // titles the intro piece in the text area.
  $("skills-title").textContent = userTitle || articleTitle || "介绍文";
  var bodyNode = $("skills-body");
  var fallbackBody = "查找完成后会生成介绍文。按收录 skill 从左到右、从上到下说明。";
  if (articleBody) {
    bodyNode.innerHTML = linkifySkillArticle(articleBody, skills);
  } else {
    bodyNode.textContent = fallbackBody;
  }
  $("skills-article").classList.toggle("is-empty", !String(articleBody).trim());
  renderResourceResults(skills);
  renderSkillsGallery();
  var images = skillImages();
  $("skills-export-btn").disabled = !images.filter(function (image) { return image.selected !== false && image.url; }).length;
  $("skills-select-all").disabled = !images.length;
  $("skills-render-btn").disabled = !selectedSkills(pack).length;
  var coverBtn = $("skills-cover-btn");
  if (coverBtn) {
    coverBtn.disabled = !images.some(function (image) { return image.kind === "cover" && image.url; });
  }
  renderGithubTokenState(pack);
  if (waitRunning("skill_search")) { paintWait(); }
}
function queuedSkillImages(pack) {
  var coverTitle = (pack && (pack.user_title || pack.title)) || "Skill 查找结果";
  var tiles = [{id: "cover", kind: "cover", title: coverTitle, queued: true, selected: true, url: "", file: "cover.png"}];
  selectedSkills(pack).forEach(function (skill, index) {
    var rank = index + 1;
    var name = skill.name || skill.source_id || ("skill-" + rank);
    tiles.push({
      id: (rank < 10 ? "0" : "") + rank + "-" + name,
      kind: "poster",
      title: name,
      queued: true,
      selected: true,
      url: "",
      file: ""
    });
  });
  return tiles;
}
function renderSkillsGallery() {
  var gallery = $("skills-gallery");
  gallery.textContent = "";
  var pack = state.skillsPackage || {};
  var images = skillImages();
  if (!images.length && (pack.skills || []).length) {
    images = queuedSkillImages(pack);
    pack.images = images;
  }
  if (!images.length) {
    gallery.appendChild(el("p", "gh-pending", "查找完成后会自动出图。封面是主题大字报，项目卡按收录 skill 生成。"));
    paintCarouselNav("skills-gallery-nav", "skills-gallery-counter", 0, 0);
    return;
  }
  if (waitRunning("skill_render")) {
    images.forEach(function (image, pos) {
      if (image.url || image.busy) { state.skillsGalleryIndex = pos; }
    });
  }
  state.skillsGalleryIndex = clampCarouselIndex(state.skillsGalleryIndex || 0, images.length);
  var index = state.skillsGalleryIndex;
  var image = images[index];
  var extras = "";
  if (image.busy) { extras += " is-running"; }
  else if (image.queued) { extras += " is-queued"; }
  if (image.error) { extras += " is-error"; }
  if (image.selected !== false) { extras += " selected"; }
  var kindClass = image.kind === "cover" ? " gh-cover" : (image.kind === "poster" ? " gh-poster" : "");
  var tile = el("div", "gh-tile" + kindClass + extras);
  tile.dataset.imageId = image.id;
  var check = document.createElement("button");
  check.type = "button";
  check.className = "gh-check";
  check.textContent = image.selected === false ? "" : "✓";
  check.addEventListener("click", function (event) {
    event.stopPropagation();
    toggleSkillImage(image.id);
  });
  tile.appendChild(check);
  if (image.url) {
    var img = document.createElement("img");
    img.alt = image.title || image.id;
    img.src = githubFreshUrl(image.url);
    img.addEventListener("click", function () { openSkillsLightbox(index); });
    tile.appendChild(img);
  }
  if (image.busy) {
    tile.appendChild(el("p", "gh-pending gh-overlay", "正在生成…"));
  } else if (image.queued) {
    tile.appendChild(el("p", "gh-pending gh-overlay", "排队中"));
  } else if (image.error) {
    tile.appendChild(el("p", "gh-pending gh-overlay", "失败 · " + image.error));
  } else if (!image.url) {
    tile.appendChild(el("p", "gh-pending", "待出图 · " + (image.title || image.id)));
  }
  gallery.appendChild(tile);
  paintCarouselNav("skills-gallery-nav", "skills-gallery-counter", index, images.length);
}
// A poster tile stands for exactly one collected skill — the plan names it after
// the skill — and the render request is built from the collected skills, so a
// tile checkbox has to move that selection too. Toggling only the image used to
// leave 生成图片 with nothing to do but 「超过条目上限」.
function skillsForImage(image) {
  if (!image || image.kind !== "poster") { return []; }
  var title = String(image.title || "");
  if (!title) { return []; }
  return collectedSkills(state.skillsPackage || {}).filter(function (skill) {
    return String(skill.name || skill.source_id || "") === title;
  });
}
function toggleSkillImage(imageId) {
  var target = null;
  skillImages().forEach(function (image) {
    if (image.id === imageId) { target = image; }
  });
  if (!target) { return; }
  var next = target.selected === false;
  target.selected = next;
  skillsForImage(target).forEach(function (skill) { skill.selected = next; });
  renderSkillPackage(state.skillsPackage);
}
function selectAllSkillImages() {
  var images = skillImages();
  var allOn = images.length && images.every(function (image) { return image.selected !== false; });
  images.forEach(function (image) {
    image.selected = !allOn;
    skillsForImage(image).forEach(function (skill) { skill.selected = !allOn; });
  });
  renderSkillPackage(state.skillsPackage);
}
var skillTitleTimer = null;
var skillTitleInFlight = false;
var skillTitleDirty = false;
function saveSkillTitle() {
  var input = $("skill-title");
  if (!input) { return; }
  var title = String(input.value || "").trim();
  // The title is shown immediately (封面 + 文本区) and persisted right after, so a
  // reload keeps it without waiting for the next render.
  var pack = state.skillsPackage || {};
  pack.user_title = title;
  renderSkillPackage(pack);
  if (skillTitleInFlight) {
    // Never drop the tail of a fast edit: remember that the newest value still
    // needs a round trip and send it as soon as the current one lands.
    skillTitleDirty = true;
    return;
  }
  skillTitleInFlight = true;
  api("/api/skills/title", {title: title}).then(function (data) {
    // A response that lost the race must not repaint an older title over what
    // the user typed while it was in flight.
    if (!skillTitleDirty) { renderSkillPackage(data); }
  }).catch(function (error) {
    status("studio-status", "标题保存失败：" + (error.reason || error.message), "error");
  }).then(function () {
    skillTitleInFlight = false;
    if (skillTitleDirty) {
      skillTitleDirty = false;
      saveSkillTitle();
    }
  });
}
function scheduleSkillTitleSave() {
  if (skillTitleTimer) { window.clearTimeout(skillTitleTimer); }
  skillTitleTimer = window.setTimeout(function () {
    skillTitleTimer = null;
    saveSkillTitle();
  }, 450);
}
function clearSkills() {
  if (waitRunning("skill_search") || waitRunning("skill_render")) {
    status("studio-status", "正在查找或出图，等这一轮结束再清空", "error");
    return;
  }
  var button = $("skills-clear-btn");
  if (button) { busy(button, true); }
  api("/api/skills/clear", {}).then(function (data) {
    renderSkillPackage(data);
    status("studio-status", "已清空收录的 skill（标题保留）", "ok");
  }).catch(function (error) {
    status("studio-status", "清空失败：" + (error.reason || error.message), "error");
  }).then(function () {
    if (button) { busy(button, false); }
  });
}
function pasteSkills(links) {
  // 粘贴进来的 skill 与检索到的一样：先进包（拿阶段、写介绍文），再出海报。
  links = String(links || "").trim();
  if (!links) {
    status("studio-status", "请先在资源探索粘贴 skill 链接", "error");
    return;
  }
  if (links.length > 20000) {
    // 输入框不再截断，所以超长要在这里说清楚，而不是把尾巴上的链接切一半当成仓库名。
    status("studio-status", "粘贴内容过长（" + links.length + " 字，上限 20000），请分几次粘贴", "error");
    return;
  }
  if (waitRunning("skill_paste")) { return; }
  if (waitRunning("skill_render")) {
    status("studio-status", "正在出图，等这一轮出完再粘贴", "error");
    return;
  }
  if (waitRunning("skill_search")) {
    status("studio-status", "查找还在进行，等它结束再粘贴", "error");
    return;
  }
  waitStart("skill_paste");
  status("studio-status", "正在读取粘贴的 skill…");
  waitApi("skill_paste", "/api/skills/paste", {links: links}).then(function (data) {
    if (waitCancelled("skill_paste")) { return; }
    renderSkillPackage(data);
    var added = Number(data.added || 0);
    var replaced = Number(data.replaced || 0);
    var rejected = Number(data.rejected_count || (data.rejected || []).length);
    var unverified = (data.unverified || []).length;
    var count = (data.skills || []).length;
    if (!added && !replaced) {
      var empty = rejected ? ("没有可加入的链接，已跳过 " + rejected + " 条") : "没有识别到 skill 链接";
      status("studio-status", empty, "error");
      waitFail(empty, "skill_paste");
      return;
    }
    var parts = [];
    if (added) { parts.push("新增 " + added + " 个"); }
    if (replaced) { parts.push("替换 " + replaced + " 个"); }
    if (rejected) { parts.push("跳过 " + rejected + " 条"); }
    if (unverified) { parts.push("有 " + unverified + " 条没读到 SKILL.md，海报会用模板文案"); }
    var summary = parts.join("，") + "；当前共 " + count + " 个 skill";
    status("studio-status", summary + "，正在出图…", unverified ? "" : "ok");
    waitDone(summary, "skill_paste");
    renderSkillImages();
  }).catch(function (error) {
    if (isWaitAbort(error, "skill_paste")) { return; }
    var reason = error.reason || error.message || "加入失败";
    var code = String(error.message || "");
    if (code.indexOf("skill_search_running") >= 0) {
      reason = "查找还在进行，等它结束再粘贴";
    } else if (code.indexOf("skill_render_running") >= 0) {
      reason = "正在出图，等这一轮出完再粘贴";
    }
    status("studio-status", "加入失败：" + reason, "error");
    waitFail(reason, "skill_paste");
  }).then(function () {
    if (button) { busy(button, false); }
  });
}
function renderGithubTokenState(data) {
  var configured = !!(data && (data.github_token !== undefined ? data.github_token : data.configured));
  state.githubTokenConfigured = configured;
  // One token card, in Settings. The creation page used to duplicate it.
  var settingsBox = $("settings-github-status");
  if (settingsBox) {
    settingsBox.textContent = configured ? "已配置（Token 不回显）" : "未配置";
  }
}
function renderResearchRuntime(data) {
  var runtime = (data && data.research) || {};
  state.researchRuntime = runtime;
  var node = $("settings-runtime-summary");
  if (!node) { return; }
  var llm = (data && data.llm) || {};
  var chat = (data && data.chat_llm) || {};
  var translation = (data && data.translation_llm) || {};
  var leaderboard = (data && data.leaderboard_llm) || {};
  var items = [
    {label: "Planner", on: !!runtime.planner_configured,
     onText: "模型端点已配置", offText: "降级规则（未接模型）"},
    {label: "Jev", on: !!runtime.jev_configured,
     onText: "模型端点已配置", offText: "未配置（保留诊断）"},
    {label: "文章模型", on: !!(llm.configured && llm.enabled),
     onText: String(llm.model || "已配置"), offText: "未配置"},
    {label: "对话模型", on: !!chat.configured,
     onText: String(chat.model || "已配置"), offText: "未配置"},
    {label: "翻译模型", on: !!(translation.configured && translation.enabled),
     onText: String(translation.model || "已配置"), offText: "未配置"},
    {label: "榜单模型", on: !!(leaderboard.configured && leaderboard.enabled),
     onText: String(leaderboard.model || "已配置"), offText: "未配置"},
  ];
  node.textContent = "";
  var grid = el("div", "runtime-grid");
  items.forEach(function (item) {
    var cell = el("div", "runtime-item " + (item.on ? "is-on" : "is-off"));
    cell.appendChild(el("span", "runtime-label", item.label));
    var stateNode = el("span", "runtime-state");
    stateNode.appendChild(el("span", "status-dot " + (item.on ? "ok" : "never")));
    stateNode.appendChild(document.createTextNode(item.on ? item.onText : item.offText));
    cell.appendChild(stateNode);
    cell.title = item.label + "：" + (item.on ? item.onText : item.offText);
    grid.appendChild(cell);
  });
  node.appendChild(grid);
  var note = el("p", "page-description");
  note.style.margin = "0.75rem 0 0";
  note.textContent = "Planner 与 Jev 直接读取上方已保存的模型端点；「降级规则」表示这一路没有可用模型端点，研究会退回规则判定而不是假装已配置。模型密钥由钥匙串管理，不在这里回显。";
  node.appendChild(note);
}
function openSkillsLightbox(index) {
  var images = skillImages().filter(function (image) { return image.url; });
  if (!images.length) { return; }
  state.draft = state.draft || {assets: {candidates: []}};
  state.draft.assets = {
    schema: "article-assets-v2",
    candidates: images.map(function (image) {
      return {id: image.id, url: image.url, source_id: "skills", source_url: image.url};
    }),
    cover_id: "",
    body_ids: [],
  };
  var start = 0;
  images.forEach(function (image, pos) {
    if (skillImages()[index] && image.id === skillImages()[index].id) { start = pos; }
  });
  openImageLightbox(start);
}
function loadSkills() {
  status("studio-status", "正在读取技能包…");
  return fetch("/api/skills").then(function (response) { return response.json(); }).then(function (data) {
    renderSkillPackage(data);
    var count = (data.skills || []).length;
    status("studio-status", count ? "已收录 " + count + " 个 skill，点「生成图片」做汇总出图" : "还没有收录 skill。去「资源探索」输入关键词或粘贴 skill 链接，再点「送到创作页」。", count ? "ok" : "");
    return data;
  }).catch(function (error) {
    status("studio-status", "读取技能包失败：" + error.message, "error");
  });
}
function abortSkillSearch(query) {
  return api("/api/skills/search", {query: query || "", abort: true}).catch(function () { return null; });
}
function skillArticleReady(data) {
  return !!(data && data.article && String(data.article.body || "").trim());
}
// A search is finished once the backend publishes skills_ready/done: the intro is
// written *before* that phase, so waiting for an article body after it can never
// end. A zero-result keyword search used to spin here forever.
var SKILL_POLL_PHASES = {
  "searching": 1, "keywords": 1, "search": 1, "group": 1, "compose": 1, "rendering": 1,
  "understanding": 1, "chain": 1, "decompose": 1
};
// ~10 minutes of 350ms ticks. A phase stuck in-flight (a crashed search thread, an
// unreachable database) must end as a visible failure, not an endless spinner.
var SKILL_POLL_MAX_TICKS = 1800;
var skillPollTicks = 0;
function pollSkillSnapshot(query) {
  if (waitCancelled("skill_search")) {
    return abortSkillSearch(query);
  }
  skillPollTicks += 1;
  if (skillPollTicks > SKILL_POLL_MAX_TICKS) {
    return Promise.reject(new Error("查找一直没有结束，请重试；若反复出现请看服务日志"));
  }
  return fetch("/api/skills", {signal: waitSignal("skill_search")}).then(function (response) { return response.json(); }).then(function (data) {
    renderSkillPackage(data);
    var phase = data.phase || "";
    if (phase === "error") {
      var error = new Error(data.error_reason || data.error || "查找失败");
      throw error;
    }
    if (phase === "skills_ready" || phase === "done") { return data; }
    if (skillArticleReady(data)) {
      return data;
    }
    if (!phase || SKILL_POLL_PHASES[phase]) {
      return new Promise(function (resolve, reject) {
        window.setTimeout(function () { pollSkillSnapshot(query).then(resolve, reject); }, 350);
      });
    }
    return data;
  });
}
function searchSkills(query) {
  query = String(query || "").trim();
  if (!query) {
    status("studio-status", "请先在资源探索输入关键词", "error");
    return;
  }
  if (waitRunning("skill_search")) { return; }
  var button = $("skills-fetch-btn");
  if (button) { busy(button, true); }
  skillPollTicks = 0;
  waitStart("skill_search");
  status("studio-status", "正在按关键词去各来源抓取 skill…");
  waitApi("skill_search", "/api/skills/search", {query: query}).then(function (data) {
    if (waitCancelled("skill_search")) { return abortSkillSearch(query); }
    renderSkillPackage(data);
    var phase = data.phase || "";
    if (phase === "searching" || !phase || !skillArticleReady(data)) {
      return pollSkillSnapshot(query);
    }
    return data;
  }).then(function (data) {
    if (!data || waitCancelled("skill_search")) { return; }
    renderSkillPackage(data);
    var count = (data.skills || []).length;
    // 环节一「查找」到此结束：结果与介绍文落地，但绝不自动出图 —— 出图只由「生成图片」触发。
    queueSkillImagesForRender();
    var text = count ? ("已收录 " + count + " 个 skill，点「生成图片」做汇总出图") : "这些关键词没有抓到合格 skill，换个词或直接粘贴链接";
    var dropped = data.capped_dropped || [];
    if (dropped.length) {
      // Never let a skill vanish silently: say the package hit its cap and who
      // made room. 粘贴的 skill 不会被挤掉，让位的只会是检索到的。
      text += "；已达 " + (data.skill_cap || 16) + " 个上限，让位 " + dropped.length + " 个检索结果（" + dropped.slice(0, 3).join("、") + (dropped.length > 3 ? " 等" : "") + "）";
    }
    status("studio-status", text, count ? "ok" : "error");
    waitDone(count ? ("已收录 " + count + " 个 skill") : "没有找到合格 skill", "skill_search");
  }).catch(function (error) {
    if (isWaitAbort(error, "skill_search")) {
      abortSkillSearch(query);
      return;
    }
    status("studio-status", "查找失败：" + (error.reason || error.message), "error");
    waitFail(error.message || "查找失败", "skill_search");
  }).then(function () {
    if (button) { busy(button, false); }
  });
}
function queueSkillImagesForRender() {
  var pack = state.skillsPackage || {};
  if (!collectedSkills(pack).length) { return; }
  if ((pack.images || []).length) { return; }
  pack.images = queuedSkillImages(pack);
  renderSkillsGallery();
}
function renderSkillCoverOnly() {
  var pack = state.skillsPackage || {};
  if (!skillImages().some(function (image) { return image.kind === "cover" && image.url; })) {
    status("studio-status", "请先生成图片，再只更新封面", "error");
    return;
  }
  if (waitRunning("skill_render")) { return; }
  var button = $("skills-cover-btn");
  if (button) { busy(button, true); }
  waitStart("skill_render");
  var studioStatus = function (text, kind) {
    status("studio-status", text, kind);
  };
  studioStatus("正在只重新生成封面海报…");
  waitApi("skill_render", "/api/skills/render", {
    theme: state.githubTheme || "dark",
    palette: visualPalette(),
    poster_template: state.posterTemplate || "1",
    title: String((($("skill-title") || {}).value) || "").trim(),
    cover_only: true
  }).then(function (data) {
    if (waitCancelled("skill_render")) { return; }
    renderSkillPackage(data);
    studioStatus("封面海报已更新", "ok");
    waitDone("封面海报已更新", "skill_render");
  }).catch(function (error) {
    if (isWaitAbort(error, "skill_render")) { return; }
    var reason = error.reason || error.message || "封面更新失败";
    studioStatus("封面更新失败：" + reason, "error");
    waitFail(reason, "skill_render");
  }).then(function () {
    if (button) { busy(button, false); }
  });
}
function renderSkillImages() {
  var pack = state.skillsPackage || {};
  if (!collectedSkills(pack).length) {
    status("studio-status", "还没有收录 skill。去「资源探索」输入关键词或粘贴 skill 链接，再点「送到创作页」。", "error");
    return;
  }
  if (!selectedSkills(pack).length) {
    status("studio-status", "请先勾选要出图的 skill", "error");
    return;
  }
  if (selectedSkills(pack).length > 10) {
    status("studio-status", "超过条目上限", "error");
    return;
  }
  if (waitRunning("skill_render")) { return; }
  var button = $("skills-render-btn");
  if (button) { busy(button, true); }
  queueSkillImagesForRender();
  waitStart("skill_render");
  var studioStatus = function (text, kind) {
    status("studio-status", text, kind);
  };
  studioStatus("正在生成技能包封面和项目卡…");
  var poll = window.setInterval(function () {
    if (waitCancelled("skill_render")) { return; }
    fetch("/api/skills").then(function (response) { return response.json(); }).then(function (data) {
      if (data && (data.images || []).length) { renderSkillPackage(data); }
    }).catch(function () {});
  }, 400);
  waitApi("skill_render", "/api/skills/render", {
    theme: state.githubTheme || "dark",
    palette: visualPalette(),
    poster_template: state.posterTemplate || "1",
    title: String((($("skill-title") || {}).value) || "").trim(),
    selected_ids: selectedSkills(pack).map(skillIdentity)
  }).then(function (data) {
    if (waitCancelled("skill_render")) { return; }
    renderSkillPackage(data);
    var images = skillImages().filter(function (image) { return image.url; });
    studioStatus(images.length ? "已生成 " + images.length + " 张，勾选后可导出" : "没有生成图片", images.length ? "ok" : "error");
    waitDone(images.length ? ("已生成 " + images.length + " 张") : "没有生成图片", "skill_render");
  }).catch(function (error) {
    if (isWaitAbort(error, "skill_render")) { return; }
    var reason = error.reason || error.message || "出图失败";
    if (String(error.message || "").indexOf("skill_package_changed") >= 0) {
      reason = "包在出图期间被改了（新粘贴或新查找），请重新点「生成图片」";
    }
    // The queued tiles are the only place a failed render is visible in the
    // gallery: without this they keep saying 排队中 while the status line reports
    // the error.
    skillImages().forEach(function (image) {
      if (!image.url) {
        image.queued = false;
        image.busy = false;
        image.error = reason;
      }
    });
    renderSkillsGallery();
    studioStatus("出图失败：" + reason, "error");
    waitFail(reason, "skill_render");
  }).then(function () {
    window.clearInterval(poll);
    if (button) { busy(button, false); }
  });
}
function exportSkillImages() {
  var selected = skillImages().filter(function (image) { return image.selected !== false && image.url; });
  if (!selected.length) { return; }
  if (waitRunning("export")) { return; }
  var button = $("skills-export-btn");
  if (button) { busy(button, true); }
  waitStart("export", {current: 0, total: selected.length, unit: "张", step: "准备导出"});
  var chain = Promise.resolve();
  selected.forEach(function (image, index) {
    chain = chain.then(function () {
      if (waitCancelled("export")) { return; }
      waitProgress(index + 1, selected.length, image.title || image.id, "export");
      return fetch(image.url, {signal: waitSignal("export")}).then(function (response) { return response.blob(); }).then(function (blob) {
        var link = document.createElement("a");
        link.href = URL.createObjectURL(blob);
        link.download = (image.file || (image.id + ".png"));
        window.setTimeout(function () { link.click(); }, index * 220);
      });
    });
  });
  chain.then(function () {
    if (waitCancelled("export")) { return; }
    status("studio-status", "已按当前顺序导出 " + selected.length + " 张选中图片", "ok");
    waitDone("已导出 " + selected.length + " 张", "export");
  }).catch(function (error) {
    if (isWaitAbort(error, "export")) { return; }
    status("studio-status", "导出失败：" + error.message, "error");
    waitFail(error.message || "导出失败", "export");
  }).then(function () {
    if (button) { busy(button, false); }
  });
}
function fillPaper(ctx) {
  var gradient = ctx.createLinearGradient(0, 0, 0, 1920);
  gradient.addColorStop(0, "#f7f1e4");
  gradient.addColorStop(1, "#efe4d2");
  ctx.fillStyle = gradient;
  ctx.fillRect(0, 0, 1080, 1920);
  ctx.fillStyle = "rgba(92,68,38,0.05)";
  var y;
  for (y = 48; y < 1920; y += 8) {
    ctx.fillRect(0, y, 1080, 1);
  }
  ctx.fillStyle = "rgba(255,255,255,0.28)";
  ctx.fillRect(0, 0, 1080, 80);
}
function drawXhsCard(ctx, card) {
  if (card.kind === "cover" && !card.image_url) {
    ctx.fillStyle = "#3d2b1f";
    ctx.fillRect(0, 0, 1080, 1920);
    ctx.strokeStyle = "#ffffff";
    ctx.lineWidth = 12;
    ctx.strokeRect(48, 48, 984, 1824);
    wrapText(ctx, card.title || "", 96, 420, 888, 96, "#ffffff", '700 72px "Songti SC", "STSong", serif');
    if (card.lede) {
      wrapText(ctx, card.lede, 96, 1100, 888, 52, "#ffffff", "500 36px ui-sans-serif, system-ui, sans-serif");
    }
    return Promise.resolve();
  }
  fillPaper(ctx);
  var handle = card.handle || "@小红书号";
  ctx.fillStyle = "#d7c4a8";
  ctx.beginPath();
  ctx.arc(96, 88, 28, 0, Math.PI * 2);
  ctx.fill();
  ctx.fillStyle = "#5a4c3c";
  ctx.font = "700 22px ui-sans-serif, system-ui, sans-serif";
  ctx.fillText(handle.replace(/^@/, "").slice(0, 1) || "A", 88, 96);
  ctx.fillStyle = "#6a5b4a";
  ctx.font = "500 28px ui-sans-serif, system-ui, sans-serif";
  ctx.fillText(handle, 140, 98);
  if (card.kind === "cover") {
    ctx.fillStyle = "#2f291f";
    ctx.font = "700 64px ui-sans-serif, system-ui, sans-serif";
    wrapText(ctx, card.title || "", 72, 220, 936, 78, "#2f291f", "700 64px ui-sans-serif, system-ui, sans-serif");
    if (card.lede) {
      wrapText(ctx, card.lede, 72, 520, 936, 52, "#4a4034", "500 36px ui-sans-serif, system-ui, sans-serif");
    }
    return Promise.resolve();
  }
  wrapText(ctx, card.text || "", 72, 200, 936, 52, "#3c342b", "500 36px ui-sans-serif, system-ui, sans-serif");
  return Promise.resolve();
}
function exportXhsImages() {
  var cards = (state.draft && state.draft.cards) || [];
  if (!cards.length) { return; }
  var canvas = $("xhs-canvas");
  canvas.width = 1080;
  canvas.height = 1920;
  var ctx = canvas.getContext("2d");
  var button = $("xhs-export-btn");
  if (button) { busy(button, true); }
  waitStart("export", {current: 0, total: cards.length, unit: "张", step: "准备导出"});
  var cover = cards[0] && cards[0].image_url ? loadGithubImage(cards[0].image_url) : Promise.resolve(null);
  cover.then(function (coverImage) {
    var chain = Promise.resolve();
    cards.forEach(function (card, index) {
      chain = chain.then(function () {
        if (waitCancelled("export")) { return; }
        waitProgress(index + 1, cards.length, card.kind || ("第 " + (index + 1) + " 张"), "export");
        return new Promise(function (resolve) {
          window.setTimeout(function () {
            drawXhsCard(ctx, card);
            if (card.kind === "cover" && coverImage) {
              var width = 936;
              var height = 1100;
              ctx.save();
              roundedRect(ctx, 72, 520, width, height, 28);
              ctx.clip();
              ctx.drawImage(coverImage, 72, 520, width, height);
              ctx.restore();
            }
            downloadCanvas(canvas, "xhs-" + String(index + 1).padStart(2, "0") + ".png");
            resolve();
          }, 220);
        });
      });
    });
    return chain;
  }).then(function () {
    if (waitCancelled("export")) { return; }
    status("article-status", "已导出 " + cards.length + " 张小红书图片", "ok");
    waitDone("已导出 " + cards.length + " 张", "export");
  }).catch(function (error) {
    if (isWaitAbort(error, "export")) { return; }
    status("article-status", "导出失败：" + error.message, "error");
    waitFail(error.message || "导出失败", "export");
  }).then(function () {
    if (button) { busy(button, false); }
  });
}
function applyNewsScope() {
  var select = $("news-scope-select");
  if (select) { select.value = state.newsScope || "all"; }
}

function setNewsScope(scope) {
  var next = String(scope || "all");
  if (next !== "official" && next !== "community" && next !== "all") { next = "all"; }
  if (state.newsScope === next) { return; }
  state.newsScope = next;
  state.collectGen += 1;
  applyNewsScope();
  // A scope change re-reads the same window; it never re-collects sources.
  clearHotspotSelection(true);
  loadHotspots().then(function (data) {
    return widenIfEmpty(data || {}, currentHours());
  });
}
document.querySelectorAll("[data-app-page-target]").forEach(function (button) {
  button.addEventListener("click", function () { setAppPage(button.dataset.appPageTarget); });
});
$("home-open-runs").addEventListener("click", function () { setAppPage("runs"); });
$("home-search-btn").addEventListener("click", function () {
  var query = String($("home-query").value || "").trim();
  var resource = /资源|repo|skill|tool|website|工具/i.test(query);
  if (resource) {
    $("resource-query").value = query;
    setAppPage("resources");
    status("resource-status", query ? "已准备资源探索：" + query : "请输入想找的资源", query ? "ok" : "error");
  } else {
    setAppPage("news");
    status("hotspot-status", query ? "资讯探索已打开：" + query : "资讯探索已打开", query ? "ok" : "");
  }
});
$("resource-search-btn").addEventListener("click", function () {
  discoverResources(false);
});
$("resource-weekly-btn").addEventListener("click", function () {
  discoverResources(true);
});
$("resource-studio-btn").addEventListener("click", function () { sendResourcesToStudio(); });
$("resource-weekly-studio-btn").addEventListener("click", function () { openWeeklyStudio(); });
$("github-fetch-btn").addEventListener("click", function () { fetchGithubWeekly(); });
$("skills-fetch-btn").addEventListener("click", function () {
  status("studio-status", "正在读取上次收录的 Skill…", "");
  loadSkills().then(function (data) {
    var count = data && Array.isArray(data.skills) ? data.skills.length : 0;
    status("studio-status", count ? ("已读取 " + count + " 个 skill") : "还没有收录的 skill，请先去资源探索查找或粘贴链接", count ? "ok" : "error");
  });
});
$("resource-kind-filter").addEventListener("change", function () {
  if (state.resourceSet) { renderResourceSet(state.resourceSet); }
});
$("source-refresh-all").addEventListener("click", function () {
  setAppPage("news");
  setNewsScope("all");
  collect();
});
$("source-open-runs").addEventListener("click", function () { setAppPage("runs"); });
// Sources-page filtering is client-side only: the registry is already loaded,
// so filtering never re-fetches and never invents a different source set.
document.querySelectorAll("[data-source-filter]").forEach(function (chip) {
  chip.addEventListener("click", function () {
    state.sourceFilter = chip.dataset.sourceFilter || "all";
    document.querySelectorAll("[data-source-filter]").forEach(function (other) {
      var on = other === chip;
      other.classList.toggle("is-on", on);
      other.setAttribute("aria-pressed", String(on));
    });
    renderSourceRegistry(state.sources || []);
  });
});
$("source-search").addEventListener("input", function () {
  state.sourceQuery = $("source-search").value || "";
  renderSourceRegistry(state.sources || []);
});
$("publishing-open-creation").addEventListener("click", function () { setAppPage("creation"); });
$("publishing-refresh-btn").addEventListener("click", function () {
  loadPublishingDrafts().catch(function (error) {
    status("publishing-summary", "读取草稿失败：" + error.message, "error");
  });
});
$("publishing-open-settings").addEventListener("click", function () {
  setAppPage("settings");
  openSettings(true);
});
$("settings-open-model").addEventListener("click", function () {
  setAppPage("settings");
  openSettings(false);
  setSettingsPage("model");
});
$("settings-open-accounts").addEventListener("click", function () {
  setAppPage("settings");
  openSettings(false);
  setSettingsPage("accounts");
});
$("settings-open-research").addEventListener("click", function () {
  setAppPage("news");
  status("hotspot-status", "请选择一个或多个资讯事件后开始研究", "");
});
$("settings-open-sources").addEventListener("click", function () { setAppPage("sources"); });
$("seg-wechat").addEventListener("click", function () { setPlatform("wechat"); });
$("seg-xhs").addEventListener("click", function () { setPlatform("xiaohongshu"); });
$("open-original-btn").addEventListener("click", function () { openOriginal(); });
$("research-btn").addEventListener("click", runResearch);
$("research-run-btn").addEventListener("click", runResearch);
$("research-back-news").addEventListener("click", function () { setAppPage("news"); });
$("research-to-creation-btn").addEventListener("click", function () {
  setArticleContextFromDossier(state.research.dossier);
});
$("article-context-clear").addEventListener("click", clearArticleContext);
$("news-scope-select").addEventListener("change", function () {
  setNewsScope($("news-scope-select").value);
});
$("original-translate").addEventListener("click", translateOriginal);
$("original-close").addEventListener("click", closeOriginal);
$("original-dialog").addEventListener("cancel", closeOriginal);
$("generate-btn").addEventListener("click", generate);
$("save-article-btn").addEventListener("click", saveArticle);
$("image-rescan").addEventListener("click", rescanImages);
$("lightbox-close").addEventListener("click", closeImageLightbox);
$("lightbox-prev").addEventListener("click", function () { stepImageLightbox(-1); });
$("lightbox-next").addEventListener("click", function () { stepImageLightbox(1); });
$("lightbox-image").addEventListener("click", onLightboxImageClick);
$("image-lightbox").addEventListener("click", function (event) {
  if (event.target === $("image-lightbox") || (event.target.classList && event.target.classList.contains("lightbox-stage"))) {
    closeImageLightbox();
  }
});
$("image-lightbox").addEventListener("cancel", function () { closeImageLightbox(); });
document.addEventListener("keydown", function (event) {
  if (!$("image-lightbox").open) { return; }
  if (event.key === "Escape") { event.preventDefault(); closeImageLightbox(); }
  if (event.key === "ArrowLeft") { event.preventDefault(); stepImageLightbox(-1); }
  if (event.key === "ArrowRight") { event.preventDefault(); stepImageLightbox(1); }
});
document.querySelectorAll("[data-template]").forEach(function (node) {
  node.addEventListener("click", function () { setPosterTemplate(node.getAttribute("data-template")); });
});
["article-title", "article-body"].forEach(function (id) {
  if (id === "article-body") {
    $(id).addEventListener("beforeinput", captureArticleInputAnchor);
    $(id).addEventListener("keydown", function (event) {
      if (event.key.length === 1 || event.key === "Backspace" || event.key === "Delete" || event.key === "Enter") {
        captureArticleInputAnchor();
      }
    });
  }
  $(id).addEventListener("input", function () {
    if (id === "article-body") { fitArticleBody(); }
    if (!state.draft) { return; }
    state.articleDirty = true;
    $("publish-btn").disabled = false;
    $("schedule-btn").disabled = false;
    status("article-status", "有未保存的修改");
  });
});
$("article-model").addEventListener("change", function (event) { changeMainModel("article", event); });
$("publish-btn").addEventListener("click", publish);
$("schedule-btn").addEventListener("click", schedulePublish);
$("creator-btn").addEventListener("click", function () {
  if (state.creatorUrl) { window.open(state.creatorUrl, "_blank", "noopener"); }
});
$("config-btn").addEventListener("click", function () {
  setAppPage("settings");
  openSettings(false);
});
$("favorites-btn").addEventListener("click", function () { toggleFavoritesDrawer(); });
$("favorites-close").addEventListener("click", function () { toggleFavoritesDrawer(false); });
if ($("article-paste")) {
  $("article-paste").addEventListener("input", function () {
    var box = $("article-paste");
    box.style.height = "40px";
    box.style.height = Math.min(Math.max(box.scrollHeight, 40), 192) + "px";
  });
}
$("endpoint-connect").addEventListener("click", connectEndpoint);
$("endpoint-use-article").addEventListener("click", function () { assignEndpoint("article"); });
$("endpoint-use-translation").addEventListener("click", function () { assignEndpoint("translation"); });
$("endpoint-use-leaderboard").addEventListener("click", function () { assignEndpoint("leaderboard"); });
$("provider-add").addEventListener("click", function () { openProviderEditor("add", null); });
$("provider-apply").addEventListener("click", applyProvider);
$("provider-cancel").addEventListener("click", closeProviderEditor);
$("provider-remove").addEventListener("click", function () {
  deleteEndpoint($("config-form").dataset.endpointId || "");
});
$("config-form").elements.article_provider.addEventListener("change", function () { onPurposeProviderChange("article"); });
$("config-form").elements.translation_provider.addEventListener("change", function () { onPurposeProviderChange("translation"); });
$("config-form").elements.leaderboard_provider.addEventListener("change", function () { onPurposeProviderChange("leaderboard"); });
$("github-export-btn").addEventListener("click", exportGithubImages);
$("github-select-all").addEventListener("click", selectAllGithubImages);
$("github-render-btn").addEventListener("click", renderGithubWeeklyImages);
$("skills-clear-btn").addEventListener("click", clearSkills);
$("skill-title").addEventListener("input", scheduleSkillTitleSave);
$("skill-title").addEventListener("change", saveSkillTitle);
$("settings-save-github-token").addEventListener("click", function () {
  var input = $("settings-github-token");
  var token = String(input && input.value || "").trim();
  if (!token) {
    status("settings-github-status", "请先输入 Token", "error");
    return;
  }
  api("/api/skills/github-token", {token: token}).then(function (data) {
    if (input) { input.value = ""; }
    renderGithubTokenState(data);
    status("settings-github-status", "GitHub Token 已保存（仅显示配置状态）", "ok");
  }).catch(function (error) {
    status("settings-github-status", "保存失败：" + (error.reason || error.message), "error");
  });
});
$("settings-clear-github-token").addEventListener("click", function () {
  api("/api/skills/github-token", {token: ""}).then(function (data) {
    renderGithubTokenState(data);
    status("settings-github-status", "GitHub Token 已清除", "ok");
  }).catch(function (error) {
    status("settings-github-status", "清除失败：" + (error.reason || error.message), "error");
  });
});
$("skills-render-btn").addEventListener("click", renderSkillImages);
$("skills-cover-btn").addEventListener("click", renderSkillCoverOnly);
$("skills-select-all").addEventListener("click", selectAllSkillImages);
$("skills-export-btn").addEventListener("click", exportSkillImages);
$("gh-theme-dark").addEventListener("click", function () { setGithubTheme("dark"); });
$("gh-theme-light").addEventListener("click", function () { setGithubTheme("light"); });
$("xhs-export-btn").addEventListener("click", exportXhsImages);
$("github-copy-btn").addEventListener("click", copyGithubXhs);
$("wechat-delete").addEventListener("click", function () { deleteAccount("wechat"); });
$("xhs-delete").addEventListener("click", function () { deleteAccount("xiaohongshu"); });
$("x-delete").addEventListener("click", function () { deleteAccount("x"); });
$("config-cancel").addEventListener("click", function () {
  state.pendingPublish = false;
  $("config-dialog").close();
});
$("config-dialog").addEventListener("cancel", function () { state.pendingPublish = false; });
document.querySelectorAll("[data-settings-platform]").forEach(function (node) {
  node.addEventListener("click", function () {
    setSettingsPlatform(node.dataset.settingsPlatform);
  });
});
document.querySelectorAll("[data-settings-page]").forEach(function (node) {
  node.addEventListener("click", function () { setSettingsPage(node.dataset.settingsPage); });
});
$("style-file").addEventListener("change", function (event) {
  var file = event.target.files && event.target.files[0];
  if (!file) { return; }
  var reader = new FileReader();
  reader.addEventListener("load", function () {
    $("config-form").elements.style.value = String(reader.result || "");
  });
  reader.readAsText(file);
});
$("config-form").addEventListener("submit", function (event) {
  event.preventDefault();
  var form = event.target;
  var button = $("config-save");
  var resumePublish = state.pendingPublish;
  busy(button, true);
  waitStart("save");
  var accountPayload = null;
  if (state.settingsPlatform === "wechat" && (form.app_id.value || form.app_secret.value || form.thumb_media_id.value)) {
    accountPayload = {
      platform: "wechat",
      app_id: form.app_id.value.trim(),
      app_secret: form.app_secret.value,
      thumb_media_id: form.thumb_media_id.value.trim(),
      author: form.author.value.trim(),
    };
  } else if (state.settingsPlatform === "xiaohongshu") {
    accountPayload = {
      platform: "xiaohongshu",
      account_name: form.account_name.value.trim(),
      creator_url: form.creator_url.value.trim(),
    };
  } else if (state.settingsPlatform === "x" && form.x_bearer_token.value) {
    accountPayload = {
      platform: "x",
      bearer_token: form.x_bearer_token.value,
    };
  }
  waitApi("save", "/api/style", {style: form.elements.style.value}).then(function (data) {
    state.style = data.style;
    return accountPayload ? waitApi("save", "/api/config", accountPayload) : data;
  }).then(function () {
    if (state.settingsPlatform === "xiaohongshu" && accountPayload) {
      state.xiaohongshu.accountName = accountPayload.account_name;
      state.xiaohongshu.creatorUrl = accountPayload.creator_url;
    }
    if (state.settingsPlatform === "wechat" && accountPayload) {
      state.platforms.wechat = {configured: true};
    }
    if (state.settingsPlatform === "x" && accountPayload) {
      state.platforms.x = {configured: true};
    }
    form.app_id.value = "";
    form.app_secret.value = "";
    form.thumb_media_id.value = "";
    form.author.value = "";
    form.account_name.value = "";
    form.creator_url.value = "";
    form.x_bearer_token.value = "";
    renderAccountPanels();
    state.pendingPublish = false;
    status("config-error", accountPayload ? "账号已保存" : "写作风格已保存", "ok");
    status("publish-status", "");
    waitDone(accountPayload ? "账号已保存" : "写作风格已保存", "save");
    if (resumePublish) {
      $("config-dialog").close();
      publish();
    }
  }).catch(function (error) {
    if (isWaitAbort(error, "save")) { return; }
    status("config-error", showApiError("保存失败：", error), "error");
    waitFail(error.message || "保存失败", "save");
  }).then(function () {
    busy(button, false);
  });
});

fetch("/api/status").then(function (response) { return response.json(); }).then(function (data) {
  if (data.llm) {
    state.llm = data.llm;
    state.style = data.style || "";
    var form = $("config-form");
    form.effort.value = data.llm.effort || "none";
    form.elements.style.value = state.style;
  }
  state.modelEndpoints = data.model_endpoints || [];
  renderResearchRuntime(data);
  renderGithubTokenState({configured: data.github_token_configured});
  if (data.translation_llm) {
    state.translation = data.translation_llm;
  }
  if (data.leaderboard_llm) {
    state.leaderboard = data.leaderboard_llm;
  }
  renderModelPicker();
  fillPurposePickers();
  renderEndpointLists();
  if (data.platforms) { state.platforms = data.platforms; }
  if (data.platforms && data.platforms.xiaohongshu) {
    state.xiaohongshu.accountName = data.platforms.xiaohongshu.account_name || "";
    state.xiaohongshu.creatorUrl = data.platforms.xiaohongshu.creator_url || state.xiaohongshu.creatorUrl;
  }
  renderAccountPanels();
  if (data.version) { $("service-status").textContent = "v" + data.version + " · 本地服务"; }
  // Prefer the DB-backed registry here too: the legacy ``sources`` list is the
  // collectable subscription subset, so using it would hide community sources
  // and every failing row.
  if (data.source_registry || data.sources) {
    renderSourceHealth(data.source_registry || data.sources);
  }
  if (data.collection_run || data.run) { renderCollectionRun(data.collection_run || data.run); }
  loadFavorites();
  // The home card reports the latest persisted run, so it must not wait for the
  // runs page to be opened before it says anything.
  loadCollectionRuns().catch(function () {});
  loadHotspots().then(function (hotspots) {
    return widenIfEmpty(hotspots || {}, currentHours());
  });
}).catch(function () {
  loadFavorites();
  loadHotspots().then(function (hotspots) {
    return widenIfEmpty(hotspots || {}, currentHours());
  });
});
</script>
</body>
</html>
"""


class WebApp:
    def __init__(
        self,
        database: Database,
        sources,
        packages_dir: str | Path,
        fetcher=None,
        wechat_base_url: str | None = None,
        wechat_opener=None,
        llm_service: LLMService | None = None,
        llm_runner=None,
        credential_store=None,
        page_fetcher=None,
        translation_service: LLMService | None = None,
        github_fetcher=None,
        leaderboard_service: LLMService | None = None,
        visual_renderer=None,
        render_fetcher=None,
        resource_discovery=None,
    ) -> None:
        self.database = database
        self.packages_dir = Path(packages_dir)
        source_tuple = tuple(load_sources(sources)) if isinstance(sources, (str, Path)) else tuple(sources)
        self.llm_service = llm_service if llm_service is not None else LLMService(database, runner=llm_runner)
        self._article_model_configured = llm_service is not None
        self.article_models = (
            [self.llm_service.settings.model] if self._article_model_configured else []
        )
        # Chat uses its own in-memory settings; the default runner only ever
        # talks to the configured OpenAI-compatible base_url.
        self.chat_service = LLMService(database)
        self.translation_service = translation_service or LLMService(database)
        self._translation_model_configured = translation_service is not None
        self.leaderboard_service = leaderboard_service or LLMService(database)
        self._leaderboard_model_configured = leaderboard_service is not None
        self.visual_renderer = visual_renderer
        self.github_fetcher = github_fetcher
        self.render_fetcher = render_fetcher or playwright_render_fetcher
        self.skill_complete = None
        self.skill_screenshot = None
        self.skill_web_search = None
        self.credential_store = (
            credential_store if credential_store is not None else MacOSKeychainCredentialStore()
        )
        self._restore_model_selections()
        self.hotspots = HotspotService(
            database,
            source_tuple,
            fetcher=fetcher,
            llm_service=(self.translation_service if self._translation_model_configured else None),
        )
        # Keep the latest radar event clusters available to the research
        # boundary. They are derived UI data, not subscription sources or
        # durable events, so the resolver falls back to the DB for persisted
        # article events.
        self._hotspot_payloads: dict[str, dict[str, object]] = {}
        self._radar_events: dict[str, dict[str, object]] = {}
        self.workbench = Workbench(
            database,
            llm_service=self.llm_service,
            page_fetcher=default_page_fetcher if page_fetcher is None else page_fetcher,
            render_fetcher=self.render_fetcher,
            event_resolver=self._resolve_research_event,
        )
        # Radar and research share the same objective/Jev service. The
        # HotspotService remains the owner of collection/ranking, while the
        # Workbench owns the research boundary and its persistence.
        self._refresh_research_runtime()
        self._resource_memory_repository = _MemoryResourceSetRepository()
        self.resource_discovery = resource_discovery or self._build_resource_discovery()
        self.workflow = ContentWorkflow(database)
        # ponytail: one local lock prevents duplicate platform submissions;
        # use per-account locks only if publish throughput becomes relevant.
        self._publish_lock = threading.Lock()
        self.wechat_config: dict[str, str] | None = None
        self.xiaohongshu_config: dict[str, str] = {
            "creator_url": XIAOHONGSHU_CREATOR_URL,
            "account_name": "",
        }
        self._wechat_base_url = wechat_base_url
        self._wechat_opener = wechat_opener
        self._wechat_poster_renderer: object = None
        self.article_style = ""
        self._original_text_cache: dict[str, dict[str, object]] = {}
        self.x_bearer_token = ""
        self.scheduler = PublishScheduler(database, self.publish, interval_seconds=15)
        self._restore_accounts()
        self._restore_github_token()

    def _resource_repository(self) -> object:
        """Use the DB v4 resource seam when present, otherwise keep loopback usable."""
        required = ("save_resource_set", "get_resource_set", "list_resource_sets")
        if all(callable(getattr(self.database, name, None)) for name in required):
            return self.database
        return self._resource_memory_repository

    def _resource_github_adapter(self, query: str, *, limit: int) -> dict[str, object]:
        getter = self.github_fetcher or default_github_get
        url = "https://api.github.com/search/repositories?" + urlencode(
            {"q": query, "sort": "stars", "order": "desc", "per_page": str(limit)}
        )
        payload = getter(url)
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            return {"results": [], "degraded": True, "cause_code": "invalid_response"}
        return {"results": [dict(item, source="github", kind="repo") for item in payload["items"] if isinstance(item, dict)]}

    def _resource_skill_adapter(self, query: str, *, limit: int) -> dict[str, object]:
        getter = self.github_fetcher or default_github_get
        rows: list[dict[str, object]] = []
        rows.extend(_github_search(f"SKILL.md {query}", getter)[:limit])
        rows.extend(_skills_sh_search(query, getter)[:limit])
        return {"results": rows[:limit]}

    def _resource_web_adapter(self, query: str, *, limit: int) -> dict[str, object]:
        searcher = self.skill_web_search
        if callable(searcher):
            result = searcher(query)
            return result if isinstance(result, dict) else {"results": list(result or [])}
        # Reuse the bounded related-news engine through its one bridge — the
        # same adapter the research path uses — so its log/credential boundary
        # is honoured here too and upstream payloads stay out of the ResourceSet
        # diagnostics.  The adapter degrades instead of raising.
        from .research.adapters import RelatedNewsSearchAdapter

        response = RelatedNewsSearchAdapter(fetcher=self.workbench.page_fetcher).search(
            query, adapters=("related_search",), limit=limit
        )
        rows = [dict(item, source="web", kind="website") for item in response.results]
        return {
            "results": rows,
            "degraded": bool(response.degraded),
            "cause_code": response.cause_code,
        }

    def _build_resource_discovery(self) -> ResourceDiscoveryService:
        # 3.2.2: the discovery planner/decision service must be the SAME objects
        # the research service uses. Omitting them made ResourceDiscoveryService
        # default to a fresh DecisionService() with an empty JevAdapter, which
        # reported not_configured forever no matter how the models were set up.
        research = getattr(self.workbench, "research_service", None)
        return ResourceDiscoveryService(
            github_adapter=self._resource_github_adapter,
            skill_adapter=self._resource_skill_adapter,
            web_adapter=self._resource_web_adapter,
            planner=getattr(research, "planner", None),
            decision_service=getattr(research, "decision_service", None),
            repository=self._resource_repository(),
        )

    @staticmethod
    def _resource_set_payload(resource_set: object) -> dict[str, object]:
        if hasattr(resource_set, "to_dict"):
            payload = resource_set.to_dict()
        else:
            payload = resource_set
        if not isinstance(payload, dict):
            raise ResourceDiscoveryError("resource_set_invalid")
        return {"ok": True, "resource_set": payload}

    def discover_resources(self, payload: object) -> dict[str, object]:
        if not isinstance(payload, dict):
            raise ValueError("resource payload must be a JSON object")
        query = payload.get("query", "")
        if query is not None and not isinstance(query, str):
            raise ValueError("query must be a string")
        pasted_text = payload.get("pasted_text", "")
        if not isinstance(pasted_text, str):
            raise ValueError("pasted_text must be a string")
        if len(pasted_text) > MAX_PASTE_CHARS:
            raise ValueError(f"pasted_text must be at most {MAX_PASTE_CHARS} characters")
        source_event_ids = payload.get("source_event_ids", ())
        if not isinstance(source_event_ids, (list, tuple)) or any(
            not isinstance(item, str) for item in source_event_ids
        ):
            raise ValueError("source_event_ids must be a list of strings")
        adapters = payload.get("adapters")
        if adapters is not None:
            if not isinstance(adapters, (list, tuple)) or any(
                not isinstance(item, str) for item in adapters
            ):
                raise ValueError("adapters must be a list of strings")
            if not adapters:
                raise ValueError("adapters must not be empty")
        limit = payload.get("limit")
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100
        ):
            raise ValueError("limit must be between 1 and 100")
        persist = payload.get("persist", True)
        if not isinstance(persist, bool):
            raise ValueError("persist must be boolean")
        preset = payload.get("preset", "")
        if not isinstance(preset, str):
            raise ValueError("preset must be a string")
        kwargs = {
            "pasted_text": pasted_text,
            "source_event_ids": source_event_ids,
            "adapters": adapters or ("github", "skills", "web"),
            "limit": limit,
            "persist": persist,
        }
        if preset.strip():
            result = self.resource_discovery.discover_preset(preset, **kwargs)
        else:
            if not isinstance(query, str) or not query.strip():
                raise ValueError("query must be 1-200 characters")
            result = self.resource_discovery.discover(query, **kwargs)
        return self._resource_set_payload(result)

    def list_resource_sets(self, limit: int = 20) -> dict[str, object]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        repository = getattr(self.resource_discovery, "repository", None)
        if repository is None or not callable(getattr(repository, "list_resource_sets", None)):
            raise ResourceDiscoveryError("resource_repository_not_configured")
        rows = repository.list_resource_sets(limit)
        return {
            "ok": True,
            "resource_sets": [
                item.to_dict() if hasattr(item, "to_dict") else dict(item)
                for item in rows
                if isinstance(item, dict) or hasattr(item, "to_dict")
            ],
        }

    def get_resource_set(self, set_id: str) -> dict[str, object]:
        return self._resource_set_payload(self.resource_discovery.restore(set_id))

    def refresh_resource_set(self, set_id: str) -> dict[str, object]:
        return self._resource_set_payload(self.resource_discovery.refresh(set_id))

    def delete_resource_set(self, set_id: str) -> dict[str, object]:
        repository = getattr(self.resource_discovery, "repository", None)
        if repository is None or not callable(getattr(repository, "delete_resource_set", None)):
            raise ResourceDiscoveryError("resource_repository_not_configured")
        return {"ok": bool(repository.delete_resource_set(set_id)), "set_id": set_id}

    def _restore_github_token(self) -> None:
        """Hand the saved GitHub token to the HTTP layer; the value stays secret."""
        try:
            token = self.credential_store.get(GITHUB_TOKEN_STORE_ID) or ""
        except Exception:
            token = ""
        self.github_token_configured = bool(token.strip())
        # None (not "") so an unset keychain entry still lets GITHUB_TOKEN apply.
        set_github_token(token.strip() or None)

    def _restore_model_selections(self) -> None:
        """Restore saved model-purpose selections without network calls."""
        for purpose, service in (
            ("article", self.llm_service),
            ("translation", self.translation_service),
            ("chat", self.chat_service),
            ("leaderboard", self.leaderboard_service),
        ):
            mapping = self.database.get_model_purpose(purpose)
            if mapping is None:
                continue
            endpoint = self.database.get_model_endpoint(str(mapping["endpoint_id"]))
            api_key = (
                self.credential_store.get(str(mapping["endpoint_id"])) if endpoint is not None else None
            )
            if endpoint is None or not api_key:
                continue
            service.configure(
                model=str(mapping["model"]),
                effort=str(mapping["effort"]),
                base_url=str(endpoint["base_url"]),
                api_key=api_key,
            )
            if purpose == "article":
                self._article_model_configured = True
                try:
                    models = json.loads(str(endpoint["models_json"]))
                except json.JSONDecodeError:
                    models = []
                if isinstance(models, list):
                    self.article_models = [model for model in models if isinstance(model, str)]
            elif purpose == "translation":
                self._translation_model_configured = True
            elif purpose == "leaderboard":
                self._leaderboard_model_configured = True

    def _account_payload(self, platform: str) -> dict[str, str] | None:
        raw = self.credential_store.get(ACCOUNT_STORE_IDS[platform])
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def _store_account(self, platform: str, payload: dict[str, str]) -> None:
        self.credential_store.set(
            ACCOUNT_STORE_IDS[platform], json.dumps(payload, ensure_ascii=False, sort_keys=True)
        )

    def _restore_accounts(self) -> None:
        wechat = self._account_payload("wechat")
        if wechat and wechat.get("app_id") and wechat.get("app_secret"):
            self.wechat_config = {
                "app_id": str(wechat["app_id"]),
                "app_secret": str(wechat["app_secret"]),
                "thumb_media_id": str(wechat.get("thumb_media_id") or ""),
                "author": str(wechat.get("author") or ""),
            }
        xhs = self._account_payload("xiaohongshu")
        if xhs:
            name = str(xhs.get("account_name") or "")
            url = str(xhs.get("creator_url") or XIAOHONGSHU_CREATOR_URL)
            if url.startswith("https://"):
                self.xiaohongshu_config = {"account_name": name, "creator_url": url}
        x_token = self._account_payload("x")
        if x_token and x_token.get("bearer_token"):
            self.x_bearer_token = str(x_token["bearer_token"])
            self.hotspots.x_bearer_token = self.x_bearer_token

    def _model_endpoints(self) -> list[dict[str, object]]:
        endpoints: list[dict[str, object]] = []
        for row in self.database.list_model_endpoints():
            try:
                models = json.loads(str(row["models_json"]))
            except json.JSONDecodeError:
                models = []
            endpoints.append(
                {
                    "id": str(row["endpoint_id"]),
                    "base_url": str(row["base_url"]),
                    "models": [model for model in models if isinstance(model, str)],
                    "credential_available": bool(
                        self.credential_store.get(str(row["endpoint_id"]))
                    ),
                }
            )
        return endpoints

    def _resolve_endpoint_credentials(self, payload: dict, purpose: str) -> tuple[str, str]:
        """Resolve base_url/api_key from an endpoint_id or full credentials."""
        base_url = payload.get("base_url")
        api_key = payload.get("api_key")
        if isinstance(base_url, str) and isinstance(api_key, str) and api_key.strip():
            return base_url.strip().rstrip("/"), api_key
        endpoint_id = payload.get("endpoint_id")
        if isinstance(endpoint_id, str) and endpoint_id.strip():
            endpoint = self.database.get_model_endpoint(endpoint_id.strip())
            if endpoint is None:
                raise ValueError("endpoint not found")
            api_key = self.credential_store.get(endpoint_id.strip())
            if not api_key:
                raise ValueError("credential unavailable for endpoint")
            return str(endpoint["base_url"]), api_key
        if not isinstance(base_url, str) or not isinstance(api_key, str):
            raise ValueError("base_url and api_key are required")
        normalized_url = base_url.strip().rstrip("/")
        services = {
            "article": self.llm_service,
            "translation": self.translation_service,
            "chat": self.chat_service,
            "leaderboard": self.leaderboard_service,
        }
        current = services[purpose].settings
        if api_key == "":
            api_key = self.credential_store.get(model_endpoint_id(normalized_url)) or ""
        if api_key == "" and current.base_url == normalized_url:
            api_key = current.api_key
        return normalized_url, api_key

    def _activate_endpoint(self, purpose: str, settings) -> None:
        """Persist endpoint metadata, its key, and the active purpose mapping."""
        endpoint_id = model_endpoint_id(settings.base_url)
        self.database.upsert_model_endpoint(endpoint_id, settings.base_url)
        self.credential_store.set(endpoint_id, settings.api_key)
        self.database.set_model_purpose(purpose, endpoint_id, settings.model, settings.effort)

    def _refresh_research_runtime(self) -> None:
        """Apply the current article endpoint to Planner/Jev without exposing credentials."""
        research_service = getattr(self.workbench, "research_service", None)
        decision_service = getattr(research_service, "decision_service", None)
        if research_service is None or decision_service is None:
            return
        self.hotspots.decision_service = decision_service
        if not str(getattr(self.llm_service.settings, "base_url", "") or "").strip():
            return
        planner = getattr(research_service, "planner", None)
        if planner is not None:
            planner.provider = self.llm_service
        jev = getattr(decision_service, "jev", None)
        if jev is None:
            decision_service.jev = JevAdapter(self.llm_service)
        else:
            jev.provider = self.llm_service

    @property
    def wechat_publisher(self) -> WeChatPublisher | None:
        config = self.wechat_config
        if config is None:
            return None
        return WeChatPublisher(
            config["app_id"],
            config["app_secret"],
            config.get("thumb_media_id", ""),
            config.get("author", ""),
            base_url=self._wechat_base_url or DEFAULT_WECHAT_BASE_URL,
            opener=self._wechat_opener,
            image_fetcher=self.workbench.image_fetcher or download_image,
            poster_renderer=self._wechat_poster_renderer,
        )

    def status(self) -> dict[str, object]:
        settings = self.llm_service.settings
        chat_settings = self.chat_service.settings
        translation_settings = self.translation_service.settings
        leaderboard_settings = self.leaderboard_service.settings
        research_service = getattr(self.workbench, "research_service", None)
        planner = getattr(research_service, "planner", None)
        decision_service = getattr(research_service, "decision_service", None)
        jev = getattr(decision_service, "jev", None)
        legacy_sources = _decorate_source_health(self.hotspots.source_health())
        return {
            "ok": True,
            "service": "autoposting",
            "version": APP_VERSION,
            "style": self.article_style,
            "llm": {
                **self.llm_service.settings_payload(settings),
                "enabled": self._article_model_configured,
                "models": list(self.article_models),
            },
            "chat_llm": (
                self.chat_service.settings_payload(chat_settings)
                if chat_settings.base_url
                else {"model": "", "base_url": "", "configured": False}
            ),
            "translation_llm": (
                {
                    **self.translation_service.settings_payload(translation_settings),
                    "enabled": True,
                }
                if self._translation_model_configured
                else {"model": "", "base_url": "", "configured": False, "enabled": False}
            ),
            "leaderboard_llm": (
                {
                    **self.leaderboard_service.settings_payload(leaderboard_settings),
                    "enabled": True,
                }
                if self._leaderboard_model_configured
                else {"model": "", "base_url": "", "configured": False, "enabled": False}
            ),
            "model_endpoints": self._model_endpoints(),
            "research": {
                "schema_version": "topic-dossier-v1",
                "planner_configured": bool(getattr(planner, "configured", False)),
                "jev_configured": bool(getattr(jev, "configured", False)),
            },
            "github_token_configured": bool(self.github_token_configured),
            # Keep the historical health list for old clients. New UI reads the
            # DB-backed registry so runtime toggles and intervals remain authoritative.
            "sources": legacy_sources,
            "source_registry": self.source_registry(),
            "platforms": {
                "wechat": {"configured": self.wechat_config is not None},
                "xiaohongshu": {
                    "configured": True,
                    "account_configured": bool(self.xiaohongshu_config.get("account_name")),
                    "account_name": self.xiaohongshu_config["account_name"],
                    "creator_url": self.xiaohongshu_config["creator_url"],
                    "manual_required": True,
                },
                "x": {"configured": bool(self.x_bearer_token)},
            },
        }

    def source_registry(self) -> list[dict[str, object]]:
        """Return the DB-authoritative source registry for the new UI."""
        for source in self.hotspots.sources:
            # upsert_source preserves runtime enabled/interval columns while
            # keeping metadata from the configured source JSON current.
            self.database.upsert_source(source)
        return _decorate_source_health(
            [_runtime_source_health(row) for row in self.database.list_sources()]
        )

    def collection_runs(self, limit: int = 20) -> list[dict[str, object]]:
        return self.database.list_collection_runs(limit)

    def collection_run(self, run_id: str) -> dict[str, object]:
        return self.database.get_collection_run(run_id)

    def _resolve_research_event(self, event_id: str) -> dict[str, object] | None:
        """Resolve a selected radar cluster before falling back to DB events."""
        key = str(event_id or "").strip()
        if not key:
            return None
        radar_event = self._radar_events.get(key)
        if radar_event is not None:
            return dict(radar_event)
        try:
            return self.database.get_event(key)
        except KeyError:
            return None

    def research(self, event_ids: object, query: object = None) -> dict[str, object]:
        if not isinstance(event_ids, list):
            raise ResearchError("event_ids_must_be_list")
        if query is not None and not isinstance(query, str):
            raise ResearchError("query_invalid")
        return {"ok": True, "dossier": self.workbench.research(event_ids, query=query)}

    def list_research(self, limit: int = 20) -> dict[str, object]:
        return {"ok": True, "dossiers": self.workbench.list_research(limit)}

    def get_research(self, dossier_id: str) -> dict[str, object]:
        dossier_id = str(dossier_id or "").strip()
        if not dossier_id:
            raise ResearchError("dossier_id_required")
        return {"ok": True, "dossier": self.workbench.get_research(dossier_id)}

    def set_source_enabled(self, source_id: str, enabled: bool) -> dict[str, object]:
        self.database.set_source_enabled(source_id, enabled)
        return {"ok": True, "sources": self.source_registry()}

    def set_source_fetch_interval(self, source_id: str, minutes: int) -> dict[str, object]:
        self.database.set_source_fetch_interval(source_id, minutes)
        return {"ok": True, "sources": self.source_registry()}

    def collect(
        self,
        hours: int,
        force: bool = True,
        source_id: str | None = None,
        scope: str = SCOPE_ALL,
    ) -> dict[str, object]:
        payload = self.hotspots.collect(
            hours, force=force, source_id=source_id, scope=scope
        )
        payload["sources"] = _decorate_source_health(payload.get("sources"))
        collection = payload.get("collection")
        if isinstance(collection, dict):
            run_id = str(collection.get("run_id") or "").strip()
            if run_id:
                try:
                    payload["collection_run"] = self.collection_run(run_id)
                except KeyError:
                    pass
            elif "requested_sources" in collection:
                payload["collection_run"] = collection
        payload["source_registry"] = self.source_registry()
        self._remember_hotspot_payloads(payload)
        return {
            "ok": True,
            "count": len(payload["candidates"]),
            **payload,
        }

    def hotspots_window(
        self, hours: int, translate: bool = False, scope: str = SCOPE_ALL
    ) -> dict[str, object]:
        payload = self.hotspots.list_window(hours, translate=translate, scope=scope)
        payload["sources"] = _decorate_source_health(payload.get("sources"))
        self._remember_hotspot_payloads(payload)
        return {
            "ok": True,
            "count": len(payload["candidates"]),
            **payload,
        }

    def _remember_hotspot_payloads(self, payload: dict[str, object]) -> None:
        buckets: list[object] = [payload.get("candidates") or []]
        rankings = payload.get("rankings")
        if isinstance(rankings, dict):
            buckets.extend(rankings.values())
        for items in buckets:
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict) and item.get("item_id"):
                    self._hotspot_payloads[str(item["item_id"])] = dict(item)
        radar = payload.get("radar")
        events = radar.get("events") if isinstance(radar, dict) else None
        if isinstance(events, list):
            for event in events:
                if not isinstance(event, dict):
                    continue
                event_id = str(event.get("event_id") or "").strip()
                if event_id:
                    self._radar_events[event_id] = dict(event)
                ids = {
                    str(value)
                    for value in (
                        event.get("item_id"),
                        event.get("representative_item_id"),
                        event.get("canonical_item_id"),
                        *(event.get("item_ids") or ()),
                    )
                    if str(value or "").strip()
                }
                for item_id in ids:
                    self._radar_events[item_id] = dict(event)

    def _favorite_payload_from_item(self, item: dict[str, object]) -> dict[str, object]:
        item_id = str(item["item_id"])
        sources = self.database.get_item_sources(item_id)
        summary = _summary(str(item.get("content_text") or item.get("title") or ""))
        return {
            "item_id": item_id,
            "title": str(item.get("title") or ""),
            "summary": summary,
            "content_text": str(item.get("content_text") or ""),
            "source_ids": [str(pair["source_id"]) for pair in sources],
            "source_urls": [str(pair.get("source_url") or "") for pair in sources],
            "observed_at": str(item.get("published_at") or item.get("fetched_at") or ""),
        }

    def list_favorites(self) -> dict[str, object]:
        return {"ok": True, "items": self.database.list_hotspot_favorites()}

    def add_favorite(self, item_id: str) -> dict[str, object]:
        item_id = str(item_id or "").strip()
        if not item_id:
            raise ValueError("item_id required")
        payload = self._hotspot_payloads.get(item_id)
        if payload is None:
            payload = self._favorite_payload_from_item(self.database.get_item(item_id))
        title = str(payload.get("title") or "")
        summary = str(payload.get("summary") or payload.get("content_text") or "")
        saved = self.database.add_hotspot_favorite(item_id, title, summary, dict(payload))
        return {"ok": True, "item": saved}

    def delete_favorite(self, item_id: str) -> dict[str, object]:
        item_id = str(item_id or "").strip()
        if not item_id:
            raise ValueError("item_id required")
        if not self.database.remove_hotspot_favorite(item_id):
            raise KeyError(item_id)
        return {"ok": True, "item_id": item_id}

    def original(self, item_id: str) -> dict[str, object]:
        cached = self._original_text_cache.get(item_id)
        if cached is not None:
            return {"ok": True, **cached}
        item = self.database.get_item(item_id)
        sources: list[dict[str, str]] = []
        seen: set[str] = set()
        pairs = self.database.get_item_sources(item_id)
        candidates = [
            (str(pair.get("source_id") or "原站"), str(pair.get("source_url") or ""))
            for pair in pairs
        ]
        candidates.append(("原站", str(item.get("canonical_url") or "")))
        for source_id, url in candidates:
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or url in seen:
                continue
            seen.add(url)
            sources.append({"source_id": source_id, "url": url})
        stored = str(item.get("content_text") or "")
        content = stored
        content_source = "feed"
        is_reddit = "reddit.com" in str(item.get("canonical_url") or "").lower()
        if is_reddit:
            reader_document = ReaderService().read_payload(
                content,
                url=str(item.get("canonical_url") or ""),
                title_hint=str(item.get("title") or ""),
                source_type="rss",
            )
            result = {
                "item_id": item_id,
                "title": str(item.get("title") or ""),
                "content": content,
                "content_source": "feed",
                "content_note": "正文和热评来自 Reddit 收录，未抓板块页面",
                "translatable": _looks_english(content or str(item.get("title") or "")),
                "sources": sources,
                **self._reader_observability(reader_document),
            }
            return {"ok": True, **result}
        reader_document: ArticleDocument | None = None
        if sources and self.workbench.page_fetcher is not None:
            try:
                reader_document = ReaderService(
                    fetcher=self.workbench.page_fetcher,
                    render_fetcher=self.workbench.render_fetcher,
                ).read(
                    sources[0]["url"],
                    rss_content=stored,
                    title_hint=str(item.get("title") or ""),
                )
            except (OSError, TimeoutError, ValueError, UnicodeError):
                reader_document = None
            fetched = reader_document.text if reader_document is not None else ""
            # A degraded read means the page body was replaced by the collected
            # feed text, so it must never be labelled ``page``. Rely on the
            # explicit reader signal first, and keep the length comparison as a
            # secondary guard rather than the only one.
            page_read_is_real = (
                reader_document is not None
                and not reader_document.degraded
                and not reader_document.fallback_source
                and reader_document.status in {"full", "partial"}
                and bool(fetched)
            )
            if page_read_is_real and (not stored or len(fetched) > max(240, len(stored) + 80)):
                content = fetched
                content_source = "page"
        if reader_document is None:
            reader_document = ReaderService().read_payload(
                stored,
                url=str(item.get("canonical_url") or ""),
                title_hint=str(item.get("title") or ""),
                source_type="rss",
            )
        result = {
            "item_id": item_id,
            "title": str(item.get("title") or ""),
            "content": content,
            "content_source": content_source,
            "content_note": (
                "已从原站提取正文" if content_source == "page" else "原站正文不可用，显示 RSS 已收录内容"
            ),
            "translatable": _looks_english(content or str(item.get("title") or "")),
            "sources": sources,
            **self._reader_observability(reader_document),
        }
        if content_source == "page":
            self._original_text_cache[item_id] = result
        return {"ok": True, **result}

    @staticmethod
    def _reader_observability(document: ArticleDocument) -> dict[str, object]:
        """Expose reader diagnostics without changing original API fields."""

        return {
            # Keep both names: ``status`` mirrors ArticleDocument while the
            # prefixed field avoids colliding with future API workflow state.
            "status": document.status,
            "reader_status": document.status,
            "quality_score": document.quality_score,
            "failure_reason": document.failure_reason,
            "canonical_url": document.canonical_url,
            "fetch_method": document.fetch_method,
            "extractor": document.extractor,
            # 3.2.2: tell a complete read from a degraded one. ``degraded`` plus
            # ``cause_code`` is what separates "the page was blocked, here is the
            # collected feed text" from a genuinely empty failure.
            "cause_code": document.cause_code,
            "degraded": document.degraded,
            "fallback_source": document.fallback_source,
            "blocked_kind": document.blocked_kind,
        }

    def translate_original(self, item_id: str) -> dict[str, object]:
        if not self._translation_model_configured:
            raise ValueError("translation_model_not_configured")
        original = self.original(item_id)
        if not original["translatable"]:
            raise ValueError("original_is_already_chinese")
        translated = self.translation_service.translate_batch(
            [
                {
                    "item_id": item_id,
                    "title": (
                        str(original["title"])
                        if not _has_cjk(str(original["title"]))
                        else "English article"
                    ),
                    "summary": str(original["content"]),
                }
            ]
        )[item_id]
        if translated["status"] != "translated":
            raise LLMError(
                str(translated.get("error_code") or "translation_failed"),
                str(translated.get("error_reason") or "文章翻译失败"),
            )
        return {
            "ok": True,
            "item_id": item_id,
            "translation": translated["summary_zh"],
            "model": self.translation_service.settings.model,
        }

    def articles(
        self,
        item_id: str,
        platform: str,
        model: str | None = None,
        provider: str | None = None,
        effort: str | None = None,
        prompt_style: str = DEFAULT_ARTICLE_PROMPT_STYLE,
        item_ids: list[str] | None = None,
        pasted_text: str | None = None,
        dossier_id: str | None = None,
    ) -> dict[str, object]:
        if not self._article_model_configured:
            raise ValueError("article_model_not_configured")
        radar_event = None
        if not (dossier_id and str(dossier_id).strip()):
            radar_event = self._radar_events.get(str(item_id or "").strip())
            if radar_event is None and isinstance(item_ids, (list, tuple)):
                for candidate_id in item_ids:
                    radar_event = self._radar_events.get(str(candidate_id or "").strip())
                    if radar_event is not None:
                        break
        return {
            "ok": True,
            **self.workbench.generate_article(
                item_id,
                platform,
                model=model,
                provider=provider,
                effort=effort,
                style=self.article_style,
                prompt_style=prompt_style,
                xhs_handle=self.xiaohongshu_config.get("account_name") or "",
                item_ids=item_ids,
                pasted_text=pasted_text,
                radar_event=radar_event,
                dossier_id=dossier_id,
            ),
        }

    def approve(self, draft_id: str, reviewer: str) -> dict[str, object]:
        self.workflow.approve_draft(draft_id, reviewer)
        return {"ok": True, "draft_id": draft_id, "status": self.database.get_draft(draft_id)["status"]}

    def list_drafts(self, limit: int = 20) -> dict[str, object]:
        """Publishing-center listing: safe draft summaries plus latest attempt."""
        rows = self.database.list_drafts(limit)
        drafts = [
            {
                "draft_id": str(row["draft_id"]),
                "event_id": str(row["event_id"]),
                "platform": str(row["platform"]),
                "title": str(row["title"]),
                "status": str(row["status"]),
                "approved_by": str(row.get("approved_by") or ""),
                "approved_at": str(row.get("approved_at") or ""),
                "created_at": str(row.get("created_at") or ""),
                "updated_at": str(row.get("updated_at") or ""),
                "attempt_status": str(row.get("attempt_status") or ""),
                "attempt_at": str(row.get("attempt_at") or ""),
            }
            for row in rows
        ]
        return {"ok": True, "drafts": drafts}

    def open_draft(self, draft_id: str) -> dict[str, object]:
        """Load one persisted draft into the creation editor without re-generating."""
        identifier = str(draft_id or "").strip()
        if not identifier:
            raise ValueError("draft_id required")
        return self._draft_view(self.database.get_draft(identifier))

    def edit_article(self, draft_id: str, title: str, body: str) -> dict[str, object]:
        draft = self.workflow.edit_draft(draft_id, title, body)
        return self._draft_view(draft)

    def _draft_view(self, draft: dict[str, object]) -> dict[str, object]:
        assets = normalize_assets(draft["assets_json"])
        payload: dict[str, object] = {
            "ok": True,
            "draft_id": str(draft["draft_id"]),
            "event_id": str(draft["event_id"]),
            "platform": str(draft["platform"]),
            "status": str(draft["status"]),
            "title": str(draft["title"]),
            "body": str(draft["body"]),
            "source_ids": json.loads(str(draft["source_ids_json"])),
            "assets": assets,
        }
        if str(draft["platform"]) == "xiaohongshu":
            payload.update(
                xhs_fields(
                    str(draft["title"]),
                    str(draft["body"]),
                    assets,
                    self.xiaohongshu_config.get("account_name") or "",
                )
            )
        return payload

    def update_article_images(self, payload: object) -> dict[str, object]:
        if not isinstance(payload, dict):
            raise ValueError("image selection must be a JSON object")
        draft_id = payload.get("draft_id")
        if not isinstance(draft_id, str) or not draft_id:
            raise ValueError("draft_id required")
        cover_id = payload.get("cover_id", "")
        if cover_id is None:
            cover_id = ""
        if not isinstance(cover_id, str):
            raise ValueError("cover_id must be a string")
        body_ids = payload.get("body_ids", [])
        if not isinstance(body_ids, list) or any(not isinstance(item, str) for item in body_ids):
            raise ValueError("body_ids must be a list of strings")
        draft = self.workflow.update_draft_images(draft_id, cover_id, body_ids)
        return self._draft_view(draft)

    def rescan_article_images(self, payload: object) -> dict[str, object]:
        if not isinstance(payload, dict) or not isinstance(payload.get("draft_id"), str):
            raise ValueError("draft_id required")
        bundle = self.workbench.rescan_images(payload["draft_id"])
        draft = self.workflow.replace_draft_image_bundle(payload["draft_id"], bundle)
        return self._draft_view(draft)

    def proxy_image(self, url: str) -> tuple[bytes, str]:
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ValueError("image_url_invalid")
        if len(url) > 2000:
            raise ValueError("image_url_invalid")
        fetcher = self.workbench.image_fetcher or download_image
        payload = fetcher(url)
        if not isinstance(payload, (bytes, bytearray)) or not payload:
            raise ValueError("image_unavailable")
        data = bytes(payload)
        return data, guess_image_content_type(data)

    def set_model(self, payload: object) -> dict[str, object]:
        if not isinstance(payload, dict):
            raise ValueError("model settings must be a JSON object")
        base_url, api_key = self._resolve_endpoint_credentials(payload, "article")
        settings = self.llm_service.configure_verified(
            model=payload.get("model"),
            effort=payload.get("effort", "none"),
            base_url=base_url,
            api_key=api_key,
        )
        self._article_model_configured = True
        if settings.model not in self.article_models:
            self.article_models.append(settings.model)
        self._activate_endpoint("article", settings)
        self._refresh_research_runtime()
        return {
            "ok": True,
            "llm": self.llm_service.settings_payload(settings),
            "model_endpoints": self._model_endpoints(),
        }

    def models(self, payload: object) -> dict[str, object]:
        if not isinstance(payload, dict):
            raise ValueError("model discovery settings must be a JSON object")
        purpose = payload.get("purpose", "article")
        if purpose not in ("article", "translation", "chat", "leaderboard"):
            raise ValueError("purpose must be article, translation, chat, or leaderboard")
        persist = payload.get("persist", True)
        if not isinstance(persist, bool):
            raise ValueError("persist must be boolean")
        saved_models = payload.get("models")
        if saved_models is not None:
            if not isinstance(saved_models, list) or any(
                not isinstance(item, str) or not item.strip() for item in saved_models
            ):
                raise ValueError("models must be a list of strings")
            saved_models = [item.strip() for item in saved_models]
        base_url, api_key = self._resolve_endpoint_credentials(payload, purpose)
        discovered = self.llm_service.discover_models(base_url, api_key)
        endpoint_id = model_endpoint_id(base_url)
        if persist:
            self.database.upsert_model_endpoint(
                endpoint_id, base_url, discovered if saved_models is None else saved_models
            )
            if api_key:
                self.credential_store.set(endpoint_id, api_key)
        return {
            "ok": True,
            "endpoint_id": endpoint_id,
            "models": list(discovered),
            "model_endpoints": self._model_endpoints(),
        }

    def set_chat_model(self, payload: object) -> dict[str, object]:
        if not isinstance(payload, dict):
            raise ValueError("chat model settings must be a JSON object")
        base_url, api_key = self._resolve_endpoint_credentials(payload, "chat")
        settings = self.chat_service.configure_verified(
            model=payload.get("model"),
            effort="none",
            base_url=base_url,
            api_key=api_key,
        )
        self._activate_endpoint("chat", settings)
        return {
            "ok": True,
            "chat_llm": self.chat_service.settings_payload(settings),
            "model_endpoints": self._model_endpoints(),
        }

    def set_translation_model(self, payload: object) -> dict[str, object]:
        if not isinstance(payload, dict):
            raise ValueError("translation model settings must be a JSON object")
        base_url, api_key = self._resolve_endpoint_credentials(payload, "translation")
        settings = self.translation_service.configure_verified(
            model=payload.get("model"),
            effort="none",
            base_url=base_url,
            api_key=api_key,
        )
        self._translation_model_configured = True
        self.hotspots.llm_service = self.translation_service
        self._activate_endpoint("translation", settings)
        return {
            "ok": True,
            "translation_llm": {
                **self.translation_service.settings_payload(settings),
                "enabled": True,
            },
            "model_endpoints": self._model_endpoints(),
        }

    def set_leaderboard_model(self, payload: object) -> dict[str, object]:
        if not isinstance(payload, dict):
            raise ValueError("leaderboard model settings must be a JSON object")
        base_url, api_key = self._resolve_endpoint_credentials(payload, "leaderboard")
        settings = self.leaderboard_service.configure_verified(
            model=payload.get("model"),
            effort="none",
            base_url=base_url,
            api_key=api_key,
        )
        self._leaderboard_model_configured = True
        self._activate_endpoint("leaderboard", settings)
        return {
            "ok": True,
            "leaderboard_llm": {
                **self.leaderboard_service.settings_payload(settings),
                "enabled": True,
            },
            "model_endpoints": self._model_endpoints(),
        }

    def github_weekly(self) -> dict[str, object]:
        snapshot = self.database.get_github_weekly()
        if snapshot is None:
            return {
                "ok": True,
                "week_start": week_window_start(),
                "fetched_at": "",
                "repos": [],
                "xhs_title": XHS_TITLE,
                "xhs_body": "",
                "model_used": "",
                "images": [],
                "visual": {},
                "count": 0,
                "degraded": False,
                "degraded_reasons": [],
                "result_status": "empty",
            }
        degraded = bool(snapshot.get("degraded"))
        return {
            "ok": True,
            "count": len(snapshot["repos"]),
            "degraded": degraded,
            "degraded_reasons": snapshot.get("degraded_reasons", []),
            "result_status": (
                "degraded" if degraded else ("ok" if snapshot["repos"] else "empty")
            ),
            **snapshot,
        }

    def github_weekly_refresh(self) -> dict[str, object]:
        diagnostics: list[dict[str, object]] = []
        repos = fetch_weekly_ai_repos(
            getter=self.github_fetcher,
            diagnostics=diagnostics,
        )
        model_used = ""
        if self._leaderboard_model_configured:
            repos = self.leaderboard_service.enrich_github_repos(repos)
            model_used = self.leaderboard_service.settings.model
        title, body = build_xhs_copy(repos)
        images = build_weekly_render_plan(repos)
        saved = self.database.save_github_weekly(
            week_window_start(),
            repos,
            title,
            body,
            model_used,
            images,
            diagnostics=diagnostics,
        )
        return {
            "ok": True,
            "count": len(repos),
            "degraded": bool(diagnostics),
            "degraded_reasons": diagnostics,
            "result_status": "degraded" if diagnostics else ("empty" if not repos else "ok"),
            **saved,
        }

    def github_weekly_render(self, payload: object) -> dict[str, object]:
        snapshot = self.database.get_github_weekly()
        if snapshot is None or not snapshot.get("repos"):
            raise ValueError("github_weekly_empty")
        body = payload if isinstance(payload, dict) else {}
        theme = "light" if str(body.get("theme") or "").strip().lower() == "light" else "dark"
        palette = parse_palette(body.get("palette"))
        poster_template = normalize_poster_template(body.get("poster_template"))
        repos = list(snapshot["repos"])
        jobs = list(snapshot.get("images") or []) or build_weekly_render_plan(repos)
        if any(str(job.get("kind") or "") != "cover" for job in jobs):
            require_enriched_copy(repos)
        if self.visual_renderer is None:
            images = render_weekly_visual(
                repos,
                jobs,
                self.packages_dir / "github-weekly",
                theme=theme,
                palette=palette,
                poster_template=poster_template,
            )
        else:
            images = self.visual_renderer(
                repos,
                jobs,
                self.packages_dir / "github-weekly",
                theme=theme,
                palette=palette,
                poster_template=poster_template,
            )
        visual = {"theme": theme, "palette": palette, "poster_template": poster_template}
        saved = self.database.save_github_weekly(
            str(snapshot["week_start"]),
            repos,
            str(snapshot["xhs_title"]),
            str(snapshot["xhs_body"]),
            str(snapshot.get("model_used") or ""),
            images,
            visual=visual,
            diagnostics=(
                list(snapshot.get("degraded_reasons") or [])
                if isinstance(snapshot.get("degraded_reasons"), list)
                else None
            ),
        )
        return {"ok": True, "count": len(repos), **saved}

    def _skill_copy_writer(self):
        """B5: skill posters go through the same copy layer as the weekly lane."""
        if not self._leaderboard_model_configured:
            raise enrichment_error("model not configured")
        return self.leaderboard_service.enrich_skill_repos

    def _skill_llm(self):
        if self.skill_complete is not None:
            return self.skill_complete
        if not self._article_model_configured:
            return None

        def complete(prompt: str) -> str:
            return self.llm_service._run(prompt, self.llm_service.settings)

        return complete

    def _merge_latest_skill_fields(self, snapshot: dict[str, object]) -> dict[str, object]:
        latest = self.database.get_skill_finder() or {}
        incoming = snapshot.get("article") if isinstance(snapshot.get("article"), dict) else {}
        latest_article = latest.get("article") if isinstance(latest.get("article"), dict) else {}
        if str(latest_article.get("body") or "").strip() and not str(incoming.get("body") or "").strip():
            snapshot["article"] = dict(latest_article)
            title = str(latest_article.get("title") or "").strip()
            if title:
                snapshot["title"] = title
        latest_images = latest.get("images")
        if isinstance(latest_images, list) and latest_images and not snapshot.get("images"):
            snapshot["images"] = list(latest_images)
        # The user's own title is state, not output: a search or a paste must never
        # silently drop it (it titles the cover poster and the intro panel).
        if str(latest.get("user_title") or "").strip() and "user_title" not in snapshot:
            snapshot["user_title"] = str(latest["user_title"])
        if isinstance(latest.get("visual"), dict) and not snapshot.get("visual"):
            snapshot["visual"] = dict(latest["visual"])
        latest_phase = str(latest.get("phase") or "")
        if latest_phase in {"rendering", "done"} and snapshot.get("phase") not in {"error", "done"}:
            snapshot["phase"] = latest_phase
        # Partial writes (progress events, image batches) must not reset the
        # package revision: the render stage uses it to detect a moved package.
        try:
            latest_revision = int(latest.get("revision") or 0)
        except (TypeError, ValueError):
            latest_revision = 0
        try:
            own_revision = int(snapshot.get("revision") or 0)
        except (TypeError, ValueError):
            own_revision = 0
        snapshot["revision"] = max(latest_revision, own_revision)
        return snapshot

    def _restore_skill_article_from_package(self, snapshot: dict[str, object]) -> dict[str, object]:
        article = snapshot.get("article") if isinstance(snapshot.get("article"), dict) else {}
        if str(article.get("body") or "").strip():
            return snapshot
        package_path = Path(str(snapshot.get("package_path") or ""))
        restored: dict[str, str] | None = None
        index_path = package_path / "index.json"
        if index_path.is_file():
            try:
                packed = json.loads(index_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                packed = {}
            packed_article = packed.get("article") if isinstance(packed, dict) else None
            if isinstance(packed_article, dict) and str(packed_article.get("body") or "").strip():
                restored = {
                    "title": str(packed_article.get("title") or snapshot.get("title") or "").strip(),
                    "body": str(packed_article.get("body") or "").strip(),
                }
        if restored is None:
            article_md = package_path / "article.md"
            if article_md.is_file():
                try:
                    text = article_md.read_text(encoding="utf-8").strip()
                except OSError:
                    text = ""
                if text:
                    title = str(snapshot.get("title") or "")
                    body = text
                    if text.startswith("# "):
                        first, _, rest = text.partition("\n")
                        title = first[2:].strip() or title
                        body = rest.strip()
                    restored = {"title": title, "body": body}
        if restored is None:
            return snapshot
        snapshot["article"] = restored
        if restored.get("title"):
            snapshot["title"] = restored["title"]
        if not snapshot.get("degraded"):
            self.database.save_skill_finder(snapshot)
        return snapshot

    def _skill_source_payload(self) -> dict[str, object]:
        """Sources of the collected skills + the configured auto-scrape sources.

        The skills lane shows two different things and must not confuse them:
        where the *collected* skills came from (GitHub / skills.sh / 粘贴), and how
        many *crawlers* are configured to go find more.
        """
        crawled = crawl_sources(github_token=bool(getattr(self, "github_token_configured", False)))
        return {
            "crawl_sources": crawled,
            "crawl_source_count": len([row for row in crawled if row.get("enabled") == "1"]),
        }

    def skills_snapshot(self) -> dict[str, object]:
        snapshot = self.database.get_skill_finder()
        if snapshot is None:
            return {
                "ok": True,
                "query": "",
                "title": "",
                "user_title": "",
                "theme": "",
                "stages": [],
                "skills": [],
                "article": {"title": "", "body": ""},
                "package_path": "",
                "images": [],
                "phase": "",
                "count": 0,
                "github_token": bool(getattr(self, "github_token_configured", False)),
                **self._skill_source_payload(),
            }
        snapshot = self._restore_skill_article_from_package(dict(snapshot))
        skills = snapshot.get("skills") if isinstance(snapshot.get("skills"), list) else []
        body = {"ok": True, "count": len(skills), **snapshot}
        body.setdefault("phase", "")
        body.setdefault("user_title", "")
        body["github_token"] = bool(getattr(self, "github_token_configured", False))
        body.update(self._skill_source_payload())
        return body

    def skills_paste(self, payload: object) -> dict[str, object]:
        """Pasted skill links join the package the same way searched ones do.

        The links become collected skills, get a stage, are named in the intro
        and are rendered as posters by the regular 生成图片 step.
        """
        if not isinstance(payload, dict):
            raise ValueError("links must be a string")
        links = payload.get("links")
        if not isinstance(links, str) or not links.strip():
            raise ValueError("links must contain at least one skill link")
        if len(links) > MAX_PASTE_CHARS:
            raise ValueError(f"links must be at most {MAX_PASTE_CHARS} characters")
        thread = getattr(self, "_skill_thread", None)
        if thread is not None and thread.is_alive():
            raise ValueError("skill_search_running")
        # Only a render that is alive *in this process* blocks a paste. The stored
        # phase may say "rendering" forever after a crash or a restart, and that
        # must not park the paste box.
        if getattr(self, "_skill_render_active", False):
            raise ValueError("skill_render_running")
        candidates, rejects = parse_skill_links(links)
        reject_total = len(rejects)
        if len(candidates) > MAX_PASTE_LINKS:
            for extra in candidates[MAX_PASTE_LINKS:]:
                rejects.append(
                    {
                        "input": str(extra.get("url") or extra.get("source_id") or ""),
                        "reason": "too_many",
                    }
                )
            candidates = candidates[:MAX_PASTE_LINKS]
        if not candidates:
            return {
                "ok": True,
                "added": 0,
                "rejected": rejects,
                **self.skills_snapshot(),
            }
        # Network work first, lock second: fetching under the lock would stall a
        # render's progress writes for as long as the paste takes.
        hits, skipped = fetch_manual_hits(
            candidates, getter=self.github_fetcher, api_ok=None
        )
        with self._skill_lock():
            # The freshest snapshot, so a render that finished during the fetch
            # (or any other writer) is not rolled back by this paste.
            latest = self.database.get_skill_finder()
            result = merge_manual_hits(
                latest,
                hits,
                complete=self._skill_llm(),
                output_root=self.packages_dir.parent / "skills",
                skipped=skipped,
            )
            saved = self.database.save_skill_finder(result)
        skills = saved.get("skills") if isinstance(saved.get("skills"), list) else []
        skipped_rows = (
            saved.get("paste_skipped") if isinstance(saved.get("paste_skipped"), list) else []
        )
        unverified = (
            saved.get("paste_unverified") if isinstance(saved.get("paste_unverified"), list) else []
        )
        replaced = int(saved.get("paste_replaced") or 0)
        return {
            "ok": True,
            "added": max(0, len(hits) - len(skipped_rows) - replaced),
            "replaced": replaced,
            # The detail is capped: pasting a block of prose turns every token into
            # a reject, and returning hundreds of rows helps nobody. The UI reports
            # the exact count from `rejected_count`.
            "rejected": rejects[:MAX_PASTE_REJECTS],
            "rejected_count": max(len(rejects), reject_total),
            "unverified": unverified,
            "count": len(skills),
            **saved,
        }

    def save_github_token(self, payload: object) -> dict[str, object]:
        """Store (or clear) the optional GitHub token for the skill finder."""
        if not isinstance(payload, dict):
            raise ValueError("token must be a string")
        token = payload.get("token")
        if not isinstance(token, str):
            raise ValueError("token must be a string")
        token = token.strip()
        if len(token) > 255:
            raise ValueError("token must be at most 255 characters")
        if token:
            self.credential_store.set(GITHUB_TOKEN_STORE_ID, token)
        else:
            self.credential_store.delete(GITHUB_TOKEN_STORE_ID)
        self.github_token_configured = bool(token)
        # Clearing falls back to the environment instead of forcing "no token".
        set_github_token(token or None)
        return {"ok": True, "configured": bool(token)}

    def set_skill_title(self, payload: object) -> dict[str, object]:
        """Store the user's own title for the skill package.

        The title is user state, not search output: it survives a new search, it
        titles the cover poster, and it titles the intro panel in the text area.
        """
        if not isinstance(payload, dict):
            raise ValueError("title must be a string")
        title = payload.get("title")
        if not isinstance(title, str):
            raise ValueError("title must be a string")
        title = title.strip()
        if len(title) > 120:
            raise ValueError("title must be at most 120 characters")
        with self._skill_lock():
            current = dict(self.database.get_skill_finder() or {})
            current["user_title"] = title
            self.database.save_skill_finder(current)
        return {"ok": True, "user_title": title, **self.skills_snapshot()}

    def clear_skills(self) -> dict[str, object]:
        """Drop the collected skills (and their posters) but keep the title.

        Keyword search adds to the package, so clearing is the way back to an
        empty sheet; without it the only reset was editing the database.
        """
        thread = getattr(self, "_skill_thread", None)
        if thread is not None and thread.is_alive():
            raise ValueError("skill_search_running")
        if getattr(self, "_skill_render_active", False):
            raise ValueError("skill_render_running")
        with self._skill_lock():
            current = dict(self.database.get_skill_finder() or {})
            title = str(current.get("user_title") or "")
            self.database.save_skill_finder(
                {
                    "query": "",
                    "user_title": title,
                    "title": title,
                    "theme": "",
                    "stages": [],
                    "skills": [],
                    "manual_skills": [],
                    "groups": [],
                    "article": {"title": "", "body": ""},
                    "images": [],
                    "package_path": "",
                    "phase": "skills_ready",
                    "revision": self._skill_revision() + 1,
                }
            )
        return self.skills_snapshot()

    def skills_search(self, payload: object) -> dict[str, object]:
        if not isinstance(payload, dict):
            raise ValueError("query must be 1-200 characters")
        if payload.get("abort"):
            cancel = getattr(self, "_skill_cancel", None)
            if isinstance(cancel, threading.Event):
                cancel.set()
            snapshot = self.database.get_skill_finder() or {}
            snapshot = dict(snapshot)
            if snapshot.get("phase") in {"searching", "rendering"}:
                snapshot["phase"] = "error"
                snapshot["error_reason"] = "已停止"
                snapshot = self.database.save_skill_finder(snapshot)
            skills = snapshot.get("skills") if isinstance(snapshot.get("skills"), list) else []
            return {"ok": True, "count": len(skills), **snapshot}
        if not isinstance(payload.get("query"), str):
            raise ValueError("query must be 1-200 characters")
        query = payload["query"].strip()
        if not query or len(query) > 200:
            raise ValueError("query must be 1-200 characters")
        # The model is optional now: keywords drive the scrape, and when a model
        # is configured it is only used to write the intro piece.
        complete = self._skill_llm()
        previous = self.database.get_skill_finder()
        cancel = threading.Event()
        self._skill_cancel = cancel
        try:
            previous_revision = int((previous or {}).get("revision") or 0)
        except (TypeError, ValueError):
            previous_revision = 0
        seed = {
            "query": query,
            "title": query,
            "user_title": str((previous or {}).get("user_title") or ""),
            "theme": query,
            "stages": [],
            "skills": [],
            "article": {"title": "", "body": ""},
            "images": [],
            # A new search replaces the skill set: bump so an in-flight render
            # refuses to write its older package back.
            "revision": previous_revision + 1,
            "package_path": "",
            "phase": "searching",
        }
        saved = self.database.save_skill_finder(seed)
        persist_lock = getattr(self, "_skill_persist_lock", None)
        if persist_lock is None:
            persist_lock = threading.Lock()
            self._skill_persist_lock = persist_lock

        def persist(kind: str, data: dict[str, object]) -> None:
            if cancel.is_set() and kind != "error":
                return
            body = dict(data)
            with persist_lock:
                self._merge_latest_skill_fields(body)
                if kind == "error":
                    body["phase"] = "error"
                elif body.get("phase") not in {"rendering", "done"}:
                    if kind in {"skills_ready", "article"}:
                        body["phase"] = "skills_ready"
                    elif str(body.get("phase") or "") in {
                        "understanding",
                        "chain",
                        "decompose",
                        "keywords",
                        "search",
                        "group",
                        "compose",
                    }:
                        pass
                    else:
                        body["phase"] = "searching"
                self.database.save_skill_finder(body)

        def mark_failed(error_code: str, reason: str) -> None:
            """Record a failed search without ever raising out of the thread.

            The previous handler read the snapshot inside `except`, so a database
            error there killed the thread and left the stored phase in-flight —
            which is exactly the state the browser polls forever.
            """
            try:
                failed = dict(self.database.get_skill_finder() or saved)
            except Exception:
                failed = dict(saved)
            failed["phase"] = "error"
            failed["error"] = error_code
            failed["error_reason"] = reason
            with persist_lock:
                try:
                    self.database.save_skill_finder(failed)
                except Exception:
                    logging.getLogger(__name__).exception(
                        "skill finder: could not persist the failure state"
                    )

        def run() -> None:
            try:
                result = find_skills(
                    query,
                    complete=complete,
                    github_get=self.github_fetcher,
                    web_search=self.skill_web_search,
                    output_root=self.packages_dir.parent / "skills",
                    on_event=persist,
                    previous=previous,
                    cancel=cancel.is_set,
                )
                result = dict(result)
                with persist_lock:
                    self._merge_latest_skill_fields(result)
                    if cancel.is_set():
                        result["phase"] = "error"
                        result["error_reason"] = "已停止"
                    elif result.get("phase") not in {"skills_ready", "done", "error", "rendering"}:
                        result["phase"] = "skills_ready"
                    self.database.save_skill_finder(result)
            except LLMError as exc:
                mark_failed(exc.code, exc.reason)
            except Exception as exc:
                mark_failed(type(exc).__name__, str(exc))

        thread = threading.Thread(target=run, daemon=True)
        self._skill_thread = thread
        thread.start()
        return self.skills_snapshot()

    def _skill_lock(self) -> threading.Lock:
        """The one lock that serializes every read-modify-write of the package."""
        lock = getattr(self, "_skill_persist_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._skill_persist_lock = lock
        return lock

    def _skill_revision(self) -> int:
        snapshot = self.database.get_skill_finder() or {}
        try:
            return int(snapshot.get("revision") or 0)
        except (TypeError, ValueError):
            return 0

    def _save_skill_render_fields(
        self,
        revision: int,
        fields: dict[str, object],
        *,
        expected_phase: str,
    ) -> dict[str, object]:
        """Write only what the render stage owns, and only for the same package.

        The package is read again under the lock, so a paste (or a new search)
        that landed while the posters were being rendered is never overwritten
        with the render's stale skill list. When the package really did change,
        the render's images belong to a skill set that no longer exists: they are
        dropped and the caller is told to render again.
        """
        with self._skill_lock():
            current = dict(self.database.get_skill_finder() or {})
            if int(current.get("revision") or 0) != revision:
                raise ValueError("skill_package_changed")
            self._merge_latest_skill_fields(current)
            current.update(fields)
            current["phase"] = expected_phase
            return self.database.save_skill_finder(current)

    def skills_render(self, payload: object) -> dict[str, object]:
        snapshot = self.database.get_skill_finder()
        if snapshot is None:
            raise ValueError("skill_package_empty")
        body = payload if isinstance(payload, dict) else {}
        theme = "light" if str(body.get("theme") or "").strip().lower() == "light" else "dark"
        palette = parse_palette(body.get("palette")) if body.get("palette") is not None else None
        stored_visual = snapshot.get("visual") if isinstance(snapshot.get("visual"), dict) else {}
        poster_template = normalize_poster_template(
            body.get("poster_template")
            if body.get("poster_template") is not None
            else stored_visual.get("poster_template")
        )
        # The title box travels with the render request so the cover cannot be
        # painted from a stale title the user already replaced.
        if isinstance(body.get("title"), str):
            title = body["title"].strip()[:120]
            with self._skill_lock():
                current = dict(self.database.get_skill_finder() or {})
                current["user_title"] = title
                snapshot = self.database.save_skill_finder(current)
        # One aggregate for the whole sheet: searched + pasted, ordered as one
        # workflow. 生成图片 only receives the rows the user checked.
        aggregated = aggregate_skills(snapshot)
        if not aggregated:
            raise ValueError("skill_package_empty")
        selected_ids = body.get("selected_ids") if isinstance(body.get("selected_ids"), list) else None
        chosen = apply_skill_selection(aggregated, selected_ids)
        if not chosen:
            raise ValueError("skill_none_selected" if selected_ids is not None else "skill_package_empty")
        cover_only = bool(body.get("cover_only"))
        if not cover_only and len(chosen) > POSTER_RENDER_CAP:
            raise ValueError("超过条目上限")
        if selected_ids is not None:
            chosen_ids = {skill_identity(row) for row in chosen}
            for row in aggregated:
                row["selected"] = skill_identity(row) in chosen_ids
        snapshot = dict(snapshot)
        snapshot["skills"] = chosen
        snapshot["manual_skills"] = []
        package_path = Path(str(snapshot.get("package_path") or self.packages_dir.parent / "skills" / "latest"))
        images_dir = package_path / "images"
        http_dir = self.packages_dir / "skills"
        revision = self._skill_revision()
        self._skill_render_active = True
        self._save_skill_render_fields(
            revision,
            {"images": list(snapshot.get("images") or []), "skills": aggregated},
            expected_phase="rendering",
        )

        def publish_images(images: list[dict[str, object]]) -> None:
            http_dir.mkdir(parents=True, exist_ok=True)
            for image in images:
                filename = str(image.get("file") or f"{image.get('id')}.png")
                source = images_dir / filename
                if source.is_file():
                    target = http_dir / filename
                    target.write_bytes(source.read_bytes())
                image["url"] = f"/api/generated/skills/{filename}"
            self._save_skill_render_fields(
                revision, {"images": list(images)}, expected_phase="rendering"
            )

        def on_image(image: dict[str, object]) -> None:
            current = list(snapshot.get("images") or [])
            replaced = False
            for index, row in enumerate(current):
                if str(row.get("id") or "") == str(image.get("id") or ""):
                    current[index] = image
                    replaced = True
                    break
            if not replaced:
                current.append(image)
            publish_images(current)

        cover_only = bool(body.get("cover_only"))
        writer = None if cover_only else self._skill_copy_writer()
        try:
            images = render_skill_package(
                snapshot,
                images_dir,
                theme=theme,
                palette=palette,
                screenshot_fn=self.skill_screenshot,
                visual_renderer=self.visual_renderer,
                copy_writer=writer,
                on_image=on_image,
                poster_template=poster_template,
                cover_only=cover_only,
            )
            if cover_only:
                previous = list(snapshot.get("images") or [])
                cover_row = next(
                    (row for row in images if str(row.get("kind") or "") == "cover"),
                    images[0] if images else None,
                )
                merged: list[dict[str, object]] = []
                replaced = False
                for row in previous:
                    if str(row.get("kind") or row.get("id") or "") == "cover":
                        merged.append(cover_row or row)
                        replaced = True
                    else:
                        merged.append(row)
                if not replaced and cover_row is not None:
                    merged.insert(0, cover_row)
                images = merged or images
        except Exception as exc:
            # Never leave the package parked in "rendering": that phase is what
            # refused the next paste.
            failed = {
                "images": list(snapshot.get("images") or []),
                "phase": "skills_ready",
                "render_error": f"{type(exc).__name__}: {exc}"[:300],
            }
            try:
                self._save_skill_render_fields(revision, failed, expected_phase="skills_ready")
            except ValueError:
                pass
            raise
        finally:
            self._skill_render_active = False
        http_dir.mkdir(parents=True, exist_ok=True)
        for image in images:
            filename = str(image.get("file") or f"{image.get('id')}.png")
            source = images_dir / filename
            if source.is_file():
                target = http_dir / filename
                target.write_bytes(source.read_bytes())
            image["url"] = f"/api/generated/skills/{filename}"
        saved = self._save_skill_render_fields(
            revision,
            {
                "images": images,
                "visual": {
                    "theme": theme,
                    "palette": palette or {},
                    "poster_template": poster_template,
                },
                "render_error": "",
            },
            expected_phase="done",
        )
        skills = saved.get("skills") if isinstance(saved.get("skills"), list) else []
        return {"ok": True, "count": len(skills), **saved}

    def github_weekly_image(self, image_id: str, payload: object = None) -> dict[str, object]:
        snapshot = self.database.get_github_weekly()
        if snapshot is None:
            raise ValueError("github_weekly_empty")
        images = list(snapshot.get("images") or [])
        match = next((item for item in images if str(item.get("id")) == image_id), None)
        if match is None:
            raise KeyError(image_id)
        if str(match.get("kind") or "") != "cover":
            require_enriched_copy(list(snapshot.get("repos") or []))
        body = payload if isinstance(payload, dict) else {}
        visual = snapshot.get("visual") if isinstance(snapshot.get("visual"), dict) else {}
        poster_template = normalize_poster_template(
            body.get("poster_template")
            if body.get("poster_template") is not None
            else visual.get("poster_template")
        )
        if body.get("theme") is not None or body.get("palette") is not None:
            theme = (
                "light"
                if str(body.get("theme") or visual.get("theme") or "").strip().lower() == "light"
                else "dark"
            )
            palette = parse_palette(
                body.get("palette") if body.get("palette") is not None else visual.get("palette")
            )
            visual = {"theme": theme, "palette": palette, "poster_template": poster_template}
        else:
            theme = "light" if str(visual.get("theme") or "").strip().lower() == "light" else "dark"
            palette = parse_palette(visual.get("palette"))
            visual = dict(visual)
            visual["poster_template"] = poster_template
        if self.visual_renderer is None:
            rendered = render_weekly_visual(
                list(snapshot["repos"]),
                [match],
                self.packages_dir / "github-weekly",
                theme=theme,
                palette=palette,
                poster_template=poster_template,
            )
        else:
            rendered = self.visual_renderer(
                list(snapshot["repos"]),
                [match],
                self.packages_dir / "github-weekly",
                theme=theme,
                palette=palette,
                poster_template=poster_template,
            )
        if not rendered:
            raise ValueError("image_generation_failed")
        updated = []
        for item in images:
            if str(item.get("id")) == image_id:
                updated.append(rendered[0])
            else:
                updated.append(item)
        saved = self.database.save_github_weekly(
            str(snapshot["week_start"]),
            list(snapshot["repos"]),
            str(snapshot["xhs_title"]),
            str(snapshot["xhs_body"]),
            str(snapshot.get("model_used") or ""),
            updated,
            visual=visual if isinstance(visual, dict) else {},
            diagnostics=(
                list(snapshot.get("degraded_reasons") or [])
                if isinstance(snapshot.get("degraded_reasons"), list)
                else None
            ),
        )
        return {"ok": True, **rendered[0], "count": len(saved["repos"])}

    def generated_image(self, name: str, collection: str = "github-weekly") -> tuple[bytes, str]:
        safe = Path(name).name
        if safe != name or not safe.endswith(".png"):
            raise ValueError("image_url_invalid")
        folder = "skills" if collection == "skills" else "github-weekly"
        path = (self.packages_dir / folder / safe).resolve()
        root = (self.packages_dir / folder).resolve()
        if root not in path.parents and path != root / safe:
            raise ValueError("image_url_invalid")
        if not path.is_file() and folder == "skills":
            snapshot = self.database.get_skill_finder()
            package = Path(str((snapshot or {}).get("package_path") or ""))
            fallback = (package / "images" / safe).resolve()
            if package.as_posix() and fallback.is_file():
                path = fallback
        if not path.is_file():
            raise KeyError(name)
        return path.read_bytes(), "image/png"

    def delete_model_endpoint(self, payload: object) -> dict[str, object]:
        if not isinstance(payload, dict) or not isinstance(payload.get("endpoint_id"), str) or not payload["endpoint_id"].strip():
            raise ValueError("endpoint_id required")
        endpoint_id = payload["endpoint_id"].strip()
        if self.database.get_model_endpoint(endpoint_id) is None:
            raise KeyError(endpoint_id)
        for purpose, service in (
            ("article", self.llm_service),
            ("translation", self.translation_service),
            ("chat", self.chat_service),
            ("leaderboard", self.leaderboard_service),
        ):
            mapping = self.database.get_model_purpose(purpose)
            if mapping is not None and str(mapping["endpoint_id"]) == endpoint_id:
                service.configure(
                    model=LLM_MODEL,
                    provider=LLM_PROVIDER,
                    effort=LLM_EFFORT,
                    base_url="",
                    api_key="",
                )
                if purpose == "article":
                    self._article_model_configured = False
                    self.article_models = []
                elif purpose == "translation":
                    self._translation_model_configured = False
                    self.hotspots.llm_service = None
                elif purpose == "leaderboard":
                    self._leaderboard_model_configured = False
        self.credential_store.delete(endpoint_id)
        self.database.delete_model_endpoint(endpoint_id)
        return {
            "ok": True,
            "llm": self.llm_service.settings_payload(self.llm_service.settings),
            "chat_llm": (
                self.chat_service.settings_payload(self.chat_service.settings)
                if self.chat_service.settings.base_url
                else {"model": "", "base_url": "", "configured": False}
            ),
            "translation_llm": (
                {
                    **self.translation_service.settings_payload(
                        self.translation_service.settings
                    ),
                    "enabled": True,
                }
                if self._translation_model_configured
                else {"model": "", "base_url": "", "configured": False, "enabled": False}
            ),
            "leaderboard_llm": (
                {
                    **self.leaderboard_service.settings_payload(
                        self.leaderboard_service.settings
                    ),
                    "enabled": True,
                }
                if self._leaderboard_model_configured
                else {"model": "", "base_url": "", "configured": False, "enabled": False}
            ),
            "model_endpoints": self._model_endpoints(),
        }

    def chat(self, messages: object, model: str | None = None) -> dict[str, object]:
        content = self.chat_service.chat(validate_chat_messages(messages), model=model)
        return {"ok": True, "message": {"role": "assistant", "content": content}}

    def set_style(self, payload: object) -> dict[str, object]:
        if not isinstance(payload, dict) or not isinstance(payload.get("style"), str):
            raise ValueError("style must be a string")
        style = payload["style"].strip()
        if len(style) > 50_000:
            raise ValueError("style must be at most 50000 characters")
        self.article_style = style
        return {"ok": True, "style": style}

    def set_config(self, payload: object) -> dict[str, object]:
        if not isinstance(payload, dict):
            raise ValueError("config must be a JSON object")
        platform = payload.get("platform")
        if platform == "wechat":
            required = ("app_id", "app_secret")
            missing = [
                field
                for field in required
                if not isinstance(payload.get(field), str) or not payload[field]
            ]
            if missing:
                raise ValueError(f"wechat config missing: {', '.join(missing)}")
            thumb_media_id = payload.get("thumb_media_id", "")
            if thumb_media_id is None:
                thumb_media_id = ""
            if not isinstance(thumb_media_id, str):
                raise ValueError("thumb_media_id must be a string")
            self.wechat_config = {
                "app_id": payload["app_id"],
                "app_secret": payload["app_secret"],
                "thumb_media_id": thumb_media_id,
                "author": str(payload.get("author", "")),
            }
            self._store_account("wechat", self.wechat_config)
            return {
                "ok": True,
                "platform": "wechat",
                "configured": True,
            }
        if platform == "xiaohongshu":
            account_name = payload.get("account_name", "")
            if not isinstance(account_name, str) or len(account_name) > 100:
                raise ValueError("xiaohongshu account_name must be at most 100 characters")
            creator_url = payload.get("creator_url", XIAOHONGSHU_CREATOR_URL)
            if not isinstance(creator_url, str) or not creator_url.startswith("https://"):
                raise ValueError("xiaohongshu creator_url must be an HTTPS URL")
            self.xiaohongshu_config = {
                "creator_url": creator_url,
                "account_name": account_name.strip(),
            }
            self._store_account("xiaohongshu", self.xiaohongshu_config)
            return {"ok": True, "platform": "xiaohongshu", "configured": True}
        if platform == "x":
            token = payload.get("bearer_token", "")
            if not isinstance(token, str) or not token.strip():
                raise ValueError("x bearer_token required")
            self.x_bearer_token = token.strip()
            self.hotspots.x_bearer_token = self.x_bearer_token
            self._store_account("x", {"bearer_token": self.x_bearer_token})
            return {"ok": True, "platform": "x", "configured": True}
        raise ValueError(f"unsupported platform: {platform}")

    def delete_config(self, payload: object) -> dict[str, object]:
        if not isinstance(payload, dict):
            raise ValueError("config must be a JSON object")
        platform = payload.get("platform")
        if platform not in ACCOUNT_STORE_IDS:
            raise ValueError(f"unsupported platform: {platform}")
        self.credential_store.delete(ACCOUNT_STORE_IDS[platform])
        if platform == "wechat":
            self.wechat_config = None
        elif platform == "xiaohongshu":
            self.xiaohongshu_config = {
                "creator_url": XIAOHONGSHU_CREATOR_URL,
                "account_name": "",
            }
        else:
            self.x_bearer_token = ""
            self.hotspots.x_bearer_token = ""
        return {"ok": True, "platform": platform, "configured": False}

    def publish(self, draft_id: str, platform: str, live: bool = False) -> dict[str, object]:
        with self._publish_lock:
            return self._publish_locked(draft_id, platform, live)

    def _publish_locked(
        self, draft_id: str, platform: str, live: bool = False
    ) -> dict[str, object]:
        draft = self.database.get_draft(draft_id)
        if str(draft["platform"]) != platform:
            raise ValueError("platform_mismatch")
        if not self.database.is_current_draft(draft_id):
            raise ValueError("draft_superseded")
        if draft["status"] != "APPROVED":
            raise ValueError("draft_not_approved")
        if self.database.get_event(str(draft["event_id"]))["status"] != "APPROVED":
            raise ValueError("event_not_approved")
        if platform == "xiaohongshu":
            result = PublicationService(self.database, self.packages_dir, dry_run=True).publish(draft_id)
            return {
                "ok": True,
                "manual_required": True,
                "creator_url": self.xiaohongshu_config["creator_url"],
                "package_path": str(result.package_path),
                "archive_path": str(result.archive_path),
                "status": result.status,
            }
        if not live:
            result = PublicationService(self.database, self.packages_dir, dry_run=True).publish(draft_id)
            return {
                "ok": True,
                "status": result.status,
                "package_path": str(result.package_path),
                "archive_path": str(result.archive_path),
            }
        publisher = self.wechat_publisher
        if publisher is None:
            raise WechatConfigurationRequired("wechat_configuration_required")
        archive_path = PublicationService(
            self.database, self.packages_dir, dry_run=True
        ).archive_draft(draft_id)
        attempt_id = self.database.publication_attempt_id(
            draft_id, str(draft["content_hash"]), False
        )
        existing = self.database.get_publication_attempt(attempt_id)
        if existing and existing["status"] in {"SUBMITTED", "DRAFT_SAVED"}:
            return {
                "ok": True,
                "status": existing["status"],
                "reference": str(existing["response_reference"]),
                "archive_path": str(archive_path),
            }
        outcome = publisher.publish(draft)
        self.database.record_publication_attempt(
            attempt_id,
            draft_id,
            "wechat",
            False,
            str(outcome["status"]),
            None,
            0,
            response_reference=str(outcome["reference"]),
        )
        return {
            "ok": True,
            "status": outcome["status"],
            "reference": outcome["reference"],
            "archive_path": str(archive_path),
        }

    def schedule_publish(self, payload: object) -> dict[str, object]:
        if not isinstance(payload, dict):
            raise ValueError("schedule payload must be a JSON object")
        draft_id = payload.get("draft_id")
        platform = payload.get("platform")
        run_at = payload.get("run_at")
        live = payload.get("live", False)
        if not isinstance(draft_id, str) or not draft_id:
            raise ValueError("draft_id required")
        if not isinstance(platform, str) or not platform:
            raise ValueError("platform required")
        if not isinstance(live, bool):
            raise ValueError("live must be boolean")
        if not isinstance(run_at, str):
            raise ValueError("run_at required")
        draft = self.database.get_draft(draft_id)
        if str(draft["platform"]) != platform:
            raise ValueError("platform_mismatch")
        if not self.database.is_current_draft(draft_id):
            raise ValueError("draft_superseded")
        if draft["status"] != "APPROVED":
            raise ValueError("draft_not_approved")
        normalized = parse_run_at(run_at)
        job = self.database.create_scheduled_job(draft_id, platform, live, normalized)
        return {
            "ok": True,
            "job_id": job["job_id"],
            "draft_id": draft_id,
            "platform": platform,
            "live": live,
            "run_at": job["run_at"],
            "status": job["status"],
        }

    def list_schedule(self) -> dict[str, object]:
        jobs = []
        for job in self.database.list_scheduled_jobs():
            item = {
                "job_id": job["job_id"],
                "draft_id": job["draft_id"],
                "platform": job["platform"],
                "live": bool(job["live"]),
                "run_at": job["run_at"],
                "status": job["status"],
                "error_message": job["error_message"],
                "result": job.get("result"),
            }
            if job.get("degraded"):
                item.update(
                    {
                        "degraded": True,
                        "degraded_reasons": job.get("degraded_reasons", []),
                        "cause_code": job.get("cause_code"),
                    }
                )
            jobs.append(item)
        return {"ok": True, "jobs": jobs}

    def cancel_schedule(self, payload: object) -> dict[str, object]:
        if not isinstance(payload, dict) or not isinstance(payload.get("job_id"), str):
            raise ValueError("job_id required")
        job = self.database.cancel_scheduled_job(payload["job_id"])
        return {"ok": True, "job_id": job["job_id"], "status": job["status"]}


def _make_handler(app: WebApp) -> type[BaseHTTPRequestHandler]:
    class ApiHandler(BaseHTTPRequestHandler):
        def _send_json(self, code: int, payload: object) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _request_id(self) -> str:
            request_id = getattr(self, "_autoposting_request_id", "")
            if not request_id:
                request_id = uuid.uuid4().hex
                self._autoposting_request_id = request_id
            return request_id

        def _request_route(self) -> str:
            try:
                route = urlsplit(self.path).path or "/"
            except ValueError:
                route = "/<invalid>"
            return route

        def _not_found_payload(self, exc: KeyError) -> dict[str, object]:
            detail = str(exc)
            if exc.args and exc.args[0] == self.path:
                try:
                    if urlsplit(self.path).query:
                        detail = str(KeyError(self._request_route()))
                except ValueError:
                    detail = str(KeyError("/<invalid>"))
            return {"ok": False, "error": f"not_found:{detail}", "error_type": "KeyError"}

        def _send_internal_error(self, exc: BaseException) -> None:
            request_id = self._request_id()
            method = str(getattr(self, "command", "") or "")
            route = self._request_route()
            stack = "".join(traceback.format_tb(exc.__traceback__)).strip()
            safe_traceback = (
                "Traceback (most recent call last):\n" + stack
                if stack
                else "Traceback unavailable"
            )
            logging.getLogger(__name__).error(
                "unhandled HTTP exception method=%s route=%s request_id=%s "
                "exception_type=%s traceback=%s",
                method,
                route,
                request_id,
                type(exc).__name__,
                safe_traceback,
                extra={
                    "method": method,
                    "route": route,
                    "request_id": request_id,
                    "exception_type": type(exc).__name__,
                    "traceback": safe_traceback,
                },
            )
            self._send_json(
                500,
                {
                    "ok": False,
                    "error": "internal_error",
                    "error_code": "internal_error",
                    "error_type": "InternalServerError",
                    "request_id": request_id,
                },
            )

        def _read_json_body(self) -> object:
            length = int(self.headers.get("Content-Length") or 0)
            max_bytes = (
                MAX_CHAT_JSON_BODY_BYTES if self.path == "/api/chat" else MAX_JSON_BODY_BYTES
            )
            if length < 0 or length > max_bytes:
                raise ValueError("request_body_too_large")
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return {}
            try:
                return json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ValueError("invalid_json") from exc

        @staticmethod
        def _require_string(payload: object, field: str) -> str:
            if not isinstance(payload, dict) or not isinstance(payload.get(field), str):
                raise ValueError(f"{field} required")
            return payload[field]

        def do_GET(self) -> None:
            self._request_id()
            try:
                if urlsplit(self.path).path == "/":
                    body = INDEX_HTML.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                parsed = urlsplit(self.path)
                if parsed.path == "/api/status":
                    self._send_json(200, app.status())
                    return
                if parsed.path == "/api/sources":
                    self._send_json(200, {"ok": True, "sources": app.source_registry()})
                    return
                if parsed.path == "/api/collection-runs":
                    raw_limit = (parse_qs(parsed.query).get("limit") or ["20"])[0]
                    try:
                        limit = int(raw_limit)
                    except ValueError as exc:
                        raise ValueError("limit must be an integer") from exc
                    self._send_json(200, {"ok": True, "runs": app.collection_runs(limit)})
                    return
                if parsed.path.startswith("/api/collection-runs/"):
                    run_id = parsed.path.rsplit("/", 1)[-1].strip()
                    if not run_id:
                        raise ValueError("run_id required")
                    self._send_json(200, {"ok": True, "run": app.collection_run(run_id)})
                    return
                if parsed.path == "/api/research":
                    raw_limit = (parse_qs(parsed.query).get("limit") or ["20"])[0]
                    try:
                        limit = int(raw_limit)
                    except ValueError as exc:
                        raise ValueError("limit must be an integer") from exc
                    if not 1 <= limit <= 100:
                        raise ValueError("limit must be between 1 and 100")
                    self._send_json(200, app.list_research(limit))
                    return
                if parsed.path.startswith("/api/research/"):
                    dossier_id = parsed.path.rsplit("/", 1)[-1].strip()
                    if not dossier_id:
                        raise ValueError("dossier_id required")
                    self._send_json(200, app.get_research(dossier_id))
                    return
                if parsed.path == "/api/resources":
                    raw_limit = (parse_qs(parsed.query).get("limit") or ["20"])[0]
                    try:
                        limit = int(raw_limit)
                    except ValueError as exc:
                        raise ValueError("limit must be an integer") from exc
                    self._send_json(200, app.list_resource_sets(limit))
                    return
                if parsed.path.startswith("/api/resources/"):
                    set_id = parsed.path.rsplit("/", 1)[-1].strip()
                    if not set_id:
                        raise ValueError("set_id required")
                    self._send_json(200, app.get_resource_set(set_id))
                    return
                if parsed.path == "/api/drafts":
                    raw_limit = (parse_qs(parsed.query).get("limit") or ["20"])[0]
                    try:
                        limit = int(raw_limit)
                    except ValueError as exc:
                        raise ValueError("limit must be an integer") from exc
                    self._send_json(200, app.list_drafts(limit))
                    return
                if parsed.path.startswith("/api/drafts/"):
                    draft_id = parsed.path.rsplit("/", 1)[-1].strip()
                    if not draft_id:
                        raise ValueError("draft_id required")
                    self._send_json(200, app.open_draft(draft_id))
                    return
                if parsed.path == "/api/github-weekly":
                    self._send_json(200, app.github_weekly())
                    return
                if parsed.path == "/api/skills":
                    self._send_json(200, app.skills_snapshot())
                    return
                if parsed.path == "/api/favorites":
                    self._send_json(200, app.list_favorites())
                    return
                if parsed.path == "/api/hotspots":
                    raw_hours = (parse_qs(parsed.query).get("hours") or ["4"])[0]
                    try:
                        hours = int(raw_hours)
                    except ValueError as exc:
                        raise ValueError("hours must be an integer") from exc
                    if hours < HOTSPOT_HOUR_MIN or hours > HOTSPOT_HOUR_MAX:
                        raise ValueError(
                            f"hours must be between {HOTSPOT_HOUR_MIN} and {HOTSPOT_HOUR_MAX}"
                        )
                    translate_flag = (parse_qs(parsed.query).get("translate") or ["0"])[0]
                    translate = translate_flag.lower() in {"1", "true", "yes"}
                    query = parse_qs(parsed.query)
                    scope = resolve_scope(
                        (query.get("scope") or [None])[0],
                        (query.get("lane") or [None])[0],
                    )
                    self._send_json(
                        200, app.hotspots_window(hours, translate=translate, scope=scope)
                    )
                    return
                if parsed.path == "/api/schedule":
                    self._send_json(200, app.list_schedule())
                    return
                if parsed.path.startswith("/api/generated/github-weekly/"):
                    name = parsed.path.rsplit("/", 1)[-1]
                    data, content_type = app.generated_image(name)
                    self.send_response(200)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if parsed.path.startswith("/api/generated/skills/"):
                    name = parsed.path.rsplit("/", 1)[-1]
                    data, content_type = app.generated_image(name, "skills")
                    self.send_response(200)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if parsed.path == "/api/image-proxy":
                    url = (parse_qs(parsed.query).get("url") or [""])[0]
                    data, content_type = app.proxy_image(url)
                    self.send_response(200)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "private, max-age=300")
                    self.end_headers()
                    self.wfile.write(data)
                    return
                raise KeyError(self.path)
            except KeyError as exc:
                self._send_json(404, self._not_found_payload(exc))
            except ResearchError as exc:
                self._send_json(
                    400,
                    {
                        "ok": False,
                        "error": exc.code,
                        "error_reason": exc.reason,
                        "error_type": "ResearchError",
                    },
                )
            except ValueError as exc:
                self._send_json(400, {"ok": False, "error": str(exc), "error_type": "ValueError"})
            except Exception as exc:
                self._send_internal_error(exc)

        def do_POST(self) -> None:
            self._request_id()
            try:
                if self.headers.get_content_type() != "application/json":
                    raise ValueError("content_type_must_be_application_json")
                origin = self.headers.get("Origin")
                if origin:
                    try:
                        parsed_origin = urlsplit(origin)
                        origin_port = parsed_origin.port or (443 if parsed_origin.scheme == "https" else 80)
                    except ValueError:
                        parsed_origin = None
                        origin_port = None
                    if (
                        parsed_origin is None
                        or parsed_origin.scheme != "http"
                        or parsed_origin.hostname not in ALLOWED_HOSTS
                        or origin_port != self.server.server_port
                    ):
                        self._send_json(
                            403,
                            {
                                "ok": False,
                                "error": "cross_origin_request_rejected",
                                "error_type": "PermissionError",
                            },
                        )
                        return
                payload = self._read_json_body()
                if self.path == "/api/sources/enabled":
                    if not isinstance(payload, dict):
                        raise ValueError("source payload must be a JSON object")
                    enabled = payload.get("enabled")
                    if not isinstance(enabled, bool):
                        raise ValueError("enabled must be boolean")
                    result = app.set_source_enabled(
                        self._require_string(payload, "source_id"), enabled
                    )
                elif self.path == "/api/sources/fetch-interval":
                    if not isinstance(payload, dict):
                        raise ValueError("source payload must be a JSON object")
                    minutes = payload.get("minutes")
                    if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes <= 0:
                        raise ValueError("minutes must be a positive integer")
                    result = app.set_source_fetch_interval(
                        self._require_string(payload, "source_id"), minutes
                    )
                elif self.path == "/api/research":
                    if not isinstance(payload, dict):
                        raise ResearchError("research_payload_must_be_object")
                    event_ids = payload.get("event_ids")
                    if not isinstance(event_ids, list):
                        raise ResearchError("event_ids_must_be_list")
                    if any(not isinstance(value, str) for value in event_ids):
                        raise ResearchError("event_id_invalid")
                    query = payload.get("query")
                    if query is not None and not isinstance(query, str):
                        raise ResearchError("query_invalid")
                    result = app.research(event_ids, query)
                elif self.path == "/api/resources/discover":
                    result = app.discover_resources(payload)
                elif self.path.startswith("/api/resources/") and self.path.endswith("/refresh"):
                    set_id = self.path[len("/api/resources/") : -len("/refresh")].strip("/")
                    if not set_id:
                        raise ValueError("set_id required")
                    result = app.refresh_resource_set(set_id)
                elif self.path == "/api/collect":
                    hours = payload.get("hours", 4) if isinstance(payload, dict) else 4
                    if isinstance(hours, bool) or not isinstance(hours, int):
                        raise ValueError("hours must be an integer")
                    if hours < HOTSPOT_HOUR_MIN or hours > HOTSPOT_HOUR_MAX:
                        raise ValueError(
                            f"hours must be between {HOTSPOT_HOUR_MIN} and {HOTSPOT_HOUR_MAX}"
                        )
                    force = payload.get("force", True) if isinstance(payload, dict) else True
                    if not isinstance(force, bool):
                        raise ValueError("force must be boolean")
                    source_id = payload.get("source_id") if isinstance(payload, dict) else None
                    if source_id is not None and (
                        not isinstance(source_id, str) or not source_id.strip()
                    ):
                        raise ValueError("source_id must be a non-empty string")
                    scope = resolve_scope(
                        payload.get("scope") if isinstance(payload, dict) else None,
                        payload.get("lane") if isinstance(payload, dict) else None,
                    )
                    result = app.collect(
                        hours,
                        force=force,
                        source_id=source_id.strip() if source_id else None,
                        scope=scope,
                    )
                elif self.path == "/api/original":
                    result = app.original(self._require_string(payload, "item_id"))
                elif self.path == "/api/original/translate":
                    result = app.translate_original(self._require_string(payload, "item_id"))
                elif self.path == "/api/favorites":
                    result = app.add_favorite(self._require_string(payload, "item_id"))
                elif self.path == "/api/articles":
                    dossier_id = payload.get("dossier_id") if isinstance(payload, dict) else None
                    if dossier_id is not None:
                        if not isinstance(dossier_id, str) or not dossier_id.strip():
                            # Same vocabulary as every other dossier failure, so
                            # the UI can branch on one error code set.
                            raise ArticleContextError("dossier_id_required")
                        dossier_id = dossier_id.strip()
                    item_ids = payload.get("item_ids") if isinstance(payload, dict) else None
                    if item_ids is not None:
                        if not isinstance(item_ids, list) or any(
                            not isinstance(value, str) for value in item_ids
                        ):
                            raise ValueError("item_ids must be a list of strings")
                    item_id = payload.get("item_id") if isinstance(payload, dict) else None
                    if isinstance(item_ids, list) and [
                        value.strip() for value in item_ids if value.strip()
                    ]:
                        if not isinstance(item_id, str) or not item_id.strip():
                            item_id = sorted(
                                {value.strip() for value in item_ids if value.strip()}
                            )[0]
                    elif dossier_id:
                        # A Topic Dossier already carries its own events and
                        # sources, so no candidate item is required.
                        item_ids = None
                        item_id = ""
                    else:
                        item_ids = None
                        item_id = self._require_string(payload, "item_id")
                    result = app.articles(
                        item_id,
                        self._require_string(payload, "platform"),
                        model=payload.get("model") if isinstance(payload, dict) else None,
                        provider=payload.get("provider") if isinstance(payload, dict) else None,
                        effort=payload.get("effort") if isinstance(payload, dict) else None,
                        prompt_style=(
                            payload.get("prompt_style", DEFAULT_ARTICLE_PROMPT_STYLE)
                            if isinstance(payload, dict)
                            else DEFAULT_ARTICLE_PROMPT_STYLE
                        ),
                        item_ids=item_ids,
                        pasted_text=payload.get("pasted_text") if isinstance(payload, dict) else None,
                        dossier_id=dossier_id,
                    )
                elif self.path == "/api/config":
                    result = app.set_config(payload)
                elif self.path == "/api/config/delete":
                    result = app.delete_config(payload)
                elif self.path == "/api/model":
                    result = app.set_model(payload)
                elif self.path == "/api/models":
                    result = app.models(payload)
                elif self.path == "/api/chat/model":
                    result = app.set_chat_model(payload)
                elif self.path == "/api/github-weekly":
                    result = app.github_weekly_refresh()
                elif self.path == "/api/github-weekly/render":
                    result = app.github_weekly_render(payload)
                elif self.path == "/api/github-weekly/image":
                    result = app.github_weekly_image(self._require_string(payload, "id"), payload)
                elif self.path == "/api/skills/search":
                    result = app.skills_search(payload)
                elif self.path == "/api/skills/title":
                    result = app.set_skill_title(payload)
                elif self.path == "/api/skills/clear":
                    result = app.clear_skills()
                elif self.path == "/api/skills/paste":
                    result = app.skills_paste(payload)
                elif self.path == "/api/skills/github-token":
                    result = app.save_github_token(payload)
                elif self.path == "/api/skills/render":
                    result = app.skills_render(payload)
                elif self.path == "/api/leaderboard/model":
                    result = app.set_leaderboard_model(payload)
                elif self.path == "/api/translation/model":
                    result = app.set_translation_model(payload)
                elif self.path == "/api/model-endpoints/delete":
                    result = app.delete_model_endpoint(payload)
                elif self.path == "/api/chat":
                    result = app.chat(
                        payload.get("messages") if isinstance(payload, dict) else None,
                        payload.get("model") if isinstance(payload, dict) else None,
                    )
                elif self.path == "/api/style":
                    result = app.set_style(payload)
                elif self.path == "/api/articles/images":
                    result = app.update_article_images(payload)
                elif self.path == "/api/articles/images/rescan":
                    result = app.rescan_article_images(payload)
                elif self.path == "/api/articles/edit":
                    result = app.edit_article(
                        self._require_string(payload, "draft_id"),
                        self._require_string(payload, "title"),
                        self._require_string(payload, "body"),
                    )
                elif self.path == "/api/approve":
                    result = app.approve(
                        self._require_string(payload, "draft_id"),
                        self._require_string(payload, "reviewer"),
                    )
                elif self.path == "/api/publish":
                    if not isinstance(payload, dict):
                        raise ValueError("publish payload must be a JSON object")
                    live = payload.get("live", False)
                    if not isinstance(live, bool):
                        raise ValueError("live must be boolean")
                    result = app.publish(
                        self._require_string(payload, "draft_id"),
                        self._require_string(payload, "platform"),
                        live=live,
                    )
                elif self.path == "/api/schedule":
                    result = app.schedule_publish(payload)
                elif self.path == "/api/schedule/cancel":
                    result = app.cancel_schedule(payload)
                else:
                    raise KeyError(self.path)
                self._send_json(200, result)
            except KeyError as exc:
                self._send_json(404, self._not_found_payload(exc))
            except ResearchError as exc:
                self._send_json(
                    400,
                    {
                        "ok": False,
                        "error": exc.code,
                        "error_reason": exc.reason,
                        "error_type": "ResearchError",
                    },
                )
            except ArticleContextError as exc:
                # A dossier that cannot feed the writer fails loudly and stably;
                # it never falls back to a second research run.
                self._send_json(
                    400,
                    {
                        "ok": False,
                        "error": exc.code,
                        "error_reason": exc.reason,
                        "error_type": "ArticleContextError",
                    },
                )
            except (ValueError, PublicationError) as exc:
                self._send_json(400, {"ok": False, "error": str(exc), "error_type": type(exc).__name__})
            except LLMError as exc:
                self._send_json(
                    502,
                    {
                        "ok": False,
                        "error": exc.code,
                        "error_code": exc.cause_code or exc.code,
                        "cause_code": exc.cause_code or exc.code,
                        "error_reason": exc.reason,
                        "error_type": "LLMError",
                    },
                )
            except Exception as exc:
                self._send_internal_error(exc)

        def do_DELETE(self) -> None:
            self._request_id()
            try:
                if self.headers.get_content_type() != "application/json":
                    raise ValueError("content_type_must_be_application_json")
                origin = self.headers.get("Origin")
                if origin:
                    try:
                        parsed_origin = urlsplit(origin)
                        origin_port = parsed_origin.port or (443 if parsed_origin.scheme == "https" else 80)
                    except ValueError:
                        parsed_origin = None
                        origin_port = None
                    if (
                        parsed_origin is None
                        or parsed_origin.scheme != "http"
                        or parsed_origin.hostname not in ALLOWED_HOSTS
                        or origin_port != self.server.server_port
                    ):
                        self._send_json(
                            403,
                            {
                                "ok": False,
                                "error": "cross_origin_request_rejected",
                                "error_type": "PermissionError",
                            },
                        )
                        return
                payload = self._read_json_body()
                if self.path == "/api/favorites":
                    result = app.delete_favorite(self._require_string(payload, "item_id"))
                elif self.path.startswith("/api/resources/"):
                    set_id = self.path.rsplit("/", 1)[-1].strip()
                    if not set_id:
                        raise ValueError("set_id required")
                    result = app.delete_resource_set(set_id)
                else:
                    raise KeyError(self.path)
                self._send_json(200, result)
            except KeyError as exc:
                self._send_json(404, self._not_found_payload(exc))
            except ValueError as exc:
                self._send_json(400, {"ok": False, "error": str(exc), "error_type": "ValueError"})
            except Exception as exc:
                self._send_internal_error(exc)

        def log_message(self, format: str, *args: object) -> None:
            status = args[1] if len(args) > 1 else ""
            size = args[2] if len(args) > 2 else ""
            logging.getLogger(__name__).info(
                "http request method=%s route=%s request_id=%s status=%s bytes=%s",
                str(getattr(self, "command", "") or ""),
                self._request_route(),
                self._request_id(),
                status,
                size,
            )

    return ApiHandler


def create_server(app: WebApp, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    if host not in ALLOWED_HOSTS:
        raise ValueError("web server host must be 127.0.0.1 or localhost")
    server = ThreadingHTTPServer((host, port), _make_handler(app))
    server.daemon_threads = True
    return server


def run_web(app: WebApp, host: str = "127.0.0.1", port: int = 8080) -> None:
    server = create_server(app, host, port)
    app.scheduler.start()
    try:
        server.serve_forever()
    finally:
        app.scheduler.stop()
        server.server_close()
