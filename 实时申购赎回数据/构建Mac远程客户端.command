#!/bin/zsh
set -e

project_dir="/Users/ellis/Desktop/ETF交割/实时申购赎回数据"
python_bin="/Users/ellis/miniconda3/envs/ag/bin/python"

cd "${project_dir}"
"${python_bin}" -m PyInstaller --noconfirm --clean ETFMacClient.spec

sign_dir="$(mktemp -d /private/tmp/etf-client-sign.XXXXXX)"
ditto --norsrc "${project_dir}/dist/ETF远程监控.app" \
  "${sign_dir}/ETF远程监控.app"
xattr -cr "${sign_dir}/ETF远程监控.app"
codesign --force --deep --sign - "${sign_dir}/ETF远程监控.app"
codesign --verify --deep --strict "${sign_dir}/ETF远程监控.app"
ditto -c -k --keepParent --norsrc "${sign_dir}/ETF远程监控.app" \
  "${project_dir}/dist/ETF远程监控-macOS-arm64.zip"

echo "构建完成：${project_dir}/dist/ETF远程监控-macOS-arm64.zip"
