<div align="center">
  <h1>AutoPosting</h1>
  <p>Turn news into source-backed drafts for WeChat and Xiaohongshu, ready for human review.</p>
</div>

[简体中文](README.md) · [Project home](https://github.com/wyzh0117/AutoPosting)

## About

AutoPosting is a local news collection and content planning tool. Sources, events, evidence, and drafts are stored in a SQLite database on your computer so you can review and manage them together.

## Quick start

Requires Python 3.11 or newer. The project has no third-party runtime dependencies.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .

# Create a local database and register the sample sources
autoposting init --db data/autoposting.db --sources config/sources.example.json

# Collect news and start the local workbench
autoposting collect --db data/autoposting.db --sources config/sources.example.json
autoposting web --db data/autoposting.db --sources config/sources.example.json --port 8080
```

Open [http://127.0.0.1:8080/](http://127.0.0.1:8080/) in your browser. The workbench runs on your computer; collection and optional AI or publishing services connect to the endpoints you configure. On Windows, activate the environment in PowerShell with `.\.venv\Scripts\Activate.ps1`.

## How it works

The app reads configured feeds, removes duplicate items, and groups related coverage into events. You can review the sources, prepare research notes, and create WeChat or Xiaohongshu drafts with source records. A person checks and approves each draft before it moves forward.

## What you can do

- Collect configured RSS and other supported sources, then review candidate stories.
- Gather selected events and their evidence into research notes.
- Create separate, editable drafts for WeChat and Xiaohongshu.
- Check source-backed facts and approve or return drafts for changes.
- Save WeChat drafts through the official API, or create a local package for manual Xiaohongshu publishing.

AI writing and the WeChat API need your own configuration. Collection, organization, and manual review still work without them. Xiaohongshu posts are always published by the user through its official creator platform.

## Example

Run a collection, choose an event in the news page, review its sources, and create a draft for each platform. After approval, save the WeChat draft to the official account or export a Xiaohongshu package for manual posting.

## License

The repository does not currently include a LICENSE file, so usage, modification, and redistribution terms have not been declared.
