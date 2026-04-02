# Copilot 平台集成修改记录

## 背景

Sub2API 已有 Copilot OAuth 认证代码，但无法通过 API 调用 Copilot 模型。管理后台测试连接正常，但 `/v1/messages` 和 `/v1/chat/completions` 调用均失败。

## 卡点与解决过程

### 卡点 1：错误的转发路径

`/v1/chat/completions` 路由走 `ForwardAsChatCompletions`，该函数把所有请求转成 Anthropic Messages 格式发到 Anthropic API。Copilot 账号的 API Key 被发到 Anthropic → 401。

管理后台测试走的是独立代码路径（`testCopilotAccountConnection`），直接调用 Copilot API，所以测试正常。最初误判为 API Key 过期。

**修改**：
- `gateway_forward_as_chat_completions.go`：开头判断 `account.Platform == PlatformCopilot`，直接路由到 `ForwardCopilot`

### 卡点 2：Messages API 不支持 Copilot

Claude Code CLI 调用 `/v1/messages`（Anthropic 格式），需要完整的格式转换链路。

**修改**：
- `routes/gateway.go`：`/v1/messages` 路由让 Copilot 分组走 `OpenAIGateway.Messages`
- `openai_gateway_handler.go`：`AllowMessagesDispatch` 权限检查跳过 Copilot 平台
- `openai_copilot_messages.go`（新增）：Anthropic Messages ↔ Chat Completions 格式转换，调用 Copilot API 并将响应转回 Anthropic 格式

### 卡点 3：调度器硬编码 `IsOpenAI()`

OpenAI Gateway 调度器多处 `!account.IsOpenAI()` 过滤，Copilot 账号被查到也会被丢弃。每修一处暴露下一处，逐层剥洋葱：

| 文件 | 位置 | 说明 |
|------|------|------|
| `openai_account_scheduler.go:587` | `selectByLoadBalance` | 过滤非 OpenAI 账号 |
| `openai_gateway_service.go:1583` | `resolveFreshSchedulableOpenAIAccount` | 二次校验过滤 |
| `openai_gateway_service.go:1605` | `recheckSelectedOpenAIAccountFromDB` | DB 回查过滤 |
| `openai_gateway_service.go:listSchedulableAccounts` | 账号查询 | 硬编码 `PlatformOpenAI` |

**修改**：所有 `!account.IsOpenAI()` 改为 `!account.IsOpenAI() && account.Platform != PlatformCopilot`；`listSchedulableAccounts` 增加 platform 参数，自动检测 Copilot 分组。

## 全部修改文件清单

### 新增文件
| 文件 | 说明 |
|------|------|
| `backend/internal/service/copilot_gateway_service.go` | ForwardCopilot：直接转发 Chat Completions 到 Copilot API |
| `backend/internal/service/copilot_token_refresher.go` | Copilot API Key 自动刷新器 |
| `backend/internal/service/openai_copilot_messages.go` | Anthropic Messages ↔ Copilot Chat Completions 转换 |
| `backend/internal/handler/copilot_oauth.go` | Copilot OAuth Device Flow handler |
| `backend/internal/pkg/copilot/oauth.go` | GitHub Device Flow + API Key 获取 |

### 修改文件
| 文件 | 说明 |
|------|------|
| `backend/internal/domain/constants.go` | 添加 `DefaultCopilotModelMapping` |
| `backend/internal/service/account.go` | `resolveModelMapping` 支持 Copilot 默认映射 |
| `backend/internal/service/gateway_forward_as_chat_completions.go` | 开头路由 Copilot 到 `ForwardCopilot` |
| `backend/internal/service/gateway_service.go` | `ForwardCopilot` 调用入口 |
| `backend/internal/service/openai_gateway_service.go` | `listSchedulableAccounts` 支持 Copilot；多处 `IsOpenAI()` 检查放行 Copilot |
| `backend/internal/service/openai_gateway_messages.go` | `ForwardAsAnthropic` 开头路由 Copilot |
| `backend/internal/service/openai_account_scheduler.go` | 调度器过滤条件放行 Copilot |
| `backend/internal/service/token_refresh_service.go` | 注册 `CopilotTokenRefresher` |
| `backend/internal/service/wire.go` | 注入 `CopilotOAuthService` 到 Token Refresh Service |
| `backend/internal/handler/openai_gateway_handler.go` | `AllowMessagesDispatch` 跳过 Copilot |
| `backend/internal/server/routes/gateway.go` | `/v1/messages` 路由支持 Copilot 分组 |
| `frontend/src/components/account/CreateAccountModal.vue` | 保存 `access_token` 和 `expires_at` 用于 Token 刷新 |

## 根因分析

Sub2API 的 OpenAI 兼容层为 OpenAI 平台专门设计，没有预留多平台扩展点。Copilot 虽然也用 OpenAI 格式，但平台标识不同，导致调度、路由、权限检查层层拦截。每一层的 `IsOpenAI()` 检查都是独立的，没有统一的平台兼容判断函数。

## 后续建议

### 重构方案（基于 LiteLLM 研究）

LiteLLM 的实现方式是让 `GithubCopilotConfig` **继承 `OpenAIConfig`**，只覆盖差异点（认证、Headers、System 消息转换）。这验证了"Copilot 复用 OpenAI 兼容层"是正确方向。

但 sub2api 的核心问题是**调度层的平台抽象缺失**。LiteLLM 没有多账号调度，所以不存在这个问题。

建议的重构路径：

1. **添加 `IsOpenAICompatible()` 方法** — 统一判断 OpenAI 格式兼容的平台（OpenAI、Copilot、未来可能的其他平台），替代所有散落的 `IsOpenAI() || Platform == PlatformCopilot` 检查
2. **调度层统一** — `openai_account_scheduler.go` 和 `openai_gateway_service.go` 中的平台过滤全部改用 `IsOpenAICompatible()`
3. **转发层保持独立** — `ForwardCopilot` 和 `forwardCopilotAsAnthropic` 保持独立实现，因为 Copilot 不需要 OpenAI 的 OAuth Token Provider、WebSocket、Codex CLI 检测等复杂逻辑
4. **参考 LiteLLM 的 System 消息处理** — LiteLLM 默认把 system 转为 assistant（Copilot 兼容性），可通过 `disable_copilot_system_to_assistant` 标志控制

### LiteLLM 实现参考

LiteLLM 源码位于 `~/litellm/litellm/llms/github_copilot/`：

| 文件 | 说明 |
|------|------|
| `authenticator.py` | OAuth Device Flow + 本地文件缓存 token + 自动刷新 |
| `chat/transformation.py` | 继承 OpenAIConfig，覆盖 Headers、System 消息转换、X-Initiator |
| `responses/transformation.py` | 继承 OpenAIResponsesAPIConfig，处理 encrypted_content |
| `embedding/transformation.py` | Embedding 请求/响应转换 |
| `common_utils.py` | 版本常量、默认 Headers、错误类型 |
