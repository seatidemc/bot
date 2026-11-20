#!/usr/bin/env bash
# seatide-bot 启动脚本（Linux / macOS）
#
#   ./start.sh                    # 开发：前台运行，日志直接打印，Ctrl+C 停止
#   SEATIDE_ENV=prod ./start.sh   # 生产：screen 后台运行
#   ./start.sh restart            # 重启（生产 screen 进程）
#   ./start.sh stop               # 停止（生产 screen 进程）
#
# 工作目录固定为脚本所在目录（项目根目录），从任意路径调用均可。
set -euo pipefail

BOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$BOT_DIR"

ENV_NAME="$(printf '%s' "${SEATIDE_ENV:-dev}" | tr '[:upper:]' '[:lower:]')"
export SEATIDE_ENV="$ENV_NAME"

VENV_DIR="$BOT_DIR/.venv"
VENV_PY="$VENV_DIR/bin/python"
DEPS_HASH_FILE="$VENV_DIR/.deps_hash"
ENV_FILE="$BOT_DIR/.env.$ENV_NAME"
DATA_DIR="$BOT_DIR/data"
LOG_FILE="$DATA_DIR/bot.log"
PID_FILE="$DATA_DIR/bot.pid"
SCREEN_NAME="seatide-bot"
ACTION="${1:-start}"

say() { printf '%s\n' "$*"; }
warn() { printf '⚠️  %s\n' "$*" >&2; }
die() { printf '❌ %s\n' "$*" >&2; exit 1; }

case "$ENV_NAME" in
    dev | prod) ;;
    *) die "SEATIDE_ENV 只能是 dev 或 prod（当前: ${ENV_NAME}）" ;;
esac

[ $# -le 1 ] || die "只支持一个命令参数：start（默认）、stop、restart"

screen_running() {
    local out
    # 老版本 screen（如 macOS 自带的 4.00.03）即使有会话也返回 1，
    # 所以不能依赖它的退出码，只能解析输出
    out="$(screen -list 2>/dev/null || true)"
    [[ "$out" =~ \.${SCREEN_NAME}[[:space:]] ]]
}

pid_running() {
    local pid
    [ -f "$PID_FILE" ] || return 1
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

stop_screen() {
    local pid=""
    if [ -f "$PID_FILE" ]; then
        pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    fi

    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        say "🛑 正在停止 Bot（pid: ${pid}）..."
        kill "$pid" 2>/dev/null || true
        for _ in $(seq 1 40); do
            kill -0 "$pid" 2>/dev/null || break
            sleep 0.25
        done
        if kill -0 "$pid" 2>/dev/null; then
            warn "进程未响应，强制结束"
            kill -9 "$pid" 2>/dev/null || true
            sleep 1
        fi
    elif screen_running; then
        say "🛑 正在停止 screen: ${SCREEN_NAME} ..."
    fi

    # screen -X quit 在新旧版本 / macOS 上不一定会把信号传给窗口内进程，
    # 所以上面的 PID 终止是主要手段，这里只负责清理会话
    if screen_running; then
        screen -S "$SCREEN_NAME" -X quit 2>/dev/null || true
        for _ in $(seq 1 20); do
            screen_running || break
            sleep 0.25
        done
    fi
    rm -f "$PID_FILE"
}

ensure_venv() {
    if [ -x "$VENV_PY" ]; then
        return 0
    fi
    if [ -d "$VENV_DIR" ]; then
        die ".venv 已存在但不完整（缺少 bin/python），请删除后重试：rm -rf .venv"
    fi
    local python_bin
    python_bin="$(command -v python3 || command -v python || true)"
    [ -n "$python_bin" ] || die "未找到 python3，请先安装 Python 3.11+"
    say "🔧 创建虚拟环境 .venv ..."
    "$python_bin" -m venv "$VENV_DIR"
}

ensure_deps() {
    local want saved
    want="$("$VENV_PY" -c 'import hashlib, pathlib; print(hashlib.sha256(pathlib.Path("pyproject.toml").read_bytes()).hexdigest())')"
    saved="$(cat "$DEPS_HASH_FILE" 2>/dev/null || true)"
    if [ "$want" = "$saved" ]; then
        return 0
    fi
    say "📦 安装依赖 ..."
    "$VENV_PY" -m pip install -e . --quiet --disable-pip-version-check
    printf '%s\n' "$want" > "$DEPS_HASH_FILE"
}

start_foreground() {
    say "🚀 启动 Bot（环境: ${ENV_NAME}，前台运行，Ctrl+C 停止）"
    exec "$VENV_PY" -u bot.py
}

start_screen() {
    command -v screen >/dev/null 2>&1 || die "未找到 screen，无法后台运行；可改用 systemd 或前台启动"
    if screen_running || pid_running; then
        die "Bot 已在运行，查看：screen -r ${SCREEN_NAME}"
    fi
    say "🚀 启动 Bot（环境: ${ENV_NAME}，screen: ${SCREEN_NAME}）"
    : > "$LOG_FILE"
    rm -f "$PID_FILE"
    # 同时输出到 screen 和 data/bot.log；记录 python 的 PID，停止时可靠终止
    screen -dmS "$SCREEN_NAME" bash -c "
        cd '$BOT_DIR'
        '$VENV_PY' -u bot.py > >(tee -a '$LOG_FILE') 2>&1 &
        echo \$! > '$PID_FILE'
        wait
    "
    sleep 2
    if screen_running; then
        say "✅ Bot 已启动"
        say ""
        say "   查看日志：screen -r ${SCREEN_NAME}（Ctrl+A 然后 D 分离）"
        say "   日志文件：$LOG_FILE"
        say "   重启：    ./start.sh restart"
        say "   停止：    ./start.sh stop"
    else
        printf '❌ 启动失败，最近日志：\n' >&2
        tail -n 20 "$LOG_FILE" >&2 2>/dev/null || true
        exit 1
    fi
}

case "$ACTION" in
    start | restart)
        if [ "$ACTION" = "restart" ] && [ "$ENV_NAME" = "prod" ]; then
            stop_screen
        fi
        [ -f "$ENV_FILE" ] || warn "未找到 ${ENV_FILE}，将使用 NoneBot 默认配置"
        [ -f "$BOT_DIR/pyproject.toml" ] || die "未找到 pyproject.toml，请在项目根目录运行"
        ensure_venv
        ensure_deps
        mkdir -p "$DATA_DIR"
        if [ "$ENV_NAME" = "prod" ]; then
            start_screen
        else
            start_foreground
        fi
        ;;
    stop)
        if screen_running || pid_running; then
            stop_screen
            say "✅ 已停止"
        elif [ "$ENV_NAME" = "dev" ]; then
            say "ℹ️  dev 模式为前台运行，请在对应终端按 Ctrl+C 停止"
        else
            say "ℹ️  $SCREEN_NAME 未在运行"
        fi
        ;;
    *)
        die "未知命令: ${ACTION}（支持: start、stop、restart）"
        ;;
esac