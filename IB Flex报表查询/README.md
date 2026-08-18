# IB Flex 报表查询

这是一个只依赖 Python 标准库的 IBKR Activity Flex 查询示例。它使用 IBKR Flex Web Service 的两步流程：

1. `SendRequest`：提交 Query ID 和日期范围。
2. `GetStatement`：使用返回的临时 ReferenceCode 获取 XML 报表。

脚本不会把 Token 打印到终端，也不会把 Token 写入代码或报表文件。

## 本地凭据文件与环境变量

本目录支持独立的本地凭据文件 `ib_flex.env`。脚本会自动读取它，因此即使原赎回收益计算器目录被移动，本查询工具仍可独立使用。

文件内容格式如下：

```bash
IBKR_FLEX_TOKEN='你的 Flex Token'
IBKR_FLEX_QUERY_ID='1574404'
```

`ib_flex.env` 已加入 `.gitignore`，并应保持为仅当前用户可读写。可以复制示例文件创建它：

```bash
cp ib_flex.env.example ib_flex.env
chmod 600 ib_flex.env
```

显式设置的环境变量优先级高于本地凭据文件。

脚本读取以下环境变量：

```bash
export IBKR_FLEX_TOKEN='你的 Flex Token'
export IBKR_FLEX_QUERY_ID='1574404'
```

本机 zsh 启动配置也会从本目录的 `ib_flex.env` 加载 `IBKR_FLEX_TOKEN`；新开一个终端或执行 `source ~/.zshrc` 后即可使用。不要把 Token 提交到 Git、同步盘或聊天记录中。

检查变量是否存在时，只检查名称，不要打印值：

```bash
test -n "$IBKR_FLEX_TOKEN" && echo 'IBKR_FLEX_TOKEN 已配置'
```

## 命令行查询

在本目录执行：

```bash
python3 ib_flex_query.py \
  --from 2026-08-14 \
  --to 2026-08-14 \
  --query-id "$IBKR_FLEX_QUERY_ID" \
  --output-dir reports
```

成功后会在 `reports/` 下保存类似下面的 XML：

```text
reports/ib_activity_20260814_20260814.xml
```

输出是一行 JSON 摘要，例如：

```json
{"from": "2026-08-14", "to": "2026-08-14", "trade_count": 19, "output": "reports/ib_activity_20260814_20260814.xml"}
```

## Python 调用方法

```python
from datetime import date

from ib_flex_query import extract_trade_rows, query_flex_statement

payload = query_flex_statement(
    date(2026, 8, 14),
    date(2026, 8, 14),
)
rows = extract_trade_rows(payload)

for row in rows:
    if row.get("symbol") == "XOP":
        print(
            row.get("tradeDate"),
            row.get("dateTime"),
            row.get("quantity"),
            row.get("tradePrice"),
        )
```

`query_flex_statement()` 默认从 `IBKR_FLEX_TOKEN` 和 `IBKR_FLEX_QUERY_ID` 读取凭据，也支持在调用时显式传入 `token=` 和 `query_id=`。建议生产代码使用环境变量，不要把凭据硬编码。

## 日期与重试

- 日期格式为 `YYYY-MM-DD`。
- 单次请求最多覆盖 365 天；更长区间应拆分成多个请求。
- 报表生成中的 IBKR 状态会自动重试，默认最多 12 次。
- Flex Activity 报表是只读查询，不会下单或修改账户。

## 常见错误

- `未配置 IBKR_FLEX_TOKEN`：当前 shell 没有加载环境变量，请新开终端或重新加载 zsh 配置。
- 本地凭据文件路径：`/Users/ellis/Desktop/ETF交割/IB Flex报表查询/ib_flex.env`。
- `IBKR Flex 返回错误 1019`：报表仍在生成，脚本会自动重试；持续失败时稍后重试。
- `IBKR Flex 网络请求失败`：检查网络、DNS 或 IBKR 服务可用性。
