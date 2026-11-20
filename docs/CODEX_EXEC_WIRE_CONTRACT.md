# Codex exec 0.158.0 HTTP 报文基准

本文描述 `codex/fork-contract-codex-http-egress` 候选的技术约定和验证边界。所有者需求、`main` 的实际状态和合入条件以 [fork 合约](../FORK_CONTRACT.md) 为准。候选的证据不能用于声称 `main` 或生产已更新。

## 适用范围

- 直接 HTTP 的 Responses、透传、Chat Completions、Messages 和 compact 在最终账号为 Codex/ChatGPT 协议（OAuth、PAT、SetupToken）时使用固定出站。调用方的 UA、`originator` 和 `codex_cli_only` 不参与这条路径的准入判断；原有鉴权、分组、额度和权限检查保留。
- 直连 OpenAI API Key、第三方上游及完整 WebSocket 链路不使用此契约。WS 转 HTTP 使用上游请求、Cookie、插件和 TLS 行为，不读取或更新固定 HTTP 的账号 Cookie。

## 正文和模式

| 项目 | Standard | Lite |
| --- | --- | --- |
| 工作区指令 | 顶层 `instructions` | `input` 内的 developer message |
| 工具声明 | 顶层 `tools` | `input` 前缀的 `additional_tools` |
| 传输字段 | `store=false`、`stream=true` | 同左，另有 Lite 路由头 |

模式由最终账号和模型能力决定。能力未知时保留请求声明，未声明则用 Standard，记录 `alignment_status=unverified`；`baseline_selected` 仅说明选择了已知布局，不代表实测通过。出站只发送调用方的任务、历史、推理状态、元数据和工具语义，不附加强制指令、todo guard 或新工具。无法无损表达的内容或引用返回 400；仅不支持的生成控制可忽略并记录字段名。会话、Cookie 和重试属于各自账号；会话头与正文元数据检查格式和关联关系，不要求每次随机值相同。

`/v1/responses/compact` 仍接受旧客户端调用。固定出站使用 `/backend-api/codex/responses`、`remote_compaction_v2` 和 `compaction_trigger`，等待完整上游终止事件后返回兼容的 `response.compaction` JSON，不请求旧上游 `/responses/compact`。

## 传输

- 身份固定为 `codex_exec`、`Version: 0.158.0` 与 `codex_exec/0.158.0 (Alpine Linux 3.24.4; x86_64) unknown (codex_exec; 0.158.0)`；版本发现、入站 UA 和手动账号 UA 不改变该身份。
- 目标为 `https://chatgpt.com/backend-api/codex/responses`。使用 `OpenAICodexLinuxProfile`（无 ALPN、无 GREASE）、有序小写 HTTP/1.1 请求头和流式 zstd。zstd 使用 2 MiB 窗口、不声明解压长度、不用字典或校验和；只要求解压内容和重放一致，不要求压缩字节与官方库相同。
- 普通直连、支持的 SOCKS/HTTP CONNECT 通道保留同一个 TLS profile。不兼容的 HTTPS 代理、未验证的插件传输或账号 TLS 配置返回明确配置错误，不回退到普通 Go TLS。可能切换到 Codex 的请求在语义输出前缓发心跳以保留 HTTP 错误状态；非续聊的 OpenAI 请求排队时也保守缓发，API Key-only 续聊及确认不含 Codex 的池在实际转发时保留对应的心跳。

## 分层证据

| 层 | 证据与局限 |
| --- | --- |
| 官方 HTTP、头序、正文和 zstd | 2026-11-17 在隔离的 Alpine Linux x86_64 环境中，用经过发行摘要核对的官方 CLI/companion、假 OAuth 和本地 HTTPS 检查 CA 采集。Standard、Lite 和 Compact V2 对应 [HTTP 结构摘录](../backend/internal/service/testdata/codex_exec_0158_linux_amd64/http.json)；完整原始资料及 SHA256 见[固定参考归档](https://github.com/BAIMOoo/sub2api/tree/deb4e7bf330aa449bfdfd1ee7a60f90432caa3c4/backend/internal/service/testdata/codex_exec_0158_linux_amd64)。候选回归将真实 builder 的请求交给 HTTP 头序写入器，并核对解压内容与此次转发正文。较早的 [arm64 夹具](../backend/internal/service/testdata/codex_exec_0158/README.md)仍验证完整正文重放，但不是 OAuth TLS 或头序证据。 |
| 官方原生 TLS | 同版本、平台和二进制，以无自定义 CA、无代理的独立运行捕获 HTTP 回退的 ClientHello（非前面的 WS 尝试）。[结构摘录](../backend/internal/pkg/tlsfingerprint/testdata/codex_exec_0158_linux_amd64_clienthello.json)来自归档 `native-tls/profile.json`；候选测试在内存连接上比较实际发出的 ClientHello、扩展和 key-share 长度。它不验证协商完成，也不是上行 HTTP 采集时的同一个连接。 |
| 真实供应商 | OAuth、PAT 和 SetupToken 均有合成账号回归。本候选没有使用真实测试凭据，三个类型均为未实测（`unverified`）；[旧分支的 OAuth 实测](https://github.com/BAIMOoo/sub2api/blob/deb4e7bf330aa449bfdfd1ee7a60f90432caa3c4/docs/CODEX_EXEC_EGRESS_VALIDATION_20261117.md)不能转移到当前 SHA。候选实测入口见[运行手册](CODEX_ALIGNMENT_RUNBOOK.md#opt-in-candidate-live-verification)。 |
| CI、数据库和部署 | 本地验证仅适用于已记录的候选 SHA；远端 CI、已安装 fork 数据库副本升级和生产部署是独立门槛，缺证据均为 `unverified`。临时数据库的测试不是生产升级验收。 |

候选的精确 SHA、实际运行的命令和验收结论记录在 [#12](https://github.com/BAIMOoo/sub2api/issues/12)。原生 TLS 与修订后的 HTTP 请求不能合并声明成一次完整的直连交易。

## 历史边界

基线 `9fc36883b` / WS 修复 `c3ed2d993` 仍使用早期动态身份规则；它们不实现上述固定身份策略。扣费合并队列未改，等待上游修复；合入 `main`、发布和部署仍各自需要批准。
