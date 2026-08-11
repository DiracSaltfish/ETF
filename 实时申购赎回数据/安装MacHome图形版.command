#!/bin/zsh
set -e

project_dir="/Users/ellis/Desktop/ETF交割/实时申购赎回数据"
source_archive="${project_dir}/dist/ETF监控主机-macOS-arm64.zip"
target_app="/Users/ellis/Applications/ETF监控主机.app"
new_label="com.etfdelivery.mac-home"
old_label="com.etfdelivery.monitor-server"
launch_agents="/Users/ellis/Library/LaunchAgents"

if [[ ! -f "${source_archive}" ]]; then
  echo "未找到已构建应用，请先双击：构建MacHome应用.command"
  exit 1
fi

mkdir -p /Users/ellis/Applications "${launch_agents}"
launchctl bootout "gui/$(id -u)/${old_label}" 2>/dev/null || true
launchctl bootout "gui/$(id -u)/${new_label}" 2>/dev/null || true
rm -f "${launch_agents}/${old_label}.plist"
install_dir="$(mktemp -d /private/tmp/etf-monitor-install.XXXXXX)"
ditto -x -k "${source_archive}" "${install_dir}"
ditto --norsrc "${install_dir}/ETF监控主机.app" "${target_app}"
xattr -cr "${target_app}"
cp "${project_dir}/${new_label}.plist" "${launch_agents}/${new_label}.plist"
launchctl bootstrap "gui/$(id -u)" "${launch_agents}/${new_label}.plist"
launchctl enable "gui/$(id -u)/${new_label}"
launchctl kickstart -k "gui/$(id -u)/${new_label}"

echo "图形版监控主机已安装并启动。"
echo "应用：${target_app}"
echo "本机网页：http://127.0.0.1:6787/"
echo "关闭主窗口只会收起；请从菜单栏图标选择彻底退出。"
