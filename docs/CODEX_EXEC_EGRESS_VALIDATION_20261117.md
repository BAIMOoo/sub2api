# Codex exec 固定 HTTP 出站验收

日期：2026-11-17，Asia/Shanghai。需求已由用户完成 grilling 整体确认，见 [需求记录](CODEX_EXEC_EGRESS_REQUIREMENTS.md)。基线提交：`9f97758e95fdb3bce5ceec587e06ee8d594cc729`；工作分支：`codex/codex-exec-egress`。

## 结果

- 目标 HTTP 通道固定为官方 exec 0.158.0 / Linux x86_64，实际参考环境 Alpine Linux 3.24.4。
- 任意 harness 的任务内容及工具语义采用保留或明确失败策略；不支持的生成控制只忽略并记录字段名。身份准入、动态版本、自动提示与工具注入不能覆盖目标约定。
- 普通/Lite 根据最终账号和模型能力选择。未知模式兼容回退日志为 unverified；已知模式日志 baseline_selected 仅说明选择依据，不代表完整实测通过。
- compact 兼容入口使用官方 0.158.0 实际支持的 Responses V2 压缩请求，并还原 unary response.compaction 返回。
- 目标通道强制已验证 TLS/头序路径；不兼容代理或插件明确失败。整个 WS 入站链路及其他上游保留原行为。

## 自动化检查

| 检查 | 结果 |
| --- | --- |
| `go test -tags=unit -parallel 1 ./...` | 通过，58 个有测试的包 |
| `go test -parallel 1 ./...` | 通过，52 个有测试的包 |
| `golangci-lint v2.14.0 run ./...` | 通过，0 issues |
| `go test -race ./internal/pkg/codexprobe` | 通过 |
| 对齐监控 Python 离线测试 | 50 项通过，使用仓库锁定依赖的临时环境 |
| 固定 x86_64 参考文件 SHA256 | 205 项全部匹配 |
| Git diff whitespace check | 通过；原始 HTTP/CRLF/二进制数据以 binary 属性保持原字节 |

全量 Go 检查对测试进程清除了代理环境变量，避免阿里云 SDK 的本地假端点被外部代理截走。使用 `-parallel 1` 避免既有 HeapAlloc 全进程内存断言受并行用例干扰；未修改该用例或放宽阈值。

回归包含：任意/缺失 UA、与固定版本不同的服务器解析器、账户配置冲突、OAuth/PAT/SetupToken 范围、WS→HTTP 隔离、大小整数工具 schema、reasoning 往返、工具名/结果保留、未知字段明确失败、模式回退、账号切换、实际发送正文上的索引重试、已知有损修复禁止、compact 完成事件、入站 beta 覆盖和非流式响应类型。

## 真实上游

临时 Linux amd64 测试程序直接调用本次网关转发逻辑和实际 HTTPUpstream 实现，复用可用 OAuth 账号；没有启动/部署应用、写入数据库、修改账号或通知他人。测试私有配置只在服务器临时目录创建，运行结束后删除。

实测模型：`gpt-6-astra`，Lite。最终同一构建连续测试结果：

| HTTP 入口 | HTTP 状态 | 完成 | 有效 JSON / 非 SSE |
| --- | --- | --- | --- |
| Responses | 200 | 是 | 是 |
| Responses 透传 | 200 | 是 | 是 |
| Chat Completions | 200 | 是 | 是 |
| Messages | 200 | 是 | 是 |
| compact 兼容入口 | 200 | 是 | 是 |

最终 live 测试退出码为 0，耗时 17.47 秒。此前实测发现透传在上游响应头与实际 SSE 正文不一致时未转换；已加入正文格式识别并用回归及最终实测证实修复。

此环境实际可用认证类型只有 OAuth。PAT/SetupToken 有自动化范围与固定传输检查，但没有声称真实认证已验收。普通模式的参考回归已通过，本轮真实调用使用 Lite；其他模型、代理和未验证插件没有被计为实测通过。

## 分层证据与审查

固定官方 Linux amd64 二进制及 companion 的发行摘要已校验。HTTP/正文/压缩用隔离、无外网、假 OAuth 的官方二进制采集；默认原生 TLS 在没有自建 CA 或解密代理的独立运行中采集。完整参考、原始数据、调用命令与源码引用见 [参考说明](../backend/internal/service/testdata/codex_exec_0158_linux_amd64/README.md)。没有将两层证据声称为同一次完整直连抓包。

采集证实固定 UA、HTTP/1.1 头序、ClientHello 结构，以及 zstd 的 2 MiB 窗口/无内容长度字段/无字典/无校验和。Go zstd 的分块与压缩字节允许不同，逐字节解压内容一致。

标准与需求两轴审查均已执行，复审确认所列问题已修复。随后实测发现并修复了透传响应格式问题，最终全量检查及真实请求均通过。

## 构建关联

- Go 代码差异 SHA256：`1bdc18cdb7145b6e00c4afe6ac451dc748a373926fcccbac115bdf5c851b94cf`
- 最终 Linux 实测程序 SHA256：`8e11f0258d15eea90a9337be71bef3e15d976e86e88927e6986bdc496b53e7bd`
- 没有合并上游、推送分支或部署生产。线上更新仍是后续独立步骤。
