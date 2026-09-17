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
    # Skill-domain categories: used by the skill finder lane (B5).
    "视频": '给一个人管多个账号的人，手上素材要变成成片时，把剪辑和发布前的粗活交出去。',
    "图像": '给要稳定出图的人，得批量产封面或配图时，把出图这一步收成固定流程。',
    "写作": '给每周都要交稿的人，从零开始憋标题和正文时，先拿到能改的初稿。',
    "数据": '给要拿数据说话的人，表格和口径乱成一团时，把整理和分析固定成几步。',
    "测试": '给要保证质量的人，改动越来越频繁时，把回归检查交给固定的检查清单。',
    "部署": '给要把东西放到线上的人，手工上线容易漏步骤时，把发布过程写成可重复的流程。',
    "演示": '给要讲清楚一件事的人，临时被要求出材料时，把大纲和页面一次做出来。',
    "设计": '给没有专职设计的人，界面和物料要统一时，按一套规则把视觉收齐。',
    "翻译": '给要看外文材料的人，术语和语气不能乱时，把翻译固定成一条可复核的流程。',
    "检索": '给要快速找到出处的人，信息散在很多地方时，把搜索和核对串成一步。',
    "音频": "给要做配音或播客的人，稿子写完还得录一遍时，把声音这一步先做出来。",
    "采集": "给要盯着一批网页的人，信息散在各处时，把抓取和归拢固定成一条流程。",
    '音频': '给要做配音或播客的人，稿子写完还得录一遍时，把声音这一步先做出来。',
    '采集': '给要盯着一批网页的人，信息散在各处时，把抓取和归拢固定成一条流程。',
    'AI 工具': '给要自己做这一步的人，这活得重复干的时候，先拿一个现成的做法顶上。',
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
    # Skill-domain categories: used by the skill finder lane (B5).
    "视频": ['把素材按脚本顺序排好，不必手动拖时间线。', '从长素材里切出可用片段，先出一版粗剪。', '只管剪辑和成片前的整理，不负责替你选题。'],
    "图像": ['按固定提示词批量出图，风格保持一致。', '把尺寸和留白统一好，直接能放进版式。', '只负责出图这一步，不替你定内容方向。'],
    "写作": ['按给定结构先出一版初稿，你只做删改。', '把同一份材料改写成不同平台的语气。', '负责初稿和改写，不替你核实事实真假。'],
    "数据": ['把散落的表格按统一口径合到一张表。', '按你的问题算出结果，并标出用了哪些列。', '负责整理和计算，不替你判断数据能不能用。'],
    "测试": ['按清单逐项跑一遍，把失败项挑出来。', '改动之后重跑关键路径，先发现退化。', '负责照单检查，不替你设计业务规则。'],
    "部署": ['把上线步骤写成脚本，减少手工操作。', '每次发布按同一顺序执行，出问题能回到上一版。', '负责发布流程，不替你决定什么时候发。'],
    "演示": ['把要点整理成有先后顺序的大纲。', '按大纲生成页面，标题和层级都排好。', '负责把材料成稿，不替你确认结论。'],
    "设计": ['按一套颜色和字号把页面统一起来。', '生成常用组件的样子，减少每次重新配色。', '负责视觉一致性，不替你定产品结构。'],
    "翻译": ['按术语表翻译，专有名词前后一致。', '保留原文段落结构，方便逐段对照。', '负责语言转换，不替你判断原文对错。'],
    "检索": ['把关键词拆成几路一起搜，扩大覆盖面。', '把命中的出处列出来，方便逐条核对。', '负责找到并列出，不替你下结论。'],
    '音频': ['把写好的稿子转成可用的配音。', '把时长和停顿调到能听。', '只管出声这一步，不替你改稿子。'],
    '采集': ['按给定的站点列表把内容抓下来。', '把抓到的条目归到一张表里。', '只管取回和整理，不替你判断真假。'],
    'AI 工具': ['接手你指定的那一步，按说明把活干完。', '把结果交回给你，不替你决定做成什么样。', '只管这一步，别处的流程还得你自己接。'],
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
    # Skill-domain categories: used by the skill finder lane (B5).
    "视频": ['把素材放进约定的目录', '按脚本说明跑一遍', '看粗剪再改提示词'],
    "图像": ['先定好风格和尺寸', '按清单批量出图', '挑出可用的再补细节'],
    "写作": ['把材料和要求交给它', '先拿初稿再逐段改', '定稿前自己核事实'],
    "数据": ['把表格放一起', '说明口径和问题', '核对它用的列'],
    "测试": ['写下要检查的清单', '改动后重跑一遍', '把失败项逐条复现'],
    "部署": ['把发布步骤写成脚本', '先在测试环境跑一遍', '按同一流程发正式'],
    "演示": ['先把要点列出来', '让它生成大纲和页面', '按听众再调顺序'],
    "设计": ['先定颜色和字号', '让组件按规则生成', '在页面上统一套用'],
    "翻译": ['准备好术语表', '按段落整篇翻译', '对照原文抽查'],
    "检索": ['把问题拆成关键词', '几路一起搜', '逐条打开核对'],
    "音频": ["把稿子按段落切好", "选好音色和语速", "导出后自己听一遍"],
    "采集": ["写好要抓的站点和字段", "先小批量跑一次", "确认格式再全量抓"],
    '音频': ['把稿子按段落切好', '选好音色和语速', '导出后自己听一遍'],
    '采集': ['写好要抓的站点和字段', '先小批量跑一次', '确认格式再全量抓'],
    'AI 工具': ['先看它要什么输入', '拿一份真实材料跑一遍', '结果不对就改说明再跑'],
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
    # Skill-domain categories: used by the skill finder lane (B5).
    "视频": ['一个人负责多个账号的内容创作者'],
    "图像": ['要批量做封面和配图的人'],
    "写作": ['每周有固定更新量的作者或运营'],
    "数据": ['要拿表格得出结论的业务或分析师'],
    "测试": ['改动频繁、还要保证可用的开发者'],
    "部署": ['要自己把服务放到线上的人'],
    "演示": ['临时被要求出材料的人'],
    "设计": ['团队里没有专职设计的人'],
    "翻译": ['要读或要发外文材料的人'],
    "检索": ['需要给结论找出处的人'],
    "音频": ["要做配音或播客的人"],
    "采集": ["要长期盯一批网页的人"],
    '音频': ['要做配音或播客的人'],
    '采集': ['要长期盯一批网页的人'],
    'AI 工具': ['要把这一步做成固定流程的人'],
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
    # Skill-domain categories: used by the skill finder lane (B5).
    "视频": ['手上素材堆着、成片交期又近的时候', '同一条内容要改成多个版本发出去'],
    "图像": ['一次要出十几张配图的时候', '风格必须和上一期保持一致'],
    "写作": ['对着空白文档开不了头的时候', '同一份材料要发到不同平台'],
    "数据": ['几份表格口径对不上的时候', '汇报前需要一组能核对的数字'],
    "测试": ['刚改完核心逻辑、要确认没弄坏别的', '上线前需要一份可复现的检查结果'],
    "部署": ['每次上线都靠手点、容易漏步骤', '出问题需要快速回到上一版'],
    "演示": ['明天就要讲、材料还没成型', '同一份内容要讲给不同听众'],
    "设计": ['几期内容看起来不像同一套', '要快速凑齐一套能用的组件'],
    "翻译": ['术语前后不一致被指出来过', '需要逐段对照原文检查'],
    "检索": ['要给一个说法找到原始出处', '信息散在好几个站点里'],
    "音频": ["稿子定了但没人录的时候", "同一篇要出多个语言版本"],
    "采集": ["同一批信息每天都要看一遍", "要把零散页面并成一张表"],
    '音频': ['稿子定了但没人录的时候', '同一篇要出多个语言版本'],
    '采集': ['同一批信息每天都要看一遍', '要把零散页面并成一张表'],
    'AI 工具': ['这一步每周都要重来一遍', '想把做法固定下来交给别人'],
}


