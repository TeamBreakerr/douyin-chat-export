#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$DIR/.server.pid"

if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "服务已在运行 (PID $(cat "$PID_FILE"))，请先运行 stop.sh"
  exit 1
fi

# 构建前端（如果 node_modules 存在）
if [ -d "$DIR/frontend/node_modules" ]; then
  echo "构建前端..."
  cd "$DIR/frontend" && npm run build 2>&1 | tail -3
  cd "$DIR"
else
  echo "提示: 前端未安装依赖，运行 cd frontend && npm install && npm run build"
  if [ ! -d "$DIR/frontend/dist" ]; then
    echo "错误: frontend/dist 不存在，无法启动"
    exit 1
  fi
fi

# 启动后端（同时 serve 前端 dist）
nohup "$DIR/venv/bin/python3" -m uvicorn backend.main:app \
  --host 127.0.0.1 --port 8000 \
  > "$DIR/.server.log" 2>&1 &

echo $! > "$PID_FILE"
sleep 1

if kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "✓ 服务已启动 (PID $(cat "$PID_FILE"))"
  echo "  浏览器访问: http://127.0.0.1:8000"
  open "http://127.0.0.1:8000"
else
  echo "✗ 启动失败，查看日志: $DIR/.server.log"
  rm -f "$PID_FILE"
  exit 1
fi
