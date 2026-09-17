# 端口拉起教程

> Notebook —— 把 AutoPosting 本地 Web 服务在 **8090** 端口完整拉起的全过程记录。
> 本文所有命令均在项目根目录 `/Users/youngi/Documents/MiniWork/AutoPosting-development` 下实际执行过，输出为真实回显。

| 项 | 值 |
| --- | --- |
| 服务地址 | http://127.0.0.1:8090/ |
| 绑定地址 | `127.0.0.1`（仅本机可访问） |
| Python | 3.11.4（仅标准库 + SQLite） |
| 项目版本 | 1.3.1 |
| 数据库 | `data/autoposting.db` |
| 源配置 | `config/sources.example.json` |
| 验证时间 | 2026-09-16 |

---

## 一、原理：端口从哪来

服务的端口不是硬编码写死的，启动入口有三层默认值，**命令行参数优先级最高**：

| 位置 | 默认值 | 说明 |
| --- | --- | --- |
| `src/autoposting/cli.py:102` | `8080` | `web` 子命令的 `--port` 默认值 |
| `src/autoposting/web.py:6937` | `8080` | `create_server(app, host, port)` 形参默认值 |
| `service_control.py:25` | `8080` | `DEFAULT_PORT`，macOS Tk 控制面板用 |

所以「换端口」不需要改任何代码，只要在启动时显式传 `--port 8090` 即可覆盖默认的 8080。

另外 `web.py` 对 host 做了白名单校验（`ALLOWED_HOSTS`），只允许 `127.0.0.1` 或 `localhost`；
传其它地址会直接抛 `ValueError: web server host must be 127.0.0.1 or localhost`。
**这意味着服务无法通过 0.0.0.0 暴露到局域网**，这是项目的安全设计，不是故障。

### 启动链路

```
python3 -m autoposting.cli web --port 8090
        └─> cli.py: args.command == "web"
              └─> WebApp(Database(db), load_sources(sources), packages)   # 装载 SQLite + 源配置
                    └─> run_web(app, host, port)
                          ├─> create_server(...)  → ThreadingHTTPServer 绑定 127.0.0.1:8090
                          ├─> app.scheduler.start()  # 后台调度器随服务一起起
                          └─> server.serve_forever() # 阻塞在此，直到收到停止信号
```

---

## 二、拉起前的前置检查

拉起前先确认三件事，避免把「端口冲突」和「文件缺失」误判成代码问题。

### 1. 端口是否已被占用

```bash
lsof -nP -iTCP:8090 -sTCP:LISTEN
```

- **有输出** → 端口已被占用，先停掉旧进程，或换端口。
- **无输出** → 端口空闲，可以拉起。本次实测结果为空（8090 空闲）。

> 注意：被占用的进程如果不是 AutoPosting，直接抢占会失败并抛 `OSError: [Errno 48] Address already in use`。

### 2. 依赖文件是否存在

```bash
ls -la data/ config/
```

必须存在：

- `data/autoposting.db` —— SQLite 数据库（本次 3.4 MB，已含历史数据）
- `config/sources.example.json` —— 采集源配置（本次 10 KB）

### 3. Python 版本

```bash
python3 --version
```

要求 `>= 3.11`。本次为 `Python 3.11.4`，满足要求（项目使用了 `from __future__ import annotations` 等 3.11 特性）。

---

## 三、拉起命令

在项目根目录执行：

```bash
PYTHONPATH=src python3 -m autoposting.cli web \
  --db data/autoposting.db \
  --sources config/sources.example.json \
  --host 127.0.0.1 \
  --port 8090
```

### 参数逐项说明

| 参数 | 作用 | 不传的后果 |
| --- | --- | --- |
| `PYTHONPATH=src` | 让 Python 找到 `src/autoposting` 包 | 报 `No module named autoposting`（项目未做 pip 安装） |
| `--db` | 指定 SQLite 数据库 | 落到默认库路径，可能读不到已有数据 |
| `--sources` | 指定采集源配置 | 落到默认源路径 |
| `--host` | 绑定地址，只能 `127.0.0.1`/`localhost` | 默认即 `127.0.0.1`，可省略 |
| `--port` | **本教程的关键**：覆盖默认 8080 | 默认监听 8080，不是 8090 |

### 为什么 `PYTHONPATH=src` 不能省

`pyproject.toml` 声明了包目录在 `src/` 下（`package-dir = {"" = "src"}`），
但本项目是「零第三方依赖、直接跑源码」的用法，没有执行 `pip install -e .`。
因此必须靠 `PYTHONPATH=src` 把 `src` 加进模块搜索路径，否则 Python 找不到 `autoposting` 包。

### 常驻运行方式（重要）

`run_web()` 里的 `serve_forever()` 是**阻塞**的：进程会一直占住终端，不返回。

三种方式的取舍：

| 方式 | 命令 | 是否可持续 |
| --- | --- | --- |
| 前台 | 直接执行上面的命令 | ❌ 占住终端，`Ctrl+C` 即停 |
| **会话托管后台作业** | Agent 的 `run_in_background` | ❌ **会话/作业被回收时进程一起被杀**，端口随之掉线 |
| **`nohup` 脱离会话** | 见下方脚本 | ✅ **退出终端、脚本结束后仍在跑** |

