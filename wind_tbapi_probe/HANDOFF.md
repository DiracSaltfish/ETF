# Wind TBAPI2 实时 ETF 申购赎回数据 — 抓取方案与后续操作

> **2026-08-11 16:14 更新：本文件中的“等待测试”已经完成。** 修复版通过唯一 dylib 文件名真正载入后，收盘后也成功收到快照回调；`JavaTableFrame` 已解析并与 Wind 页面数值完全一致。可运行解码器、真实捕获样本、测试和最新交接请直接查看：`/Users/ellis/Desktop/ETF交割/实时申购赎回数据/TBAPI2_HANDOFF.md`。

## 一、目标

从万得（Wind）金融终端实时抓取 ETF（159518.SZ）的申购赎回数据：
- `etfbuynumber` — 申购笔数
- `etfbuyamount` — 申购份额
- `etfbuymoney` — 申购金额
- `etfsellnumber` — 赎回笔数
- `etfsellamount` — 赎回份额
- `etfsellmoney` — 赎回金额

最终通过 Unix Socket / 本地 HTTP 提供给外部程序使用。

---

## 二、项目文件结构

```
/Users/ellis/Desktop/ETF交割/wind_tbapi_probe/
├── probe.c                          # v1 原始探针（仅 CJAVAQuery，已废弃）
├── probe_v2.c                       # v2 首次尝试订阅（CJAVAInit(NULL) 崩溃）
├── probe_v3.c                       # v3 当前主力版本（见下方关键发现）
├── probe_v4.c                       # v4 多 SQL 格式尝试
├── libwind_tbapi_probe.dylib        # v1 编译产物
├── libwind_tbapi_probe_v2.dylib     # v2 编译产物
├── libwind_tbapi_probe_v3.dylib     # v3 编译产物（已部署到沙盒）
├── libwind_tbapi_probe_v4.dylib     # v4 编译产物
└── README.md

/Users/ellis/Desktop/ETF交割/万得股票实时申购赎回接口研究.md  # 详细研究文档
/Users/ellis/Desktop/ETF交割/实时申购赎回数据/               # QMT 备选方案

# 部署位置（Wind 沙盒内，避免 sandbox 拦截）：
/Users/ellis/Library/Containers/com.windin.mac.free/Data/tmp/wind_tbapi_probe/
├── libwind_tbapi_probe.dylib
├── libwind_tbapi_probe_v2.dylib
├── libwind_tbapi_probe_v3.dylib
└── libwind_tbapi_probe_v4.dylib
```

---

## 三、已验证可行的技术路径

### 3.1 dylib 注入 ✅

- **方法**：lldb 附加到 Wind 进程，执行 `dlopen` 加载自定义 dylib
- **沙盒绕过**：dylib 必须放在 Wind 自己的沙盒目录 `/Users/ellis/Library/Containers/com.windin.mac.free/Data/tmp/wind_tbapi_probe/`
- **注入命令**：
  ```bash
  lldb -p <Wind_PID>
  # 在 lldb 中：
  expr (void)dlopen("/Users/ellis/Library/Containers/com.windin.mac.free/Data/tmp/wind_tbapi_probe/libwind_tbapi_probe_v3.dylib", 0x6)
  ```
- ⚠️ 注入后务必 `detach`，否则 Wind 会卡死（彩虹圈）
- ⚠️ 如果 lldb 意外断开，进程进入 SX 状态（被调试中），需先 `pkill -9 lldb` 再 kill Wind

### 3.2 TBAPI2 模块获取 ✅

两种互补策略（已在 probe_v3.c 中实现）：

- **策略 A**：通过 `dladdr(CJAVAInit, &info)` 获取 TBAPI2 基址，直接从数据段 `base + 0xB0568`（arm64 偏移）读取全局 JavaAPIModule 指针
- **策略 B**：Fallback — 用零初始化的 options 结构体安全调用 `CJAVAInit(zero_opts)` 创建新模块

### 3.3 订阅接口调用 ✅

