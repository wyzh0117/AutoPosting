from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .background import RELATED_SELECTION_LIMIT, article_page_text
from .config import load_sources
from .content import ContentWorkflow
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
from .github_weekly import (
    XHS_TITLE,
    build_xhs_copy,
    fetch_weekly_ai_repos,
    set_github_token,
    week_window_start,
)
from .image_gen import build_weekly_render_plan
from .skill_finder import (
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
from .visual.tokens import parse_palette
from .visual.weekly import render_weekly_visual
from .images import default_page_fetcher, guess_image_content_type, normalize_assets
from .publishing import PublicationError, PublicationService
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

APP_VERSION = "1.4.3"
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


def _looks_english(text: str) -> bool:
    cjk = sum("\u3400" <= char <= "\u9fff" for char in text)
    latin = sum(char.isascii() and char.isalpha() for char in text)
    return latin > cjk

INDEX_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AutoPosting</title>
<style>
:root {
  --bg-main: #12110e;
  --bg-card: #1b1914;
  --bg-input: #23211b;
  --bg-raised: #2a271f;
  --text-main: #f3eee4;
  --text-muted: #9a9284;
  --accent: #c4a46a;
  --accent-glow: rgba(196, 164, 106, 0.16);
  --accent-gradient-1: #d4b57a;
  --accent-gradient-2: #8a7348;
  --border: #3a352c;
  --ok: #6f9e7a;
  --err: #c47a6a;
  --warn: #c4a46a;
  --radius-lg: 20px;
  --radius-md: 14px;
  --radius-sm: 8px;
  --ease: cubic-bezier(0.22, 1, 0.36, 1);
  --t-fast: 160ms;
  --t-mid: 280ms;
  --t-slow: 460ms;
  --hotspot-list-h: 29rem;
  --favorites-drawer-w: 20rem;
  --font: "Avenir Next", "Segoe UI", -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  --display: "Iowan Old Style", "Palatino Linotype", Palatino, "Songti SC", Georgia, serif;
}

*{box-sizing:border-box; letter-spacing:0;}
html,body{margin:0; padding:0;}
body{
  background-color:var(--bg-main); color:var(--text-main);
  font:14px/1.6 var(--font); padding:1.5rem 2rem 2rem;
  display:flex; flex-direction:column; align-items:center;
  min-height:100dvh; height:auto; overflow-x:hidden; overflow-y:auto;
  background-image:
    radial-gradient(circle at 88% -8%, rgba(196, 164, 106, 0.12), transparent 42%),
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
  background:rgba(196, 164, 106, 0.06); padding:.28rem .8rem;
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
#news-supplement-btn{
  border-radius:99px; padding:.5rem 1.2rem;
  border:1px solid var(--border); background:var(--bg-card);
  color:var(--text-main); font-weight:500;
}
#news-supplement-btn:hover:not(:disabled){background:var(--bg-input); border-color:var(--accent); box-shadow:none;}
#news-supplement-btn[aria-expanded="true"]{border-color:var(--accent); color:var(--accent);}
#favorites-drawer,#news-supplement-drawer{
  display:none; position:fixed; top:0; right:0; bottom:0;
  width:var(--favorites-drawer-w); z-index:40;
  background:linear-gradient(180deg, rgba(36, 33, 26, 0.98), rgba(22, 20, 16, 0.98));
  border-left:1px solid var(--border);
  flex-direction:column; padding:1rem 1rem 1.2rem;
  overflow:hidden;
}
body.favorites-open #favorites-drawer{display:flex;}
body.news-supplement-open #news-supplement-drawer{display:flex;}
body.favorites-open header,
body.favorites-open #app-nav,
body.favorites-open main,
body.favorites-open #wait-banner{
  margin-right:var(--favorites-drawer-w);
  width:calc(100% - var(--favorites-drawer-w));
}
body.news-supplement-open header,
body.news-supplement-open #app-nav,
body.news-supplement-open main,
body.news-supplement-open #wait-banner{
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
#news-supplement-list{
  list-style:none; margin:0; padding:0; overflow-y:auto; min-height:0; flex:1;
  display:flex; flex-direction:column; gap:.75rem;
}
.news-supplement-item{
  background:var(--bg-input); border:1px solid transparent;
  border-radius:var(--radius-md); padding:.8rem .9rem;
}
.news-supplement-item.is-focus{border-color:var(--accent);}
.news-supplement-item a{display:block; color:var(--accent); font-weight:600; line-height:1.4; word-break:break-word;}
.news-supplement-item .news-summary{display:block; margin-top:.35rem; color:var(--text-muted); font-size:.78rem; line-height:1.45; word-break:break-word;}
.news-supplement-item .news-meta{display:block; margin-top:.45rem; color:var(--text-muted); font-size:.7rem;}
.fav-item{
  position:relative; background:var(--bg-input); border:1px solid transparent;
  border-radius:var(--radius-md); padding:.8rem 2.4rem 2.1rem .9rem;
}
.fav-item .ht-pick{bottom:auto; top:2.5rem;}
.fav-item .fav-open-original{margin-right:1.6rem;}
.fav-item.is-picked, .hotspot-item.is-picked{
  border-color:var(--accent);
  background:linear-gradient(145deg, rgba(196, 164, 106, 0.16), rgba(196, 164, 106, 0.05));
  box-shadow:inset 0 0 0 1px rgba(196, 164, 106, 0.35);
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
.wait-job.is-done{border-color:var(--ok); box-shadow:0 0 0 4px rgba(111,158,122,.18);}
.wait-job.is-error{border-color:var(--err); box-shadow:0 0 0 4px rgba(196,122,106,.18);}
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
  font-size:.78rem; font-weight:700; color:var(--err); border-color:rgba(196,122,106,.55);
  background:rgba(196,122,106,.08);
}
.wait-stop:hover:not(:disabled){background:rgba(196,122,106,.18); border-color:var(--err);}
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
.gh-tile.is-error .gh-overlay{color:#f3eee4; background:rgba(196,122,106,.92);}

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
  box-shadow:0 18px 40px -18px rgba(0, 0, 0, 0.72), inset 0 1px 0 rgba(243, 238, 228, 0.04);
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
  padding:.25rem; border:1px solid rgba(243, 238, 228, 0.04);
}
.platform-tabs button{flex:0 1 auto;}
.platform-tabs[aria-label="候选栏"]{width:100%; max-width:36rem;}
.platform-tabs[aria-label="候选栏"] button.seg{
  flex:1 1 0; font-size:.82rem; padding:.42rem .55rem; letter-spacing:0;
}
body.lane-github .panel-title{letter-spacing:0; font-weight:700;}
body.lane-github .panel-header .row{width:100%;}
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
#source-health-wrap{
  flex:0 0 auto; margin-top:.6rem; padding-top:.55rem;
  border-top:1px solid var(--border);
}
#source-health-toggle{
  width:100%; justify-content:space-between; text-align:left;
  height:auto; min-height:34px; padding:.4rem .7rem; font-size:.75rem;
}
#source-health-legend{display:none; margin:.45rem 0 0; font-size:.72rem; color:var(--text-muted); line-height:1.45;}
#hotspots-panel.sources-open #source-health-legend{display:block;}
#source-health{
  list-style:none; margin:.5rem 0 0; padding:0 .25rem 0 0;
  display:flex; flex-direction:column; gap:.35rem;
  max-height:none; overflow:visible; min-height:0;
}
#source-health[hidden]{display:none;}
.source-health{
  display:flex; align-items:center; gap:.45rem; min-width:0;
  font-size:.75rem; color:var(--text-muted);
}
.source-health .sh-name{flex:1 1 auto; min-width:0; color:var(--text-main); overflow:hidden; text-overflow:ellipsis; white-space:nowrap;}
.source-health .sh-meta{flex:0 1 42%; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;}
.source-health .sh-dot{width:7px; height:7px; border-radius:50%; flex:0 0 auto;}
.source-health .sh-dot.ok{background:var(--ok);}
#picked-dock{
  position:fixed; left:1.5rem; bottom:1.5rem; z-index:45;
  display:flex; flex-direction:column; align-items:stretch;
  gap:.35rem; min-width:0; max-width:18rem; max-height:40vh; overflow-y:auto;
  padding:.65rem .75rem; pointer-events:auto;
  border:1px solid var(--border); border-radius:var(--radius-md);
  background:var(--bg-raised);
  box-shadow:0 18px 40px -18px rgba(0, 0, 0, 0.72), inset 0 1px 0 rgba(243, 238, 228, 0.04);
}
#picked-dock[hidden]{display:none !important;}
.picked-dock-label{
  font-size:.72rem; color:var(--text-muted); font-weight:600;
}
.picked-dock-chip{
  pointer-events:auto; max-width:100%; width:100%;
  font:inherit; font-size:.82rem; font-weight:600; line-height:1.5;
  color:var(--text-main); text-align:left; justify-content:flex-start; align-items:flex-start;
  background:rgba(196, 164, 106, 0.12);
  border:1px solid rgba(196, 164, 106, 0.35);
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
.ht-pick:hover{border-color:rgba(196, 164, 106, 0.45);}
.ht-pick.is-on{border-color:var(--accent); color:var(--accent); font-weight:700;}
.source-health .sh-dot.error{background:var(--err);}
.source-health .sh-dot.stale{background:var(--warn);}
.source-health .sh-dot.never,.source-health .sh-dot.disabled{background:#5a564c;}
.source-health button{height:28px; min-height:28px; padding:0 .6rem; font-size:.72rem;}
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
#hotspot-list::-webkit-scrollbar, .dialog-content::-webkit-scrollbar, .article-scroll::-webkit-scrollbar, #image-picker-list::-webkit-scrollbar, #hook-list::-webkit-scrollbar, #news-supplement-list::-webkit-scrollbar{width:4px;}
#hotspot-list::-webkit-scrollbar-thumb, .dialog-content::-webkit-scrollbar-thumb, .article-scroll::-webkit-scrollbar-thumb, #image-picker-list::-webkit-scrollbar-thumb, #hook-list::-webkit-scrollbar-thumb, #news-supplement-list::-webkit-scrollbar-thumb{background:var(--accent-gradient-2); border-radius:4px;}
.hotspot-item{
  display:block; width:100%; text-align:left;
  background:var(--bg-input); border:1px solid transparent;
  border-radius:var(--radius-md); padding:1rem 2.4rem 1rem .9rem;
  cursor:pointer; position:relative; word-break:break-word;
  transition:background var(--t-fast) var(--ease), border-color var(--t-fast) var(--ease), transform var(--t-fast) var(--ease), box-shadow var(--t-fast) var(--ease);
}
.hotspot-item:hover:not(:disabled){border-color:rgba(196, 164, 106, 0.35); transform:translateY(-2px);}
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
  background:rgba(196, 164, 106, 0.08); border:1px solid rgba(196, 164, 106, 0.18);
  padding:.15rem .45rem; border-radius:4px; font-weight:600; margin:0 0 .4rem;
}
.ht-title{display:block; font-size:.95rem; font-weight:600; line-height:1.4; color:var(--text-main); margin-bottom:.35rem; word-break:break-word; cursor:pointer;}
.ht-summary{
  display:block; font-size:.82rem; color:var(--text-muted); line-height:1.45; margin-bottom:.55rem;
  word-break:break-word; display:-webkit-box; -webkit-line-clamp:1; -webkit-box-orient:vertical; overflow:hidden;
}
.ht-zh-unavailable{display:block; color:var(--warn); margin:0 0 .4rem; font-size:.82rem;}
.ht-meta{display:block; font-size:.72rem; color:var(--text-muted); border-top:1px solid rgba(243, 238, 228, 0.05); padding-top:.65rem;}
.hotspot-empty{padding:.8rem .2rem; color:var(--text-muted);}
#hook-list{
  list-style:none; margin:0 0 .75rem; padding:0 .3rem 0 0;
  display:flex; flex-direction:column; gap:.55rem;
  max-height:18rem; overflow-y:auto; overscroll-behavior:contain;
  scrollbar-width:thin; scrollbar-gutter:stable;
}
#hook-list[hidden]{display:none;}
.hook-card{
  background:var(--bg-input); border:1px solid var(--border);
  border-radius:var(--radius-md); padding:.65rem .75rem;
}
.hook-row{display:flex; align-items:flex-start; gap:.55rem;}
.hook-row input{margin:.28rem 0 0; flex:0 0 auto;}
.hook-body{min-width:0; flex:1 1 auto;}
.hook-title-row{display:flex; flex-wrap:wrap; align-items:baseline; gap:.35rem .5rem; margin-bottom:.25rem;}
.hook-phrase{font-weight:600; color:var(--text-main);}
.hook-kind{
  font-size:.68rem; color:var(--accent); font-weight:600;
  background:rgba(196, 164, 106, 0.08); border:1px solid rgba(196, 164, 106, 0.18);
  padding:.1rem .4rem; border-radius:4px;
}
.hook-priority{font-size:.68rem; color:var(--accent); font-weight:600;}
.hook-why{margin:0 0 .3rem; font-size:.78rem; color:var(--text-muted); line-height:1.4;}
.hook-results{margin:.15rem 0 0; padding:0 0 0 1.05rem; display:flex; flex-direction:column; gap:.25rem;}
.hook-results a{color:var(--accent); word-break:break-word;}
.hook-snippet{display:block; font-size:.75rem; color:var(--text-muted); line-height:1.4;}

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
#related-search-btn{height:32px; min-height:32px; padding:0 .75rem; font-size:.76rem; white-space:nowrap;}
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
  box-shadow:inset 0 1px 0 rgba(243, 238, 228, 0.04);
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
.image-card:hover{transform:translateY(-1px); border-color:rgba(196, 164, 106, 0.35);}
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
#config-dialog h2{margin:0; font-size:1.1rem; color:#e0e0e4;}
#config-dialog label{display:flex; flex-direction:column; gap:.4rem; font-size:.85rem; font-weight:600; color:var(--text-muted);}
#config-dialog fieldset{border:0; border-top:1px solid var(--border); padding:1rem 0 0; margin:0; min-width:0;}
#config-dialog legend{font-size:.85rem; font-weight:600; padding:0 6px 0 0; color:#a0a4b0;}
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
  object-fit:contain; border-radius:8px; background:#111; cursor:zoom-out;
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
body.lane-github .rank-toolbar,
body.lane-github #source-health-wrap,
body.lane-github label[for="hours-select"],
body.lane-github #hours-select,
body.lane-github #hours-custom,
body.lane-github #translate-btn,
body.lane-github #hotspot-list,
body.lane-github #article-panel .panel-header .platform-tabs,
body.lane-github #prompt-style-select,
body.lane-github #find-hooks-btn,
body.lane-github #generate-btn,
body.lane-github #article-paste,
body.lane-github #article-empty,
body.lane-github #article-view,
body.lane-github #hook-list,
body.lane-skills .rank-toolbar,
body.lane-skills #source-health-wrap,
body.lane-skills label[for="hours-select"],
body.lane-skills #hours-select,
body.lane-skills #hours-custom,
body.lane-skills #collect-btn,
body.lane-skills #translate-btn,
body.lane-skills #hotspot-list,
body.lane-skills #github-list,
body.lane-skills #github-studio,
body.lane-skills #prompt-style-select,
body.lane-skills #find-hooks-btn,
body.lane-skills #generate-btn,
body.lane-skills #article-paste,
body.lane-skills #article-empty,
body.lane-skills #article-view,
body.lane-skills #hook-list{display:none !important;}
body.lane-github #picked-dock, body.lane-skills #picked-dock { display:none !important; }
#github-list,#github-studio,#skill-search-row,#skill-source-bar,#skill-source-toggle,#skill-crawl-sources,#skills-studio,
#skill-degraded-note,#skill-paste-row,#skill-paste-hint,#skill-paste-status,#skill-token-box,#skill-list{display:none;}
body.lane-github #github-list{display:flex; flex-direction:column; gap:.55rem; margin:0; padding:0; list-style:none;}
body.lane-github #github-studio{display:flex; flex-direction:column; gap:1rem; min-height:0;}
body.lane-skills #skill-search-row{display:flex; gap:.5rem; align-items:center; width:100%; margin:.35rem 0 .55rem;}
body.lane-skills #skill-search-row input{
  flex:1 1 auto; width:auto; min-width:0; height:40px;
}
body.lane-skills #skill-search-btn,
body.lane-skills #skill-clear-btn{
  flex:0 0 auto; min-width:4.75rem; height:40px; padding:0 1.15rem;
  white-space:nowrap; line-height:1; display:inline-flex;
  align-items:center; justify-content:center;
}
body.lane-skills #skill-clear-btn{min-width:3.6rem; padding:0 .85rem;}
/* The paste box is a textarea on purpose: a real paste is multi-line, and the
   box has to grow with it instead of scrolling one line at a time. */
