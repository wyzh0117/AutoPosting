#!/bin/bash
# 在 8090 端口拉起 AutoPosting Web 服务（幂等：已拉起则直接复用，不重复启动）
set -u
# 切到项目根目录（脚本位于 scripts/ 下，必须上跳一级，否则 PYTHONPATH=src 与 data/ 会解析到 scripts/ 下）
cd "$(dirname "$0")/.."

PORT=8090
HOST=127.0.0.1
LOG=data/web-${PORT}.log

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "[skip] 端口 $PORT 已在监听，复用现有进程："
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN | tail -n +2
  exit 0
fi

mkdir -p data
nohup env PYTHONPATH=src python3 -m autoposting.cli web \
  --db data/autoposting.db \
  --sources config/sources.example.json \
  --host "$HOST" \
  --port "$PORT" \
  > "$LOG" 2>&1 < /dev/null &

for _ in $(seq 1 20); do
  sleep 0.5
  if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "[ok] AutoPosting 已在 http://${HOST}:${PORT}/ 拉起"
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN | tail -n +2
    exit 0
  fi
done

echo "[fail] 5 秒内未监听 ${PORT} ，最后日志："
cat "$LOG"
exit 1
