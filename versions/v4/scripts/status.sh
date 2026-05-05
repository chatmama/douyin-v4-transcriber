#!/bin/bash
# v4 — 状态查看脚本

V4_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DB="$V4_DIR/database/pipeline.db"

echo "========== v4 管道状态 =========="
echo ""

# 进程状态
echo "--- 进程 ---"
running=0
for mod in scanner url-fetch download transcribe publish monitor cleaner; do
    pid=$(pgrep -f "python3.*modules/$mod" | grep -v grep | head -1)
    if [ -n "$pid" ]; then
        mem=$(ps -o rss --no-headers -p $pid 2>/dev/null | xargs)
        echo "  ✅ $mod  (PID $pid, ${mem}KB)"
        running=$((running + 1))
    else
        echo "  ❌ $mod  (未运行)"
    fi
done
echo "  运行中: $running/7"
echo ""

# 数据库统计
if [ -f "$DB" ]; then
    echo "--- 数据库状态 ---"
    sqlite3 "$DB" "SELECT status, COUNT(*) as cnt FROM videos GROUP BY status ORDER BY cnt DESC;" 2>/dev/null | while IFS='|' read status cnt; do
        printf "  %-15s %d篇\n" "$status" "$cnt"
    done
    echo ""
    echo "--- 最新日志 ---"
    sqlite3 "$DB" "SELECT substr(created_at,1,19), module, level, substr(message,1,60) FROM pipeline_log ORDER BY id DESC LIMIT 8;" 2>/dev/null | while IFS='|' read ts mod lvl msg; do
        echo "  [$ts] [$mod/$lvl] $msg"
    done
else
    echo "  ❌ 数据库文件不存在: $DB"
fi
echo ""

# 磁盘
echo "--- 磁盘 ---"
du -sh "$V4_DIR/tmp_cache" 2>/dev/null || echo "  tmp_cache: 无"
du -sh "$V4_DIR/output" 2>/dev/null || echo "  output: 无"
echo ""

# GPU
echo "--- GPU ---"
nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total --format=csv,noheader 2>/dev/null || echo "  N/A"
echo ""

# Chrome
echo "--- Chrome ---"
curl -s http://127.0.0.1:9222/json/version | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'  ✅ {d[\"Browser\"]}')" 2>/dev/null || echo "  ❌ Chrome 不可用"
