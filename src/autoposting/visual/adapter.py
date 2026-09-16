from __future__ import annotations

import re
from typing import Any, Iterable

from autoposting.github_weekly import iso_week_issue

from .components import AUTHOR_BRAND, author_avatar_data_uri
from .marks import is_author_avatar

GENERIC_HOW = ("安装步骤和文档", "文档在项目主页", "开源仓库，安装", "打开仓库按文档")
LATIN_RE = re.compile(r"[A-Za-z]")
CJK_RE = re.compile(r"[\u3400-\u9fff]")
CJK_SPACE_RE = re.compile(r"(?<=[\u3400-\u9fff])[ \t]+(?=[\u3400-\u9fff])")
CLIP_MARK_RE = re.compile(r"(…|\.{3})$")

KIND_ONE_LINER = {
    "Agent": "给想搭小型 AI 团队的人，在本地把开发和内容串成流水线时，把不同角色的助手组织起来一起干活。",
    "CLI": "给习惯开终端的开发者，在项目目录里试验提示词时，直接对着屏幕问模型拿结果。",
    "MCP": "给已经在用 AI 助手的人，想让助手动手操作工具时，把外部服务接到对话里。",
    "RAG": "给手里有一堆文档的人，需要先查再答时，让助手只根据你的材料开口。",
    "模型": "给想自己跑模型的人，在本机试效果时，把模型拉起来对比速度和表现。",
    "框架": "给要把 AI 嵌进现有项目的开发者，少写胶水代码时，按模块把能力接进代码库。",
    "WebUI": "给不想记命令的人，打开网页就能试 AI 时，把复杂操作收成按钮。",
    "SDK": "给要在自己程序里调 AI 的开发者，对接接口时，用几行代码把能力接进去。",
    "数据集": "给做训练或评测的人，开工前先看样本时，提供能直接练、能直接评的数据。",
    "插件": "给已经有常用工具的人，不想换窗口时，把 AI 接到正在用的软件里。",
}

KIND_DOES = {
    "Agent": [
        "把不同职责的助手编成一支能一起干活的小团队。",
        "下达目标后，各角色按职责分头产出，再把结果交给下一位。",
        "只管分工和交接，并不代替你完成某一个具体业务。",
    ],
    "CLI": [
        "在终端里直接跟模型说话，不必再绕一层网页。",
        "把常用操作写成命令，结果立刻打在屏幕上。",
        "只管对话和执行，不替你做完整的产品界面。",
    ],
    "MCP": [
        "把外部工具接到 AI 助手上，让助手真能动手做事。",
        "用平常说话的方式指挥助手读写文件或调用服务。",
        "提供的是接线能力，并不自带某一个具体业务功能。",
    ],
    "RAG": [
        "让助手先查你自己的资料再回答，少一本正经地胡说。",
        "把文档放进去之后，可以用大白话追问里面写了什么。",
        "只根据你提供的材料作答，不负责替你改原文。",
    ],
    "模型": [
        "把模型在自己的机器上跑起来、试起来。",
        "拿自己的例子比速度和效果，少在云上绕一圈。",
        "负责把模型跑通，不替你完成某一个具体产品。",
    ],
    "框架": [
        "少写重复的胶水代码，把 AI 能力嵌进现有项目。",
        "按模块把能力接上，先跑通一个最小例子。",
        "提供骨架，不替你决定产品要做成什么样。",
    ],
    "WebUI": [
        "把 AI 能力收成网页上的按钮，打开就能用。",
        "不必先啃命令行，也能点着试效果。",
        "管的是界面入口，不负责背后模型怎么训练。",
    ],
    "SDK": [
        "用几行代码把 AI 接到自己的程序里。",
        "少自己对接接口细节，先跑官方最小例子。",
        "提供调用方式，不替你完成业务逻辑。",
    ],
    "数据集": [
        "提供能直接练、能直接评的样本，少自己到处爬。",
        "先抽一小份看字段和质量，再决定怎么用。",
        "给的是数据，不负责替你把模型训练完。",
    ],
    "插件": [
        "把 AI 接到已经在用的工具里，不用换窗口。",
        "在熟悉的界面里用一条真实任务试能不能省事。",
        "只加一层能力，不替换原来的工具。",
    ],
}

KIND_HOW = {
    "Agent": ["先定角色和各自职责", "用任务把角色串起来", "看过程，再改提示词"],
    "CLI": ["装到本机命令行", "对着项目直接下指令", "不满意就改一句话再跑"],
    "MCP": ["在助手里启用这个接口", "把要操作的工具连上", "用平常说话的方式指挥助手"],
    "RAG": ["把资料放到能检索的地方", "提问时先检索再生成", "不对的地方补文档再问"],
    "模型": ["选好要跑的模型", "按说明启动", "用自己的例子试一遍"],
    "框架": ["装进现有项目", "按模块把能力接上", "先跑通一个小例子"],
    "WebUI": ["启动服务", "打开页面", "在界面上点着试"],
    "SDK": ["引入这个库", "填自己的密钥或模型", "先跑官方最小例子"],
    "数据集": ["下载或克隆这份数据", "按字段说明读进去", "先抽一小份看质量"],
    "插件": ["在宿主工具里安装", "打开权限", "用一条真实任务试"],
}

