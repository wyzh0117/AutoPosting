<div align="center">
  <h1>AutoPosting</h1>
  <p>把资讯整理成来源可查、经过人工审核的公众号与小红书草稿。</p>
</div>

[English](README.en.md) · [项目主页](https://github.com/wyzh0117/AutoPosting)

## 项目介绍

AutoPosting 是一个本地运行的资讯采集与内容整理工具。它把来源、事件、证据和草稿放在本机 SQLite 数据库中，方便逐步复核和管理。

## 快速开始

需要 Python 3.11 或更新版本。项目没有第三方运行依赖。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .

# 创建本地数据库并注册示例来源
autoposting init --db data/autoposting.db --sources config/sources.example.json

# 采集资讯并打开本地工作台
autoposting collect --db data/autoposting.db --sources config/sources.example.json
autoposting web --db data/autoposting.db --sources config/sources.example.json --port 8080
```

然后打开[本地工作台](http://127.0.0.1:8080/)。工作台仅监听本机地址；采集资讯和使用 AI 时会连接你配置的信息源或服务。Windows PowerShell 用户运行下面的命令：

```powershell
.\.venv\Scripts\Activate.ps1
```

## 工作方式

工具定时或手动读取已配置的资讯源，整理重复条目并聚合相关事件。你可以查看事件证据，再生成带来源记录的公众号或小红书草稿。草稿要经过人工检查和批准后，才会进入下一步。

## 可以做什么

- 采集 RSS 等已配置来源，查看候选资讯和事件；
- 将选中的事件整理成带来源的研究资料；
- 生成可单独编辑、审核的公众号与小红书草稿；
- 为草稿补充事实来源、批准或退回修改；
- 将公众号草稿交给官方接口，或生成小红书本地发布包供人工发布。

AI 写作和公众号接口需要自行配置；不配置时仍可使用采集、整理和人工审核流程。小红书内容始终由用户在官方创作平台手动发布。

## 示例

早上运行一次采集，打开资讯页挑选关注的事件，查看每条结论对应的来源，再为公众号或小红书分别生成草稿。批准后可把公众号草稿保存到公众号草稿箱，或导出小红书发布包。

## License

仓库当前没有提供 LICENSE 文件，使用、修改和再发布的授权方式尚未声明。
