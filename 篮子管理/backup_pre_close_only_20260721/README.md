# 篮子管理

一个面向 `IBKR TWS API` 的 PyQt 桌面工具，解决三件事：

1. 连接并探测 `TWS / IB Gateway`
2. 导入篮子文件并检查当前 IB 持仓是否满足目标篮子
3. 对导入篮子中的 `SELL` 行做保护式一键卖出

## 结构

- `main.py`
  - 启动入口
- `ui.py`
  - PyQt 主界面、后台任务调度、日志、交互保护
- `config_store.py`
  - 本地配置读写
- `basket_loader.py`
  - CSV / Excel 篮子导入与列识别
- `basket_models.py`
  - 篮子、持仓、核对结果、订单结果的数据结构
- `reconcile.py`
  - 目标持仓匹配 + 可卖库存校验
- `ib_service.py`
  - IBKR 连接、持仓读取、卖单提交
- `style.py`
  - UI 主题样式

## 导入格式

优先支持：

1. `Orders` sheet，包含 `action / ticker / quantity`
2. 任意 sheet，包含 `action / ticker / quantity`
3. 任意 sheet，包含 `ticker / target_short_shares`
4. 任意 sheet，包含 `ticker / quantity`，默认按 `SELL` 解释

支持常见别名：

- `ticker / symbol / 证券代码 / 股票代码 / 代码`
- `action / side / 方向 / 买卖方向 / 操作`
- `quantity / qty / shares / 数量 / 股数 / 目标股数`

## 设计口径

界面里故意把两个概念拆开：

- `目标匹配`
  - 当前净仓位是否已等于导入篮子的目标方向和数量
- `可卖校验`
  - 对于 `SELL` 行，当前是否存在足够多头库存可直接卖出

这两个口径不能混为一谈。比如你已经有 `-95` 股空头仓位，那么它可能满足目标篮子，但并不代表你还能“卖出 95 股库存”。

## 一键卖出保护规则

- 只会对 `SELL` 行发卖单
- `BUY` 行只参与持仓匹配，不参与一键卖出
- 下单前必须：
  - 成功连接 TWS
  - 选定账户
  - 刷新持仓
  - 所有 SELL 行都有足够多头库存
- 发单前有二次确认弹窗

## 运行

```bash
cd /Users/ellis/Desktop/ETF交割/篮子管理
conda run -n ag python main.py
```

或者直接双击：

- [启动篮子管理.command](/Users/ellis/Desktop/ETF交割/篮子管理/启动篮子管理.command)

## 已知限制

- 当前版本的一键卖出只支持美股 `STK/ETF` 常见股票合约
- 限价单价格使用 `bid` 或回退到 `last/close` 生成的 marketable limit
- 当前没有做 IBKR `what-if order` 的真实保证金预估
- 当前没有“按目标仓位自动补差下单”，只做核对和 SELL 篮子执行