KIND_AUDIENCE = {
    "Agent": ["想搭小型 AI 团队的人", "想把流程交给助手的产品或运营"],
    "CLI": ["习惯终端的开发者", "不想开网页就能用模型的人"],
    "MCP": ["已经在用 AI 助手、还想让它动手的人"],
    "RAG": ["有一堆内部文档、希望 AI 先查再答的人"],
    "模型": ["想自己跑模型、对比效果的人"],
    "框架": ["要把 AI 嵌进产品里的开发者"],
    "WebUI": ["更想点按钮、不想记命令的人"],
    "SDK": ["要在自己程序里调 AI 的开发者"],
    "数据集": ["做训练、评测、研究的人"],
    "插件": ["已经有常用工具、只想加一层 AI 的人"],
}

KIND_SCENE = {
    "Agent": ["本地把开发、内容和运营串成一条流水线", "先用一个真实任务看完整协作"],
    "CLI": ["在项目目录里快速试验提示词", "把重复命令交给模型跑"],
    "MCP": ["让助手读写文件、调用外部服务", "把日常工具接进对话里"],
    "RAG": ["拿公司或个人文档来问答", "先检索、再写成能核对的回答"],
    "模型": ["本机试模型、比速度和效果", "不把数据送到别人的云上"],
    "框架": ["在现有代码库里加一层 AI 能力", "先做一个最小可运行的功能"],
    "WebUI": ["给不会命令行的同事试用", "把演示收成一个页面"],
    "SDK": ["在自己的服务里调用模型", "把生成结果接到业务接口"],
    "数据集": ["训练前先看样本长什么样", "用公开数据做对比实验"],
    "插件": ["在编辑器或浏览器里直接叫 AI", "不离开正在用的窗口"],
}


def ranking_from_repos(
    repos: list[dict[str, Any]],
    *,
    brand: str = AUTHOR_BRAND,
    series: str = "GitHub 本周爆火 AI 项目榜",
    issue: str = "",
    date_range: str = "",
    avatar: str = "",
) -> dict[str, Any]:
    items = []
    tags: list[str] = []
    for index, repo in enumerate(repos[:10], start=1):
        kind = str(repo.get("kind") or repo.get("category") or "")
        if kind and kind not in tags:
            tags.append(kind)
        items.append(
            {
                "rank": int(repo.get("rank") or index),
                "name": str(repo.get("name") or ""),
                "description": str(repo.get("summary") or repo.get("description") or ""),
                "category": kind,
                "logo": _safe_logo(repo),
                "url": str(repo.get("url") or ""),
                "metric": {
                    "label": "Stars",
                    "value": int(repo.get("stars") or 0),
                },
            }
        )
    return {
        "page": "ranking-overview",
        "brand": brand,
        "series": series,
        "issue": issue or iso_week_issue(),
        "dateRange": date_range,
        "kicker": "WEEKLY RANKING",
        "title": "GitHub",
        "subtitle": "本周爆火 AI 项目榜",
        "source": "GitHub",
        "footer": "专业 AI 科技媒体 · 每周更新",
        "tags": tags[:6],
        "items": items,
        "avatar": avatar or (author_avatar_data_uri() if brand == AUTHOR_BRAND else ""),
    }


def project_from_repo(
    repo: dict[str, Any],
    *,
    brand: str = AUTHOR_BRAND,
    series: str = "GitHub 本周爆火 AI 项目榜",
    issue: str = "",
    page_number: str = "",
    avatar: str = "",
) -> dict[str, Any]:
    kind = str(repo.get("kind") or "AI 工具")
    features = to_bullets(repo.get("does") or repo.get("description") or "", max_len=48)
    if not features:
        features = list(KIND_DOES.get(kind) or ["把这个开源工具用起来"])
    how = [item for item in to_bullets(repo.get("how") or "") if not is_generic_how(item)]
    if not how:
        how = list(KIND_HOW.get(kind) or ["按自己的任务试一遍", "看能不能省事", "再决定要不要留下"])
    audience = to_bullets(repo.get("audience") or "")
    scene = to_bullets(repo.get("scene") or "")
    if audience and not scene:
        scene = list(KIND_SCENE.get(kind) or audience)
        audience = list(KIND_AUDIENCE.get(kind) or audience)
    if not audience:
        audience = list(KIND_AUDIENCE.get(kind) or ["想先看懂再决定要不要用的人"])
    if not scene:
        scene = list(KIND_SCENE.get(kind) or ["先拿一个真实小任务试"])
    name = str(repo.get("name") or "")
    key_message = capability_line(repo)
    tagline = str(repo.get("tagline") or "").strip()
    if (
        not tagline
        or tagline == name
        or tagline == key_message
        or tagline == str(repo.get("summary") or "").strip()
        or is_generic_how(tagline)
        or _looks_english(tagline)
    ):
        tagline = ""
    return {
        "page": "project-detail",
        "brand": brand,
        "series": series,
        "issue": issue or iso_week_issue(),
        "avatar": avatar or (author_avatar_data_uri() if brand == AUTHOR_BRAND else ""),
        "pageNumber": page_number,
        "rank": int(repo.get("rank") or 1),
        "category": kind,
        "name": name,
        "tagline": tagline,
        "description": str(repo.get("description") or ""),
        "keyMessage": key_message,
        "logo": _safe_logo(repo),
        "markSvg": str(repo.get("markSvg") or ""),
        "features": features,
        "how": how,
        "audience": audience,
        "scene": scene,
        "url": str(repo.get("url") or ""),
        "source": "GitHub",
        "footer": "专业 AI 科技媒体 · 每周更新",
    }


