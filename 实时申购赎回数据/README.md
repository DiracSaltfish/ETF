# ETF 申购赎回变化监控

## 当前推荐版本

当前正式入口是 `ETF监控主机`：一个适用于 Mac-home 的图形程序，主界面展示多标的实时数据，同时内嵌监听 `0.0.0.0:6787` 的 HTTP/WebSocket 服务。桌面远程客户端和浏览器页面都连接同一服务，因此只会建立一组 Wind 订阅。

Mac arm64 交付包：

```text
dist/ETF监控主机-macOS-arm64.zip
```

解压后可直接双击 `ETF监控主机.app`。若需要登录后自动运行并在交易日 `09:10` 自动开始订阅、`15:00` 自动停止，双击 `安装MacHome图形版.command`。安装脚本会把程序放到 `~/Applications`，登录后自动显示主界面，同时启动内网服务。关闭主窗口只会收起到菜单栏，菜单栏图标中可重新显示或彻底退出。

图形版功能：

- 本机维护多个 6 位深圳代码，内部自动补 `.SZ`。
- 申购/赎回笔数只作参考；变化提醒的唯一触发依据是申购份额与赎回份额。`轧差份额 = 申购份额 - 赎回份额` 仅用于展示和机会判断；Wind 中金额字段即使为 0 或变化也不参与提醒。
- 变化时本机弹窗并响铃，同时向所有 WebSocket 客户端推送。
- “设置”菜单提供 5 种内置原创短提示音，可立即试听；也可选择本机 WAV、MP3、M4A、AAC、OGG 或 FLAC 文件。可设置重复 1～10 次，“轻亮三音”默认重复 3 次，共 9 响；音量、弹窗自动关闭时间和连续提醒冷却时间均可调整。
- 远程桌面客户端默认不主动连接，红色“未连接”按钮点击后才会连接；成功连接会自动变绿，点击绿色按钮可断开。服务器 IP/主机名、端口、自动连接、断线重连间隔和心跳间隔均可在设置中保存。Mac 主机固定连接自身服务，避免误连到另一台主机。
- 初始全量数据会自动作为变化基准。后续任一标的收到申赎变化后整行会持续高亮，直至点击“重置变化基准”；同时右下角显示可手动关闭、默认持续 60 秒的全局变化横幅。
- Wind 重启后，交易时段内自动检测并重建订阅。
- 每个工作日 `08:30` 后为观察列表逐个拉取深圳 PCF；程序对旧配置也强制最早 `08:30`，避免在交易所发布当日 PCF 前请求。成功后按日期落盘，当天再次启动或查看详情不会重复请求。失败标的每 15 分钟重试、单标的每天最多 8 轮自动尝试；只抓结构化 XML，不额外抓 TXT。底层请求最少间隔 8 秒，并继承 V3 拉取器的 403/429/503 冷却保护。
- 开盘/首次读取只建立累计份额基准，不直接标记机会。盘中赎回份额相对申购份额多增一个完整篮子时，提示“盘中申购机会”；盘中申购份额反向多增时，提示“盘中赎回机会”。PCF 只用于核验最小单位、当日有效性和方向是否开放。
- 首页不展示 PCF 日期和最小单位，更新时间统一为本地 `HH:MM:SS`；双击标的仍可查看完整 PCF。
- Mac/Windows 客户端和内网页面在本地以 `轧差份额 = 申购份额 - 赎回份额` 对比 PCF 的 `NetCreationLimit`：达到净申购上限显示红色“已满”，赎回释放额度后自动转为绿色“未满”；PCF 未就绪时不猜测，显示“待确认”。
- 双击桌面客户端或网页中的标的，可查看主机已缓存的 PCF 摘要和成分证券明细；手动刷新 PCF 只保留在 Mac-home 主机 UI。
- 主机观察列表和服务配置持久化到 `~/Library/Application Support/ETFDelivery/config/etf_monitor_server.json`。
- PCF 日期缓存位于 `~/Library/Application Support/ETFDelivery/pcf_cache/YYYY-MM-DD/xml/`。当前程序只会请求深圳 PCF，不建立上海标的队列。
- 技术日志不出现在主页，可从“服务日志”打开，文件位于 `~/Library/Application Support/ETFDelivery/logs/server.log`，自动轮换。

## 内网访问

浏览器直接打开：

```text
http://<Mac-home 内网 IP>:6787/
```

