# C++ / Qt 6 重构方案

## 当前结论

Mac-home 上的 541MB 并不是正常 Python 包体积。故障应用被错误展开复制，
`Contents/Frameworks` 与 `Contents/Resources` 各保留了一整套依赖，并丢失
`_CodeSignature`；正确的 macOS `.app` 约 118MB，压缩包约 48MB。

因此当前优先级是修复交付方式并稳定现有业务协议，而不是立即整套重写。

## 推荐迁移顺序

1. 冻结当前 HTTP/WebSocket v1 协议和变化事件字段。
2. 先用 C++/Qt 6 实现远程客户端：`QWebSocket`、`QNetworkAccessManager`、
   `QSettings`、`QMediaPlayer`，复用现有服务端，无需触碰 Wind 注入链路。
3. 客户端稳定后，再评估 Mac 主机服务迁移：
   - `QHttpServer` / `QWebSocketServer` 替代 FastAPI/uvicorn；
   - `QNetworkAccessManager` + `QXmlStreamReader` 迁移深圳 PCF；
   - `QSaveFile`/SQLite 维护配置与日期缓存；
   - 保留现有 Objective-C/C dylib 探针与 JSON 帧协议。
4. Python 主机与 C++ 主机并行一段时间，用相同捕获文件跑一致性回归后切换。

## 预期收益与代价

- C++ 远程客户端预计可压缩到约 25–60MB，启动和常驻内存更可控。
- 完整主机重构预计涉及 UI、HTTP/WebSocket、PCF、调度、日志和进程注入，
  风险远高于客户端迁移；在正确打包后的 118MB 可接受时，不建议立即实施。
- 若正确签名/解压的 Python 包仍在 macOS 上持续原生崩溃，或常驻内存长期超过
  300MB，再启动第二阶段主机迁移。
