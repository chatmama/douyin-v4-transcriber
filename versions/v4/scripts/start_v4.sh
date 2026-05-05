#!/bin/bash
# v4 — 一次性启动脚本（不再依赖 tmux 窗口级别的隔离）
# 每个模块作为后台进程启动，由 start 函数管理

V4_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SESSION="douyin-v4"
LOGS="$V4_DIR/logs"

mkdir -p "$LOGS" "$V4_DIR/database" "$V4_DIR/tmp_cache"

# 预初始化 DB 防止竞态
echo "初始化数据库..."
python3 "$V4_DIR/lib/db.py" 2>/dev/null || true

# 检查是否已有 tmux session
tmux has-session -t "$SESSION" 2>/dev/null && {
    echo "⚠️  session $SESSION 已存在"
    echo "   先: tmux kill-session -t $SESSION"
    exit 1
}

# 创建 tmux 主窗口
tmux new-session -d -s "$SESSION" -n status "watch -n 5 'ps aux | grep python3 | grep -v grep | grep modules'"

# 启动所有模块
cd "$V4_DIR"

start_mod() {
    local name="$1"
    local module="$2"
    local delay="${3:-0}"
    sleep "$delay"
    while true; do
        echo "[$(date +%H:%M:%S)] 启动 $name..."
        python3 "modules/$module.py" >> "$LOGS/${name}.log" 2>&1
        echo "[$(date +%H:%M:%S)] ⚠️  $name 退出 (code=$?), 5秒后重启" >> "$LOGS/${name}.log"
        sleep 5
    done
}

echo "启动模块..."

# scanner
start_mod "scanner" "scanner.py" 0 &
echo "  scanner ✅"

# url_fetcher (延时2秒)
start_mod "url_fetcher" "url_fetcher.py" 2 &
echo "  url-fetch ✅"

# downloader (延时3秒)
start_mod "downloader" "downloader.py" 3 &
echo "  download ✅"

# transcriber (延时5秒)
start_mod "transcriber" "transcriber.py" 5 &
echo "  transcribe ✅"

# publisher (延时6秒)
start_mod "publisher" "publisher.py" 6 &
echo "  publish ✅"

# monitor (延时7秒)
start_mod "monitor" "monitor.py" 7 &
echo "  monitor ✅"

# cleaner (延时8秒)
start_mod "cleaner" "cleaner.py" 8 &
echo "  cleaner ✅"

echo ""
echo "✅ 所有模块已启动"
echo "   查看日志: tail -f $LOGS/*.log"
echo "   查看状态: tmux attach -t $SESSION"
echo "   停止: killall python3 或 pkill -f 'modules/'"

# 保持脚本运行
wait