def _lane_text(value: str | None, default: str) -> str:
    """``None`` keeps the GitHub weekly default; an explicit ``""`` stays blank."""
    return default if value is None else value


def ranking_from_repos(
    repos: list[dict[str, Any]],
    *,
    brand: str | None = AUTHOR_BRAND,
    series: str | None = "GitHub 本周爆火 AI 项目榜",
    issue: str | None = None,
    date_range: str | None = "",
    avatar: str = "",
    source: str | None = "GitHub",
    footer: str | None = "专业 AI 科技媒体 · 每周更新",
    kicker: str | None = "WEEKLY RANKING",
    title: str | None = "GitHub",
    subtitle: str | None = "本周爆火 AI 项目榜",
    metric_label: str = "Stars",
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
                    "label": metric_label,
                    "value": int(repo.get("stars") or 0),
                },
            }
        )
    resolved_brand = _lane_text(brand, AUTHOR_BRAND)
    return {
        "page": "ranking-overview",
        "brand": resolved_brand,
        "series": _lane_text(series, "GitHub 本周爆火 AI 项目榜"),
        "issue": iso_week_issue() if issue is None else issue,
        "dateRange": _lane_text(date_range, ""),
        "kicker": _lane_text(kicker, "WEEKLY RANKING"),
        "title": _lane_text(title, "GitHub"),
        "subtitle": _lane_text(subtitle, "本周爆火 AI 项目榜"),
        "source": _lane_text(source, "GitHub"),
        "footer": _lane_text(footer, "专业 AI 科技媒体 · 每周更新"),
        "tags": tags[:6],
        "items": items,
        "avatar": avatar or (author_avatar_data_uri() if resolved_brand == AUTHOR_BRAND else ""),
    }


def project_from_repo(
    repo: dict[str, Any],
    *,
    brand: str | None = AUTHOR_BRAND,
    series: str | None = "GitHub 本周爆火 AI 项目榜",
    issue: str | None = None,
    page_number: str = "",
    avatar: str = "",
    source: str | None = "GitHub",
    footer: str | None = "专业 AI 科技媒体 · 每周更新",
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
    resolved_brand = _lane_text(brand, AUTHOR_BRAND)
    return {
        "page": "project-detail",
        "brand": resolved_brand,
        "series": _lane_text(series, "GitHub 本周爆火 AI 项目榜"),
        "issue": iso_week_issue() if issue is None else issue,
        "avatar": avatar or (author_avatar_data_uri() if resolved_brand == AUTHOR_BRAND else ""),
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
        "source": _lane_text(source, "GitHub"),
        "footer": _lane_text(footer, "专业 AI 科技媒体 · 每周更新"),
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
