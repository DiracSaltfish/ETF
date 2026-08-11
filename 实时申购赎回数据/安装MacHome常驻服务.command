#!/bin/zsh
set -e

project_dir="/Users/ellis/Desktop/ETF交割/实时申购赎回数据"
label="com.etfdelivery.monitor-server"
target="/Users/ellis/Library/LaunchAgents/${label}.plist"

mkdir -p "${project_dir}/logs" /Users/ellis/Library/LaunchAgents
cp "${project_dir}/${label}.plist" "${target}"
launchctl bootout "gui/$(id -u)/${label}" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "${target}"
launchctl enable "gui/$(id -u)/${label}"
launchctl kickstart -k "gui/$(id -u)/${label}"

echo "常驻服务已安装并启动。"
echo "状态接口：http://127.0.0.1:6787/api/v1/health"
echo "日志目录：${project_dir}/logs"