body.lane-skills #skill-paste-row{display:flex; gap:.5rem; align-items:flex-start; width:100%; margin:0 0 .35rem;}
body.lane-skills #skill-paste-hint{display:block; margin:0 0 .55rem;}
/* The paste result keeps its own line: the shared status line is overwritten by
   the render that starts right after a paste, which hid skips and warnings. */
#skill-paste-status{margin:0 0 .55rem; font-size:.78rem; line-height:1.5; word-break:break-word; color:var(--text-muted);}
#skill-paste-status[hidden]{display:none;}
body.lane-skills #skill-paste-status:not([hidden]){display:block;}
body.lane-skills #skill-paste-status.status-ok{color:var(--ok);}
body.lane-skills #skill-paste-status.status-error{color:var(--err);}
body.lane-skills #skill-paste-row textarea{
  flex:1 1 auto; width:auto; min-width:0; min-height:76px; max-height:40vh;
  padding:.6rem .7rem; line-height:1.5; resize:vertical; overflow-y:auto;
  font:inherit; font-size:.85rem;
}
body.lane-skills #skill-paste-btn{
  flex:0 0 auto; min-width:4.75rem; height:40px; padding:0 1.15rem;
  white-space:nowrap; line-height:1; display:inline-flex;
  align-items:center; justify-content:center;
}
body.lane-skills #skill-token-box{display:block; margin:0 0 .6rem;}
#skill-token-box summary{
  cursor:pointer; font-size:.74rem; color:var(--text-muted); font-weight:600;
}
#skill-token-box .skill-token-row{display:flex; gap:.5rem; align-items:center; margin:.45rem 0 .35rem;}
#skill-token-box .skill-token-row input{flex:1 1 auto; min-width:0; height:36px;}
#skill-token-box .skill-token-row button{flex:0 0 auto; height:36px; min-height:36px; padding:0 .9rem;}
#skill-token-status{margin:0; font-size:.72rem; line-height:1.45;}
/* One merged block replaces the stage chips + gap note: it answers the only
   question the user asks here — where did the collected skills come from. */
