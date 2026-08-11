#!/bin/zsh
set -e

label="com.etfdelivery.mac-home"
target="/Users/ellis/Library/LaunchAgents/${label}.plist"

launchctl bootout "gui/$(id -u)/${label}" 2>/dev/null || true
rm -f "${target}"
echo "已移除登录自启动；应用和监控配置仍保留。"
