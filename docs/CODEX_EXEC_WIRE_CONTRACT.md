# Codex exec 0.171.0 HTTP 报文基准

本文描述 `codex/egress-baseline-0171` 候选的技术约定和验证边界。所有者需求、`main` 的实际状态和合入条件以 [fork 合约](../FORK_CONTRACT.md) 为准。候选的证据不能用于声称 `main` 或生产已更新。

## 适用范围

- 直接 HTTP 的 Responses、透传、Chat Completions、Messages 和 compact 在最终账号为 Codex/ChatGPT 协议（OAuth、PAT、SetupToken）时使用固定出站。调用方的 UA、`originator` 和 `codex_cli_only` 不参与这条路径的准入判断；原有鉴权、分组、额度和权限检查保留。
- 直连 OpenAI API Key、第三方上游及完整 WebSocket 链路不使用此契约。WS 转 HTTP 使用上游请求、Cookie、插件和 TLS 行为，不读取或更新固定 HTTP 的账号 Cookie。

## 正文和模式

| 项目 | Standard (`0.171.0`) | Lite (`0.171.0`) |
| --- | --- | --- |
| 工作区指令 | 移除顶层 `instructions`；非空指令置入 `input` 开头的 `developer` message（`content_item_kinds: ["model.base_instructions"]`） | 移除顶层 `instructions`；非空指令置入 `input` 内的 `developer` message（位于已声明工具的 `additional_tools` 之后） |
| 工具声明 | 保留顶层 `tools`（无工具时为 `[]`） | 有工具声明时位于 `input` 前缀的 `additional_tools` 且无顶层 `tools`；未声明工具时不插入空的 `additional_tools` 载体 |
| 传输字段 | `store=false`、`stream=true` | 同左，另有 `x-openai-responses-lite: true`、`parallel_tool_calls=false`、`reasoning.context="all_turns"` |

模式由最终账号和模型能力决定。能力未知时保留请求声明，未声明则用 Standard，记录 `alignment_status=unverified`；`baseline_selected` 仅说明选择了已知布局，不代表实测通过。出站只发送调用方的任务、历史、推理状态、元数据和工具语义，不附加强制指令、todo guard 或新工具。无法无损表达的内容或引用（包括非字符串顶层 `instructions`）返回 400；仅不支持的生成控制可忽略并记录字段名。会话、Cookie 和重试属于各自账号；会话头与正文元数据检查格式和关联关系，不要求每次随机值相同。

`/v1/responses/compact` 仍接受旧客户端调用。固定出站使用 `/backend-api/codex/responses`、`remote_compaction_v2` 和 `compaction_trigger`，沿用 `0.171.0` 正文布局（无顶层 `instructions`），等待完整上游终止事件后返回兼容的 `response.compaction` JSON，不请求旧上游 `/responses/compact`。

## 传输

- 身份固定为 `codex_exec`、`Version: 0.171.0` 与 `codex_exec/0.171.0 (Alpine Linux 3.24.4; x86_64) unknown (codex_exec; 0.171.0)`；版本发现、入站 UA 和手动账号 UA 不改变该身份。固定出站版本须满足内置 Codex 模型目录中所有 `minimal_client_version` 下限，且不改写上游编译回退常量 `codexCLIVersion = "0.146.0"`。
- 目标为 `https://chatgpt.com/backend-api/codex/responses`。使用 `OpenAICodexLinuxProfile`（无 ALPN、无 GREASE、`DisableCompression: true`）、有序小写 HTTP/1.1 请求头和流式 zstd。按 `0.171.0` 官方二进制实采，请求头显式发送 `accept-encoding: zstd,gzip`，位置固定在 `user-agent` 之后、`cookie` / `host` 之前；网关响应路径按 `Content-Encoding` 原生流式解压 `gzip` 与 `zstd`（含 SSE 与 4xx/5xx 错误体）。zstd 请求体使用 2 MiB 窗口、不声明解压长度、不用字典或校验和；只要求解压内容和重放一致，不要求压缩字节与官方库相同。
- 普通直连、支持的 SOCKS/HTTP CONNECT 通道保留同一个 TLS profile。不兼容的 HTTPS 代理、未验证的插件传输或账号 TLS 配置返回明确配置错误，不回退到普通 Go TLS。可能切换到 Codex 的请求在语义输出前缓发心跳以保留 HTTP 错误状态；非续聊的 OpenAI 请求排队时也保守缓发，API Key-only 续聊及确认不含 Codex 的池在实际转发时保留对应的心跳。
- 上游返回 `400` 且错误体明确属于客户端版本过旧拒绝（`client version ... too old` / `upgrade Codex to ...`）或对应模型元数据已知 `minimal_client_version` 高于固定出站版本时，只透传错误给调用方，不触发号池模型级冷却，也不把 `400` 放大为 `429`。

