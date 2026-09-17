from __future__ import annotations

from html import escape
import hashlib
import re


def svg_id(prefix: str, seed: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", seed).strip("-")[:24] or "x"
    return f"{prefix}-{cleaned}"


def rounded_mark(name: str, kind: str = "", seed: str = "") -> str:
    uid = svg_id("mark", seed or name or kind)
    letter = escape((name or kind or "?").strip()[:1].upper() or "?")
    return f"""<svg class="c-logo-3d-svg" viewBox="0 0 128 128" aria-hidden="true">
  <defs>
    <linearGradient id="{uid}-face" x1="18%" y1="8%" x2="88%" y2="96%">
      <stop offset="0%" stop-color="currentColor" stop-opacity="0.95"/>
      <stop offset="55%" stop-color="currentColor" stop-opacity="0.72"/>
      <stop offset="100%" stop-color="currentColor" stop-opacity="0.42"/>
    </linearGradient>
    <linearGradient id="{uid}-shine" x1="0%" y1="0%" x2="0%" y2="100%">
      <stop offset="0%" stop-color="#fff" stop-opacity="0.38"/>
      <stop offset="48%" stop-color="#fff" stop-opacity="0"/>
    </linearGradient>
  </defs>
  <rect x="8" y="8" width="112" height="112" rx="32" fill="url(#{uid}-face)"/>
  <rect x="8" y="8" width="112" height="112" rx="32" fill="url(#{uid}-shine)"/>
  <text x="64" y="76" text-anchor="middle" font-size="46" font-weight="700"
    font-family="ui-sans-serif, system-ui, sans-serif" fill="#F3F0E8">{letter}</text>
</svg>"""


def feature_icon(label: str, seed: str = "") -> str:
    key = (label or "").lower()
    return _icon_svg(_feature_path(key, seed or label), seed or label)


def named_icon(name: str, seed: str = "") -> str:
    path = _GLYPHS.get(name) or _feature_path(name, seed or name)
    return _icon_svg(path, seed or name)


def _icon_svg(path: str, seed: str) -> str:
    uid = svg_id("feat", seed)
    return f"""<svg class="c-feature-icon" viewBox="0 0 32 32" aria-hidden="true">
  <defs>
    <linearGradient id="{uid}" x1="20%" y1="0%" x2="80%" y2="100%">
      <stop offset="0%" stop-color="currentColor" stop-opacity="0.95"/>
      <stop offset="100%" stop-color="currentColor" stop-opacity="0.55"/>
    </linearGradient>
  </defs>
  <rect x="1.5" y="1.5" width="29" height="29" rx="9" fill="url(#{uid})" opacity="0.16"/>
  <rect x="1.5" y="1.5" width="29" height="29" rx="9" fill="none" stroke="currentColor" stroke-width="1.4" opacity="0.55"/>
  {path}
</svg>"""


_STROKE = 'fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"'

_GLYPHS = {
    "people": (
        '<circle cx="11" cy="12" r="3.2" fill="currentColor"/>'
        '<circle cx="21" cy="12" r="3.2" fill="currentColor"/>'
        '<path d="M6 23c.6-4 3.2-6 6.2-6s5.5 2 6.1 6M14 23c.6-4 3.2-6 6.2-6s5.5 2 6.1 6" '
        f"{_STROKE}/>"
    ),
    "person": (
        '<circle cx="16" cy="12" r="3.4" fill="currentColor"/>'
        f'<path d="M8 24c1-5 4.2-7.2 8-7.2S23 19 24 24" {_STROKE}/>'
    ),
    "blocks": (
        f'<path d="M8 8h6v6H8zM18 8h6v6h-6zM13 18h6v6h-6z" {_STROKE}/>'
    ),
    "flow": (
        f'<path d="M7 16h18M16 7v18M10 11l6-4 6 4M10 21l6 4 6-4" {_STROKE}/>'
    ),
    "loop": (
        f'<path d="M9 11a7 7 0 1 1 2 5.2L8 18" {_STROKE}/>'
        f'<path d="M8 18h4.2" {_STROKE}/>'
    ),
    "search": (
        '<circle cx="14" cy="14" r="6" fill="none" stroke="currentColor" stroke-width="1.8"/>'
        f'<path d="M19 19l5 5" {_STROKE}/>'
    ),
    "terminal": (
        f'<rect x="7" y="8" width="18" height="16" rx="3" {_STROKE}/>'
        f'<path d="M10.5 13l3 3-3 3M15.5 19h5" {_STROKE}/>'
    ),
    "window": (
        f'<rect x="7" y="8" width="18" height="16" rx="3" {_STROKE}/>'
        f'<path d="M7 12.5h18" {_STROKE}/>'
        '<circle cx="10.2" cy="10.2" r="1" fill="currentColor"/>'
        '<circle cx="13.2" cy="10.2" r="1" fill="currentColor"/>'
        f'<path d="M12 17h8v4h-8z" {_STROKE}/>'
    ),
    "code": (
        f'<path d="M13 9l-6 7 6 7M19 9l6 7-6 7" {_STROKE}/>'
    ),
    "chip": (
        f'<rect x="10" y="10" width="12" height="12" rx="2" {_STROKE}/>'
        f'<path d="M13 10V7.5M19 10V7.5M13 24.5V22M19 24.5V22M10 13H7.5M10 19H7.5M24.5 13H22M24.5 19H22" {_STROKE}/>'
        '<rect x="13.5" y="13.5" width="5" height="5" rx="1" fill="currentColor" opacity="0.85"/>'
    ),
    "data": (
        f'<ellipse cx="16" cy="10" rx="7" ry="3" {_STROKE}/>'
        f'<path d="M9 10v9c0 1.7 3.1 3 7 3s7-1.3 7-3v-9" {_STROKE}/>'
        f'<path d="M9 14.5c0 1.7 3.1 3 7 3s7-1.3 7-3" {_STROKE}/>'
    ),
    "puzzle": (
        f'<path d="M8 8h6c0 2 2.2 3.2 3.4 1.6C19 8 20.2 9.4 19 11.2V14h5.5c.8 0 1.5.7 1.5 1.5S25.3 17 24.5 17H19v5.2c0 .8-.7 1.5-1.5 1.5h-3.2c0-2-2.4-3-3.6-1.4C9.4 24 8 22.8 8 20.8V8z" {_STROKE}/>'
    ),
    "plug": (
        f'<path d="M12 8v5M20 8v5M10 13h12v4.2c0 4-3.2 7.3-6 7.3s-6-3.3-6-7.3z" {_STROKE}/>'
        f'<path d="M16 24.5V26" {_STROKE}/>'
    ),
    "fence": (
        f'<path d="M8 22V10l8-3 8 3v12" {_STROKE}/>'
        f'<path d="M8 14h16M16 7.2V22" {_STROKE}/>'
    ),
    "flag": (
        f'<path d="M10 25V8" {_STROKE}/>'
        f'<path d="M10 8h11l-2.4 4.2L21 16.5H10z" {_STROKE}/>'
        '<path d="M10 8h11l-2.4 4.2L21 16.5H10z" fill="currentColor" opacity="0.35"/>'
    ),
    "handoff": (
        f'<path d="M7 16h7l-2.4-2.4M14 16l-2.4 2.4" {_STROKE}/>'
        f'<path d="M25 16h-7l2.4-2.4M18 16l2.4 2.4" {_STROKE}/>'
        '<circle cx="16" cy="16" r="2" fill="currentColor"/>'
    ),
    "pin": (
        f'<path d="M16 25s-7-8.2-7-12.2a7 7 0 1 1 14 0C23 16.8 16 25 16 25z" {_STROKE}/>'
        '<circle cx="16" cy="12.6" r="2.2" fill="currentColor"/>'
    ),
    "key": (
        f'<circle cx="12.5" cy="16" r="4.2" {_STROKE}/>'
        f'<path d="M16.4 16H25v3.2h-2.2V16m0 0V19" {_STROKE}/>'
        '<circle cx="12.5" cy="16" r="1.3" fill="currentColor"/>'
    ),
    "play": (
        '<circle cx="16" cy="16" r="9" fill="none" stroke="currentColor" stroke-width="1.8"/>'
        '<path d="M13.2 11.5v9l8-4.5z" fill="currentColor"/>'
    ),
    "spark": (
        f'<path d="M16 7l1.6 6.4L24 16l-6.4 1.6L16 25l-1.6-7.4L8 16l6.4-2.6z" {_STROKE}/>'
        '<path d="M16 7l1.6 6.4L24 16l-6.4 1.6L16 25l-1.6-7.4L8 16l6.4-2.6z" fill="currentColor" opacity="0.28"/>'
    ),
    "stack": (
        f'<path d="M8 13l8-4 8 4-8 4z" {_STROKE}/>'
        f'<path d="M8 17l8 4 8-4M8 21l8 4 8-4" {_STROKE}/>'
    ),
    "chat": (
        f'<path d="M8 10.5c0-1.7 1.6-3 3.6-3h8.8c2 0 3.6 1.3 3.6 3v6c0 1.7-1.6 3-3.6 3h-4.2L11 22.5V16.5H11.6c-2 0-3.6-1.3-3.6-3z" {_STROKE}/>'
        '<circle cx="13.2" cy="13.2" r="1.1" fill="currentColor"/>'
        '<circle cx="16.4" cy="13.2" r="1.1" fill="currentColor"/>'
        '<circle cx="19.6" cy="13.2" r="1.1" fill="currentColor"/>'
    ),
}

_KEYWORD_GLYPHS = (
    (("terminal", "终端", "命令行", "命令", "屏幕"), "terminal"),
    (("网页", "按钮", "界面", "页面", "webui"), "window"),
    (("代码", "几行", "sdk", "调用", "接口细节"), "code"),
    (("模型", "跑起来", "跑通", "本机"), "chip"),
    (("样本", "数据", "评测", "训练", "字段"), "data"),
    (("文档", "资料", "检索", "知识", "rag", "查你"), "search"),
    (("插件", "换窗口", "宿主", "编辑器"), "puzzle"),
    (("接线", "mcp", "外部工具", "调用服务"), "plug"),
    (("只管", "不代替", "不负责", "不替你", "边界"), "fence"),
    (("目标", "下达"), "flag"),
    (("交接", "交给", "下一位"), "handoff"),
    (("流水线", "工作流", "workflow", "串成"), "flow"),
    (("自动", "反馈", "循环"), "loop"),
    (("分工", "模块", "split"), "blocks"),
    (("角色", "岗位", "团队", "助手", "agent"), "people"),
    (("密钥", "引入这个库"), "key"),
    (("启动", "打开页面", "点着试"), "play"),
    (("场景", "场合", "本地把"), "pin"),
    (("开发者", "适合"), "person"),
    (("胶水", "骨架", "嵌进"), "stack"),
    (("说话", "对话", "追问"), "chat"),
)


def _feature_path(key: str, seed: str = "") -> str:
    for tokens, name in _KEYWORD_GLYPHS:
        if any(token in key for token in tokens):
            return _GLYPHS[name]
    names = list(_GLYPHS)
    digest = hashlib.md5((seed or key or "x").encode("utf-8")).digest()
    return _GLYPHS[names[digest[0] % len(names)]]

