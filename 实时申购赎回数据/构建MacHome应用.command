#!/bin/zsh
set -e

project_dir="/Users/ellis/Desktop/ETF交割/实时申购赎回数据"
python_bin="/Users/ellis/miniconda3/envs/ag/bin/python"

cd "${project_dir}"
"${python_bin}" -m PyInstaller --noconfirm --clean ETFMacHome.spec

# Desktop may be managed by a File Provider that attaches FinderInfo while
# signing. Sign in /private/tmp, then deliver a clean ZIP for extraction.
sign_dir="$(mktemp -d /private/tmp/etf-monitor-sign.XXXXXX)"
ditto --norsrc "${project_dir}/dist/ETF监控主机.app" "${sign_dir}/ETF监控主机.app"
xattr -cr "${sign_dir}/ETF监控主机.app"
codesign --force --deep --sign - "${sign_dir}/ETF监控主机.app"
codesign --verify --deep --strict "${sign_dir}/ETF监控主机.app"
ditto -c -k --keepParent --norsrc "${sign_dir}/ETF监控主机.app" \
  "${project_dir}/dist/ETF监控主机-macOS-arm64.zip"

echo "构建完成：${project_dir}/dist/ETF监控主机-macOS-arm64.zip"
open "${project_dir}/dist"