网页是只读客户端：连接后先收到全量快照，之后通过 WebSocket 接收变化、状态和心跳；“拉取当前全量”不会改变 Wind 订阅。观察列表、开始/停止监控和 PCF 刷新只允许 Mac-home 本机执行。浏览器首次需要点击“启用声音”；后台系统通知需要点击“允许系统通知”。

主要接口：

```text
GET  /api/v1/health
GET  /api/v1/snapshot
GET  /api/v1/watchlist
PUT  /api/v1/watchlist
POST /api/v1/monitor/start
POST /api/v1/monitor/stop
GET  /api/v1/pcf
GET  /api/v1/pcf/<6位深圳代码>
POST /api/v1/pcf/refresh
WS   /ws/v1/changes
```

按需求，内网接口不使用令牌或实名验证。不要把 `6787` 映射到公网。

## Windows 客户端

Windows 源码交付包：

```text
dist/ETF远程监控-Windows源码.zip
```

解压后可双击 `启动Windows客户端.bat` 运行源码，或双击 `打包Windows客户端.bat` 在 Windows 本机生成 `dist\\ETF远程监控\\ETF远程监控.exe`。客户端会记住服务器 IP、端口和连接策略；弹窗与可配置声音都在 Windows 本机触发。详见 `README_WINDOWS.md`。

## 源码入口

- `etf_mac_home_app.py`：Mac-home 图形主机和内嵌服务生命周期。
- `etf_monitor_server.py`：订阅、轮询、定时计划、HTTP 和 WebSocket。
- `etf_pcf_service.py`：深圳 PCF 每日队列、日期缓存适配和机会判断。
- `szse_pcf.py`：从 `赎回收益计算器V3` 复用的 PCF 拉取/解析/流控实现。
- `etf_remote_client.py`：Mac/Windows 通用 PyQt6 客户端。
- `web/monitor.html`：同源内网页面。
- `wind_etf_realtime_ui.py`：Wind 注入与批量订阅控制器。
- `wind_tbapi_frame_parser.py`：JavaTableFrame 解码器。

完整回归测试：

```bash
cd "/Users/ellis/Desktop/ETF交割/实时申购赎回数据"
QT_QPA_PLATFORM=offscreen /Users/ellis/miniconda3/envs/ag/bin/python -m unittest -v
```

“盘中申购机会/盘中赎回机会”只表示盘中新发生的反向申赎可能释放了相应通道容量，并经 PCF 最小单位与开放状态校验；它不包含溢价、价差、交易成本、成交能力或最终收益计算。开盘即存在的静态额度不会被标记为机会。

> Wind TBAPI2 路径已经在 2026-08-11 完成收盘后快照回调与数据帧解析验证。最新结果、注入陷阱和后续任务见 [`TBAPI2_HANDOFF.md`](TBAPI2_HANDOFF.md)，可运行解码器为 [`wind_tbapi_frame_parser.py`](wind_tbapi_frame_parser.py)。本文件以下内容保留 QMT 备选路径。

## PyQt6 桌面界面

`wind_etf_realtime_ui.py` 提供浅色桌面界面，可手动输入标的、设置刷新频率、开始/停止 TBAPI2 订阅、显示数据时效和运行日志。由于当前业务只使用深圳标的，界面只需输入 6 位代码，例如 `159518`；程序内部会自动转换成 `159518.SZ`。

主页使用中性标题，不展示底层接口、探针路径和运行日志。日志保存在 `logs/etf_realtime_ui.log`，也可以通过界面的“查看日志”按钮在独立窗口中查看。

从终端启动：

```bash
cd "/Users/ellis/Desktop/ETF交割/实时申购赎回数据"
/Users/ellis/miniconda3/envs/ag/bin/python wind_etf_realtime_ui.py
```

也可以在 Finder 中双击 `启动WindETF实时申赎.command`。首次点击“开始订阅”时，macOS 可能要求允许 Terminal 使用开发者工具；Wind 必须已启动并登录。

界面后端由 `wind_tbapi_runtime_probe.c` 和 `libwind_tbapi_runtime_probe.dylib` 提供。代码不是编译时写死的，而是由 UI 在订阅时传给探针。每个标的的稳定快照文件为：

```text
~/Library/Containers/com.windin.mac.free/Data/tmp/
wind_tbapi_live_<证券代码>.json
```

例如 `159518.SZ` 对应 `wind_tbapi_live_159518_SZ.json`。

### 多标的变化监控

当前启动器默认打开 `wind_etf_multi_monitor_ui.py`。可以维护多个 6 位深圳代码，并通过一次“开始全部监控”批量建立订阅。程序第一次读取每个标的时只记录基线；此后以下原始份额字段发生变化才会触发提醒：

