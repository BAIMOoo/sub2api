# Codex exec 0.158.0 报文基准

本仓库以官方 `rust-v0.158.0` 的请求构造逻辑为结构基准。本次对齐和验收仅覆盖 HTTP 分支，WebSocket 不在范围内。版本、模型模式、传输路径必须同时明确，不能把某一种模型的抓包结论推广到全部 Codex 请求。

## 普通模式与 Responses Lite

官方源码：[build_responses_request](https://github.com/openai/codex/blob/rust-v0.158.0/codex-rs/core/src/client.rs#L912)。同一构造器用于 HTTP 与 WebSocket，请求布局由 `model_info.use_responses_lite` 决定。

| 项目 | 普通 Responses | Responses Lite |
| --- | --- | --- |
| 基础指令 | 顶层 `instructions` | `input` 中的 developer message |
| 工具声明 | 顶层 `tools` | `input` 前缀中的 `additional_tools` |
| 命名空间 | 保留调用方的原生声明 | 保留原生 namespace 声明 |
| `parallel_tool_calls` | 由 prompt 决定 | `false` |
| reasoning context | 使用服务端默认值 | `all_turns` |
| `store` / `stream` | `false` / `true` | `false` / `true` |

此前 fork 中“官方从不发送顶层 tools/instructions”的注释过度概括了 Lite 形态。普通模式的顶层字段是合法的，不能一律搬迁或删除。

## 转发约定

- 普通模式保留原生顶层工具和基础指令；只对 Lite 执行已有的第三方函数工具搬迁。
- 已有的 Lite `additional_tools`、命名空间、基础提示和有效 item ID 应保留，不重复插入。
- 原生 Codex HTTP 请求的有效 `call_id`、跨轮次调用引用及 `custom_tool_call_output.id` 保留；结果条目 ID 不套用兼容入口的 `fc` 前缀限制，空值和超过 64 字节的 ID 仍清理。第三方、API-key 和 WebSocket 路径继续使用原有兼容规则。
- 搜索历史兼容仍按模式补充声明：普通模式使用顶层 `tools`，Lite 使用 `additional_tools`。无工具时补充 cached-only 搜索并设置 `tool_choice=none` 是网关兼容修复，会有意区别于原生请求，防止引入新的搜索调用。该修复是独立 OAuth 兼容能力，可覆盖 HTTP 与既有 WebSocket 路径；它不作为 `codex_exec` HTTP 精确对齐或监控验收的证据。
- compact 有独立协议约定，不能套用主 Responses 的工具搬迁与头序规则。
- 账号与 API Key 作用域化继续处理会话标识；比较时核对格式和跨字段关联关系，鉴权值、会话值和随机材料不能作为固定字节基准。
- 对官方 `codex exec` 的原生 HTTP 请求保留 input item 的 `internal_chat_message_metadata_passthrough`。2026-11-12 的生产对齐检测 `20261112T010000Z-88612991` 中，官方 `codex exec` OAuth 请求实际携带该字段并完成上游请求；因此当前原生路径按实测报文透传。
- 非 `codex exec` 的兼容入口、TUI/Desktop/IDE 以及旧兼容路径继续执行该字段清理。历史 [#7066](https://github.com/Wei-Shaw/sub2api/issues/7066) 的清理规则仍保留在这些路径，不能把原生 `codex exec` 的结论推广到其他客户端。
- TUI/Desktop/IDE 的已有 native HTTP 身份和工具 ID 行为保持冻结；本次 `codex_exec` 精确 reasoning/metadata 策略不得扩散到这些客户端。范围外不等于允许顺手改变现有行为。

## 传输基准与配置

已有的 Linux ClientHello 摘录在 `backend/internal/pkg/tlsfingerprint/profiles.go`，HTTP/1.1 主 Responses 的头顺序摘录在 `httporder.go`。后者只覆盖 `chatgpt.com/backend-api/codex/responses`，不覆盖 compact 与 WebSocket 握手。

原生 Codex HTTP 的 User-Agent、originator 和 version 经一致性校验后保留；缺失 originator/version 时从有效 UA 补齐。身份冲突、非法版本或低于上游最低版本的请求按现有兼容身份规则回退。账号级自定义 UA 与 ForceCodexCLI 的显式覆盖仍有优先级。原生识别只用于报文兼容，不替代鉴权或访问控制。

后台客户端版本配置及自动同步继续决定兼容请求、凭据请求等路径的规范身份，不再覆盖有效原生 HTTP 的版本。0.158.0 与 0.163.0 可以同时保持各自身份，无需降低网关全局版本。WebSocket 身份策略不在本次修改范围。TLS 指纹需启用并选择对应的 Codex Linux profile；自定义 profile、关闭身份统一及插件传输都会影响实际线上报文。

zstd 测试验证编码、解压后的 JSON、压缩后长度与重放一致性，不证明不同压缩库产出相同字节。

本轮用校验过 release digest 的官方 Linux arm64 0.158.0 二进制，在隔离容器、虚拟 API Key 和本地 HTTP mock 中采集了普通 / Lite 两个完整正文，已保存至 `backend/internal/service/testdata/codex_exec_0158/`，来源和摘要见该目录 README。回归通过完整 HTTP `Forward()` 路径，分别覆盖 transform / passthrough，比较原生 `codex exec` 的完整正文，并验证其他客户端仍执行旧元数据清理。测试还核对 0.158.0 身份、Lite 头和 zstd 编码声明。

这些采集不是 ChatGPT OAuth 实包；现网 OAuth 对内部元数据的接受情况由生产对齐运行 `20261112T010000Z-88612991` 单独证明。采集本身不证明 Cookie、TLS 或压缩字节相同，历史 ClientHello 和头序摘录也未附完整原始抓包。

## 回归验证

```sh
cd backend
go test -tags=unit ./internal/service \
  -run '(Codex|OAuthWebSearchHistory|Responses.*Namespace|ClientTransport)' -count=1
go test -tags=unit ./internal/pkg/tlsfingerprint ./internal/repository \
  -run '^(TestOpenAIHeaderOrder.*|TestBuildClientHelloSpec.*|TestGetClientEntryWithTLS.*|TestUpstreamCookieJar.*|TestOpenAIUpstreamCookieSeedPath.*|TestBuildUpstreamCookieSeedRequest.*)$' -count=1
```

这些测试锁定 HTTP 的已知结构与传输约定。完整验收还需要固定版本、模型、平台和配置，以相同输入采集官方 exec 与网关实际发送的 OAuth HTTP 请求，比较解压后的正文、有序请求头、TLS 握手及续轮状态。WebSocket 不属于本次验收范围。

`openai_codex_native_http_test.go` 使用合成工具记录，覆盖两版客户端、标准/Lite、转换/透传和 compact，核对身份、工具配对及引用。规范身份故意与入站身份不同，避免测试通过固定相同 UA 隐藏覆盖问题。合成 ID 不代表从生产 HMAC 恢复的原文。