## 分层证据

| 层 | 证据与局限 |
| --- | --- |
| 官方 HTTP、头序、正文和 zstd | 2026-11-24 在隔离的 Alpine Linux 3.24.4 x86_64 环境中，用经过发行摘要核对的官方 `rust-v0.171.0` Linux `x86_64` musl CLI/code-mode-host 二进制、合成 OAuth 凭据和本地 HTTPS 检查 CA 采集。Standard、Lite 和 Compact V2 对应 [HTTP 结构摘录](../backend/internal/service/testdata/codex_exec_0171_linux_amd64/http.json)、[正文样本目录](../backend/internal/service/testdata/codex_exec_0171_linux_amd64/README.md) 与 [发行 provenance](../backend/internal/service/testdata/codex_exec_0171_linux_amd64/provenance.json)。候选回归将真实 builder 的请求交给 HTTP 头序写入器，并核对解压内容与此次转发正文。较早的 `0.158.0` [旧格式夹具](../backend/internal/service/testdata/codex_exec_0158/README.md) 仅用于验证旧顶层 `instructions` 入站向 `0.171.0` 布局的无损兼容转换，不再作为现行出站基准。 |
| 官方原生 TLS | 同版本（`rust-v0.171.0`）、同平台和同二进制，以无自定义 CA、无代理的独立运行捕获 HTTP 回退的 ClientHello（非前面的 WS 尝试）。[结构摘录](../backend/internal/pkg/tlsfingerprint/testdata/codex_exec_0171_linux_amd64_clienthello.json) 验证 JA3 与全部 TLS 字段与 `OpenAICodexLinuxProfile` 一致；候选测试在内存连接上比较实际发出的 ClientHello、扩展和 key-share 长度。它不验证协商完成，也不是上行 HTTP 采集时的同一个连接。 |
| 真实供应商 | OAuth、PAT 和 SetupToken 均有合成账号回归；候选的真实供应商实测结论按凭据类型、模型与路由分别记账在验收票 [#19](https://github.com/BAIMOoo/sub2api/issues/19)，任何未实测的凭据类型或模型显式标记为 `unverified`。候选实测入口见[运行手册](CODEX_ALIGNMENT_RUNBOOK.md#opt-in-candidate-live-verification)。 |
| CI、数据库和部署 | 本地串行单包验证、远端 GitHub Actions CI、自线上 `v0.2.23`（`schema_migrations=306`）升级的数据库迁移兼容性复核与生产部署均为独立门槛，缺证据均为 `unverified`。临时数据库的测试不是生产升级验收。 |

候选的精确 SHA、实际运行的命令、远端 CI 链接和分层验收结论记录在 [#13](https://github.com/BAIMOoo/sub2api/issues/13) 与 [#19](https://github.com/BAIMOoo/sub2api/issues/19)。原生 TLS 与修订后的 HTTP 请求不能合并声明成一次完整的直连交易。

## 历史边界

`main`（`v0.2.24`）使用早期固定 `0.158.0` 出站基线，线上因 `0.158.0` 低于 `gpt-6.2-sol` / `gpt-6.2-astra` 的 `minimal_client_version: 0.166.0` 已临时回退至 `v0.2.23`（数据库维持 `schema_migrations=306`）；更早基线 `9fc36883b` / WS 修复 `c3ed2d993` 使用动态身份规则。扣费合并队列未改，等待上游修复；合入 `main`、发布与生产部署（含停机升级及监控基线切换）均受 [#20](https://github.com/BAIMOoo/sub2api/issues/20) 人工门禁控制。