#skill-source-bar{margin:.15rem 0 .55rem;}
#skill-source-chips{display:flex; flex-wrap:wrap; gap:.4rem; margin:0; padding:0; list-style:none;}
#skill-source-chips li{
  padding:.2rem .65rem; border-radius:99px; border:1px solid var(--border);
  font-size:.72rem; color:var(--text-muted);
}
#skill-source-chips li.is-manual{border-style:dashed; color:var(--text-main);}
#skill-degraded-note{
  margin:.35rem 0 0; font-size:.78rem; line-height:1.5;
  color:var(--text-muted); word-break:break-word;
}
#skill-degraded-note[hidden]{display:none;}
body.lane-skills #skill-source-bar:not([hidden]),
body.lane-skills #skill-degraded-note:not([hidden]){display:block;}
body.lane-skills #skill-source-bar{display:block;}
body.lane-skills #skill-list:not([hidden]){
  display:flex; flex-direction:column; gap:.35rem;
  margin:.15rem 0 .55rem; padding:0; list-style:none;
  max-height:16rem; overflow-y:auto; overscroll-behavior:contain;
}
body.lane-skills #skill-list[hidden]{display:none;}
#skill-list li{
  display:flex; align-items:center; gap:.55rem;
  padding:.4rem .65rem;
  border:1px solid var(--border); border-radius:var(--radius-sm);
  background:var(--bg-card);
}
#skill-list li.is-selected{border-color:rgba(196, 164, 106, 0.45);}
#skill-list input[type=checkbox]{
  margin:0; accent-color:var(--accent); width:1rem; height:1rem; flex:0 0 auto;
}
#skill-list a{color:var(--accent); font-weight:600; text-decoration:none; min-width:0; word-break:break-word;}
#skill-list a:hover{text-decoration:underline;}
#skill-list .skill-source{margin-left:auto; color:var(--text-muted); font-size:.72rem; white-space:nowrap;}
/* Clicking the shared status line expands the list of configured crawlers. */
body.lane-skills #skill-source-toggle{
  display:block; width:100%; text-align:left; cursor:pointer;
  margin:.15rem 0 0; padding:.3rem .55rem; border-radius:var(--radius-sm,6px);
  border:1px dashed var(--border); background:transparent;
  font-size:.76rem; color:var(--text-muted); font-weight:600;
}
body.lane-skills #skill-source-toggle:hover{color:var(--text-main); border-color:var(--accent);}
#skill-source-toggle[hidden]{display:none !important;}
body.lane-skills #skill-crawl-sources{
  display:block; margin:.4rem 0 0; padding:.55rem .7rem; list-style:none;
  border:1px solid var(--border); border-radius:var(--radius-md); background:var(--bg-card);
}
#skill-crawl-sources[hidden]{display:none !important;}
body.lane-skills #skill-crawl-sources li{
  display:flex; flex-direction:column; gap:.15rem; padding:.4rem 0;
  border-bottom:1px solid var(--border); font-size:.76rem; line-height:1.5;
}
body.lane-skills #skill-crawl-sources li:last-child{border-bottom:0;}
body.lane-skills #skill-crawl-sources strong{font-size:.8rem; color:var(--text-main);}
body.lane-skills #skill-crawl-sources a{color:var(--accent); word-break:break-all; font-size:.72rem;}
body.lane-skills #skill-crawl-sources span{color:var(--text-muted);}
#skill-title{font-size:.92rem;}
body.lane-skills #skills-studio > .article-field{flex:0 0 auto;}
body.lane-skills #skills-studio{display:flex; flex-direction:column; gap:1rem; min-height:0; flex:1 1 auto;}
body.lane-skills #skills-gallery{display:flex; justify-content:center; min-height:12rem;}
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
.gh-row{
  display:grid; grid-template-columns:2rem 2.4rem minmax(0,1fr) auto; gap:.7rem;
  align-items:center; padding:.7rem .8rem; border:1px solid var(--border);
  border-radius:var(--radius-md); background:var(--bg-card);
}
.gh-row img{width:2.4rem; height:2.4rem; border-radius:10px; object-fit:cover; background:#111;}
.gh-row strong{display:block; color:var(--text-main); font-size:.92rem;}
.gh-row .gh-kind{color:var(--text-muted); font-size:.75rem; white-space:nowrap;}
.gh-stars{display:flex; flex-direction:column; align-items:flex-end; gap:.25rem; min-width:4.8rem;}
.gh-bar{width:4.6rem; height:5px; border-radius:99px; background:#2c2923; overflow:hidden;}
.gh-bar > span{display:block; height:100%; background:var(--accent); border-radius:99px;}
#github-render-controls{display:none; align-items:center; gap:.45rem; flex-wrap:wrap;}
body.lane-github #github-render-controls,
body.lane-skills #github-render-controls{display:flex;}
body.lane-skills #github-render-btn{display:none;}
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
</style>
</head>
<body>
<header>
  <div class="brand">
    <h1 class="logo">AutoPosting</h1>
    <p class="brand-kicker">本地工作台</p>
  </div>
  <span id="service-status">v1.4.3 · 本地服务</span>
  <button type="button" id="config-btn">设置</button>
  <button type="button" id="favorites-btn">收藏</button>
  <button type="button" id="news-supplement-btn" aria-expanded="false">新闻补充</button>
</header>
<aside id="favorites-drawer" hidden>
  <div class="favorites-drawer-head">
    <h2>收藏</h2>
    <button type="button" id="favorites-close">关闭</button>
  </div>
  <ul id="favorites-list" aria-label="收藏列表"></ul>
</aside>
<aside id="news-supplement-drawer" hidden>
  <div class="favorites-drawer-head">
    <h2>新闻补充</h2>
    <button type="button" id="news-supplement-close">关闭</button>
  </div>
  <p id="news-supplement-status" role="status" aria-live="polite"></p>
  <ul id="news-supplement-list" aria-label="新闻补充列表"></ul>
</aside>
<nav id="app-nav" aria-label="栏目与平台">
  <div class="platform-tabs" role="tablist" aria-label="候选栏">
    <button type="button" class="seg active" data-lane="hotspots" aria-selected="true">热点候选</button>
    <button type="button" class="seg" data-lane="viewpoints" aria-selected="false">观点候选</button>
    <button type="button" class="seg" data-lane="github" aria-selected="false">GitHub 周榜</button>
    <button type="button" class="seg" data-lane="skills" aria-selected="false">Skill查找器</button>
  </div>
  <div class="platform-tabs" role="group" aria-label="平台选择">
    <button type="button" id="seg-wechat" class="seg active" data-platform="wechat" aria-pressed="true">微信</button>
    <button type="button" id="seg-xhs" class="seg" data-platform="xiaohongshu" aria-pressed="false">小红书</button>
  </div>
</nav>
<aside id="wait-banner" hidden role="status" aria-live="assertive">
  <div id="wait-jobs"></div>
</aside>
<main>
  <section class="panel" id="hotspots-panel" aria-labelledby="h-hotspots">
    <div class="panel-header">
      <div>
        <h2 id="h-hotspots" class="panel-title">热点候选</h2>
        <p id="hotspot-sub" class="panel-sub">下面是话题/新闻候选。热度看独立来源数和讨论量；社区帖过阈会升温，一线说法用某某称，不是权威转载</p>
      </div>
      <div class="row">
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
        <button type="button" id="translate-btn">翻译标题</button>
      </div>
    </div>
    <div class="rank-toolbar">
      <div id="rank-tabs" class="platform-tabs" role="tablist" aria-label="排序方式">
        <button type="button" class="seg active" data-rank="heat" aria-selected="true">热度优先</button>
        <button type="button" class="seg" data-rank="time" aria-selected="false">时间优先</button>
      </div>
      <button type="button" id="open-original-btn" disabled>打开原文</button>
    </div>
    <div id="hotspot-pane">
      <div id="skill-search-row">
        <input id="skill-query" type="text" maxlength="200" aria-label="Skill 查找关键词" placeholder="关键词，空格分隔多个：视频 剪辑 字幕">
        <button type="button" id="skill-search-btn">查找</button>
        <button type="button" id="skill-clear-btn" title="清空已收录的 skill（标题保留）">清空</button>
      </div>
      <div id="skill-paste-row">
        <textarea id="skill-links" rows="3" aria-label="粘贴 skill 链接" title="粘贴 skill 链接（可多行、可多个），加入后和检索到的一样出简介和海报" placeholder="每行一个 skill 链接，可以粘贴多条"></textarea>
        <button type="button" id="skill-paste-btn">加入</button>
      </div>
      <p id="skill-paste-hint" class="muted">支持 GitHub / skills.sh 的 skill 链接；可多行、每行一条，加入后和检索到的一样出海报。</p>
      <p id="skill-paste-status" role="status" aria-live="polite" hidden></p>
      <details id="skill-token-box">
        <summary>GitHub Token（可选）</summary>
        <div class="skill-token-row">
          <input id="skill-github-token" type="password" autocomplete="off" aria-label="GitHub Token" title="粘贴 GitHub Personal Access Token；只存本机钥匙串，不回显">
          <button type="button" id="skill-token-save">保存</button>
          <button type="button" id="skill-token-clear">清除</button>
        </div>
        <p id="skill-token-status" class="muted">未配置。不配也能用，但 GitHub 接口每小时只给 60 次，容易限流。</p>
      </details>
      <div id="skill-source-bar">
        <ul id="skill-source-chips" aria-label="已收录 skill 的来源"></ul>
        <p id="skill-degraded-note" role="status" hidden></p>
      </div>
      <ul id="skill-list" hidden aria-label="已收录的 skill"></ul>
      <ul id="hotspot-list"></ul>
      <ul id="github-list"></ul>
      <p id="hotspot-status" role="status" aria-live="polite"></p>
      <button type="button" id="skill-source-toggle" aria-expanded="false" aria-controls="skill-crawl-sources" hidden></button>
      <ul id="skill-crawl-sources" hidden aria-label="已配置的自动抓取源"></ul>
    </div>
    <div id="source-health-wrap">
      <button type="button" id="source-health-toggle" aria-expanded="false" aria-controls="source-health">订阅源状态</button>
      <p id="source-health-legend" class="source-legend">这些不是新闻。抓取成功=这个订阅上次拉 RSS 没报错；「只刷这个」只重新抓这一路。</p>
      <ul id="source-health" hidden aria-label="来源健康"></ul>
    </div>
  </section>
  <section class="panel" id="article-panel" aria-labelledby="h-article">
    <h2 id="h-article" class="sr-only">文章</h2>
    <div class="panel-header">
      <div class="row">
      <select id="prompt-style-select" aria-label="生成风格" title="生成风格">
        <option value="AI专家" selected>AI专家</option>
        <option value="经验分享">经验分享</option>
      </select>
      <button type="button" id="find-hooks-btn" disabled>找切口</button>
      <button type="button" id="generate-btn" class="btn-magic" disabled>生成文章</button>
      <textarea id="article-paste" rows="1" placeholder="可粘贴原文页面，点生成文章时一并送进模型"></textarea>
      </div>
    </div>
    <p id="article-status" role="status" aria-live="polite"></p>
    <ul id="hook-list" hidden></ul>
    <p id="article-empty">尚未生成文章</p>
    <div id="github-studio">
      <div class="gh-toolbar">
        <button type="button" id="github-select-all" disabled>全选</button>
        <button type="button" id="github-export-btn" disabled>导出选中图片</button>
        <button type="button" id="github-copy-btn" disabled>复制小红书文案</button>
      </div>
      <div id="github-gallery" class="gh-gallery"></div>
      <div class="image-carousel-nav" id="github-gallery-nav" hidden>
        <button type="button" id="github-gallery-prev">上一张</button>
        <span id="github-gallery-counter" class="image-carousel-counter">0 / 0</span>
        <button type="button" id="github-gallery-next">下一张</button>
      </div>
      <label class="article-field">小红书标题<input id="github-xhs-title" type="text" readonly value="本周值得关注的github ai项目前10"></label>
      <label class="article-field">小红书正文<textarea id="github-xhs-body" rows="12"></textarea></label>
      <canvas id="github-canvas" hidden></canvas>
    </div>
    <div id="skills-studio">
      <label class="article-field" for="skill-title">标题
        <input id="skill-title" type="text" maxlength="120" placeholder="封面海报标题；同时作为介绍文标题">
      </label>
      <div class="gh-toolbar">
        <button type="button" id="skills-render-btn">生成图片</button>
        <button type="button" id="skills-select-all" disabled>全选</button>
        <button type="button" id="skills-export-btn" disabled>导出选中图片</button>
      </div>
      <div id="skills-gallery" class="gh-gallery"></div>
      <div class="image-carousel-nav" id="skills-gallery-nav" hidden>
        <button type="button" id="skills-gallery-prev">上一张</button>
        <span id="skills-gallery-counter" class="image-carousel-counter">0 / 0</span>
        <button type="button" id="skills-gallery-next">下一张</button>
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
            <p class="panel-sub">原文配图。上一张 / 下一张切换；点击预览可看大图；勾选封面和正文。默认只勾选封面，正文图需另行勾选。</p>
            <button type="button" id="image-rescan">重新扫描</button>
          </div>
          <div id="image-picker-list"></div>
          <div class="image-carousel-nav" id="image-picker-nav" hidden>
            <button type="button" id="image-picker-prev">上一张</button>
            <span id="image-picker-counter" class="image-carousel-counter">0 / 0</span>
            <button type="button" id="image-picker-next">下一张</button>
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
            <button type="button" id="related-search-btn" disabled>相关检索</button>
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
  hours: 4,
  rank: "heat",
  lane: "hotspots",
  newsStyle: "AI专家",
  rankings: {heat: [], time: []},
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
  newsSupplements: [],
  articleSessions: {},
  articleLastKeys: {},
  articleRequestGen: 0,
  relatedSearchGen: 0,
  githubWeekly: {repos: [], xhs_title: "本周值得关注的github ai项目前10", xhs_body: ""},
  githubTheme: "dark",
  skillsPackage: {query: "", title: "", user_title: "", theme: "", stages: [], skills: [], article: {title: "", body: ""}, images: [], crawl_sources: []},
  crawlSourcesOpen: false,
  githubTokenConfigured: false,
  modelEndpoints: [],
  platforms: {wechat: {configured: false}, xiaohongshu: {account_configured: false}, x: {configured: false}},
  providerDraft: {open: false, mode: "add", endpointId: "", models: []},
  originalGen: 0,
  originalItemId: "",
  originalTranslatable: false,
  newsFocus: null,
  hotspotGen: 0,
  collectGen: 0,
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
  node.textContent = text;
  node.className = kind ? "status-" + kind : "";
}

var WAIT_HINTS = {
  collect: {title: "正在抓取订阅源", hint: "通常 10–30 秒，源越多越久"},
  collect_one: {title: "正在刷新这一路订阅", hint: "通常几秒到十几秒"},
  viewpoints: {title: "正在抓取 Reddit 热帖", hint: "通常 10–30 秒"},
  translate: {title: "正在翻译标题", hint: "通常 5–20 秒，看条数和模型"},
  github_fetch: {title: "正在抓取 GitHub 周榜", hint: "通常 5–20 秒；配了榜单模型会再改写简介"},
  github_render: {title: "正在生成周榜图片", hint: "每张大约 8–25 秒；11 张大约 2–5 分钟", unit: "张"},
  generate: {title: "正在生成文章", hint: "通常 15–90 秒，看模型和篇幅"},
  hooks: {title: "正在找切口", hint: "通常 10–40 秒"},
  related_search: {title: "正在检索相关新闻", hint: "通常几秒到几十秒"},
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
  state.sources = sources || [];
  var list = $("source-health");
  if (!list) { return; }
  list.textContent = "";
  var counts = {ok: 0, error: 0, stale: 0, never: 0, disabled: 0};
  state.sources.forEach(function (source) {
    var statusKey = source.status || "never";
    counts[statusKey] = (counts[statusKey] || 0) + 1;
    var row = el("li", "source-health");
    var dot = el("span", "sh-dot " + statusKey);
    row.appendChild(dot);
    row.appendChild(el("span", "sh-name", source.name || source.source_id));
    var detail = sourceStatusLabel(statusKey);
    if (statusKey === "error" && source.last_error) {
      detail = "抓取失败：" + String(source.last_error).slice(0, 36);
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
  invalidateRelatedSearch();
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
  if ($("related-search-btn")) { $("related-search-btn").disabled = true; }
  status("article-status", "");
  status("publish-status", "");
}

function articleContextKey(lane, platform) {
  return String(lane || "hotspots") + "|" + String(platform || "wechat");
}

function articleTargetIds() {
  var picked = pickedIdList();
  if (picked.length) { return picked; }
  return state.item && state.item.item_id ? [String(state.item.item_id)] : [];
}

function articleSessionKey(lane, platform, ids) {
  return articleContextKey(lane, platform) + "|" + (ids || []).slice().sort().join(",");
}

function currentArticleSessionKey() {
  var context = articleContextKey(state.lane, state.platform);
  return state.articleLastKeys[context] || articleSessionKey(state.lane, state.platform, articleTargetIds());
}

function rememberArticleSession(lane) {
  if (lane !== "hotspots" && lane !== "viewpoints") { return ""; }
  var ids = articleTargetIds();
  var context = articleContextKey(lane, state.platform);
  var key = ids.length ? articleSessionKey(lane, state.platform, ids) : (state.articleLastKeys[context] || "");
  if (!key) { return ""; }
  state.articleSessions[key] = {
    item: state.item,
    draft: state.draft,
    title: $("article-title").value,
    body: $("article-body").value,
    articleDirty: state.articleDirty,
    creatorUrl: state.creatorUrl,
  };
  state.articleLastKeys[context] = key;
  return key;
}

function storeGeneratedArticle(key, data, item) {
  state.articleSessions[key] = {
    item: item || null,
    draft: data,
    title: data.title || "",
    body: data.body || "",
    articleDirty: false,
    creatorUrl: "",
  };
  var separator = key.indexOf("|");
  var context = separator >= 0 ? key.slice(0, key.lastIndexOf("|")) : key;
  state.articleLastKeys[context] = key;
}

function restoreArticleSession(lane) {
  if (lane !== "hotspots" && lane !== "viewpoints") { return false; }
  var key = state.articleLastKeys[articleContextKey(lane, state.platform)] || "";
  var session = key ? state.articleSessions[key] : null;
  if (!session) {
    resetArticle();
    return false;
  }
  state.item = session.item || null;
  state.draft = session.draft || null;
  state.creatorUrl = session.creatorUrl || "";
  state.articleDirty = !!session.articleDirty;
  if (!state.draft) {
    resetArticle();
    $("find-hooks-btn").disabled = !state.item;
    $("open-original-btn").disabled = !state.item;
    syncGenerateEnabled();
    return false;
  }
  renderArticle(state.draft);
  $("article-title").value = session.title || "";
  $("article-body").value = session.body || "";
  state.articleDirty = !!session.articleDirty;
  $("publish-btn").disabled = false;
  $("schedule-btn").disabled = false;
  $("find-hooks-btn").disabled = !state.item;
  $("open-original-btn").disabled = !state.item;
  syncGenerateEnabled();
  syncRelatedSearchEnabled();
  return true;
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

function endpointLabel(endpoint) {
  return endpoint.base_url + " · " + endpoint.models.length + " 个模型";
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

function renderSettingsModels(purpose, models, selected) {
  var form = $("config-form");
  var names = {article: "model", translation: "translation_model", leaderboard: "leaderboard_model"};
  var select = form.elements[names[purpose]];
  fillSelect(
    select,
    (models || []).map(function (model) { return {value: model, label: model}; }),
    selected,
    "先选择提供方"
  );
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
    $("find-hooks-btn").disabled = false;
    $("open-original-btn").disabled = false;
    syncGenerateEnabled();
    if (typeof renderPickedDock === "function") renderPickedDock();
    return;
  }
  state.newsFocus = null;
  state.item = item;
  clearHooks();
  var nodes = $("hotspot-list").querySelectorAll(".hotspot-item");
  nodes.forEach(function (node) {
    var active = node === button;
    node.setAttribute("aria-pressed", String(active));
    node.classList.toggle("selected", active);
  });
  $("find-hooks-btn").disabled = false;
  $("open-original-btn").disabled = false;
  syncGenerateEnabled();
  if (typeof renderPickedDock === "function") renderPickedDock();
}

function clearHotspotSelection(keepArticle) {
  state.item = null;
  state.newsFocus = null;
  if (!keepArticle) {
    state.articleRequestGen += 1;
    resetArticle();
  }
  clearHooks();
  closeOriginal();
  $("find-hooks-btn").disabled = true;
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
  state.newsFocus = null;
  var list = $("hotspot-list");
  if (list) {
    list.querySelectorAll(".hotspot-item").forEach(function (node) {
      node.setAttribute("aria-pressed", "false");
      node.classList.remove("selected");
    });
  }
  $("find-hooks-btn").disabled = true;
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
  button.disabled = !(state.item || pickedIdList().length);
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
  if (typeof renderPickedDock === "function") renderPickedDock();
}

function renderPickedDock() {
  var dock = $("picked-dock");
  if (!dock) { return; }
  var ids = pickedIdList();
  dock.textContent = "";
  // 焦点与「已选 N」是两个互不覆盖的分组：多选非空时焦点仍然可见。
  if (state.newsFocus) {
    dock.hidden = false;
    dock.appendChild(el("span", "picked-dock-label", "焦点"));
    var newsFocusChip = el("span", "picked-dock-chip is-static");
    newsFocusChip.appendChild(el("span", "picked-dock-chip-label", state.newsFocus.title || ""));
    dock.appendChild(newsFocusChip);
  } else if (state.item) {
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
  if (!state.newsFocus && !state.item) {
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

function focusNewsSupplement(item) {
  if (!item || typeof item !== "object") { return; }
  var title = String(item.title || item.name || item.url || item.link || "相关新闻").trim();
  if (!title) { return; }
  state.newsFocus = {title: title, url: String(item.url || item.link || "").trim()};
  renderNewsSupplements();
  renderPickedDock();
}

function selectedArticleContext() {
  var body = $("article-body");
  if (!state.draft || !body) { return null; }
  var start = Number(body.selectionStart);
  var end = Number(body.selectionEnd);
  if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) { return null; }
  var text = body.value || "";
  var selected = text.slice(start, end).trim();
  if (!selected) { return null; }
  var paragraphs = [];
  var cursor = 0;
  var separator = /\\r?\\n[ \\t]*\\r?\\n/g;
  var match;
  while ((match = separator.exec(text))) {
    paragraphs.push({start: cursor, end: match.index});
    cursor = match.index + match[0].length;
  }
  paragraphs.push({start: cursor, end: text.length});
  var first = paragraphs.findIndex(function (paragraph) {
    return start < paragraph.end && end > paragraph.start;
  });
  var lastPosition = Math.max(start, end - 1);
  var last = paragraphs.findIndex(function (paragraph) {
    return lastPosition >= paragraph.start && lastPosition < paragraph.end;
  });
  if (first < 0) { first = 0; }
  if (last < first) { last = first; }
  var contextStart = paragraphs[Math.max(0, first - 1)].start;
  var contextEnd = paragraphs[Math.min(paragraphs.length - 1, last + 1)].end;
  return {
    body: text,
    selected_text: selected,
    selection_start: start,
    selection_end: end,
    context: text.slice(contextStart, contextEnd),
  };
}

function syncRelatedSearchEnabled() {
  var button = $("related-search-btn");
  if (!button) { return; }
  button.disabled = !selectedArticleContext();
}

function renderNewsSupplements() {
  var list = $("news-supplement-list");
  if (!list) { return; }
  list.textContent = "";
  var items = Array.isArray(state.newsSupplements) ? state.newsSupplements : [];
  if (!items.length) {
    list.appendChild(el("li", "muted", "暂无相关新闻"));
    return;
  }
  items.forEach(function (item) {
    if (!item || typeof item !== "object") { return; }
    var row = el("li", "news-supplement-item");
    var title = String(item.title || item.name || item.url || item.link || "相关新闻");
    var url = String(item.url || item.link || item.source_url || "").trim();
    var isFocus = state.newsFocus && state.newsFocus.url === url && state.newsFocus.title === title;
    row.classList.toggle("is-focus", !!isFocus);
    if (/^https?:\/\//i.test(url)) {
      var link = document.createElement("a");
      link.href = url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = title;
      if (isFocus) { link.setAttribute("aria-current", "true"); }
      link.addEventListener("click", function () { focusNewsSupplement(item); });
      row.appendChild(link);
    } else {
      var newsTitle = el("span", "news-title", title);
      newsTitle.tabIndex = 0;
      newsTitle.setAttribute("role", "button");
      if (isFocus) { newsTitle.setAttribute("aria-current", "true"); }
      newsTitle.addEventListener("click", function () { focusNewsSupplement(item); });
      newsTitle.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          focusNewsSupplement(item);
        }
      });
      row.appendChild(newsTitle);
    }
    var summary = String(item.snippet || item.summary || item.description || "").trim();
    if (summary) { row.appendChild(el("span", "news-summary", summary)); }
    var meta = [item.source || item.source_id || "", item.published_at || item.published || ""]
      .map(function (value) { return String(value || "").trim(); })
      .filter(Boolean);
    if (meta.length) { row.appendChild(el("span", "news-meta", meta.join(" · "))); }
    list.appendChild(row);
  });
}

function invalidateRelatedSearch() {
  state.relatedSearchGen += 1;
  if (waitRunning("related_search")) { waitStop("related_search"); }
  state.newsSupplements = [];
  state.newsFocus = null;
  renderNewsSupplements();
  renderPickedDock();
}

function toggleNewsSupplementDrawer(open) {
  var next = open === undefined ? !document.body.classList.contains("news-supplement-open") : !!open;
  if (next) { toggleFavoritesDrawer(false); }
  document.body.classList.toggle("news-supplement-open", next);
  var drawer = $("news-supplement-drawer");
  var button = $("news-supplement-btn");
  if (drawer) { drawer.hidden = !next; }
  if (button) { button.setAttribute("aria-expanded", String(next)); }
  if (next) { renderNewsSupplements(); }
}

var RELATED_SEARCH_FAILURE_HINTS = {
  timeout: "检索超时：这段选区太久没有源返回结果。可以选短一点，或稍后重试。",
  transport_unavailable: "网络不可达：新闻源当前都连不上。稍后重试，或检查代理设置。",
  parse_error: "检索结果解析失败：源返回的内容无法识别，换一段选区再试。"
};

function relatedSearchFailureMessage(error) {
  var cause = String((error && error.code) || "");
  var hint = RELATED_SEARCH_FAILURE_HINTS[cause] || "";
  if (hint) { return hint; }
  return showApiError("相关检索失败：", error);
}

function relatedSearch() {
  var selection = selectedArticleContext();
  if (!selection) {
    syncRelatedSearchEnabled();
    status("article-status", "请先选中正文中的关键词或句子", "error");
    return;
  }
  if (!state.draft || waitRunning("related_search")) { return; }
  var requestDraftId = state.draft.draft_id;
  var requestSessionKey = currentArticleSessionKey();
  var gen = ++state.relatedSearchGen;
  var button = $("related-search-btn");
  state.newsSupplements = [];
  renderNewsSupplements();
  toggleNewsSupplementDrawer(true);
  status("news-supplement-status", state.articleDirty ? "正在保存修改并检索相关新闻…" : "正在检索相关新闻…");
  busy(button, true);
  waitStart("related_search");
  var save = state.articleDirty ? persistArticle("related_search") : Promise.resolve(state.draft);
  save.then(function () {
    if (waitCancelled("related_search") || gen !== state.relatedSearchGen) { return null; }
    if (!state.draft || state.draft.draft_id !== requestDraftId || currentArticleSessionKey() !== requestSessionKey) { return null; }
    var body = $("article-body");
    body.focus();
    body.setSelectionRange(selection.selection_start, selection.selection_end);
    var currentSelection = selectedArticleContext() || selection;
    return waitApi("related_search", "/api/related-search", Object.assign({
      draft_id: state.draft.draft_id,
    }, currentSelection));
  }).then(function (data) {
    if (!data || waitCancelled("related_search") || gen !== state.relatedSearchGen) { return; }
    if (!state.draft || state.draft.draft_id !== requestDraftId || currentArticleSessionKey() !== requestSessionKey) { return; }
    state.newsSupplements = Array.isArray(data.items) ? data.items : [];
    renderNewsSupplements();
    // 「没找到」和「检索失败」是两回事：空结果不是错误。
    var empty = String(data.search_status || "") === "empty" || !state.newsSupplements.length;
    status(
      "news-supplement-status",
      state.newsSupplements.length
        ? "找到 " + state.newsSupplements.length + " 条相关新闻"
        : "没找到相关新闻：检索词「" + String(data.query || "").slice(0, 24) + "」没有匹配，换一段更具体的选区再试",
      state.newsSupplements.length ? "ok" : ""
    );
    waitDone(state.newsSupplements.length ? "相关新闻已找到" : "没找到相关新闻", "related_search");
  }).catch(function (error) {
    if (isWaitAbort(error, "related_search")) { return; }
    if (gen !== state.relatedSearchGen || !state.draft || state.draft.draft_id !== requestDraftId || currentArticleSessionKey() !== requestSessionKey) { return; }
    var message = relatedSearchFailureMessage(error);
    status("news-supplement-status", message, "error");
    waitFail(message, "related_search");
  }).then(function () {
    if (gen === state.relatedSearchGen) { busy(button, false); }
  });
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
  if (next) { toggleNewsSupplementDrawer(false); }
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
  if (data.sources) { renderSourceHealth(data.sources); }
  setRank(state.rank);
  var shown = (state.rankings[state.rank] || []).length;
  var text = shown ? "共 " + shown + " 条候选" : "暂无候选";
  if (extraStatus) { text += extraStatus; }
  status("hotspot-status", text, shown ? "ok" : "");
}

function loadHotspots(translate, signal) {
  var gen = ++state.hotspotGen;
  var url = "/api/hotspots?hours=" + encodeURIComponent(currentHours())
    + "&lane=" + encodeURIComponent(state.lane || "hotspots");
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
    if (state.lane !== "github") {
      status("hotspot-status", shown ? "共 " + shown + " 条候选 · 已尝试翻译标题" : "暂无候选", shown ? "ok" : "");
    }
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
  if ((state.lane || "hotspots") === "github") {
    refreshGithubWeekly();
    return;
  }
  if (waitRunning("collect")) { return; }
  var lane = state.lane || "hotspots";
  var requestGen = ++state.collectGen;
  var hours = currentHours();
  if (sourceId) {
    waitStart("collect", {title: "正在刷新这一路订阅"});
  } else if (lane === "viewpoints") {
    waitStart("collect", {title: "正在抓取 Reddit 热帖"});
  } else {
    waitStart("collect");
  }
  status("hotspot-status", sourceId ? "正在刷新该源，已有候选会留在上面…" : (lane === "viewpoints" ? "正在从 Reddit 抓取当前热帖，已有候选会留在上面…" : "正在从各订阅源抓取，已有候选会留在上面…"));
  var payload = {hours: hours, force: true, lane: lane};
  if (sourceId) { payload.source_id = sourceId; }
  waitApi("collect", "/api/collect", payload).then(function (data) {
    if (waitCancelled("collect")) { return; }
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
    if (state.lane === lane) {
      applyHotspots(data, extra);
      if (!rankedCount(data)) {
        return widenIfEmpty(data, hours).then(function (next) {
          if (next && failed && state.lane === lane) {
            $("hotspot-status").className = "status-error";
          }
          waitDone(doneText, "collect");
        });
      }
      if (failed) { $("hotspot-status").className = "status-error"; }
    }
    waitDone(doneText, "collect");
  }).catch(function (error) {
    if (isWaitAbort(error, "collect")) { return; }
    if (requestGen !== state.collectGen) {
      waitDone("已切换栏目", "collect");
      return;
    }
    status("hotspot-status", "抓取失败：" + error.message, "error");
    waitFail(error.message || "抓取失败", "collect");
    if (state.lane === lane) { loadHotspots(); }
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
function paintCarouselNav(navId, prevId, nextId, counterId, index, total) {
  var nav = $(navId);
  if (nav) { nav.hidden = total < 2; }
  var prev = $(prevId);
  var next = $(nextId);
  if (prev) { prev.disabled = total < 2; }
  if (next) { next.disabled = total < 2; }
  var counter = $(counterId);
  if (counter) { counter.textContent = total ? ((index + 1) + " / " + total) : "0 / 0"; }
}
function stepImagePicker(delta) {
  var assets = normalizeClientAssets(state.draft && state.draft.assets);
  var total = (assets.candidates || []).length;
  state.imagePickerIndex = clampCarouselIndex((state.imagePickerIndex || 0) + delta, total);
  renderImagePicker(assets);
}
function stepGithubGallery(delta) {
  state.githubGalleryIndex = clampCarouselIndex((state.githubGalleryIndex || 0) + delta, githubImages().length);
  renderGithubGallery();
}
function stepSkillsGallery(delta) {
  state.skillsGalleryIndex = clampCarouselIndex((state.skillsGalleryIndex || 0) + delta, skillImages().length);
  renderSkillsGallery();
}

function renderImagePicker(raw) {
  var picker = $("image-picker");
  var list = $("image-picker-list");
  var assets = normalizeClientAssets(raw);
  if (state.draft) { state.draft.assets = assets; }
  list.textContent = "";
  if (!state.draft) {
    picker.hidden = true;
    paintCarouselNav("image-picker-nav", "image-picker-prev", "image-picker-next", "image-picker-counter", 0, 0);
    return;
  }
  picker.hidden = false;
  if (!assets.candidates.length) {
    list.appendChild(el("p", "muted", "未从原文找到配图。可点重新扫描。"));
    var sourceLine = "来源：" + (state.draft.source_ids || []).join(", ");
    $("article-sources").textContent = sourceLine + " · 无配图";
    paintCarouselNav("image-picker-nav", "image-picker-prev", "image-picker-next", "image-picker-counter", 0, 0);
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
  paintCarouselNav("image-picker-nav", "image-picker-prev", "image-picker-next", "image-picker-counter", index, assets.candidates.length);
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
  syncRelatedSearchEnabled();
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

state.hooks = [];
state.chosenHookIds = [];
state.findHooksGen = 0;

function clearHooks() {
  state.hooks = [];
  state.chosenHookIds = [];
  $("hook-list").textContent = "";
  $("hook-list").hidden = true;
}

function selectedHookCards() {
  return state.chosenHookIds.map(function (id) {
    return state.hooks.find(function (hook) { return hook.hook_id === id; });
  }).filter(Boolean);
}

function relabelHookCards() {
  $("hook-list").querySelectorAll(".hook-card").forEach(function (card) {
    var label = card.querySelector(".hook-priority");
    if (!label) { return; }
    var pos = state.chosenHookIds.indexOf(card.dataset.hookId);
    label.textContent = pos === 0 ? "主切口" : (pos > 0 ? "须进开头" : "");
  });
}

function renderHooks(hooks) {
  state.hooks = hooks || [];
  state.chosenHookIds = [];
  var list = $("hook-list");
  list.textContent = "";
  state.hooks.forEach(function (hook) {
    var card = el("li", "hook-card");
    card.dataset.hookId = hook.hook_id;
    var row = el("div", "hook-row");
    var check = document.createElement("input");
    check.type = "checkbox";
    check.addEventListener("change", function () {
      var id = hook.hook_id;
      var pos = state.chosenHookIds.indexOf(id);
      if (check.checked) {
        if (pos < 0) { state.chosenHookIds.push(id); }
      } else if (pos >= 0) {
        state.chosenHookIds.splice(pos, 1);
      }
      relabelHookCards();
    });
    row.appendChild(check);
    var body = el("div", "hook-body");
    var titleRow = el("div", "hook-title-row");
    titleRow.appendChild(el("span", "hook-phrase", hook.phrase || ""));
    titleRow.appendChild(el("span", "hook-kind", hook.kind || ""));
    titleRow.appendChild(el("span", "hook-priority", ""));
    body.appendChild(titleRow);
    if (hook.why) { body.appendChild(el("p", "hook-why", hook.why)); }
    var results = el("ul", "hook-results");
    (hook.results || []).slice(0, 3).forEach(function (result) {
      var item = document.createElement("li");
      var link = document.createElement("a");
      link.href = result.url || "";
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = result.title || result.url || "";
      item.appendChild(link);
      if (result.snippet) { item.appendChild(el("span", "hook-snippet", result.snippet)); }
      results.appendChild(item);
    });
    if (results.childNodes.length) { body.appendChild(results); }
    row.appendChild(body);
    card.appendChild(row);
    list.appendChild(card);
  });
  list.hidden = state.hooks.length === 0;
}

function findHooks() {
  if (!state.item) { return; }
  var itemId = state.item.item_id;
  var gen = ++state.findHooksGen;
  var button = $("find-hooks-btn");
  busy(button, true);
  waitStart("hooks");
  status("article-status", "正在找切口…");
  waitApi("hooks", "/api/hooks/find", {item_id: itemId}).then(function (data) {
    if (waitCancelled("hooks")) { return; }
    if (!state.item || state.item.item_id !== itemId || gen !== state.findHooksGen) { return; }
    renderHooks(data.hooks || []);
    status("article-status", (data.hooks && data.hooks.length) ? "" : (data.message || "未发现可用切口"), (data.hooks && data.hooks.length) ? "" : "error");
    waitDone((data.hooks && data.hooks.length) ? ("找到 " + data.hooks.length + " 个切口") : "未发现可用切口", "hooks");
  }).catch(function (error) {
    if (isWaitAbort(error, "hooks")) { return; }
    if (!state.item || state.item.item_id !== itemId || gen !== state.findHooksGen) { return; }
    status("article-status", showApiError("找切口失败：", error), "error");
    waitFail(error.message || "找切口失败", "hooks");
  }).then(function () {
    if (gen === state.findHooksGen) { busy(button, false); }
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
  var picked = pickedIdList();
  if (!state.item && !picked.length) { return; }
  var requestLane = state.lane;
  var requestPlatform = state.platform;
  var requestItem = state.item;
  var requestIds = picked.length ? picked.slice() : [state.item.item_id];
  var requestKey = articleSessionKey(requestLane, requestPlatform, requestIds);
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
  if (picked.length >= 1) {
    payload.item_ids = picked;
  } else {
    payload.item_id = state.item.item_id;
  }
  var chosen = selectedHookCards();
  if (chosen.length > 0) {
    payload.chosen_hooks = chosen;
  }
  var pasted = String((($("article-paste") || {}).value) || "").trim();
  if (pasted) { payload.pasted_text = pasted.slice(0, 20000); }
  if (waitRunning("generate")) { return; }
  busy(button, true);
  waitStart("generate");
  status("article-status", "正在生成文章…");
  waitApi("generate", "/api/articles", payload).then(function (data) {
    if (waitCancelled("generate") || requestGen !== state.articleRequestGen) { return; }
    storeGeneratedArticle(requestKey, data, requestItem);
    if (state.lane !== requestLane || state.platform !== requestPlatform) { return; }
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
    state.articleLastKeys[articleContextKey(state.lane, state.platform)] = "";
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
  var repos = state.githubWeekly.repos || [];
  var list = $("github-list");
  list.textContent = "";
  repos.forEach(function (repo) {
    var row = el("li", "gh-row");
    row.appendChild(el("span", "muted", String(repo.rank)));
    var img = document.createElement("img");
    img.src = githubIcon(repo.icon_url);
    img.alt = repo.name;
    row.appendChild(img);
    var meta = document.createElement("div");
    meta.appendChild(el("strong", "", repo.name));
    meta.appendChild(el("span", "gh-kind", (repo.kind || repo.language || "AI 工具") + (repo.fresh ? " · 本周新建" : " · 本周涨星")));
    row.appendChild(meta);
    var stars = el("div", "gh-stars");
    var bar = el("div", "gh-bar");
    var fill = document.createElement("span");
    fill.style.width = Math.max(8, Math.round((repo.star_share || 0) * 100)) + "%";
    bar.appendChild(fill);
    stars.appendChild(bar);
    stars.appendChild(el("span", "", repo.stars_label || String(repo.stars || 0)));
    row.appendChild(stars);
    list.appendChild(row);
  });
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
    paintCarouselNav("github-gallery-nav", "github-gallery-prev", "github-gallery-next", "github-gallery-counter", 0, 0);
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
  paintCarouselNav("github-gallery-nav", "github-gallery-prev", "github-gallery-next", "github-gallery-counter", index, total);
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
  status("hotspot-status", "正在读取周榜…");
  return fetch("/api/github-weekly").then(function (response) { return response.json(); }).then(function (data) {
    renderGithubWeekly(data);
    var count = (data.repos || []).length;
    status("hotspot-status", count ? "已显示 " + count + " 个仓库，可再点抓取周榜刷新" : "还没有周榜。点「抓取周榜」拉取本周新建的 AI 仓库，不够再用本周涨星补齐。", count ? "ok" : "");
    return data;
  }).catch(function (error) {
    status("hotspot-status", "读取周榜失败：" + error.message, "error");
  });
}
function refreshGithubWeekly() {
  if (waitRunning("github_fetch")) { return; }
  waitStart("github_fetch");
  status("hotspot-status", "正在按星标抓取本周 GitHub AI 项目…");
  waitApi("github_fetch", "/api/github-weekly", {}).then(function (data) {
    if (waitCancelled("github_fetch")) { return; }
    renderGithubWeekly(data);
    var count = (data.repos || []).length;
    var extra = data.model_used ? " · 榜单模型已改写简介" : " · 未配置榜单模型，简介用仓库描述";
    if (state.lane === "github") {
      status("hotspot-status", (count ? "已抓取 " + count + " 个仓库" : "没有抓到仓库") + extra + "。选配色后点「生成图片」。", count ? "ok" : "error");
    }
    waitDone(count ? ("已抓取 " + count + " 个仓库") : "没有抓到仓库", "github_fetch");
  }).catch(function (error) {
    if (isWaitAbort(error, "github_fetch")) { return; }
    status("hotspot-status", "抓取周榜失败：" + error.message, "error");
    waitFail(error.message || "抓取周榜失败", "github_fetch");
  });
}
function githubThemeDefaults(theme) {
  return theme === "light"
    ? {main: "#F3EFE4", secondary: "#FFFCF6", accent: "#5C4FD6"}
    : {main: "#06155A", secondary: "#101F63", accent: "#F3F0E8"};
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
  var defaults = githubThemeDefaults(state.githubTheme);
  if ($("gh-color-main")) { $("gh-color-main").value = defaults.main; }
  if ($("gh-color-secondary")) { $("gh-color-secondary").value = defaults.secondary; }
  if ($("gh-color-accent")) { $("gh-color-accent").value = defaults.accent; }
}
function renderGithubWeeklyImages() {
  var button = $("github-render-btn");
  if (!(state.githubWeekly.repos || []).length) {
    status("hotspot-status", "请先点「抓取周榜」，再生成图片", "error");
    return;
  }
  var images = githubImages();
  if (!images.length) {
    status("hotspot-status", "没有可生成的图片任务", "error");
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
  status("hotspot-status", "开始生成 1 / " + total + " 张周榜图…");
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
        status("hotspot-status", "出图失败：全部 " + total + " 张都没有生成", "error");
      } else {
        waitDone(failed ? ("已生成 " + done + " / " + total + " 张，失败 " + failed + " 张") : ("已全部生成 " + done + " 张"), "github_render");
        status("hotspot-status", failed ? ("已生成 " + done + " 张，失败 " + failed + " 张") : ("已全部生成 " + done + " 张周榜图，勾选后可导出"), failed ? "error" : "ok");
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
    if (state.lane === "github") {
      status("hotspot-status", "正在生成第 " + (index + 1) + " / " + total + " 张 · " + (job.title || job.id));
    }
    renderGithubGallery();
    // 逐张调用 /api/github-weekly/image；全量 /api/github-weekly/render 仍给脚本用
    waitApi("github_render", "/api/github-weekly/image", {id: job.id, theme: theme, palette: palette}).then(function (data) {
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
      status("hotspot-status", "小红书文案已复制", "ok");
    }).catch(function () {
      $("github-xhs-body").select();
      status("hotspot-status", "请手动复制文案", "");
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
function drawGithubCover(ctx, repos, icons) {
  ctx.fillStyle = "#141210";
  ctx.fillRect(0, 0, 1080, 1920);
  ctx.fillStyle = "#d4a574";
  ctx.font = "600 22px ui-sans-serif, system-ui, sans-serif";
  ctx.fillText("WEEKLY", 72, 110);
  ctx.fillStyle = "#f4efe6";
  ctx.font = "700 96px ui-sans-serif, system-ui, sans-serif";
  ctx.fillText("GitHub", 72, 230);
  ctx.fillStyle = "#d4a574";
  ctx.font = "500 36px ui-sans-serif, system-ui, sans-serif";
  ctx.fillText("本周爆火ai工具", 72, 292);
  repos.forEach(function (repo, index) {
    var y = 360 + index * 148;
    ctx.fillStyle = "#9a9284";
    ctx.font = "600 24px ui-sans-serif, system-ui, sans-serif";
    ctx.fillText(String(repo.rank).padStart(2, "0"), 72, y + 38);
    var icon = icons[index];
    if (icon) {
      ctx.save();
      roundedRect(ctx, 128, y, 56, 56, 14);
      ctx.clip();
      ctx.drawImage(icon, 128, y, 56, 56);
      ctx.restore();
    }
    ctx.fillStyle = "#f4efe6";
    ctx.font = "650 30px ui-sans-serif, system-ui, sans-serif";
    ctx.fillText(repo.name, 204, y + 28);
    ctx.fillStyle = "#9a9284";
    ctx.font = "500 20px ui-sans-serif, system-ui, sans-serif";
    ctx.fillText(repo.kind || "", 204, y + 56);
    ctx.fillStyle = "#2c2923";
    roundedRect(ctx, 760, y + 28, 180, 8, 4);
    ctx.fill();
    ctx.fillStyle = "#c4a46a";
    roundedRect(ctx, 760, y + 28, Math.max(16, 180 * (repo.star_share || 0)), 8, 4);
    ctx.fill();
    ctx.fillStyle = "#f4efe6";
    ctx.font = "600 24px ui-sans-serif, system-ui, sans-serif";
    ctx.fillText(repo.stars_label || "", 960, y + 38);
  });
}
function drawGithubPoster(ctx, repo, icon) {
  ctx.fillStyle = "#141210";
  ctx.fillRect(0, 0, 1080, 1920);
  ctx.fillStyle = "#d4a574";
  ctx.font = "600 22px ui-sans-serif, system-ui, sans-serif";
  ctx.fillText("NO." + String(repo.rank).padStart(2, "0"), 72, 110);
  if (icon) {
    ctx.save();
    roundedRect(ctx, 72, 150, 120, 120, 28);
    ctx.clip();
    ctx.drawImage(icon, 72, 150, 120, 120);
    ctx.restore();
  }
  ctx.fillStyle = "#f4efe6";
  ctx.font = "700 64px ui-sans-serif, system-ui, sans-serif";
  ctx.fillText(repo.name, 220, 230);
  ctx.fillStyle = "#9a9284";
  ctx.font = "500 24px ui-sans-serif, system-ui, sans-serif";
  ctx.fillText((repo.kind || "") + "  ·  " + (repo.stars_label || ""), 220, 272);
  wrapText(ctx, repo.summary || repo.description || "", 72, 360, 936, 42, "#c9c1b4", "500 28px ui-sans-serif, system-ui, sans-serif");
  var blocks = [
    ["做什么", repo.does],
    ["适合什么场景", repo.scene],
    ["如何实现", repo.how]
  ];
  blocks.forEach(function (block, index) {
    var y = 620 + index * 380;
    ctx.fillStyle = "#1c1916";
    roundedRect(ctx, 72, y, 936, 340, 28);
    ctx.fill();
    ctx.fillStyle = "#d4a574";
    ctx.font = "600 22px ui-sans-serif, system-ui, sans-serif";
    ctx.fillText(block[0], 108, y + 52);
    wrapText(ctx, block[1] || "", 108, y + 108, 864, 44, "#f4efe6", "500 30px ui-sans-serif, system-ui, sans-serif");
  });
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
    status("hotspot-status", "已按当前顺序导出 " + selected.length + " 张选中图片", "ok");
    waitDone("已导出 " + selected.length + " 张", "export");
  }).catch(function (error) {
    if (isWaitAbort(error, "export")) { return; }
    status("hotspot-status", "导出失败：" + error.message, "error");
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
    text += "配好下面的 GitHub Token 后，GitHub 每小时额度从 60 次升到 5000 次，收录会明显变多。";
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
  var chips = $("skill-source-chips");
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
    var chip = el("li", label === "手动粘贴" ? "is-manual" : "", label + " · " + counts[label]);
    chip.title = "来自" + label + "的 " + counts[label] + " 个 skill";
    chips.appendChild(chip);
  });
}
function renderCrawlSources(pack) {
  var toggle = $("skill-source-toggle");
  var list = $("skill-crawl-sources");
  if (!toggle || !list) { return; }
  var sources = (pack && pack.crawl_sources) || [];
  var count = Number((pack && pack.crawl_source_count) || sources.length || 0);
  toggle.hidden = !sources.length;
  toggle.textContent = sources.length
    ? ("当前自动抓取 skill 的源：" + count + " 个 · 点击" + (state.crawlSourcesOpen ? "收起" : "展开"))
    : "";
  toggle.setAttribute("aria-expanded", String(!!state.crawlSourcesOpen));
  list.textContent = "";
  list.hidden = !state.crawlSourcesOpen || !sources.length;
  sources.forEach(function (source) {
    var row = el("li", "");
    row.appendChild(el("strong", "", source.name || source.id || ""));
    var link = document.createElement("a");
    link.href = String(source.url || "#");
    link.target = "_blank";
    link.rel = "noreferrer noopener";
    link.textContent = String(source.url || "");
    row.appendChild(link);
    row.appendChild(el("span", "", (source.enabled === "1" ? "已启用" : "未启用") + " · " + String(source.detail || "")));
    list.appendChild(row);
  });
}
function toggleCrawlSources() {
  state.crawlSourcesOpen = !state.crawlSourcesOpen;
  renderCrawlSources(state.skillsPackage || {});
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
function selectedSkills(pack) {
  return ((pack && pack.skills) || []).filter(function (skill) {
    return skill && !skill.gap && skill.selected !== false;
  });
}
function renderSkillList(skills) {
  var list = $("skill-list");
  if (!list) { return; }
  list.textContent = "";
  var rows = (skills || []).filter(function (skill) { return skill && !skill.gap; });
  if (!rows.length) {
    list.hidden = true;
    return;
  }
  list.hidden = false;
  rows.forEach(function (skill) {
    var row = el("li", skill.selected === false ? "" : "is-selected");
    var box = document.createElement("input");
    box.type = "checkbox";
    box.checked = skill.selected !== false;
    box.setAttribute("aria-label", "选中后生成图片");
    box.addEventListener("change", function () { toggleSkillIncluded(skill, box.checked); });
    row.appendChild(box);
    var url = skillPageUrl(skill);
    var name = skill.name || skill.source_id || "未命名";
    if (url) {
      var link = document.createElement("a");
      link.href = url;
      link.target = "_blank";
      link.rel = "noreferrer noopener";
      link.textContent = name;
      row.appendChild(link);
    } else {
      row.appendChild(el("span", "", name));
    }
    var source = skillSourceLabel(skill);
    if (source) { row.appendChild(el("span", "skill-source", source)); }
    list.appendChild(row);
  });
}
function toggleSkillIncluded(skill, on) {
  skill.selected = !!on;
  var pack = state.skillsPackage || {};
  if (!(pack.images || []).some(function (image) { return image && image.url; })) {
    pack.images = queuedSkillImages(pack);
  }
  renderSkillPackage(pack);
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
  var skills = (pack.skills || []).filter(function (skill) { return skill && !skill.gap; });
  pack.stages = (pack.stages || []).filter(function (stage) { return !!stage; });
  pack.skills = skills;
  renderSkillSourceChips(skills);
  renderCrawlSources(pack);
  // B1: a degraded source must be visible, not silent — GitHub results still
  // count, but the user should know one source did not answer.
  var degradedNote = $("skill-degraded-note");
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
  $("skills-body").textContent = articleBody || "查找完成后会生成介绍文。按收录 skill 从左到右、从上到下说明。";
  $("skills-article").classList.toggle("is-empty", !String(articleBody).trim());
  renderSkillList(skills);
  renderSkillsGallery();
  var images = skillImages();
  $("skills-export-btn").disabled = !images.filter(function (image) { return image.selected !== false && image.url; }).length;
  $("skills-select-all").disabled = !images.length;
  $("skills-render-btn").disabled = !selectedSkills(pack).length;
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
    paintCarouselNav("skills-gallery-nav", "skills-gallery-prev", "skills-gallery-next", "skills-gallery-counter", 0, 0);
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
  paintCarouselNav("skills-gallery-nav", "skills-gallery-prev", "skills-gallery-next", "skills-gallery-counter", index, images.length);
}
function toggleSkillImage(imageId) {
  skillImages().forEach(function (image) {
    if (image.id === imageId) {
      image.selected = image.selected === false;
    }
  });
  renderSkillPackage(state.skillsPackage);
}
function selectAllSkillImages() {
  var images = skillImages();
  var allOn = images.length && images.every(function (image) { return image.selected !== false; });
  images.forEach(function (image) { image.selected = !allOn; });
  renderSkillPackage(state.skillsPackage);
}
function setPasteStatus(text, kind) {
  // 粘贴结果单独一行：紧随其后的出图会覆写共享状态行，不这样做结果就永远看不见。
  var node = $("skill-paste-status");
  if (!node) { return; }
  node.textContent = text || "";
  node.hidden = !text;
  node.className = kind ? "status-" + kind : "";
}
// The paste box grows with its content instead of scrolling one line at a time:
// a real paste is several links plus whatever prose came with them.
function growSkillLinks() {
  var box = $("skill-links");
  if (!box) { return; }
  var ceiling = Math.round(window.innerHeight * 0.4);
  box.style.height = "auto";
  box.style.height = Math.min(Math.max(box.scrollHeight, 76), ceiling) + "px";
}
function resetSkillLinks() {
  var box = $("skill-links");
  if (!box) { return; }
  box.value = "";
  growSkillLinks();
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
    status("hotspot-status", "标题保存失败：" + (error.reason || error.message), "error");
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
    status("hotspot-status", "正在查找或出图，等这一轮结束再清空", "error");
    return;
  }
  var button = $("skill-clear-btn");
  if (button) { busy(button, true); }
  api("/api/skills/clear", {}).then(function (data) {
    renderSkillPackage(data);
    status("hotspot-status", "已清空收录的 skill（标题保留）", "ok");
  }).catch(function (error) {
    status("hotspot-status", "清空失败：" + (error.reason || error.message), "error");
  }).then(function () {
    if (button) { busy(button, false); }
  });
}
function pasteSkills() {
  // 粘贴进来的 skill 与检索到的一样：先进包（拿阶段、写介绍文），再出海报。
  var links = ($("skill-links").value || "").trim();
  if (!links) {
    status("hotspot-status", "请先粘贴 skill 链接", "error");
    return;
  }
  if (links.length > 20000) {
    // 输入框不再截断，所以超长要在这里说清楚，而不是把尾巴上的链接切一半当成仓库名。
    setPasteStatus("链接太长（" + links.length + " 字，上限 20000），请分几次粘贴", "error");
    status("hotspot-status", "粘贴内容过长，请分几次粘贴", "error");
    return;
  }
  if (waitRunning("skill_paste")) { return; }
  if (waitRunning("skill_render")) {
    setPasteStatus("正在出图，等这一轮出完再粘贴", "error");
    status("hotspot-status", "正在出图，等这一轮出完再粘贴", "error");
    return;
  }
  var button = $("skill-paste-btn");
  if (button) { busy(button, true); }
  setPasteStatus("");
  waitStart("skill_paste");
  status("hotspot-status", "正在读取粘贴的 skill…");
  waitApi("skill_paste", "/api/skills/paste", {links: links}).then(function (data) {
    if (waitCancelled("skill_paste")) { return; }
    renderSkillPackage(data);
    var added = Number(data.added || 0);
    var replaced = Number(data.replaced || 0);
    var rejected = Number(data.rejected_count || (data.rejected || []).length);
    var unverified = (data.unverified || []).length;
    resetSkillLinks();
    var count = (data.skills || []).length;
    if (!added && !replaced) {
      var empty = rejected ? ("没有可加入的链接，已跳过 " + rejected + " 条") : "没有识别到 skill 链接";
      setPasteStatus(empty, "error");
      status("hotspot-status", empty, "error");
      waitFail(empty, "skill_paste");
      return;
    }
    var parts = [];
    if (added) { parts.push("新增 " + added + " 个"); }
    if (replaced) { parts.push("替换 " + replaced + " 个"); }
    if (rejected) { parts.push("跳过 " + rejected + " 条"); }
    if (unverified) { parts.push("有 " + unverified + " 条没读到 SKILL.md，海报会用模板文案"); }
    var summary = parts.join("，") + "；当前共 " + count + " 个 skill";
    setPasteStatus(summary, unverified ? "" : "ok");
    status("hotspot-status", summary + "，正在出图…", unverified ? "" : "ok");
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
    setPasteStatus("加入失败：" + reason, "error");
    status("hotspot-status", "加入失败：" + reason, "error");
    waitFail(reason, "skill_paste");
  }).then(function () {
    if (button) { busy(button, false); }
  });
}
function renderGithubTokenState(data) {
  var configured = !!(data && data.github_token);
  state.githubTokenConfigured = configured;
  var box = $("skill-token-status");
  if (box) {
    box.textContent = configured
      ? "已配置 GitHub Token：每小时 5000 次额度，收录更稳。"
      : "未配置。不配也能用，但 GitHub 接口每小时只给 60 次，容易限流。";
  }
}
function saveGithubToken() {
  var input = $("skill-github-token");
  var token = (input && input.value || "").trim();
  if (!token) {
    status("hotspot-status", "请先粘贴 GitHub Token", "error");
    return;
  }
  api("/api/skills/github-token", {token: token}).then(function (data) {
    if (input) { input.value = ""; }
    renderGithubTokenState(data);
    status("hotspot-status", "GitHub Token 已保存，下次查找生效", "ok");
  }).catch(function (error) {
    status("hotspot-status", "保存失败：" + (error.reason || error.message), "error");
  });
}
function clearGithubToken() {
  api("/api/skills/github-token", {token: ""}).then(function (data) {
    renderGithubTokenState(data);
    status("hotspot-status", "GitHub Token 已清除", "ok");
  }).catch(function (error) {
    status("hotspot-status", "清除失败：" + (error.reason || error.message), "error");
  });
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
  status("hotspot-status", "正在读取技能包…");
  return fetch("/api/skills").then(function (response) { return response.json(); }).then(function (data) {
    renderSkillPackage(data);
    var count = (data.skills || []).length;
    status("hotspot-status", count ? "已收录 " + count + " 个 skill；再输入关键词可继续累加，点「生成图片」做汇总出图" : "输入关键词（空格分隔多个），点「查找」；也可以直接粘贴 skill 链接。", count ? "ok" : "");
    return data;
  }).catch(function (error) {
    status("hotspot-status", "读取技能包失败：" + error.message, "error");
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
function pollSkillSnapshot() {
  if (waitCancelled("skill_search")) {
    return abortSkillSearch(($("skill-query").value || "").trim());
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
        window.setTimeout(function () { pollSkillSnapshot().then(resolve, reject); }, 350);
      });
    }
    return data;
  });
}
function searchSkills() {
  var query = ($("skill-query").value || "").trim();
  if (!query) {
    status("hotspot-status", "请先输入关键词", "error");
    return;
  }
  if (waitRunning("skill_search")) { return; }
  var button = $("skill-search-btn");
  if (button) { busy(button, true); }
  skillPollTicks = 0;
  waitStart("skill_search");
  status("hotspot-status", "正在按关键词去各来源抓取 skill…");
  waitApi("skill_search", "/api/skills/search", {query: query}).then(function (data) {
    if (waitCancelled("skill_search")) { return abortSkillSearch(query); }
    renderSkillPackage(data);
    var phase = data.phase || "";
    if (phase === "searching" || !phase || !skillArticleReady(data)) {
      return pollSkillSnapshot();
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
    status("hotspot-status", text, count ? "ok" : "error");
    waitDone(count ? ("已收录 " + count + " 个 skill") : "没有找到合格 skill", "skill_search");
  }).catch(function (error) {
    if (isWaitAbort(error, "skill_search")) {
      abortSkillSearch(query);
      return;
    }
    status("hotspot-status", "查找失败：" + (error.reason || error.message), "error");
    waitFail(error.message || "查找失败", "skill_search");
  }).then(function () {
    if (button) { busy(button, false); }
  });
}
function queueSkillImagesForRender() {
  var pack = state.skillsPackage || {};
  if (!(pack.skills || []).length) { return; }
  if ((pack.images || []).length) { return; }
  pack.images = queuedSkillImages(pack);
  renderSkillsGallery();
}
function renderSkillImages() {
  var pack = state.skillsPackage || {};
  if (!(pack.skills || []).length) {
    status("hotspot-status", "还没有收录 skill：先输入关键词查找，或粘贴 skill 链接", "error");
    return;
  }
  if (!selectedSkills(pack).length) {
    status("hotspot-status", "请先勾选要出图的 skill", "error");
    return;
  }
  if (waitRunning("skill_render")) { return; }
  var button = $("skills-render-btn");
  if (button) { busy(button, true); }
  queueSkillImagesForRender();
  waitStart("skill_render");
  var laneStatus = function (text, kind) {
    // 结果只报给还在第四栏的用户；切走以后别在别人的状态行里喊。
    if (state.lane === "skills") { status("hotspot-status", text, kind); }
  };
  laneStatus("正在生成技能包封面和项目卡…");
  var poll = window.setInterval(function () {
    if (waitCancelled("skill_render")) { return; }
    fetch("/api/skills").then(function (response) { return response.json(); }).then(function (data) {
      if (data && (data.images || []).length) { renderSkillPackage(data); }
    }).catch(function () {});
  }, 400);
  waitApi("skill_render", "/api/skills/render", {
    theme: state.githubTheme || "dark",
    palette: visualPalette(),
    title: String((($("skill-title") || {}).value) || "").trim(),
    selected_ids: selectedSkills(pack).map(skillIdentity)
  }).then(function (data) {
    if (waitCancelled("skill_render")) { return; }
    renderSkillPackage(data);
    var images = skillImages().filter(function (image) { return image.url; });
    laneStatus(images.length ? "已生成 " + images.length + " 张，勾选后可导出" : "没有生成图片", images.length ? "ok" : "error");
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
    laneStatus("出图失败：" + reason, "error");
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
    status("hotspot-status", "已按当前顺序导出 " + selected.length + " 张选中图片", "ok");
    waitDone("已导出 " + selected.length + " 张", "export");
  }).catch(function (error) {
    if (isWaitAbort(error, "export")) { return; }
    status("hotspot-status", "导出失败：" + error.message, "error");
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
function setLane(lane) {
  if (lane !== "hotspots" && lane !== "viewpoints" && lane !== "github" && lane !== "skills") { return; }
  if (state.lane === lane) { return; }
  state.collectGen += 1;
  invalidateRelatedSearch();
  rememberArticleSession(state.lane);
  var select = $("prompt-style-select");
  if (state.lane === "hotspots" && select && select.value !== "经验分享") {
    state.newsStyle = select.value;
  }
  state.lane = lane;
  document.body.classList.toggle("lane-github", lane === "github");
  document.body.classList.toggle("lane-skills", lane === "skills");
  document.querySelectorAll("[data-lane]").forEach(function (tab) {
    var on = tab.dataset.lane === lane;
    tab.classList.toggle("active", on);
    tab.setAttribute("aria-selected", String(on));
  });
  $("collect-btn").textContent = lane === "github" ? "抓取周榜" : "刷新全部";
  delete $("collect-btn").dataset.originalLabel;
  $("h-hotspots").textContent = lane === "github" ? "GitHub AI 项目周榜" : (lane === "viewpoints" ? "观点候选" : (lane === "skills" ? "Skill查找器" : "热点候选"));
  $("hotspot-sub").textContent = lane === "github"
    ? "优先本周新建的 AI 仓库，不够再用本周涨星补齐。先抓取周榜，再选浅色/暗色和三色盘后点生成图片。"
    : (lane === "viewpoints"
    ? "下面是 Reddit 上正在热的 AI 使用/经验帖。热度看分数和评论数；这是社区说法，不是官方新闻。"
    : (lane === "skills"
    ? "输入关键词（空格分隔多个），直接去 GitHub / skills.sh / 网页抓 skill；也可以粘贴链接。点「生成图片」把所有已收录 skill 汇总成封面 + 每个 skill 一张海报。"
    : "下面是话题/新闻候选。热度看独立来源数和讨论量；社区帖过阈会升温，一线说法用某某称，不是权威转载"));
  $("source-health-wrap").hidden = lane !== "hotspots";
  if (select) {
    select.value = lane === "viewpoints" ? "经验分享" : (state.newsStyle || "AI专家");
  }
  if (lane === "github") {
    clearHotspotSelection(true);
    loadGithubWeekly();
    return;
  }
  if (lane === "skills") {
    clearHotspotSelection(true);
    growSkillLinks();
    loadSkills();
    return;
  }
  clearHotspotSelection(true);
  restoreArticleSession(lane);
  loadHotspots().then(function (data) {
    return widenIfEmpty(data || {}, currentHours());
  });
}
document.querySelectorAll("[data-lane]").forEach(function (tab) {
  tab.addEventListener("click", function () { setLane(tab.dataset.lane); });
});
$("seg-wechat").addEventListener("click", function () { setPlatform("wechat"); });
$("seg-xhs").addEventListener("click", function () { setPlatform("xiaohongshu"); });
$("find-hooks-btn").addEventListener("click", findHooks);
$("open-original-btn").addEventListener("click", function () { openOriginal(); });
$("original-translate").addEventListener("click", translateOriginal);
$("original-close").addEventListener("click", closeOriginal);
$("original-dialog").addEventListener("cancel", closeOriginal);
$("generate-btn").addEventListener("click", generate);
$("save-article-btn").addEventListener("click", saveArticle);
$("image-rescan").addEventListener("click", rescanImages);
$("image-picker-prev").addEventListener("click", function () { stepImagePicker(-1); });
$("image-picker-next").addEventListener("click", function () { stepImagePicker(1); });
$("github-gallery-prev").addEventListener("click", function () { stepGithubGallery(-1); });
$("github-gallery-next").addEventListener("click", function () { stepGithubGallery(1); });
$("skills-gallery-prev").addEventListener("click", function () { stepSkillsGallery(-1); });
$("skills-gallery-next").addEventListener("click", function () { stepSkillsGallery(1); });
$("lightbox-close").addEventListener("click", closeImageLightbox);
$("lightbox-prev").addEventListener("click", function () { stepImageLightbox(-1); });
$("lightbox-next").addEventListener("click", function () { stepImageLightbox(1); });
$("lightbox-image").addEventListener("click", closeImageLightbox);
$("image-lightbox").addEventListener("click", function (event) {
  if (event.target === $("image-lightbox") || (event.target.classList && event.target.classList.contains("lightbox-stage"))) {
    closeImageLightbox();
  }
});
$("image-lightbox").addEventListener("cancel", function () { closeImageLightbox(); });
document.addEventListener("keydown", function (event) {
  if (!$("image-lightbox").open) { return; }
  if (event.key === "ArrowLeft") { event.preventDefault(); stepImageLightbox(-1); }
  if (event.key === "ArrowRight") { event.preventDefault(); stepImageLightbox(1); }
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
    if (id === "article-body") { syncRelatedSearchEnabled(); }
    if (!state.draft) { return; }
    state.articleDirty = true;
    $("publish-btn").disabled = false;
    $("schedule-btn").disabled = false;
    status("article-status", "有未保存的修改");
  });
});
$("article-body").addEventListener("select", syncRelatedSearchEnabled);
$("article-body").addEventListener("mouseup", syncRelatedSearchEnabled);
$("article-body").addEventListener("keyup", syncRelatedSearchEnabled);
$("article-model").addEventListener("change", function (event) { changeMainModel("article", event); });
$("publish-btn").addEventListener("click", publish);
$("schedule-btn").addEventListener("click", schedulePublish);
$("creator-btn").addEventListener("click", function () {
  if (state.creatorUrl) { window.open(state.creatorUrl, "_blank", "noopener"); }
});
$("config-btn").addEventListener("click", function () {
  openSettings(false);
});
$("favorites-btn").addEventListener("click", function () { toggleFavoritesDrawer(); });
$("favorites-close").addEventListener("click", function () { toggleFavoritesDrawer(false); });
$("news-supplement-btn").addEventListener("click", function () { toggleNewsSupplementDrawer(); });
$("news-supplement-close").addEventListener("click", function () { toggleNewsSupplementDrawer(false); });
$("related-search-btn").addEventListener("click", relatedSearch);
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
$("skill-search-btn").addEventListener("click", searchSkills);
$("skill-query").addEventListener("keydown", function (event) {
  if (event.key === "Enter") {
    event.preventDefault();
    searchSkills();
  }
});
$("skill-paste-btn").addEventListener("click", pasteSkills);
// In a textarea Enter is the user's line break, so pasting is the button only:
// Ctrl/Cmd+Enter is the shortcut for "加入".
$("skill-links").addEventListener("input", growSkillLinks);
$("skill-links").addEventListener("keydown", function (event) {
  if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
    event.preventDefault();
    pasteSkills();
  }
});
$("skill-clear-btn").addEventListener("click", clearSkills);
$("skill-source-toggle").addEventListener("click", toggleCrawlSources);
$("skill-title").addEventListener("input", scheduleSkillTitleSave);
$("skill-title").addEventListener("change", saveSkillTitle);
$("skill-token-save").addEventListener("click", saveGithubToken);
$("skill-token-clear").addEventListener("click", clearGithubToken);
$("skills-render-btn").addEventListener("click", renderSkillImages);
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
  if (data.sources) { renderSourceHealth(data.sources); }
  loadFavorites();
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
        self.workbench = Workbench(
            database,
            llm_service=self.llm_service,
            page_fetcher=default_page_fetcher if page_fetcher is None else page_fetcher,
        )
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
        self._hotspot_payloads: dict[str, dict[str, object]] = {}
        self.x_bearer_token = ""
        self.scheduler = PublishScheduler(database, self.publish, interval_seconds=15)
        self._restore_accounts()
        self._restore_github_token()

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
            "sources": self.hotspots.source_health(),
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

    def collect(
        self,
        hours: int,
        force: bool = True,
        source_id: str | None = None,
        lane: str = "hotspots",
    ) -> dict[str, object]:
        payload = self.hotspots.collect(
            hours, force=force, source_id=source_id, lane=lane
        )
        self._remember_hotspot_payloads(payload)
        return {
            "ok": True,
            "count": len(payload["candidates"]),
            **payload,
        }

    def hotspots_window(
        self, hours: int, translate: bool = False, lane: str = "hotspots"
    ) -> dict[str, object]:
        payload = self.hotspots.list_window(hours, translate=translate, lane=lane)
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

    def find_hooks(self, item_id: str) -> dict[str, object]:
        return self.workbench.find_hooks(item_id)

    def related_search(self, payload: object) -> dict[str, object]:
        if not isinstance(payload, dict):
            raise ValueError("related search payload must be a JSON object")
        draft_id = payload.get("draft_id")
        selection = payload.get("selected_text", payload.get("selection"))
        start = payload.get("selection_start")
        end = payload.get("selection_end")
        if not isinstance(draft_id, str) or not draft_id.strip():
            raise ValueError("draft_id required")
        if not isinstance(selection, str) or not selection.strip():
            raise ValueError("selection required")
        if len(selection) > RELATED_SELECTION_LIMIT:
            raise ValueError(f"selection must be at most {RELATED_SELECTION_LIMIT} characters")
        if (
            isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(end, bool)
            or not isinstance(end, int)
        ):
            raise ValueError("selection_start and selection_end must be integers")
        return self.workbench.related_search(
            draft_id.strip(), selection, start, end
        )

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
            result = {
                "item_id": item_id,
                "title": str(item.get("title") or ""),
                "content": content,
                "content_source": "feed",
                "content_note": "正文和热评来自 Reddit 收录，未抓板块页面",
                "translatable": _looks_english(content or str(item.get("title") or "")),
                "sources": sources,
            }
            return {"ok": True, **result}
        if sources and self.workbench.page_fetcher is not None:
            try:
                fetched = article_page_text(self.workbench.page_fetcher(sources[0]["url"]))
            except (OSError, TimeoutError, ValueError, UnicodeError):
                fetched = ""
            if fetched and (not stored or len(fetched) > max(240, len(stored) + 80)):
                content = fetched
                content_source = "page"
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
        }
        if content_source == "page":
            self._original_text_cache[item_id] = result
        return {"ok": True, **result}

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
        chosen_hooks: object = None,
        prompt_style: str = DEFAULT_ARTICLE_PROMPT_STYLE,
        item_ids: list[str] | None = None,
        pasted_text: str | None = None,
    ) -> dict[str, object]:
        if not self._article_model_configured:
            raise ValueError("article_model_not_configured")
        return {
            "ok": True,
            **self.workbench.generate_article(
                item_id,
                platform,
                model,
                provider,
                effort,
                self.article_style,
                chosen_hooks,
                prompt_style,
                self.xiaohongshu_config.get("account_name") or "",
                item_ids,
                pasted_text=pasted_text,
            ),
        }

    def approve(self, draft_id: str, reviewer: str) -> dict[str, object]:
        self.workflow.approve_draft(draft_id, reviewer)
        return {"ok": True, "draft_id": draft_id, "status": self.database.get_draft(draft_id)["status"]}

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
            }
        return {"ok": True, "count": len(snapshot["repos"]), **snapshot}

    def github_weekly_refresh(self) -> dict[str, object]:
        repos = fetch_weekly_ai_repos(getter=self.github_fetcher)
        model_used = ""
        if self._leaderboard_model_configured:
            try:
                repos = self.leaderboard_service.enrich_github_repos(repos)
                model_used = self.leaderboard_service.settings.model
            except LLMError:
                model_used = ""
        title, body = build_xhs_copy(repos)
        images = build_weekly_render_plan(repos)
        saved = self.database.save_github_weekly(
            week_window_start(), repos, title, body, model_used, images
        )
        return {"ok": True, "count": len(repos), **saved}

    def github_weekly_render(self, payload: object) -> dict[str, object]:
        snapshot = self.database.get_github_weekly()
        if snapshot is None or not snapshot.get("repos"):
            raise ValueError("github_weekly_empty")
        body = payload if isinstance(payload, dict) else {}
        theme = "light" if str(body.get("theme") or "").strip().lower() == "light" else "dark"
        palette = parse_palette(body.get("palette"))
        repos = list(snapshot["repos"])
        jobs = list(snapshot.get("images") or []) or build_weekly_render_plan(repos)
        if self.visual_renderer is None:
            images = render_weekly_visual(
                repos,
                jobs,
                self.packages_dir / "github-weekly",
                theme=theme,
                palette=palette,
            )
        else:
            images = self.visual_renderer(
                repos,
                jobs,
                self.packages_dir / "github-weekly",
                theme=theme,
                palette=palette,
            )
        visual = {"theme": theme, "palette": palette}
        saved = self.database.save_github_weekly(
            str(snapshot["week_start"]),
            repos,
            str(snapshot["xhs_title"]),
            str(snapshot["xhs_body"]),
            str(snapshot.get("model_used") or ""),
            images,
            visual=visual,
        )
        return {"ok": True, "count": len(repos), **saved}

    def _skill_copy_writer(self):
        """B5: skill posters go through the same copy layer as the weekly lane."""
        if not self._leaderboard_model_configured:
            return None
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

        try:
            images = render_skill_package(
                snapshot,
                images_dir,
                theme=theme,
                palette=palette,
                screenshot_fn=self.skill_screenshot,
                visual_renderer=self.visual_renderer,
                copy_writer=self._skill_copy_writer(),
                on_image=on_image,
            )
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
                "visual": {"theme": theme, "palette": palette or {}},
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
        body = payload if isinstance(payload, dict) else {}
        visual = snapshot.get("visual") if isinstance(snapshot.get("visual"), dict) else {}
        if body.get("theme") is not None or body.get("palette") is not None:
            theme = (
                "light"
                if str(body.get("theme") or visual.get("theme") or "").strip().lower() == "light"
                else "dark"
            )
            palette = parse_palette(
                body.get("palette") if body.get("palette") is not None else visual.get("palette")
            )
            visual = {"theme": theme, "palette": palette}
        else:
            theme = "light" if str(visual.get("theme") or "").strip().lower() == "light" else "dark"
            palette = parse_palette(visual.get("palette"))
        if self.visual_renderer is None:
            rendered = render_weekly_visual(
                list(snapshot["repos"]),
                [match],
                self.packages_dir / "github-weekly",
                theme=theme,
                palette=palette,
            )
        else:
            rendered = self.visual_renderer(
                list(snapshot["repos"]),
                [match],
                self.packages_dir / "github-weekly",
                theme=theme,
                palette=palette,
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
            jobs.append(
                {
                    "job_id": job["job_id"],
                    "draft_id": job["draft_id"],
                    "platform": job["platform"],
                    "live": bool(job["live"]),
                    "run_at": job["run_at"],
                    "status": job["status"],
                    "error_message": job["error_message"],
                }
            )
        return {"ok": True, "jobs": jobs}

    def cancel_schedule(self, payload: object) -> dict[str, object]:
        if not isinstance(payload, dict) or not isinstance(payload.get("job_id"), str):
            raise ValueError("job_id required")
        job = self.database.cancel_scheduled_job(payload["job_id"])
        return {"ok": True, "job_id": job["job_id"], "status": job["status"]}


def _make_handler(app: WebApp) -> type[BaseHTTPRequestHandler]:
    class ApiHandler(BaseHTTPRequestHandler):
        def _send_json(self, code: int, payload: object) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

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
            try:
                if urlsplit(self.path).path == "/":
                    body = INDEX_HTML.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path == "/api/status":
                    self._send_json(200, app.status())
                    return
                if self.path == "/api/github-weekly":
                    self._send_json(200, app.github_weekly())
                    return
                if self.path == "/api/skills":
                    self._send_json(200, app.skills_snapshot())
                    return
                if self.path == "/api/favorites":
                    self._send_json(200, app.list_favorites())
                    return
                parsed = urlsplit(self.path)
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
                    lane = (parse_qs(parsed.query).get("lane") or ["hotspots"])[0]
                    if lane not in {"hotspots", "viewpoints"}:
                        raise ValueError("lane must be hotspots or viewpoints")
                    self._send_json(
                        200, app.hotspots_window(hours, translate=translate, lane=lane)
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
                self._send_json(404, {"ok": False, "error": f"not_found:{exc}", "error_type": "KeyError"})
            except ValueError as exc:
                self._send_json(400, {"ok": False, "error": str(exc), "error_type": "ValueError"})
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": str(exc), "error_type": type(exc).__name__})

        def do_POST(self) -> None:
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
                if self.path == "/api/collect":
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
                    lane = payload.get("lane", "hotspots") if isinstance(payload, dict) else "hotspots"
                    if lane not in {"hotspots", "viewpoints"}:
                        raise ValueError("lane must be hotspots or viewpoints")
                    result = app.collect(
                        hours,
                        force=force,
                        source_id=source_id.strip() if source_id else None,
                        lane=lane,
                    )
                elif self.path == "/api/hooks/find":
                    result = app.find_hooks(self._require_string(payload, "item_id"))
                elif self.path == "/api/original":
                    result = app.original(self._require_string(payload, "item_id"))
                elif self.path == "/api/original/translate":
                    result = app.translate_original(self._require_string(payload, "item_id"))
                elif self.path == "/api/favorites":
                    result = app.add_favorite(self._require_string(payload, "item_id"))
                elif self.path == "/api/articles":
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
                    else:
                        item_ids = None
                        item_id = self._require_string(payload, "item_id")
                    result = app.articles(
                        item_id,
                        self._require_string(payload, "platform"),
                        payload.get("model") if isinstance(payload, dict) else None,
                        payload.get("provider") if isinstance(payload, dict) else None,
                        payload.get("effort") if isinstance(payload, dict) else None,
                        payload.get("chosen_hooks") if isinstance(payload, dict) else None,
                        payload.get("prompt_style", DEFAULT_ARTICLE_PROMPT_STYLE)
                        if isinstance(payload, dict)
                        else DEFAULT_ARTICLE_PROMPT_STYLE,
                        item_ids,
                        payload.get("pasted_text") if isinstance(payload, dict) else None,
                    )
                elif self.path == "/api/related-search":
                    result = app.related_search(payload)
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
                self._send_json(404, {"ok": False, "error": f"not_found:{exc}", "error_type": "KeyError"})
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
                self._send_json(500, {"ok": False, "error": str(exc), "error_type": type(exc).__name__})

        def do_DELETE(self) -> None:
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
                else:
                    raise KeyError(self.path)
                self._send_json(200, result)
            except KeyError as exc:
                self._send_json(404, {"ok": False, "error": f"not_found:{exc}", "error_type": "KeyError"})
            except ValueError as exc:
                self._send_json(400, {"ok": False, "error": str(exc), "error_type": "ValueError"})
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": str(exc), "error_type": type(exc).__name__})

        def log_message(self, format: str, *args: object) -> None:
            pass

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