- 申购份额
- 赎回份额

申购和赎回金额已从订阅 SQL 与主页中移除。笔数仍作为参考展示，但笔数单独变化不会触发提醒，因为一笔可能包含多个篮子。轧差份额正数表示净申购，负数表示净赎回，例如 `3,000,000 - 2,000,000 = +1,000,000`；它随份额变化自动刷新，但不会独立触发提醒。

同一轮多个字段或多个标的变化会合并成一次置顶弹窗，并只播放一次系统提示音。主页的“最近变化”列保留具体的旧值与新值。弹窗和声音可以分别关闭。

观察列表、刷新频率及提醒开关会自动保存。修改观察列表前需要先停止全部监控。

## 结论

QMT/迅投的数据接口中**有**深圳 ETF 实时申购赎回统计，周期名为 `etfstatistics`，包含：

- 申购笔数、申购数量、申购金额
- 赎回笔数、赎回数量、赎回金额

但是，这不是“买了普通 QMT L2 就必然拥有”的标准 L2 字段。迅投官方页面将它标为“迅投研专属”，并明确写明需配合**尊享投研端**获取。普通 L2、ETF 申赎清单、IOPV、ETF 交易委托和这里的实时申赎统计是不同的数据/功能。

## 内置 Python 测试

将 `QMT内置Python_深圳ETF实时申赎.py` 的内容复制到 QMT 的模型研究/策略编辑器，在实盘行情环境运行。

1. 脚本当前只订阅并检测 `159518.SZ`。
2. 在深圳交易时段运行策略；该数据不能靠普通历史回测验证。
3. 控制台出现 `[订阅成功] ... sub_id=正整数` 只代表订阅请求已建立。
4. 出现 `[ETF申赎] ...` 才代表真实收到了 `etfstatistics` 推送。
5. 如果订阅号为正，但交易时段持续没有任何推送，优先向迅投确认账号是否开通“尊享投研端 + 基金实时申赎数据”权限。
6. 如果直接提示周期不支持或参数错误，说明当前客户端的内置 Python 包装层/版本未暴露该特色周期；官方明确给出的原生 XtQuant 路径仍是 `xtdata.subscribe_quote(stock, period='etfstatistics', callback=...)`。

### 普通 Level-2 原始字段探测

`QMT内置Python_159518_L2原始字段探测.py` 不请求 `etfstatistics`，而是直接订阅 `159518.SZ` 的 `l2quote` 和 `l2quoteaux`。首次行情回调会完整打印 QMT 实际返回的所有字段，并自动查找 `xw/xx`、`etfBuy*`、`creation*`、`redemption*`、申购、赎回等可能的字段名。

如果两个 L2 周期订阅成功、能够收到完整字段，但持续显示“尚未发现 xw/xx 或 ETF 申赎字段”，说明当前 QMT 普通 L2 的 Python 包装层没有透传深交所 300111 的 ETF 实时申赎扩展字段。

脚本兼容官方示例中的两类字段表示：

| 中文含义 | 回调英文键 |
| --- | --- |
| 申购笔数 | `buyNumber` |
| 申购数量 | `buyAmount` |
| 申购金额 | `buyMoney` |
| 赎回笔数 | `sellNumber` |
| 赎回数量 | `sellAmount` |
| 赎回金额 | `sellMoney` |

官方回调样例里 `buyMoney ` 曾带有尾部空格，测试脚本已做兼容清洗。

## 判断边界

- 官方“场内基金”页明确提供 `etfstatistics` 的字段、回调样例和原生 XtQuant 订阅代码。
- 同一官方知识库明确提供 QMT 内置 Python 的通用 `ContextInfo.subscribe_quote(..., period=..., callback=...)`。
- 官方页面没有单独展示一段“内置 Python + etfstatistics”的现成样例，因此本目录脚本是把上述两个官方接口组合起来的可运行验证代码。最终是否收到数据，仍以你的客户端版本和账号数据权限为准。

## 官方资料

- [迅投知识库：场内基金—基金实时申赎数据](https://dict.thinktrader.net/dictionary/floorfunds.html#【🔔迅投研专属】基金实时申赎数据)
- [迅投知识库：内置 Python `ContextInfo.subscribe_quote`](https://dict.thinktrader.net/innerApi/data_function.html#contextinfo-subscribe-quote-订阅行情数据)
- [迅投知识库：XtQuant 行情模块](https://dict.thinktrader.net/nativeApi/xtdata.html)
