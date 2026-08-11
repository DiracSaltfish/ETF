# 深圳 PCF 集成交接

## 已完成

- 复用 `赎回收益计算器V3/szse_pcf.py`，交付目录内保留自包含副本 `szse_pcf.py`，方便 PyInstaller 和 Windows 源码交付。
- 当前业务层固定使用 `EXCHANGE_SZSE`，仅接受 6 位深圳代码；没有上海 PCF 队列或上海请求入口。
- 主机每天在配置时间窗内为观察列表自动补齐 PCF，默认 `08:30-23:00`，且对旧配置强制最早 `08:30`、失败间隔 900 秒、每标的每天最多 8 轮自动尝试；正常路径只抓结构化 XML。
- PCF 按 `pcf_cache/YYYY-MM-DD/xml/<code>.xml` 缓存。底层拉取器负责跨线程锁、8 秒请求间隔、请求状态落盘和限流冷却。
- 快照中每个标的新增 `pcf` 与 `opportunity`；HTTP 支持 PCF 列表、单标的详情和手动刷新。
- Mac/Windows PyQt 客户端和 Web 页面均支持双击标的查看 PCF 摘要与成分证券。

## 机会判断口径

只使用份额，不使用笔数：

```text
盘中申购增量 = 当前申购份额 - 上次申购份额
盘中赎回增量 = 当前赎回份额 - 上次赎回份额
可能释放的申购容量 = 盘中赎回增量 - 盘中申购增量
```

- 第一次数据只建立基准，不标记机会。
- 可能释放的申购容量达到一个完整篮子，且 PCF 当日开放申购：`盘中申购机会`。
- 上式为负且绝对值达到一个完整篮子，且 PCF 当日开放赎回：`盘中赎回机会`。
- 不足一个篮子、PCF 非当日、PCF 缺失或对应方向关闭：只显示信号/待确认，不设为可操作。
- 累计份额回落按跨日重置或数据修正处理，不产生机会。

这是盘中容量释放信号，不是收益判断；未纳入价格、溢价、成本和成交能力。开盘即存在的静态额度通常不具备用户所述的溢价条件，因此不会作为机会推送。

## 关键文件

- `etf_pcf_service.py`：序列化、缓存回退、机会判断。
- `etf_monitor_server.py`：每日任务、WebSocket 快照、PCF API。
- `etf_remote_client.py`：双击详情窗口。
- `web/monitor.html`：浏览器详情弹层。
- `test_etf_pcf_service.py`：日期缓存、完整/不完整篮子、双方向测试。

## API

```text
GET  /api/v1/pcf
GET  /api/v1/pcf/159518
POST /api/v1/pcf/refresh
```

单标的详情响应包含 `summary_fields`、`component_columns`、`components` 和 `opportunity`。快照只推送 PCF 摘要，不会把完整成分表塞进每次 WebSocket 更新。
