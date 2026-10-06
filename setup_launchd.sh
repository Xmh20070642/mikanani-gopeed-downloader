#!/bin/bash
# 生成并加载 mikan-gopeed 的两个 launchd 服务：
#   1) 下载巡检（每 10 分钟一轮，登录自启）
#   2) 本地网页面板（常驻，http://127.0.0.1:8787，仅本机可访问）
# 用法：在项目根目录执行 ./setup_launchd.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="${MIKAN_GOPED_LABEL:-com.mikan-gopeed}"
WEBUI_LABEL="${LABEL}-webui"
PLIST_DIR="$HOME/Library/LaunchAgents"
MAIN_PLIST="$PLIST_DIR/$LABEL.plist"
WEBUI_PLIST="$PLIST_DIR/$WEBUI_LABEL.plist"
LOG="$HOME/Library/Logs/mikan-gopeed.log"
PYTHON="$(command -v python3 || echo /usr/bin/python3)"

if [ ! -f "$SCRIPT_DIR/config.json" ]; then
    echo "错误：找不到 $SCRIPT_DIR/config.json"
    echo "请编辑 config.json，填入你的 RSS 订阅链接与番剧目录。"
    exit 1
fi

mkdir -p "$PLIST_DIR" "$HOME/Library/Logs"

cat > "$MAIN_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$SCRIPT_DIR/mikan_gopeed.py</string>
        <string>--once</string>
        <string>--verbose</string>
    </array>
    <key>StartInterval</key>
    <integer>600</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$LOG</string>
    <key>StandardErrorPath</key>
    <string>$LOG</string>
    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
EOF

cat > "$WEBUI_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$WEBUI_LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$SCRIPT_DIR/webui.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$MAIN_PLIST"
launchctl bootout "gui/$(id -u)/$WEBUI_LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$WEBUI_PLIST"

echo "已安装并启动："
echo "  下载巡检  : $MAIN_PLIST（日志 $LOG）"
echo "  网页面板  : $WEBUI_PLIST（http://127.0.0.1:8787）"
echo "卸载：launchctl bootout gui/\$(id -u)/$LABEL gui/\$(id -u)/$WEBUI_LABEL && rm $MAIN_PLIST $WEBUI_PLIST"