- `CJAVACreateSubscription(module, sql, isCoord)` — 返回正数 = 订阅 ID，负数 = 错误
- 直接用完整 SQL（含 `LATENCY(500 MS)`）创建订阅成功，返回 `sub_id = 720898`

---

## 四、关键发现（重要！）

### 4.1 JavaAPIModule 有两个回调槽位

通过逆向 `libWind.Cosmos.TBAPI2.dylib`（arm64）发现：

| 偏移 | 用途 | 设置方式 |
|------|------|----------|
| `+0x00` | **订阅回调**（JavaAPIModule::SubCB 调用） | 需手动写入 |
| `+0x20` | **查询回调**（JavaAPIModule::QueryCB 调用） | `CJAVARegisterQueryCallBack` 设置 |

`CJAVARegisterQueryCallBack` **只设置 offset 0x20**，不设置 0x00。因此订阅回调必须手动写入。

### 4.2 订阅回调签名

从 `JavaAPIModule::SubCB` 反汇编推断（arm64 调用约定）：
```c
void subscription_callback(
    uint32_t sub_id,         // x0/w0 — 订阅 ID
    int32_t  error_code,     // x1    — 错误码（0 = 成功）
    const char *error_msg,   // x2    — 错误信息
    void *frame_ptr          // x3    — JavaTableFrame 指针（NULL 表示无数据）
);
```

> ⚠️ 与查询回调不同：查询回调第一个参数是 `int64_t request_id`，订阅回调是 `uint32_t sub_id`。

### 4.3 错误码含义

| 错误码 | 含义 | 来源 |
|--------|------|------|
| `-12314` | 提交错误（Query 不适用于此数据） | `CJAVAQuery` |
| `-12293` | SQL 解析失败 | `CJAVACreateSubscription`（空 windcode `''`） |

### 4.4 一次成功订阅即可

不需要模仿 Wind UI 的两步法（Create 空订阅 → Modify）。直接用完整 SQL 创建订阅即可：
```sql
SELECT etfbuynumber, etfbuyamount, etfbuymoney,
       etfsellnumber, etfsellamount, etfsellmoney
FROM ETFComprehensive.WholeETFData
WHERE windcode = '159518.SZ' LATENCY(500 MS)
```

---

## 五、当前状态

### probe_v3.c（已修复回调偏移，等待测试）

probe_v3.c 已更新：同时设置 offset 0x00（订阅回调）和 offset 0x20（查询回调）为同一个 `sub_callback` 函数。

**编译命令**：
```bash
clang -arch arm64 -O2 -Wall -Wextra -dynamiclib \
  -o /Users/ellis/Desktop/ETF交割/wind_tbapi_probe/libwind_tbapi_probe_v3.dylib \
  /Users/ellis/Desktop/ETF交割/wind_tbapi_probe/probe_v3.c \
  -Wno-unused-parameter
```

**部署命令**：
```bash
cp /Users/ellis/Desktop/ETF交割/wind_tbapi_probe/libwind_tbapi_probe_v3.dylib \
   "/Users/ellis/Library/Containers/com.windin.mac.free/Data/tmp/wind_tbapi_probe/libwind_tbapi_probe_v3.dylib"
```

**注入命令**（需 Wind 运行中，建议在深市交易时段 9:30-11:30 / 13:00-15:00）：
```bash
# 1. 获取 Wind PID
ps aux | grep WindPersonFree | grep -v grep

# 2. 附加并注入（替换 <PID>）
lldb -p <PID>
# lldb 中执行：
expr (void)dlopen("/Users/ellis/Library/Containers/com.windin.mac.free/Data/tmp/wind_tbapi_probe/libwind_tbapi_probe_v3.dylib", 0x6)
```

**验证**：
```bash
# 1. 检查状态（是否拿到模块、订阅是否成功）
cat "/Users/ellis/Library/Containers/com.windin.mac.free/Data/tmp/wind_tbapi_probe_v3_status.json"

# 2. 检查回调文件（每个 500ms 推送生成一个 JSON）
ls -la "/Users/ellis/Library/Containers/com.windin.mac.free/Data/tmp/wind_tbapi_probe_sub_"*.json

# 3. 查看回调内容
cat "/Users/ellis/Library/Containers/com.windin.mac.free/Data/tmp/wind_tbapi_probe_sub_1.json"
```

