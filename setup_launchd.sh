#!/bin/bash
# 生成并加载 mikan-gopeed 的 launchd 定时服务（每 10 分钟一轮，登录自启）。
# 用法：在项目根目录执行 ./setup_launchd.sh
# 如需自定义服务标签：MIKAN_GOPED_LABEL=my.label ./setup_launchd.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="${MIKAN_GOPED_LABEL:-com.mikan-gopeed}"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST="$PLIST_DIR/$LABEL.plist"
LOG="$HOME/Library/Logs/mikan-gopeed.log"
PYTHON="$(command -v python3 || echo /usr/bin/python3)"

if [ ! -f "$SCRIPT_DIR/config.json" ]; then
    echo "错误：找不到 $SCRIPT_DIR/config.json"
    echo "请先：cp config.example.json config.json 并填好你的 RSS 订阅与番剧目录。"
    exit 1
fi

mkdir -p "$PLIST_DIR" "$HOME/Library/Logs"

cat > "$PLIST" <<EOF
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

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "已安装并启动：$PLIST"
echo "日志：$LOG"
echo "卸载：launchctl bootout gui/\$(id -u)/$LABEL && rm $PLIST"