def to_bullets(text: Any, *, limit: int = 5, max_len: int = 28) -> list[str]:
    if isinstance(text, (list, tuple)):
        parts = [str(item).strip(" -•\t") for item in text if str(item).strip()]
    else:
        raw = str(text or "").strip()
        if not raw or is_generic_how(raw):
            return []
        if "\n" in raw:
            parts = [line.strip(" -•\t") for line in raw.splitlines() if line.strip()]
        elif "。" in raw:
            parts = [f"{piece.strip()}。" for piece in raw.split("。") if piece.strip()]
        else:
            parts = _split(raw)
        if len(parts) == 1 and len(parts[0]) > 40 and "。" not in parts[0]:
            parts = [
                piece.strip()
                for piece in re.split(r"[；;]|(?<=[\u4e00-\u9fff])，(?=[\u4e00-\u9fff]{4,})", parts[0])
                if piece.strip()
            ]
    parts = _explode_cjk_spaces(parts)
    cleaned: list[str] = []
    for part in parts:
        piece = re.sub(r"^(可以|能够|支持|提供)", "", part).strip(" ，、；;")
        if _looks_clipped(piece):
            continue
        if (
            piece
            and piece not in cleaned
            and not is_generic_how(piece)
            and not _looks_english(piece)
            and not _is_it_sentence(piece)
        ):
            cleaned.append(piece)
        if len(cleaned) >= limit:
            break
    return cleaned


def capability_line(repo: dict[str, Any]) -> str:
    kind = str(repo.get("kind") or "")
    for candidate in (repo.get("summary"),):
        text = str(candidate or "").strip()
        if not text or is_generic_how(text) or _looks_english(text) or _is_it_sentence(text):
            continue
        if not _looks_identity_line(text):
            continue
        return _clip_line(text, 72)
    return KIND_ONE_LINER.get(
        kind, "给想先看懂再动手的人，拿一个真实小任务试的时候，先判断这个工具能不能省事。"
    )


def _is_it_sentence(text: str) -> bool:
    compact = (text or "").lstrip()
    return (
        compact.startswith("它")
        or compact.startswith("这是")
        or compact.startswith("这个工具")
        or compact[:8].find("帮你") >= 0
    )


def _looks_identity_line(text: str) -> bool:
    compact = (text or "").lstrip()
    return compact.startswith("给") and ("时" in compact or "时候" in compact)


def is_generic_how(text: str) -> bool:
    compact = (text or "").replace(" ", "")
    return any(hint.replace(" ", "") in compact for hint in GENERIC_HOW)


def _safe_logo(repo: dict[str, Any]) -> str:
    url = str(repo.get("icon_url") or repo.get("logo") or "")
    if not url or is_author_avatar(url):
        return ""
    return url


def _bare_capability(text: str) -> str:
    piece = re.sub(r"^(这是|这款|一个|一种)", "", text.strip())
    return piece or text.strip()


def _looks_english(text: str) -> bool:
    latin = len(LATIN_RE.findall(text or ""))
    cjk = len(CJK_RE.findall(text or ""))
    return latin > cjk


def _clip_line(text: str, limit: int) -> str:
    value = " ".join((text or "").split())
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _metrics(repo: dict[str, Any]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = [{"label": "Stars", "value": int(repo.get("stars") or 0)}]
    week = int(repo.get("stars_week") or 0)
    if week:
        rows.append({"label": "本周", "value": week})
    return rows


def _split(text: str) -> list[str]:
    for separator in ("、", "，", ",", "；", ";"):
        if separator in text:
            return [part.strip() for part in text.split(separator) if part.strip()]
    return [text.strip()] if text.strip() else []


def _explode_cjk_spaces(parts: list[str]) -> list[str]:
    out: list[str] = []
    for part in parts:
        chunks = [item.strip() for item in CJK_SPACE_RE.split(part) if item.strip()]
        out.extend(chunks)
    return out


def _looks_clipped(text: str) -> bool:
    compact = (text or "").rstrip()
    if not CLIP_MARK_RE.search(compact):
        return False
    stem = CLIP_MARK_RE.sub("", compact).rstrip()
    if not stem:
        return True
    return not stem.endswith(("。", "！", "？", "："))