> ⚠️ **实测坑（务必避开）**：本次先用「会话托管后台作业」拉起，作业随后收到 SIGTERM 被回收，
> **8090 端口随之掉线**（`lsof` 无输出、`curl` 返回 `status=000`）。
> 所以要让服务真正常驻，必须用 `nohup` 脱离会话，不能依赖托管作业。

已把可靠方式固化成脚本 `scripts/start_8090.sh`（**幂等**：已在监听则直接复用，不重复启动）：

```bash
./scripts/start_8090.sh
```

脚本核心就是 `nohup` + 三处重定向，缺一不可：

```bash
nohup env PYTHONPATH=src python3 -m autoposting.cli web \
  --db data/autoposting.db \
  --sources config/sources.example.json \
  --host 127.0.0.1 \
  --port 8090 \
  > data/web-8090.log 2>&1 < /dev/null &
```

| 片段 | 为什么不能省 |
| --- | --- |
| `nohup` | 忽略挂断信号，终端退出后进程不被带走 |
| `> data/web-8090.log 2>&1` | 把 stdout/stderr 落到文件，便于排错；否则仍绑在终端上 |
| `< /dev/null` | 切断 stdin，防止进程因等待输入而卡住 |
| `env PYTHONPATH=src` | `nohup` 后不能直接写 `PYTHONPATH=src cmd` 之外的前缀，用 `env` 最稳 |

脚本自身的两个坑也已在脚本里修掉，自己写脚本时注意：

1. **必须 `cd` 到项目根目录**。脚本放在 `scripts/` 下时若写 `cd "$(dirname "$0")"`，
   工作目录会变成 `scripts/`，导致 `PYTHONPATH=src` 找不到包、`data/...` 也找不到文件，
   报错是 `ModuleNotFoundError: No module named 'autoposting'`。
   正确写法：`cd "$(dirname "$0")/.."`。
2. **`$PORT` 后面紧跟中文标点要写成 `${PORT}`**。`echo "... $PORT，最后日志"`
   里全角逗号 `，`（UTF-8 字节 `ef bc 8c`）会被 bash 当成变量名的一部分，
   在 `set -u` 下直接报 `PORT，: unbound variable`。加花括号即可：`${PORT}，`。

---

## 四、验证：确认真的拉起来了

**能连上端口 ≠ 服务可用**。下面四步逐层验证，缺一不可。

### 第 1 步：确认端口处于 LISTEN 状态

```bash
lsof -nP -iTCP:8090 -sTCP:LISTEN
```

实测回显：

```
COMMAND   PID   USER   FD   TYPE             DEVICE SIZE/OFF NODE NAME
Python  88733 youngi    6u  IPv4 0x7cdc7f28d5deeb54      0t0  TCP 127.0.0.1:8090 (LISTEN)
```

✅ 关键信息：`TCP 127.0.0.1:8090 (LISTEN)` —— 进程已在 8090 上监听，且绑定在回环地址。

### 第 2 步：确认 HTTP 能正常应答

```bash
curl -s -o /dev/null -w "status=%{http_code} time=%{time_total}s\n" http://127.0.0.1:8090/
```

实测回显：

```
status=200 time=0.002690s
```

✅ HTTP 200，响应 2.7 ms。

### 第 3 步：确认页面真的渲染出了内容

```bash
curl -s http://127.0.0.1:8090/ | head -c 700
curl -s http://127.0.0.1:8090/ | grep -o "<title>[^<]*</title>"
curl -s http://127.0.0.1:8090/ | grep -o "1\.3\.1" | head -3
```

实测回显（节选）：

```html
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>AutoPosting</title>
<style>
:root {
  --bg-main: #12110e;
  --accent: #c4a46a;
  ...
```

```
<title>AutoPosting</title>
1.3.1          ← 版本徽标与 __init__.py / pyproject.toml 的 1.3.1 一致
```

✅ 首页完整 HTML 216,551 字节，标题与版本徽标均正确 —— 证明静态页面装配链路正常。

### 第 4 步：确认业务数据接口真的返回数据（功能验收）

只验证首页是「表面检查」。必须再打一个真实业务接口，证明 **SQLite + 源配置都装载成功**：

```bash
curl -s http://127.0.0.1:8090/api/hotspots
```

实测回显（节选）：

```json
{"ok": true, "count": 0, "hours": 4, "candidates": [], "rankings": {"time": [], "heat": []},
 "sources": [{"source_id": "openai_news", "name": "OpenAI News", "type": "rss", "tier": "S1",
 "enabled": true, "fetch_interval_minutes": 15,
 "last_fetched_at": "2026-09-16T13:32:43.437497+00:00", ...}]}
```

✅ `ok: true`，且 `sources` 数组里带着真实的源定义和 `last_fetched_at` 历史抓取时间。
这说明服务不只是「端口通了」，而是**真的读到了配置和数据库**。

