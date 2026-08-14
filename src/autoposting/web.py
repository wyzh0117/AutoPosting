from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

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
    validate_chat_messages,
)
from .images import default_page_fetcher, guess_image_content_type, normalize_assets
from .publishing import PublicationError, PublicationService
from .scheduler import PublishScheduler, parse_run_at
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
)

APP_VERSION = "0.3.4"
ALLOWED_HOSTS = ("127.0.0.1", "localhost")
MAX_JSON_BODY_BYTES = 1_048_576
MAX_CHAT_JSON_BODY_BYTES = 4_194_304

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
.btn-magic{
  background:linear-gradient(135deg, var(--accent-gradient-2), var(--accent-gradient-1));
  color:#1a160f; border:1px solid var(--accent); font-weight:700;
  display:flex; align-items:center; gap:.5rem;
  border-radius:99px; padding:.55rem 1.35rem;
  box-shadow:0 6px 16px var(--accent-glow);
}
.btn-magic:hover:not(:disabled){filter:brightness(1.08); transform:translateY(-1px);}

#rank-tabs{margin:0 0 .65rem; align-self:flex-start;}
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
#hotspot-list::-webkit-scrollbar, .dialog-content::-webkit-scrollbar, .article-scroll::-webkit-scrollbar, #chat-output::-webkit-scrollbar, #image-picker-list::-webkit-scrollbar{width:4px;}
#hotspot-list::-webkit-scrollbar-thumb, .dialog-content::-webkit-scrollbar-thumb, .article-scroll::-webkit-scrollbar-thumb, #chat-output::-webkit-scrollbar-thumb, #image-picker-list::-webkit-scrollbar-thumb{background:var(--accent-gradient-2); border-radius:4px;}
.hotspot-item{
  display:block; width:100%; text-align:left;
  background:var(--bg-input); border:1px solid transparent;
  border-radius:var(--radius-md); padding:.8rem .9rem;
  cursor:pointer; position:relative; word-break:break-word;
  transition:background var(--t-fast) var(--ease), border-color var(--t-fast) var(--ease), transform var(--t-fast) var(--ease), box-shadow var(--t-fast) var(--ease);
}
.hotspot-item:hover:not(:disabled){border-color:rgba(196, 164, 106, 0.35); transform:translateY(-2px);}
.hotspot-item.selected{
  border-color:var(--accent);
  background:linear-gradient(145deg, var(--bg-input), rgba(196, 164, 106, 0.08));
  box-shadow:0 4px 20px var(--accent-glow);
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
.ht-title{display:block; font-size:.95rem; font-weight:600; line-height:1.4; color:var(--text-main); margin-bottom:.35rem; word-break:break-word;}
.ht-summary{
  display:block; font-size:.82rem; color:var(--text-muted); line-height:1.45; margin-bottom:.55rem;
  word-break:break-word; display:-webkit-box; -webkit-line-clamp:1; -webkit-box-orient:vertical; overflow:hidden;
}
.ht-zh-unavailable{display:block; color:var(--warn); margin:0 0 .4rem; font-size:.82rem;}
.ht-meta{display:block; font-size:.72rem; color:var(--text-muted); border-top:1px solid rgba(243, 238, 228, 0.05); padding-top:.65rem;}
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
#article-body{flex:1 0 auto; min-height:260px; height:auto; overflow:hidden; resize:none; white-space:pre-wrap; word-break:break-word; line-height:1.8;}
textarea[name=style]{resize:vertical; max-height:50vh;}
.article-meta{margin:0; font-size:.8rem;}
#image-picker{
  display:flex; flex-direction:column; gap:.55rem; margin-top:.2rem;
  max-height:20.5rem; overflow:hidden;
  border:1px solid var(--border); border-radius:var(--radius-md);
  background:var(--bg-raised); padding:.7rem .75rem .65rem;
  box-shadow:inset 0 1px 0 rgba(243, 238, 228, 0.04);
}
#image-picker[hidden]{display:none;}
.image-picker-head{display:flex; justify-content:space-between; align-items:flex-start; gap:.6rem;}
.image-picker-head .panel-sub{margin:0; flex:1 1 auto;}
#image-picker-list{
  display:flex; flex-direction:column; gap:.55rem;
  flex:1 1 auto; min-height:0; max-height:16.5rem;
  overflow-y:auto; overscroll-behavior:contain;
  padding-right:.3rem; scrollbar-width:thin;
}
.image-card{
  display:grid; grid-template-columns:64px minmax(0,1fr) auto; gap:.6rem;
  align-items:center; background:rgba(0,0,0,0.22);
  border:1px solid var(--border); border-radius:var(--radius-sm); padding:.5rem;
  animation:rise var(--t-mid) var(--ease) both;
  transition:border-color var(--t-fast) var(--ease), transform var(--t-fast) var(--ease), box-shadow var(--t-fast) var(--ease);
}
.image-card:hover{transform:translateY(-1px); border-color:rgba(196, 164, 106, 0.35);}
.image-card img{
  width:64px; height:86px; object-fit:cover; aspect-ratio:3/4;
  border-radius:6px; background:#111; display:block; cursor:zoom-in;
}
.image-card .image-meta{min-width:0; font-size:.75rem; color:var(--text-muted);}
.image-card .image-url{overflow:hidden; text-overflow:ellipsis; white-space:nowrap;}
.image-card .image-actions{display:flex; flex-wrap:wrap; gap:.35rem;}
.image-card .image-actions button{height:28px; min-height:28px; padding:0 .55rem; font-size:.72rem;}
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
.chat-section{
  border-top:1px solid var(--border); margin-top:.75rem; padding-top:.75rem;
  display:flex; flex-direction:column; gap:.75rem; flex:0 0 auto; min-height:0;
}
.chat-section.chat-active{flex:0 0 auto; overflow:visible;}
.chat-row{display:flex; align-items:center; gap:.5rem; min-width:0;}
.chat-row > .model-picker,.chat-row > input,.chat-row > button{height:40px; min-height:40px; padding-top:0; padding-bottom:0;}
.chat-row .model-picker{flex:0 0 auto;}
#chat-input{flex:1 1 auto; min-width:0;}
#chat-send-btn{flex:0 0 auto; white-space:nowrap;}
#chat-output{
  display:flex; flex:0 0 auto; min-height:0; flex-direction:column; gap:.75rem;
  overflow:visible; overscroll-behavior:contain;
  background:rgba(0,0,0,0.25); border:1px solid var(--border);
  border-radius:var(--radius-md); padding:1rem;
}
#chat-output[hidden]{display:none;}
.chat-msg{display:flex; flex-direction:column; gap:.25rem;}
.chat-msg-user{align-items:flex-end;}
.chat-msg-assistant{align-items:flex-start;}
.chat-role{font-size:.7rem; color:var(--text-muted); font-weight:600;}
.chat-content{
  background:transparent; border-top:1px solid var(--border); border-bottom:1px solid var(--border);
  border-radius:0; padding:.6rem .9rem; font-size:.9rem;
  white-space:pre-wrap; word-break:break-word; max-width:85%;
}
.chat-msg-error .chat-content{color:var(--err);}
.chat-pending{color:var(--text-muted); font-size:.8rem; font-style:italic;}
.muted{color:var(--text-muted); font-size:12px; word-break:break-all;}
#package-path{overflow-wrap:anywhere; word-break:normal; background:rgba(0,0,0,0.3); border:1px solid var(--border); border-radius:6px; padding:4px 6px;}
[role=status]{color:var(--text-muted); font-size:.8rem;}
[role=status]:empty{display:none;}
.status-error{color:var(--err);}
.status-ok{color:var(--ok);}
[role=alert]{color:var(--err); font-size:.8rem;}
body.chat-expanded{min-height:100dvh; overflow-y:auto;}
body.chat-expanded main{flex:1 0 auto;}
body.chat-expanded .panel{height:auto; overflow:visible;}
body.chat-expanded #article-view{flex:1 0 auto;}

