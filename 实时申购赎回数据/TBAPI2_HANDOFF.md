# Wind TBAPI2 收尾交接（2026-08-11）

## 已完成结论

TBAPI2 路径现已端到端验证成功，包括修复版 dylib 注入、收盘后快照回调、`JavaTableFrame` 字段描述解析和数据行解析。

2026-08-11 16:14（深市收盘后）向 Wind PID `98400` 注入唯一命名的修复版：

```text
/Users/ellis/Library/Containers/com.windin.mac.free/Data/tmp/wind_tbapi_probe/
libwind_tbapi_probe_v3_cbfix_20260811_1619.dylib
```

订阅创建成功：

```json
{"status":"create_sub_A","code":720899}
```

随后立即收到 `wind_tbapi_probe_sub_1.json`，`error_code=0`。这证明：

1. `JavaAPIModule + 0x00` 确实是订阅回调槽位。
2. 即使不在交易时段，TBAPI2 也会推送当前缓存/页面快照。
3. 截图中的六项数据可以从 `JavaTableFrame` 无损还原。

## 本次快照的解码结果

```json
{
  "etfbuynumber": 3,
  "etfbuyamount": 3000000,
  "etfbuymoney": 0,
  "etfsellnumber": 2,
  "etfsellamount": 2000000,
  "etfsellmoney": 0,
  "windcode": "159518.SZ"
}
```

与 Wind 页面显示完全一致：申购笔数 3、金额 0、份额 300 万；赎回笔数 2、金额 0、份额 200 万。

## JavaTableFrame 已确认布局

### `field_info`

- 前 2 字节：字段数量，big-endian。
- 每个字段记录依次包含：
  - `name_len`: 2 字节 big-endian
  - UTF-8 字段名 + `NUL`
  - `type_code`: 1 字节
  - `format_code`: 2 字节 big-endian
  - `width`: 8 字节 big-endian
  - `reserved`: 8 字节 big-endian
  - `flags`: 4 字节 little-endian
  - `offset`: 2 字节 big-endian
  - `offset_reserved`: 2 字节（本帧均为零）
  - `field_id`: 2 字节 big-endian
- 已观察类型：`0x25=int32`、`0x27=int64`、`0x52=fixed string`。

### `buffer_58`

- 前 12 字节为三个 big-endian `uint32`：`row_count`、`row_size`、未命名 header word。
- 行内数值按字段 offset 放置，int32/int64 为 little-endian。
- 本帧行大小 111 字节，其中已知值区域 104 字节。
- 行尾剩余 7 字节刚好对应 7 个字段，本帧全部为零；可能是 null/状态位，但尚未取得带 null 的样本，当前解码器只原样保留，不赋予未经验证的语义。

## 可直接运行的文件

```bash
cd "/Users/ellis/Desktop/ETF交割/实时申购赎回数据"

# 解析目录中的固定真实样本
python3 wind_tbapi_frame_parser.py sample_wind_tbapi_probe_sub_1.json

# 等价的显式样本模式（不会读取 Wind 沙盒）
python3 wind_tbapi_frame_parser.py --sample

# 无参数时解析 Wind 沙盒内最新的 sub_*.json
python3 wind_tbapi_frame_parser.py

# 显示字段元数据
python3 wind_tbapi_frame_parser.py --metadata

# 回归测试
python3 -m unittest -v test_wind_tbapi_frame_parser.py
```

## 最重要的注入陷阱

在同一个 Wind 进程内覆盖磁盘上的 dylib 后，再对**同一路径**执行 `dlopen`，dyld 只会增加旧镜像引用计数，不会加载新文件或重跑 constructor。

这次旧状态文件写于 `15:56:26`，修复版却在 `16:08` 才编译，因而第一次所谓“复测”实际上仍是旧镜像。将修复版复制为唯一文件名后再注入，才真正得到回调。今后每次热更新应使用带时间戳或内容哈希的唯一 dylib 文件名，或者重启 Wind 后再使用原路径。

## 当前运行状态与下一步

- Wind 在注入后已经正常 detach，没有停在调试态。
- 修复版订阅 ID：`720899`。
- 探针收到 10 个回调后会调用 `CJAVAPauseSubscription`；本次收盘后只收到初始快照。
- 原订阅 `720898` 来自旧镜像，回调槽位错误，不会写出有效捕获文件。

### PyQt6 运行时订阅更新

同日已新增 `wind_tbapi_runtime_probe.c` 与 `wind_etf_realtime_ui.py`。运行时探针通过导出函数接收证券代码，不再把 `159518.SZ` 编译进 SQL。实机测试由 UI 后端动态创建订阅 `720900`，生成 `wind_tbapi_live_159518_SZ.json`，成功解码页面六项数据，并通过 `CJAVATerminateSubscription` 正常停止。

UI 支持：

- 界面严格校验 6 位深圳代码，内部自动补充 `.SZ`。
- 手动设置 0.5–60 秒订阅/刷新频率。
- 开始和停止订阅。
- 独立的每标的最新快照文件。
- 数据年龄和缓存/过期提示。
- 读取旧 v3 捕获作为兼容回退。

启动：

```bash
cd "/Users/ellis/Desktop/ETF交割/实时申购赎回数据"
/Users/ellis/miniconda3/envs/ag/bin/python wind_etf_realtime_ui.py
```

### 多标的监控更新

`wind_etf_multi_monitor_ui.py` 已成为默认入口。批量控制器会在一次 lldb 附加中加载多个唯一 dylib 并分别调用 `wind_tbapi_subscribe`，停止时同样只附加一次。

实机验证结果：同时订阅 `159518.SZ` 和 `159393.SZ`，分别得到订阅 ID `393223`、`393224`，两个独立快照均成功解码，随后批量停止无错误。