---

## 六、待完成的工作

### 6.1 优先级 1：验证订阅回调

- [ ] 在交易时段注入 v3 探针
- [ ] 确认收到 `sub_*.json` 回调文件
- [ ] 解析 `JavaTableFrame` 中的字段名和数据值
- [ ] 验证数值单位（份额是否为"股"，金额是否为"元"）

### 6.2 优先级 2：解析 JavaTableFrame

probe_v3 当前只做了 hex dump。下一步需要：
1. 解析 `field_info` 缓冲区 → 获取字段名、类型、偏移
2. 解析数据缓冲区 → 按字段偏移提取实际数值
3. 输出 JSON：`{"windcode":"159518.SZ", "etfbuynumber":3, ...}`

### 6.3 优先级 3：建立桥接服务

长期方案（见研究文档第七章）：
```
Wind 进程内 TBAPI2
        ↓
白名单订阅管理器（probe dylib）
        ↓
Unix Domain Socket（/tmp/wind_etf_bridge.sock）
        ↓
外部 Python / Go 程序
```

或本地 HTTP：
```
GET /etf/realtime?windcode=159518.SZ
→ {"windcode":"159518.SZ","etfbuynumber":3,...}
```

### 6.4 备选路径 B：直接读页面内存

如果要绕过 TBAPI2 订阅，可以直接搜索 Wind 进程内存中已渲染的 ETF 数据：

- 已定位 `159518.SZ` 字符串在多处出现（0x1420d75c8、0x1421a816c 等）
- 已知页面显示值：申购笔数=3, 份额=3000000, 赎回笔数=2, 份额=2000000
- 下一步：在 lldb 中搜索 3000000 (0x002DC6C0) 和 2000000 (0x001E8480) 的二进制表示，找到后在附近字段中定位完整数据结构
- Wind 使用 WebKit 渲染，数据可能在 JavaScript heap（可通过 `vmmap` 查看 MALLOC 区域）

### 6.5 备选路径 C：QMT/迅投

`/Users/ellis/Desktop/ETF交割/实时申购赎回数据/` 目录有 QMT 内置 Python 脚本：
- `QMT内置Python_深圳ETF实时申赎.py` — 使用 `etfstatistics` 周期
- `QMT内置Python_159518_L2原始字段探测.py` — 使用 `l2quote` 周期探测

需尊享投研端 + etfstatistics 数据权限。若权限具备，可直接用 QMT 的 `subscribe_quote` 获取数据，无需注入。

---

## 七、环境信息

```
Wind 客户端：WindPersonFree 25.4.1 (39916)
路径：/Applications/WindPersonFree.app/Contents/MacOS/WindPersonFree
架构：arm64 (Apple Silicon)
TBAPI2：/Applications/WindPersonFree.app/Contents/Frameworks/libWind.Cosmos.TBAPI2.dylib
部署沙盒：/Users/ellis/Library/Containers/com.windin.mac.free/Data/tmp/
平台：macOS 27.0 (26A5406e)
```

## 八、注意事项

1. **交易时段**：深市 ETF 实时申赎数据仅在交易时段（9:30-11:30, 13:00-15:00）推送。非交易时段订阅可能返回全零或无推送。
2. **会话依赖**：probe 复用 Wind 已登录的 TBAPI2 会话。Wind 必须保持运行且已登录。
3. **私有接口**：TBAPI2 是 Wind 客户端私有接口，升级后签名/偏移可能变化。
4. **崩溃风险**：回调函数内不要阻塞、不要抛异常。数据必须在回调内立即复制（回调返回后 TBAPI2 释放缓冲区）。
5. **权限**：lldb 附加需要 macOS 开发者工具权限（系统设置 → 隐私与安全性 → 开发者工具）。
