# AutoPosting 1.0.9

## 变更

- 详情海报不再用 GitHub 作者头像当 Logo。README 里有专用 logo/icon 才用原图；否则用主题 SVG（Agent / CLI / RAG 等），写入 `visual-marks/catalog.json`，按标签复用，不打牵强标签。
- 「一句话看懂」改成能力范围白话，不再使用「开源仓库，安装步骤和文档在项目主页」。
- 删掉功能示意图、指标面板、项目地址卡。主体只留：一句话看懂、做什么？（主面积）、如何实现、适合哪些人群、使用场景。长句拆成短 bullet。
- 积木仍是 BrandHeader / Masthead+Logo / QuoteCard / BulletPanel / Footer，没有再拆更碎的组件。

## 边界

- 榜单封面仍用同一套 Logo 规则。StoryBoard / 功能示意图代码还在，详情页不再调用。
- 标签词表是受控的（agent、cli、rag…），不会给所有项目打「AI」「开源」。

## 验证

- unittest 283 通过。
- 离线 self-check 10/10。
- 服务 `/api/status` 版本 1.0.9。
- example PNG：`output/visual/project-detail/project-detail.png`。

Source snapshot: `archives/autoposting-patch-1.0.9-update-20260904.tar.gz`.