变化检测规则：首次快照建立基线，不报警；后续只订阅 `etfbuynumber`、`etfbuyamount`、`etfsellnumber`、`etfsellamount`，不再请求两个金额字段。主页计算 `轧差份额 = etfbuyamount - etfsellamount`；提醒只由申购份额、赎回份额或轧差份额变化触发，笔数仅展示参考。提醒支持非模态置顶弹窗和系统提示音。

新 SQL 已通过真实订阅 `393228` 验证，返回字段只有上述四项和 `Windcode`；`159518.SZ` 的实测轧差为 `3000000 - 2000000 = +1000000`。

### Mac-home、内网服务与客户端（最终交付）

原计划中的本地接口已经完成，并进一步扩展为统一图形主机：

- `etf_mac_home_app.py` 是 Mac-home 正式入口。UI 内嵌 `etf_monitor_server.py`，本机界面也通过 `127.0.0.1:6787` 使用同一个 WebSocket/HTTP 状态源，不会重复订阅。
- 服务器监听 `0.0.0.0:6787`，向内网提供全量拉取、变化推送和心跳；观察列表更新、启停和 PCF 刷新只允许 `127.0.0.1/::1`。按用户要求不使用令牌。
- `web/monitor.html` 可直接由浏览器访问，支持声音、系统通知和自动重连。
- `etf_remote_client.py` 是跨平台 PyQt6 客户端，Mac/Windows 均可运行，断线自动重连并在客户端本地弹窗/响铃。
- 生命周期任务默认工作日 `09:00` 清空上一日实时状态，`09:10` 启动 Wind，`09:15:05` 用第一只观察标的执行一次临时订阅预热，`09:15:30` 建立全部正式订阅，`15:00` 先停订再关闭 Wind；确认 Wind PID 完全消失后，才清理严格匹配的临时 dylib。预热专门吸收 TBAPI2 首次延迟初始化可能产生的 `EXC_BAD_ACCESS address=0x0`，其他错误不会被吞掉。Wind 盘中意外退出时按冷却时间重新启动并重建订阅。Mac-home 首页另有仅限本机调用的“启动 Wind”“关闭 Wind 并清理”按钮和状态灯。
- 服务端对每个已观察到的份额变化按 callback 毫秒时间写入 `Application Support/ETFDelivery/history/changes_YYYY-MM-DD.jsonl`，默认保留 120 天；桌面客户端与网页均提供日期/标的查询入口。
- 观察列表持久化；技术日志单独保存在 Application Support，不显示在主页。

实机/构建验证：

- 服务器成功读取两份真实缓存：`159518` 轧差 `+1,000,000`，`159393` 轧差 `-900,000`。
- HTTP 全量、WebSocket 初始快照/心跳/拉取、网页声音解锁均已验证。
- Mac arm64 打包产物已通过 `codesign --verify --deep --strict`；2026-08-15 已部署到 Mac-home `192.168.1.113`，内嵌服务在 `0.0.0.0:6787` 正常监听并返回 protocol 1、11 个标的。
- 49 项 Python 回归测试全部通过；GPT-5.6 Terra 两轮静态终检均未发现 P0/P1。
- 实机生命周期闭环通过：先从 Wind 临时目录精确删除 56 个历史生成 dylib（2,853,568 字节），再由主机启动 Wind，状态达到 `running=true`、`tbapi_loaded=true`、单个经路径校验的 PID；随后以 SIGTERM 温和关闭并再次清理，最终 Wind PID 与生成 dylib 均为 0，主机和 6787 服务继续运行。
- 实测发现临时签名更新会让 macOS TCC 原授权失配。清理现已放入最长 15 秒的可终止隔离进程，不再因权限框无限阻塞。当前部署已恢复临时签名并可正常启动；长期免重复授权必须改用 `Developer ID Application` + Apple 公证，不能使用 `Apple Development` 证书冒充分发签名。

交付物：

```text
/Users/ellis/Desktop/ETF交割/实时申购赎回数据/dist/ETF监控主机-macOS-arm64.zip
/Users/ellis/Desktop/ETF交割/实时申购赎回数据/dist/ETF远程监控-Windows源码.zip
```

安装入口：

```text
/Users/ellis/Desktop/ETF交割/实时申购赎回数据/安装MacHome图形版.command
```

最终现场状态（2026-08-15 00:50）：图形版已安装到
`/Users/ellis/Applications/ETF监控主机.app`，LaunchAgent
`com.etfdelivery.mac-home` 为 `running`；旧纯后台 LaunchAgent 已卸载。健康接口 `last_error=null`，主机进程正常、Wind 已按测试流程关闭、历史生成 dylib 为 0。通过 Mac-home 本机 `127.0.0.1:6787` 和内网服务验证成功；当天为周六，`monitoring=false` 是预期状态。工作日真实 09:00/09:10/09:15:05/09:15:30/15:00 边界仍应在下一个交易日观察日志复核。

下一位 agent 的优先事项：

1. 必须在正常交易时段做一次“真实数值变化”验收，确认服务器、网页和至少一台远程客户端同时收到同一个 change 事件并各自响铃。
2. Wind 升级后优先复核 TBAPI2 模块偏移 `base + 0xB0568`；目前尚未做版本签名/偏移自动扫描，错误版本不应盲目注入。
3. 继续采集多行、字段为 null 的样本，确认 JavaTableFrame 行尾 7 字节语义。
4. 变化历史现已按日 JSONL 持久化；若未来需要更复杂的统计、联表或多年数据，再考虑迁移 SQLite。

旧的完整逆向与注入记录仍见：

```text
/Users/ellis/Desktop/ETF交割/wind_tbapi_probe/HANDOFF.md
```