#config-dialog{
  background:var(--bg-card); color:var(--text-main);
  border:1px solid var(--border); border-radius:var(--radius-lg); padding:1.5rem;
  width:min(520px, calc(100% - 32px)); max-height:calc(100vh - 32px); overflow:hidden;
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
#endpoint-list{list-style:none; margin:0; padding:0; display:flex; flex-direction:column; gap:.6rem;}
.endpoint-card{background:var(--bg-input); border:1px solid var(--border); border-radius:var(--radius-sm); padding:.7rem .8rem;}
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
  .chat-section.chat-active{flex:0 0 auto; overflow:visible;}
  #chat-output{flex:0 0 auto; max-height:none;}
  .chat-row{display:grid; grid-template-columns:minmax(0, 1fr) auto;}
  .chat-row .model-picker{grid-column:1 / -1; width:100%;}
  #chat-input{width:100%;}
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
  <span id="service-status">v0.3.4 · 本地服务</span>
  <button type="button" id="config-btn">设置</button>
</header>
<main>
  <section class="panel" id="hotspots-panel" aria-labelledby="h-hotspots">
    <div class="panel-header">
      <div>
        <h2 id="h-hotspots" class="panel-title">热点候选</h2>
        <p class="panel-sub">下面是话题/新闻候选。热度看独立来源数和讨论量；社区帖过阈会升温，一线说法用某某称，不是权威转载</p>
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
        <button type="button" id="translate-btn">翻译标题</button>
      </div>
    </div>
    <div id="rank-tabs" class="platform-tabs" role="tablist" aria-label="排序方式">
      <button type="button" class="seg active" data-rank="heat" aria-selected="true">热度优先</button>
      <button type="button" class="seg" data-rank="time" aria-selected="false">时间优先</button>
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
  <section class="panel" id="article-panel" aria-labelledby="h-article">
    <h2 id="h-article" class="sr-only">文章</h2>
    <div class="panel-header">
      <div class="platform-tabs" role="group" aria-label="平台选择">
        <button type="button" id="seg-wechat" class="seg active" data-platform="wechat" aria-pressed="true">微信</button>
        <button type="button" id="seg-xhs" class="seg" data-platform="xiaohongshu" aria-pressed="false">小红书</button>
      </div>
      <button type="button" id="generate-btn" class="btn-magic" disabled>生成文章</button>
    </div>
    <p id="article-status" role="status" aria-live="polite"></p>
    <p id="article-empty">尚未生成文章</p>
    <article id="article-view">
      <div id="article-scroll" class="article-scroll" hidden>
        <label class="article-field">标题<input id="article-title" type="text"></label>
        <div id="image-picker" hidden>
          <div class="image-picker-head">
            <p class="panel-sub">原文配图。点击预览可看大图；封面和正文分开选；1–2 张也能用，多的全部列出由你挑。</p>
            <button type="button" id="image-rescan">重新扫描</button>
          </div>
          <div id="image-picker-list"></div>
          <p id="image-picker-status" role="status"></p>
        </div>
        <label class="article-field">正文<textarea id="article-body" rows="14"></textarea></label>
        <p id="article-sources" class="article-meta muted"></p>
        <p id="draft-id" class="muted"></p>
      </div>
      <div class="article-actions" aria-label="文章操作">
        <div class="row">
          <button type="button" id="save-article-btn" disabled>保存修改</button>
          <select id="article-model" class="model-picker" aria-label="文章模型"></select>
        </div>
        <div class="row">
          <input id="schedule-at" type="datetime-local" aria-label="定时发布时间">
          <button type="button" id="schedule-btn" disabled>定时发布</button>
          <button type="button" id="publish-btn" disabled class="primary">发布</button>
        </div>
      </div>
      <p id="publish-status" role="status" aria-live="polite"></p>
      <div id="manual-box" hidden>
        <p>发布包已生成，仍需在官方创作中心人工提交。</p>
        <p>本地发布包：<code id="package-path"></code></p>
        <button type="button" id="creator-btn">打开官方创作中心</button>
      </div>
    </article>
    <div class="chat-section" aria-label="对话">
      <div class="chat-row">
        <select id="chat-model" class="model-picker" aria-label="对话模型"></select>
        <input id="chat-input" type="text" maxlength="20000" aria-label="对话消息">
        <button type="button" id="chat-send-btn">发送</button>
      </div>
      <div id="chat-output" hidden aria-live="polite"></div>
    </div>
  </section>
