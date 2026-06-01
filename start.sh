#!/bin/bash
# seatide-bot 启动脚本（screen 管理）
set -e

SCREEN_NAME="seatide-bot"
BOT_DIR="$HOME/bot"

# ---- 处理命令 ----
case "${1:-}" in
    restart)
        echo "🔄 正在重启..."
        screen -S "$SCREEN_NAME" -X quit 2>/dev/null || true
        sleep 1
        ;;
    stop)
        echo "🛑 正在停止..."
        screen -S "$SCREEN_NAME" -X quit 2>/dev/null || true
        echo "✅ 已停止"
        exit 0
        ;;
esac

cd "$BOT_DIR"

# ---- 检查是否已运行 ----
if screen -list 2>/dev/null | grep -q "\.${SCREEN_NAME}\b"; then
    echo "⚠️  Bot 已在运行中 (screen: $SCREEN_NAME)"
    echo "   查看日志: screen -r $SCREEN_NAME"
    echo "   强制重启: $0 restart"
    exit 1
fi

# ---- 首次运行检查 ----
if [ ! -d ".venv" ]; then
    echo "🔧 创建虚拟环境..."
    python3 -m venv .venv
fi

source .venv/bin/activate

if [ ! -f ".venv/.deps_ok" ]; then
    echo "📦 安装依赖..."
    pip install -e . -q
    touch .venv/.deps_ok
fi

mkdir -p data

# ---- 启动 ----
echo "🚀 启动 Bot (screen: $SCREEN_NAME)..."
screen -dmS "$SCREEN_NAME" bash -c "
    cd '$BOT_DIR'
    source .venv/bin/activate
    echo '=== Bot started at \$(date) ==='
    exec python bot.py
"

sleep 1

if screen -list 2>/dev/null | grep -q "\.${SCREEN_NAME}\b"; then
    echo "✅ Bot 已启动"
    echo ""
    echo "   查看日志:       screen -r $SCREEN_NAME"
    echo "   分离会话:       Ctrl+A 然后按 D"
    echo "   停止:           screen -S $SCREEN_NAME -X quit"
    echo "   重启:           $0 restart"
else
    echo "❌ 启动失败，请检查日志"
    exit 1
fi
