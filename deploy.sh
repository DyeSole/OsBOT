#!/usr/bin/env bash
set -euo pipefail

# ── OsBOT 一键部署脚本 ──
# 用法: bash deploy.sh

REPO_URL="https://github.com/DyeSole/OsBOT.git"
INSTALL_DIR="$HOME/OsBOT"
VENV_DIR="$INSTALL_DIR/venv"
SERVICE_NAME="osbot"
PYTHON_MIN="3.11"

# ── 颜色 ──
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[✓]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
error() { echo -e "${RED}[✗]${NC} $*"; exit 1; }

# ── 1. 系统依赖 ──
info "安装系统依赖..."
if command -v apt-get &>/dev/null; then
    sudo apt-get update -qq
    sudo apt-get install -y -qq python3 python3-venv python3-pip git xvfb
elif command -v dnf &>/dev/null; then
    sudo dnf install -y python3 python3-pip git xorg-x11-server-Xvfb
elif command -v yum &>/dev/null; then
    sudo yum install -y python3 python3-pip git xorg-x11-server-Xvfb
else
    warn "未识别的包管理器，请确保已安装 python3、pip、git、xvfb"
fi

# ── 2. 检查 Python 版本 ──
PYTHON_CMD=""
for cmd in python3.12 python3.11 python3; do
    if command -v "$cmd" &>/dev/null; then
        PYTHON_CMD="$cmd"
        break
    fi
done
[ -z "$PYTHON_CMD" ] && error "未找到 Python3，请先安装"

PY_VER=$($PYTHON_CMD -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
info "Python 版本: $PY_VER ($PYTHON_CMD)"

# ── 3. 拉取/更新代码 ──
if [ -d "$INSTALL_DIR/.git" ]; then
    info "更新代码..."
    cd "$INSTALL_DIR"
    git pull origin main || git pull
else
    info "克隆仓库..."
    git clone "$REPO_URL" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

# ── 4. 创建虚拟环境 ──
if [ ! -d "$VENV_DIR" ]; then
    info "创建虚拟环境..."
    $PYTHON_CMD -m venv "$VENV_DIR"
fi
source "$VENV_DIR/bin/activate"
info "虚拟环境: $VENV_DIR"

# ── 5. 安装依赖 ──
info "安装 Python 依赖..."
pip install --upgrade pip -q
pip install -r requirements.txt -q
playwright install --with-deps chromium 2>/dev/null || warn "Playwright 浏览器安装失败（截图功能不可用，其他功能不受影响）"
info "依赖安装完成"

# ── 6. 创建数据目录 ──
mkdir -p "$INSTALL_DIR/data/chat_history"
mkdir -p "$INSTALL_DIR/data/memory"

# ── 7. 配置 .env ──
if [ ! -f "$INSTALL_DIR/.env" ]; then
    warn "未找到 .env 文件，正在创建模板..."
    cat > "$INSTALL_DIR/.env" << 'ENVEOF'
# ── 必填 ──
DISCORD_BOT_TOKEN=
API_KEY=
BASE_URL=

# ── 可选 ──
BOT_KEY=Haze
MODEL=claude-4.6-opus
APP_MODE=normal

# 打字等待与会话
SESSION_TIMEOUT_SECONDS=15.0
TYPING_DETECT_DELAY_SECONDS=1.0
RESET_TIMER_SECONDS=3.0
TYPING_WAIT=true

# 主动消息
PROACTIVE_IDLE_SECONDS=300.0
TYPING_NUDGE_SECONDS=60.0

# 消息分割
SPLIT_MODE=chat
CHAT_REPLY_DELAY_SECONDS=0.8

# 静默时间
QUIET_ENABLED=false
QUIET_START=23:00
QUIET_END=07:00

# 监视用户上线 (逗号分隔 Discord 用户 ID)
WATCH_USER_IDS=
WATCH_ONLINE_IDLE_SECONDS=600.0

# 调试
SHOW_ERROR_DETAIL=false
SHOW_API_PAYLOAD=false
SHOW_INTERACTION_LOGS=true
ENVEOF
    warn "请编辑 $INSTALL_DIR/.env 填写 DISCORD_BOT_TOKEN、API_KEY、BASE_URL"
    warn "完成后重新运行此脚本或手动启动: bash deploy.sh"
fi

# ── 8. 检查必填配置 ──
source_env() {
    while IFS='=' read -r key value; do
        key=$(echo "$key" | xargs)
        [[ -z "$key" || "$key" == \#* ]] && continue
        value=$(echo "$value" | xargs | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")
        export "$key=$value" 2>/dev/null || true
    done < "$INSTALL_DIR/.env"
}

source_env
MISSING=""
[ -z "${DISCORD_BOT_TOKEN:-}" ] && MISSING="$MISSING DISCORD_BOT_TOKEN"
[ -z "${API_KEY:-}" ] && MISSING="$MISSING API_KEY"
[ -z "${BASE_URL:-}" ] && MISSING="$MISSING BASE_URL"

if [ -n "$MISSING" ]; then
    warn "以下必填项为空:$MISSING"
    warn "请编辑 $INSTALL_DIR/.env 后重新运行"
    exit 0
fi

# ── 9. 设置 systemd 服务 ──
info "配置 systemd 服务..."

# 创建启动脚本（负责拉起 Xvfb 虚拟显示器）
sudo tee /usr/local/bin/osbot-start.sh > /dev/null << 'STARTEOF'
#!/usr/bin/env bash
pkill -f "Xvfb :99" 2>/dev/null || true
Xvfb :99 -screen 0 1280x800x24 -ac &
sleep 1
export DISPLAY=:99
exec "$@"
STARTEOF
sudo chmod +x /usr/local/bin/osbot-start.sh

sudo tee /etc/systemd/system/${SERVICE_NAME}.service > /dev/null << SVCEOF
[Unit]
Description=OsBOT Discord Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$INSTALL_DIR
ExecStart=/usr/local/bin/osbot-start.sh $VENV_DIR/bin/python bot.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SVCEOF

sudo systemctl daemon-reload
sudo systemctl enable ${SERVICE_NAME}

# ── 10. 启动/重启服务 ──
if systemctl is-active --quiet ${SERVICE_NAME}; then
    info "重启 OsBOT..."
    sudo systemctl restart ${SERVICE_NAME}
else
    info "启动 OsBOT..."
    sudo systemctl start ${SERVICE_NAME}
fi

sleep 2
if systemctl is-active --quiet ${SERVICE_NAME}; then
    info "OsBOT 已成功运行！"
else
    error "启动失败，查看日志: journalctl -u ${SERVICE_NAME} -n 50"
fi

echo ""
info "常用命令:"
echo "  查看日志:   journalctl -u ${SERVICE_NAME} -f"
echo "  重启:       sudo systemctl restart ${SERVICE_NAME}"
echo "  停止:       sudo systemctl stop ${SERVICE_NAME}"
echo "  编辑配置:   nano $INSTALL_DIR/.env"
