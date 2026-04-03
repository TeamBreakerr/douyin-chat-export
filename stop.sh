#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$DIR/.server.pid"

if [ ! -f "$PID_FILE" ]; then
  echo "未找到运行中的服务"
  exit 0
fi

PID=$(cat "$PID_FILE")
if kill -0 "$PID" 2>/dev/null; then
  kill "$PID"
  rm -f "$PID_FILE"
  echo "✓ 服务已停止 (PID $PID)"
else
  echo "进程 $PID 已不存在，清理 PID 文件"
  rm -f "$PID_FILE"
fi
