#!/bin/zsh
set -e

cd "/Users/ellis/Desktop/ETF交割/实时申购赎回数据"
exec /Users/ellis/miniconda3/envs/ag/bin/python etf_remote_client.py