</main>

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
        <legend>添加模型接口</legend>
        <p class="muted">和 Hermes 一样：填 OpenAI 兼容根地址和 API Key，连接后选模型。文章与对话可共用同一接口。</p>
        <label>接口地址<input name="base_url" type="text" autocomplete="off"></label>
        <label>API Key<input name="api_key" type="password" autocomplete="off"></label>
        <div class="row">
          <button type="button" id="endpoint-connect">连接并拉取模型</button>
        </div>
        <div id="endpoint-assign" hidden>
          <div class="row">
            <label style="flex:1 1 180px">文章模型<select name="model"></select></label>
            <label style="flex:0 1 120px">Effort<select name="effort">
              <option value="none">none</option><option value="minimal">minimal</option>
              <option value="low">low</option><option value="medium">medium</option>
              <option value="high">high</option><option value="xhigh">xhigh</option>
              <option value="max">max</option><option value="ultra">ultra</option>
            </select></label>
          </div>
          <div class="row">
            <label style="flex:1 1 180px">对话模型<select name="chat_model"></select></label>
          </div>
          <div class="row">
            <button type="button" id="endpoint-use-article">用于文章</button>
            <button type="button" id="endpoint-use-chat">用于对话</button>
          </div>
        </div>
      </fieldset>
      <fieldset class="settings-page" data-page="model" id="model-endpoint-settings">
        <legend>已保存接口</legend>
        <ul id="endpoint-list"></ul>
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
          <label>App ID<input name="app_id" type="text"></label>
          <label>App Secret<input name="app_secret" type="password"></label>
          <label>Thumb Media ID<input name="thumb_media_id" type="text"></label>
          <label>作者（可选）<input name="author" type="text"></label>
        </div>
        <div id="settings-xiaohongshu" class="settings-panel" hidden>
          <label>账号名称（可选）<input name="account_name" type="text"></label>
          <label>官方创作中心<input name="creator_url" type="text"></label>
        </div>
        <div id="settings-x" class="settings-panel" hidden>
          <p id="x-status" class="muted">X 官方读取令牌：未配置。只读 Bearer，留在进程内存，重启需重填。</p>
          <label>Bearer Token<input name="x_bearer_token" type="password" autocomplete="off"></label>
          <p class="muted">用于读取已启用的 X 账号时间线。不爬网页。S4 讨论号可直接开；S1/S2 仍要先人工确认身份。</p>
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
  rankings: {heat: [], time: []},
  sources: [],
  platform: "wechat",
  item: null,
  draft: null,
  lightboxIndex: 0,
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
  chat: {model: "", base_url: "", configured: false},
  modelEndpoints: [],
  chatMessages: [],
};

function $(id) { return document.getElementById(id); }

function el(tag, className, text) {
  var node = document.createElement(tag);
  if (className) { node.className = className; }
  if (text !== undefined) { node.textContent = text; }
  return node;
}

function busy(button, on) {
  if (button.dataset.originalLabel === undefined) {
    button.dataset.originalLabel = button.textContent;
  }
  button.disabled = on;
  button.textContent = on ? "处理中…" : button.dataset.originalLabel;
}

function status(id, text, kind) {
  var node = $(id);
  node.textContent = text;
  node.className = kind ? "status-" + kind : "";
}

function formatTime(iso) {
  var date = new Date(iso);
  if (isNaN(date.getTime())) { return iso || ""; }
  return date.toLocaleString();
}

