#!/bin/zsh
set -e

label="com.etfdelivery.monitor-server"
target="/Users/ellis/Library/LaunchAgents/${label}.plist"

launchctl bootout "gui/$(id -u)/${label}" 2>/dev/null || true
rm -f "${target}"
echo "常驻服务已卸载。项目文件和历史日志未删除。"
