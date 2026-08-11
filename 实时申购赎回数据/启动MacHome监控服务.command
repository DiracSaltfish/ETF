#!/bin/zsh
set -e

cd "/Users/ellis/Desktop/ETF交割/实时申购赎回数据"
mkdir -p logs
exec /Users/ellis/miniconda3/envs/ag/bin/python etf_monitor_server.py \
  --config config/etf_monitor_server.json