function api(path, payload) {
  return fetch(path, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload || {}),
  }).then(function (response) {
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
    if (error instanceof TypeError && !error.apiError) { throw new Error("网络错误"); }
    throw error;
  });
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
  list.textContent = "";
  if (!items.length) {
    var empty = el("li", "hotspot-empty");
    empty.appendChild(el("p", "ht-title", "当前时间范围内没有话题或新闻"));
    empty.appendChild(el("p", "ht-summary", "可以拉长上方时间范围，或点「刷新全部」去各订阅源重新抓取。底部「抓取成功 / 只刷这个」只表示订阅健康，不是新闻标题。"));
    list.appendChild(empty);
    scheduleFitHotspotList();
    return;
  }
  items.forEach(function (item, index) {
    var button = el("button", "hotspot-item");
    button.type = "button";
    button.setAttribute("data-item-id", item.item_id);
    button.setAttribute("aria-pressed", "false");
    button.appendChild(el("span", "idx", (index + 1) + "."));
    button.appendChild(el("span", "ht-label", "原文"));
    button.appendChild(el("span", "ht-title", item.title));
    button.appendChild(el("span", "ht-summary", item.summary));
    var originalCjk = /[\u3400-\u9fff]/.test(String(item.title || ""));
    if (!originalCjk) {
      button.appendChild(el("span", "ht-label", "中文（AI翻译，非官方表述）"));
      if (item.title_zh || item.summary_zh) {
        button.appendChild(el("span", "ht-title", item.title_zh));
        button.appendChild(el("span", "ht-summary", item.summary_zh));
      } else {
        var unavailable = (state.llm && state.llm.configured) ? "未翻译，可点上方「翻译标题」" : "未翻译（未配置模型）";
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
    li.appendChild(button);
    list.appendChild(li);
  });
  scheduleFitHotspotList();
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
  $("publish-btn").dataset.originalLabel = "发布";
  $("publish-btn").textContent = "发布";
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
      var option = el("option", "", model + " · " + endpoint.base_url);
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

function renderChatModelPicker() {
  renderMainModelPicker("chat-model", state.chat);
}

function renderSettingsModels(purpose, models, selected) {
  var form = $("config-form");
  var select = form.elements[purpose === "article" ? "model" : "chat_model"];
  var values = (models || []).slice();
  if (selected && values.indexOf(selected) < 0) { selected = values[0] || ""; }
  select.textContent = "";
  if (!values.length) {
    var empty = el("option", "", "先连接接口");
    empty.value = "";
    empty.disabled = true;
    empty.selected = true;
    select.appendChild(empty);
    return;
  }
  values.forEach(function (model) {
    var option = el("option", "", model);
    option.value = model;
    option.selected = model === selected;
    select.appendChild(option);
  });
}

function renderEndpointLists() {
  var list = $("endpoint-list");
  if (!list) { return; }
  list.textContent = "";
  if (!state.modelEndpoints.length) {
    list.appendChild(el("li", "muted", "还没有保存的接口"));
    return;
  }
  state.modelEndpoints.forEach(function (endpoint) {
    var card = el("li", "endpoint-card");
    card.appendChild(el("div", "endpoint-url", endpointLabel(endpoint)));
    var row = el("div", "row");
    var use = el("button", "", "选用");
    use.type = "button";
    use.addEventListener("click", function () { reuseEndpoint(endpoint.id); });
    var del = el("button", "", "删除");
    del.type = "button";
    del.addEventListener("click", function () { deleteEndpoint(endpoint.id); });
    row.appendChild(use);
    row.appendChild(del);
    card.appendChild(row);
    list.appendChild(card);
  });
}

function fillDiscoveredModels(models) {
  var form = $("config-form");
  renderSettingsModels("article", models, state.llm.model);
  renderSettingsModels("chat", models, state.chat.model);
  $("endpoint-assign").hidden = false;
}

function reuseEndpoint(endpointId) {
  var endpoint = state.modelEndpoints.find(function (item) { return item.id === endpointId; });
  if (!endpoint) { return; }
  var form = $("config-form");
  form.dataset.endpointId = endpoint.id;
  form.base_url.value = endpoint.base_url;
  form.api_key.value = "";
  fillDiscoveredModels(endpoint.models || []);
  status("model-endpoint-status", "已选用，选模型后点用于文章或用于对话", "ok");
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
  status("config-error", "正在连接接口…");
  var payload = {purpose: "article", base_url: baseUrl, api_key: apiKey};
  if (endpointId && !apiKey) { payload.endpoint_id = endpointId; }
  api("/api/models", payload).then(function (data) {
    applyModelResponse(data);
    form.dataset.endpointId = data.endpoint_id || endpointId || "";
    fillDiscoveredModels(data.models || []);
    status("config-error", "已拉取 " + (data.models || []).length + " 个模型，选好后点用于文章或用于对话", "ok");
  }).catch(function (error) {
    status("config-error", showApiError("连接失败：", error), "error");
  }).then(function () { busy(button, false); });
}

function assignEndpoint(purpose) {
  var form = $("config-form");
  var model = form.elements[purpose === "article" ? "model" : "chat_model"].value.trim();
  var baseUrl = form.base_url.value.trim();
  var apiKey = form.api_key.value;
  var endpointId = form.dataset.endpointId || "";
  if (!model) {
    status("config-error", "请先连接并选择模型", "error");
    return;
  }
  var payload = {model: model, base_url: baseUrl, api_key: apiKey};
  if (endpointId) { payload.endpoint_id = endpointId; }
  if (purpose === "article") { payload.effort = form.effort.value; }
  var button = $(purpose === "article" ? "endpoint-use-article" : "endpoint-use-chat");
  busy(button, true);
  api(purpose === "article" ? "/api/model" : "/api/chat/model", payload).then(function (data) {
    applyModelResponse(data);
    status("config-error", purpose === "article" ? "已设为文章模型" : "已设为对话模型", "ok");
  }).catch(function (error) {
    status("config-error", showApiError("保存失败：", error), "error");
  }).then(function () { busy(button, false); });
}

function deleteEndpoint(endpointId) {
  if (!endpointId) { return; }
  if (!window.confirm("删除该接口及其 API Key？")) { return; }
  api("/api/model-endpoints/delete", {endpoint_id: endpointId}).then(function (data) {
    applyModelResponse(data);
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
  if (data.chat_llm) { state.chat = data.chat_llm; }
  renderModelPicker();
  renderChatModelPicker();
  renderEndpointLists();
}

function activateModel(purpose, endpointId, model) {
  var payload = {endpoint_id: endpointId, model: model};
  var path = purpose === "article" ? "/api/model" : "/api/chat/model";
  if (purpose === "article") { payload.effort = state.llm.effort || "none"; }
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
    renderChatModelPicker();
  }).then(function () { select.disabled = false; });
}

function showApiError(prefix, error) {
  var message = error.reason || error.message;
  if (error.code) { message += "（错误码：" + error.code + "）"; }
  return prefix + message;
}

function selectHotspot(item, button) {
  state.item = item;
  resetArticle();
  var nodes = $("hotspot-list").querySelectorAll(".hotspot-item");
  nodes.forEach(function (node) {
    var active = node === button;
    node.setAttribute("aria-pressed", String(active));
    node.classList.toggle("selected", active);
  });
  $("generate-btn").disabled = false;
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

function loadHotspots(translate) {
  var url = "/api/hotspots?hours=" + encodeURIComponent(currentHours());
  if (translate) { url += "&translate=1"; }
  return fetch(url).then(function (response) {
    return response.json().catch(function () { return {}; }).then(function (data) {
      if (!response.ok || data.ok === false) {
        throw new Error(data.error_reason || data.error || ("HTTP " + response.status));
      }
      applyHotspots(data);
      return data;
    });
  });
}

function widenIfEmpty(data, previousHours) {
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
    applyHotspots(next, " · 近 " + (previousHours || 4) + " 小时没有候选，已改看 " + wider + " 小时");
    return next;
  });
}

function translateTitles() {
  var button = $("translate-btn");
  if (button) { busy(button, true); }
  status("hotspot-status", "正在翻译当前列表标题…");
  loadHotspots(true).then(function (data) {
    var shown = rankedCount(data || {});
    status("hotspot-status", shown ? "共 " + shown + " 条候选 · 已尝试翻译标题" : "暂无候选", shown ? "ok" : "");
  }).catch(function (error) {
    status("hotspot-status", "翻译失败：" + error.message, "error");
  }).then(function () {
    if (button) { busy(button, false); }
  });
}

function collect(sourceId) {
  var button = sourceId
    ? document.querySelector('[data-source-id="' + sourceId + '"]')
    : $("collect-btn");
  if (button) { busy(button, true); }
  var hours = currentHours();
  status("hotspot-status", sourceId ? "正在刷新该源，已有候选会留在上面…" : "正在从各订阅源抓取，已有候选会留在上面…");
  if (!$("hotspot-list").querySelector(".hotspot-item")) {
    loadHotspots();
  }
  var payload = {hours: hours, force: true};
  if (sourceId) { payload.source_id = sourceId; }
  api("/api/collect", payload).then(function (data) {
    var inserted = data.collection && data.collection.inserted ? data.collection.inserted : 0;
    var failed = data.collection && data.collection.failed ? data.collection.failed : 0;
    var extra = "";
    if (inserted) { extra += " · 新入库 " + inserted + " 条"; }
    if (failed) { extra += " · " + failed + " 个源失败"; }
    applyHotspots(data, extra);
    if (!rankedCount(data)) {
      return widenIfEmpty(data, hours).then(function () {
        if (failed) { $("hotspot-status").className = "status-error"; }
      });
    }
    if (failed) { $("hotspot-status").className = "status-error"; }
  }).catch(function (error) {
    status("hotspot-status", "抓取失败：" + error.message, "error");
    loadHotspots();
  }).then(function () {
    if (button) { busy(button, false); }
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
  var button = $("image-rescan");
  busy(button, true);
  status("image-picker-status", "正在从原文扫描配图…");
  api("/api/articles/images/rescan", {draft_id: state.draft.draft_id}).then(function (data) {
    state.draft = Object.assign({}, state.draft, data);
    $("article-body").value = data.body || $("article-body").value;
    renderImagePicker(data.assets);
    var count = (data.assets && data.assets.candidates) ? data.assets.candidates.length : 0;
    status("image-picker-status", count ? "已扫描到 " + count + " 张原图" : "原文里没有可用配图", count ? "ok" : "error");
  }).catch(function (error) {
    status("image-picker-status", showApiError("扫描失败：", error), "error");
  }).then(function () { busy(button, false); });
}

function renderImagePicker(raw) {
  var picker = $("image-picker");
  var list = $("image-picker-list");
  var assets = normalizeClientAssets(raw);
  if (state.draft) { state.draft.assets = assets; }
  list.textContent = "";
  if (!state.draft) {
    picker.hidden = true;
    return;
  }
  picker.hidden = false;
  if (!assets.candidates.length) {
    list.appendChild(el("p", "muted", "未从原文找到配图。可点重新扫描。"));
    var sourceLine = "来源：" + (state.draft.source_ids || []).join(", ");
    $("article-sources").textContent = sourceLine + " · 无配图";
    return;
  }
  var coverCount = assets.cover_id ? 1 : 0;
  var sourceLine = "来源：" + ((state.draft && state.draft.source_ids) || []).join(", ");
  $("article-sources").textContent = sourceLine + " · 封面 " + coverCount + " · 正文图 " + assets.body_ids.length + " / " + assets.candidates.length;
  assets.candidates.forEach(function (image, index) {
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
    var coverBtn = el("button", "", image.id === assets.cover_id ? "封面" : "设为封面");
    coverBtn.type = "button";
    coverBtn.addEventListener("click", function () {
      changeImageSelection(function (next) { next.cover_id = image.id; });
    });
    var bodyOn = assets.body_ids.indexOf(image.id) >= 0;
    var bodyBtn = el("button", "", bodyOn ? "移出正文" : "加入正文");
    bodyBtn.type = "button";
    bodyBtn.addEventListener("click", function () {
      changeImageSelection(function (next) {
        var ids = next.body_ids.slice();
        var pos = ids.indexOf(image.id);
        if (pos >= 0) { ids.splice(pos, 1); } else { ids.push(image.id); }
        next.body_ids = ids;
      });
    });
    actions.appendChild(coverBtn);
    actions.appendChild(bodyBtn);
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
  });
}

function proxiedImage(url) {
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

function fitArticleBody() {
  var body = $("article-body");
  if (!body || body.closest("[hidden]")) { return; }
  body.style.height = "auto";
  var next = Math.max(260, body.scrollHeight + 2);
  body.style.height = next + "px";
}

function renderArticle(data) {
  $("article-empty").hidden = true;
  $("article-scroll").hidden = false;
  $("article-title").value = data.title || "";
  $("article-body").value = data.body || "";

  $("article-sources").textContent = "来源：" + (data.source_ids || []).join(", ");
  renderImagePicker(data.assets);
  $("draft-id").textContent = "draft_id: " + data.draft_id;
  $("article-status").textContent = "";
  $("save-article-btn").disabled = false;
  state.articleDirty = false;
  fitArticleBody();
}

function generate() {
  if (!state.item) { return; }
  var button = $("generate-btn");
  var llm = selectedLLM();
  if (!llm.model) {
    status("article-status", "请先在设置中填写模型", "error");
    return;
  }
  busy(button, true);
  api("/api/articles", {
    item_id: state.item.item_id,
    platform: state.platform,
    model: llm.model,
    effort: llm.effort,
  }).then(function (data) {
    state.draft = data;
    renderArticle(data);
    $("publish-btn").disabled = false;
    $("schedule-btn").disabled = false;
  }).catch(function (error) {
    status("article-status", showApiError("生成失败：", error), "error");
  }).then(function () {
    busy(button, false);
  });
}

function persistArticle() {
  if (!state.draft) { return Promise.resolve(); }
  return api("/api/articles/edit", {
    draft_id: state.draft.draft_id,
    title: $("article-title").value,
    body: $("article-body").value,
  }).then(function (data) {
    state.draft = Object.assign({}, state.draft, data);
    renderArticle(state.draft);
    return data;
  });
}

function saveArticle() {
  if (!state.draft) { return; }
  var button = $("save-article-btn");
  busy(button, true);
  persistArticle().then(function () {
    $("publish-btn").disabled = false;
    $("schedule-btn").disabled = false;
    status("article-status", "修改已保存，需重新审批", "ok");
  }).catch(function (error) {
    status("article-status", showApiError("保存失败：", error), "error");
  }).then(function () {
    busy(button, false);
  });
}

function setPlatform(platform) {
  state.platform = platform;
  resetArticle();
  var nodes = document.querySelectorAll("button[data-platform]");
  nodes.forEach(function (node) {
    var active = node.dataset.platform === platform;
    node.setAttribute("aria-pressed", String(active));
    node.classList.toggle("active", active);
  });
}

function appendChatMessage(role, text) {
  var output = $("chat-output");
  var wrap = el("div", "chat-msg chat-msg-" + role);
  var labels = {user: "我", assistant: "助手", error: "错误"};
  wrap.appendChild(el("span", "chat-role", labels[role] || role));
  wrap.appendChild(el("div", "chat-content", text));
  output.appendChild(wrap);
  output.scrollTop = output.scrollHeight;
}

function sendChat() {
  var input = $("chat-input");
  var button = $("chat-send-btn");
  if (button.disabled) { return; }
  var text = input.value.trim();
  if (!text) { return; }
  var output = $("chat-output");
  output.hidden = false;
  document.body.classList.add("chat-expanded");
  output.parentElement.classList.add("chat-active");
  appendChatMessage("user", text);
  input.value = "";
  var messages = state.chatMessages.slice(-48).concat([{role: "user", content: text}]);
  if (!state.chat.configured) {
    appendChatMessage("error", "请先在设置中配置对话模型接口");
    return;
  }
  var pending = el("p", "chat-pending", "思考中…");
  output.appendChild(pending);
  output.scrollTop = output.scrollHeight;
  busy(button, true);
  api("/api/chat", {messages: messages}).then(function (data) {
    var reply = (data.message && data.message.content) || "";
    appendChatMessage("assistant", reply);
    state.chatMessages = messages.concat([{role: "assistant", content: reply}]);
  }).catch(function (error) {
    appendChatMessage("error", showApiError("对话失败：", error));
  }).then(function () {
    pending.remove();
    busy(button, false);
    input.focus();
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

function openSettings(pendingPublish) {
  state.pendingPublish = pendingPublish;
  var form = $("config-form");
  form.effort.value = state.llm.effort || "none";
  form.base_url.value = state.llm.base_url || "";
  form.api_key.value = "";
  form.dataset.endpointId = (endpointForState(state.llm) || {}).id || "";
  form.elements.style.value = state.style;
  form.account_name.value = state.xiaohongshu.accountName;
  form.creator_url.value = state.xiaohongshu.creatorUrl;
  renderEndpointLists();
  var articleEndpoint = endpointForState(state.llm);
  var models = articleEndpoint ? articleEndpoint.models : [];
  renderSettingsModels("article", models, state.llm.model);
  renderSettingsModels("chat", models, state.chat.model);
  $("endpoint-assign").hidden = !models.length;
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
  (state.articleDirty ? persistArticle() : Promise.resolve()).then(function () {
    return persistImages();
  }).then(function () {
    draft = state.draft;
    return api("/api/approve", {draft_id: draft.draft_id, reviewer: "web-user"});
  }).then(function () {
    return api("/api/schedule", {
      draft_id: draft.draft_id,
      platform: state.platform,
      live: true,
      run_at: when,
    });
  }).then(function (result) {
    status("publish-status", "已预约 " + (result.run_at || when), "ok");
  }).catch(function (error) {
    if (error.code === "wechat_configuration_required") {
      status("publish-status", "请先配置微信", "error");
      openSettings(true);
      return;
    }
    status("publish-status", showApiError("定时失败：", error), "error");
  }).then(function () { busy(button, false); });
}

function publish() {
  var draft = state.draft;
  if (!draft) { return; }
  var button = $("publish-btn");
  var completed = false;
  busy(button, true);
  $("manual-box").hidden = true;
  status("publish-status", "保存文章…");
  (state.articleDirty ? persistArticle() : Promise.resolve()).then(function () {
    return persistImages();
  }).then(function () {
    draft = state.draft;
    status("publish-status", "提交审批…");
    return api("/api/approve", {draft_id: draft.draft_id, reviewer: "web-user"});
  }).then(function () {
    status("publish-status", "已审批，正在发布…");
    return api("/api/publish", {draft_id: draft.draft_id, platform: state.platform, live: true});
  }).then(function (result) {
    if (result.manual_required) {
      state.creatorUrl = result.creator_url || "";
      $("package-path").textContent = result.package_path || "";
      $("manual-box").hidden = false;
      status("publish-status", "发布包已生成", "ok");
    } else {
      status("publish-status", "已提交发布：" + (result.reference || result.status), "ok");
    }
    completed = true;
    button.dataset.originalLabel = result.manual_required ? "已生成发布包" : "已提交";
  }).catch(function (error) {
    if (error.code === "wechat_configuration_required") {
      status("publish-status", "请先配置微信", "error");
      openSettings(true);
    } else {
      status("publish-status", "发布失败：" + error.message, "error");
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
$("seg-wechat").addEventListener("click", function () { setPlatform("wechat"); });
$("seg-xhs").addEventListener("click", function () { setPlatform("xiaohongshu"); });
$("generate-btn").addEventListener("click", generate);
$("save-article-btn").addEventListener("click", saveArticle);
$("image-rescan").addEventListener("click", rescanImages);
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
$("chat-model").addEventListener("change", function (event) { changeMainModel("chat", event); });
$("chat-send-btn").addEventListener("click", sendChat);
$("chat-input").addEventListener("keydown", function (event) {
  if (event.key === "Enter") { event.preventDefault(); sendChat(); }
});
$("publish-btn").addEventListener("click", publish);
$("schedule-btn").addEventListener("click", schedulePublish);
$("creator-btn").addEventListener("click", function () {
  if (state.creatorUrl) { window.open(state.creatorUrl, "_blank", "noopener"); }
});
$("config-btn").addEventListener("click", function () {
  openSettings(false);
});
$("endpoint-connect").addEventListener("click", connectEndpoint);
$("endpoint-use-article").addEventListener("click", function () { assignEndpoint("article"); });
$("endpoint-use-chat").addEventListener("click", function () { assignEndpoint("chat"); });
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
  api("/api/style", {style: form.elements.style.value}).then(function (data) {
    state.style = data.style;
    return accountPayload ? api("/api/config", accountPayload) : data;
  }).then(function () {
    if (state.settingsPlatform === "xiaohongshu" && accountPayload) {
      state.xiaohongshu.accountName = accountPayload.account_name;
      state.xiaohongshu.creatorUrl = accountPayload.creator_url;
    }
    state.pendingPublish = false;
    form.reset();
    $("config-dialog").close();
    if (state.settingsPlatform === "wechat" && accountPayload) {
      status("wechat-status", "微信公众号 API：已配置", "ok");
    }
    status("publish-status", "");
    if (resumePublish) { publish(); }
  }).catch(function (error) {
    status("config-error", showApiError("保存失败：", error), "error");
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
    form.base_url.value = data.llm.base_url || "";
    form.elements.style.value = state.style;
  }
  state.modelEndpoints = data.model_endpoints || [];
  if (data.chat_llm) {
    state.chat = data.chat_llm;
  }
  renderModelPicker();
  renderChatModelPicker();
  renderEndpointLists();
  if (data.platforms && data.platforms.xiaohongshu) {
    state.xiaohongshu.accountName = data.platforms.xiaohongshu.account_name || "";
    state.xiaohongshu.creatorUrl = data.platforms.xiaohongshu.creator_url || state.xiaohongshu.creatorUrl;
  }
  if (data.platforms && data.platforms.wechat && data.platforms.wechat.configured) {
    status("wechat-status", "微信公众号 API：已配置", "ok");
  }
  if (data.platforms && data.platforms.x && data.platforms.x.configured) {
    status("x-status", "X 官方读取令牌：已配置", "ok");
  }
  if (data.version) { $("service-status").textContent = "v" + data.version + " · 本地服务"; }
  if (data.sources) { renderSourceHealth(data.sources); }
  loadHotspots().then(function (hotspots) {
    return widenIfEmpty(hotspots || {}, currentHours());
  });
}).catch(function () {
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
        self.credential_store = (
            credential_store if credential_store is not None else MacOSKeychainCredentialStore()
        )
        self._restore_model_selections()
        self.hotspots = HotspotService(database, source_tuple, fetcher=fetcher, llm_service=self.llm_service)
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
        self.article_style = ""
        self.x_bearer_token = ""
        self.scheduler = PublishScheduler(database, self.publish, interval_seconds=15)

    def _restore_model_selections(self) -> None:
        """Restore saved article/chat endpoint selections without network calls."""
        for purpose, service in (("article", self.llm_service), ("chat", self.chat_service)):
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
        current = self.llm_service.settings if purpose == "article" else self.chat_service.settings
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
            config["thumb_media_id"],
            config.get("author", ""),
            base_url=self._wechat_base_url or DEFAULT_WECHAT_BASE_URL,
            opener=self._wechat_opener,
        )

    def status(self) -> dict[str, object]:
        settings = self.llm_service.settings
        chat_settings = self.chat_service.settings
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
        self, hours: int, force: bool = True, source_id: str | None = None
    ) -> dict[str, object]:
        payload = self.hotspots.collect(hours, force=force, source_id=source_id)
        return {
            "ok": True,
            "count": len(payload["candidates"]),
            **payload,
        }

    def hotspots_window(self, hours: int, translate: bool = False) -> dict[str, object]:
        payload = self.hotspots.list_window(hours, translate=translate)
        return {
            "ok": True,
            "count": len(payload["candidates"]),
            **payload,
        }

    def find_hooks(self, item_id: str) -> dict[str, object]:
        return self.workbench.find_hooks(item_id)

    def articles(
        self,
        item_id: str,
        platform: str,
        model: str | None = None,
        provider: str | None = None,
        effort: str | None = None,
        chosen_hooks: object = None,
    ) -> dict[str, object]:
        if not self._article_model_configured:
            raise ValueError("article_model_not_configured")
        return {
            "ok": True,
            **self.workbench.generate_article(
                item_id, platform, model, provider, effort, self.article_style, chosen_hooks
            ),
        }

    def approve(self, draft_id: str, reviewer: str) -> dict[str, object]:
        self.workflow.approve_draft(draft_id, reviewer)
        return {"ok": True, "draft_id": draft_id, "status": self.database.get_draft(draft_id)["status"]}

    def edit_article(self, draft_id: str, title: str, body: str) -> dict[str, object]:
        draft = self.workflow.edit_draft(draft_id, title, body)
        return {
            "ok": True,
            "draft_id": draft_id,
            "event_id": str(draft["event_id"]),
            "platform": str(draft["platform"]),
            "status": str(draft["status"]),
            "title": str(draft["title"]),
            "body": str(draft["body"]),
            "source_ids": json.loads(str(draft["source_ids_json"])),
            "assets": normalize_assets(draft["assets_json"]),
        }

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
        return {
            "ok": True,
            "draft_id": draft_id,
            "title": str(draft["title"]),
            "body": str(draft["body"]),
            "status": str(draft["status"]),
            "assets": normalize_assets(draft["assets_json"]),
            "source_ids": json.loads(str(draft["source_ids_json"])),
        }

    def rescan_article_images(self, payload: object) -> dict[str, object]:
        if not isinstance(payload, dict) or not isinstance(payload.get("draft_id"), str):
            raise ValueError("draft_id required")
        bundle = self.workbench.rescan_images(payload["draft_id"])
        draft = self.workflow.replace_draft_image_bundle(payload["draft_id"], bundle)
        return {
            "ok": True,
            "draft_id": payload["draft_id"],
            "title": str(draft["title"]),
            "body": str(draft["body"]),
            "status": str(draft["status"]),
            "assets": normalize_assets(draft["assets_json"]),
            "source_ids": json.loads(str(draft["source_ids_json"])),
        }

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
        if purpose not in ("article", "chat"):
            raise ValueError("purpose must be article or chat")
        base_url, api_key = self._resolve_endpoint_credentials(payload, purpose)
        discovered = self.llm_service.discover_models(base_url, api_key)
        endpoint_id = model_endpoint_id(base_url)
        self.database.upsert_model_endpoint(endpoint_id, base_url, discovered)
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

    def delete_model_endpoint(self, payload: object) -> dict[str, object]:
        if not isinstance(payload, dict) or not isinstance(payload.get("endpoint_id"), str) or not payload["endpoint_id"].strip():
            raise ValueError("endpoint_id required")
        endpoint_id = payload["endpoint_id"].strip()
        if self.database.get_model_endpoint(endpoint_id) is None:
            raise KeyError(endpoint_id)
        for purpose, service in (("article", self.llm_service), ("chat", self.chat_service)):
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
            required = ("app_id", "app_secret", "thumb_media_id")
            missing = [
                field
                for field in required
                if not isinstance(payload.get(field), str) or not payload[field]
            ]
            if missing:
                raise ValueError(f"wechat config missing: {', '.join(missing)}")
            self.wechat_config = {
                "app_id": payload["app_id"],
                "app_secret": payload["app_secret"],
                "thumb_media_id": payload["thumb_media_id"],
                "author": str(payload.get("author", "")),
            }
            return {
                "ok": True,
                "platform": "wechat",
                "configured": True,
                "author": self.wechat_config["author"],
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
            return {"ok": True, "platform": "xiaohongshu", "configured": True}
        if platform == "x":
            token = payload.get("bearer_token", "")
            if not isinstance(token, str) or not token.strip():
                raise ValueError("x bearer_token required")
            self.x_bearer_token = token.strip()
            self.hotspots.x_bearer_token = self.x_bearer_token
            return {"ok": True, "platform": "x", "configured": True}
        raise ValueError(f"unsupported platform: {platform}")

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
        if existing and existing["status"] == "SUBMITTED":
            return {
                "ok": True,
                "status": "SUBMITTED",
                "reference": str(existing["response_reference"]),
                "archive_path": str(archive_path),
            }
        outcome = publisher.publish(draft)
        self.database.record_publication_attempt(
            attempt_id,
            draft_id,
            "wechat",
            False,
            "SUBMITTED",
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
                if self.path == "/":
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
                    self._send_json(200, app.hotspots_window(hours, translate=translate))
                    return
                if parsed.path == "/api/schedule":
                    self._send_json(200, app.list_schedule())
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
                    result = app.collect(
                        hours, force=force, source_id=source_id.strip() if source_id else None
                    )
                elif self.path == "/api/hooks/find":
                    result = app.find_hooks(self._require_string(payload, "item_id"))
                elif self.path == "/api/articles":
                    result = app.articles(
                        self._require_string(payload, "item_id"),
                        self._require_string(payload, "platform"),
                        payload.get("model") if isinstance(payload, dict) else None,
                        payload.get("provider") if isinstance(payload, dict) else None,
                        payload.get("effort") if isinstance(payload, dict) else None,
                        payload.get("chosen_hooks") if isinstance(payload, dict) else None,
                    )
                elif self.path == "/api/config":
                    result = app.set_config(payload)
                elif self.path == "/api/model":
                    result = app.set_model(payload)
                elif self.path == "/api/models":
                    result = app.models(payload)
                elif self.path == "/api/chat/model":
                    result = app.set_chat_model(payload)
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
                        "error_reason": exc.reason,
                        "error_type": "LLMError",
                    },
                )
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