> ⚠️ 排查提示：如果 `curl /api/articles` 或 `/api/config` 返回
> `{"ok": false, "error": "not_found:'/api/articles'"}`，
> **这不是故障**。这两个路由在 `web.py:6770` / `web.py:6802` 中只处理 POST，
> 用 GET 直接访问自然会 404 KeyError。要用 POST 方式调用。

---

## 五、访问与停止

### 打开页面

浏览器访问：**http://127.0.0.1:8090/**

### 停止服务

通用方式（按端口反查并结束进程，不依赖记住 PID）：

```bash
kill $(lsof -t -nP -iTCP:8090 -sTCP:LISTEN)
```

⚠️ **停完必须复查**：`kill` 发出的是 SIGTERM，端口不会立刻释放。用循环等它真正空出来：

```bash
for i in $(seq 1 20); do
  lsof -nP -iTCP:8090 -sTCP:LISTEN >/dev/null 2>&1 && sleep 1 || { echo "free after ${i}s"; break; }
done
```

实测本次进程在 **1 秒**内释放端口。若不等就立刻重启，新进程会以
`{"error": "[Errno 48] Address already in use", "ok": false}` 退出——这是本次踩过的真实坑。

### 重启

先停（上一步，并确认端口已释放），再执行第三节的拉起命令：

```bash
./scripts/start_8090.sh
```

注意：**重启会丢弃写作风格设置**；
模型端点、微信/X/小红书凭据、模型 API Key 存在 SQLite 与 macOS keychain 中，重启后仍在。

---

## 六、换端口 / 常见问题

### 换成其它端口

只改 `--port` 一个参数，例如 9090：

```bash
PYTHONPATH=src python3 -m autoposting.cli web \
  --db data/autoposting.db --sources config/sources.example.json --port 9090
```

### 用 macOS 控制面板拉起（另一种方式）

项目自带 Tk 控制面板，同样支持指定端口：

```bash
python3 service_control.py --port 8090
```

或双击 `AutoPosting服务控制.command`。
**注意：同一端口同时只能有一个进程**，控制面板与命令行方式不要同时拉同一个端口。

> 📌 **环境提醒**：本机已另有一个 AutoPosting 实例常驻在 **8080**（非本教程拉起）。
> 8080 与 8090 互不影响，但排查时别把两个实例搞混：
> `lsof -nP -iTCP:8080,8090 -sTCP:LISTEN` 可同时看到两者。

### 故障对照表

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `No module named autoposting` | 漏了 `PYTHONPATH=src`，**或脚本 `cd` 到了 `scripts/` 等子目录** | 在项目根目录执行；脚本用 `cd "$(dirname "$0")/.."` |
| `Address already in use` | 端口被占用，或停旧进程后未等端口释放就重启 | 按端口查进程并结束，**等 `lsof` 无输出后再启** |
| `PORT，: unbound variable` | `$PORT` 后紧跟全角中文标点，被并入变量名 | 写成 `${PORT}，` |
| `host must be 127.0.0.1 or localhost` | 传了 `0.0.0.0` 等地址 | 改回 `127.0.0.1`；本项目设计上不支持对外暴露 |
| `/api/articles` 返回 `not_found` | 用 GET 调了 POST 接口 | 改用 POST 调用，非故障 |
| `BrokenPipeError` 刷屏 | 客户端提前断开（如刷新/取消请求） | 单个连接的告警，不影响服务，可忽略 |
| 服务跑一会儿就掉线 | 用了会话托管的后台作业，被回收时连带杀掉 | 改用 `nohup` 脱离会话（见第三节） |
| 浏览器打不开但 curl 200 | 用了错误的 host/端口 | 确认地址是 `127.0.0.1:8090` 而非 `localhost:8080` |

---

## 七、本次拉起结论

最终以 `nohup` 脱离会话方式在 8090 冷启动，并逐层验证：

| 检查项 | 结果 |
| --- | --- |
| 端口 8090 前置占用检查 | ✅ 已确认空闲 |
| 服务进程 | ✅ PID 34208（`nohup` 脱离会话，独立于启动它的终端/作业） |
| 8090 LISTEN | ✅ `TCP 127.0.0.1:8090 (LISTEN)` |
| `GET /` | ✅ HTTP 200，2.09 ms |
| 首页体积 / 标题 | ✅ 222,568 字节 / `<title>AutoPosting</title>` |
| 版本徽标 | ✅ 1.3.1 |
| `GET /api/hotspots` 业务数据 | ✅ `ok = True`，candidates = 10，sources = 31 |
| 启动日志 | ✅ `data/web-8090.log` 为空（0 行，无任何报错） |
| 跨 shell 存活 | ✅ 脚本退出后在全新 shell 中同一 PID 仍在服务 |

**服务已在 http://127.0.0.1:8090/ 常驻拉起并完成功能验证。**

### 过程中修正的两个真实问题（已并入本教程）

1. **会话托管后台作业会被回收** —— 端口随之掉线，改用 `nohup` 脱离会话后常驻。
2. **启动脚本两处 bug** —— `cd` 到 `scripts/` 导致找不到包；`$PORT` 后接全角逗号导致 `unbound variable`。两者均已修复，脚本以「停止 → 确认端口释放 → 冷启动」完整路径实测通过。
