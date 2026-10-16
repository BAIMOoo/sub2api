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
- 搜索历史兼容仍按模式补充声明：普通模式使用顶层 `tools`，Lite 使用 `additional_tools`。无工具时补充 cached-only 搜索并设置 `tool_choice=none` 是网关兼容修复，会有意区别于原生请求，防止引入新的搜索调用。
- compact 有独立协议约定，不能套用主 Responses 的工具搬迁与头序规则。
- 账号与 API Key 作用域化继续处理会话标识；比较时核对格式和跨字段关联关系，鉴权值、会话值和随机材料不能作为固定字节基准。
- 保留上游对 input item 的 `internal_chat_message_metadata_passthrough` 清理。官方 0.158.0 在 provider 名为 `OpenAI` 时会保留此字段；历史 [#7066](https://github.com/Wei-Shaw/sub2api/issues/7066) 则记录了 ChatGPT 对此字段返回 400。因此这是明确的兼容例外，不能宣称逐字段完全相同，也不能把它误归因为 OAuth 与 API Key 的布局区别。

## 传输基准与配置

已有的 Linux ClientHello 摘录在 `backend/internal/pkg/tlsfingerprint/profiles.go`，HTTP/1.1 主 Responses 的头顺序摘录在 `httporder.go`。后者只覆盖 `chatgpt.com/backend-api/codex/responses`，不覆盖 compact 与 WebSocket 握手。

版本字段由后台客户端版本配置及自动同步决定；选用 0.158.0 结构基准不等于运行中的版本已固定为 0.158.0。需要固定版本对照时，应配置客户端版本并关闭自动同步。TLS 指纹需启用并选择对应的 Codex Linux profile；自定义 profile、关闭身份统一及插件传输都会影响实际线上报文。

zstd 测试验证编码、解压后的 JSON、压缩后长度与重放一致性，不证明不同压缩库产出相同字节。

本轮用校验过 release digest 的官方 Linux arm64 0.158.0 二进制，在隔离容器、虚拟 API Key 和本地 HTTP mock 中采集了普通 / Lite 两个完整正文，已保存至 `backend/internal/service/testdata/codex_exec_0158/`，来源和摘要见该目录 README。回归通过完整 HTTP `Forward()` 路径，分别覆盖 transform / passthrough，显式检查上述元数据例外与会话字段关联后，比较其余全部正文、原生提示、工具和 item ID。测试还核对 0.158.0 身份、Lite 头和 zstd 编码声明。

这些采集不是 ChatGPT OAuth 实包，不证明现网 OAuth 对内部元数据的接受情况，也不证明 Cookie、TLS 或压缩字节相同。历史 ClientHello 和头序摘录未附完整原始抓包，不能把它们称为本轮 OAuth 抓包结果。

## 回归验证

```sh
cd backend
go test -tags=unit ./internal/service \
  -run '(Codex|OAuthWebSearchHistory|Responses.*Namespace|ClientTransport)' -count=1
go test -tags=unit ./internal/pkg/tlsfingerprint ./internal/repository \
  -run '^(TestOpenAIHeaderOrder.*|TestBuildClientHelloSpec.*|TestGetClientEntryWithTLS.*|TestUpstreamCookieJar.*|TestOpenAIUpstreamCookieSeedPath.*|TestBuildUpstreamCookieSeedRequest.*)$' -count=1
```

这些测试锁定 HTTP 的已知结构与传输约定。完整验收还需要固定版本、模型、平台和配置，以相同输入采集官方 exec 与网关实际发送的 OAuth HTTP 请求，比较解压后的正文、有序请求头、TLS 握手及续轮状态。WebSocket 不属于本次验收范围。
