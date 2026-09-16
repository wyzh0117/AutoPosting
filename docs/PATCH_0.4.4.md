# AutoPosting 0.4.4

## 变更

- 将“打开原文”弹窗中的“翻译文章”改为无边框的轻量文字按钮，宽度随文字自然展开。
- 翻译按钮和右上角关闭按钮固定为单行排列，不再换行。
- 长标题在空间不足时自动省略，避免窄屏下把操作按钮挤出弹窗。

## 边界

- 本次仅调整弹窗头部样式，不改变原文抓取、翻译模型或翻译流程。
- 未新增运行时依赖。

## 验证

- `PYTHONPATH=src:tests python3 -m unittest discover -s tests -v`：210 项通过。
- `PYTHONPATH=src python3 -m autoposting.cli self-check --workspace data/self-check`：通过。
- 浏览器实测桌面和 320px 宽度：按钮文字均为 1 行，操作区 `nowrap`，关闭按钮完整可见，页面无横向溢出。
- `graft build` 与 `git diff --check`：通过。
