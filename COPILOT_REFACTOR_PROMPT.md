## 重构需求：Copilot 平台从 OpenAI Gateway 侵入式修改改为独立路径

### 背景

当前 Copilot 集成通过在 OpenAI Gateway 调度器和服务中添加 `|| account.Platform != PlatformCopilot` 的方式实现，散落在多个文件中，有影响现有 OpenAI 功能的风险。需要重构为更干净的架构。

详细的修改记录和根因分析见 `COPILOT_CHANGES.md`。

### 目标

将 Copilot 从 OpenAI Gateway 的侵入式修改中剥离，改为通过主 GatewayService 的独立路径处理。**回退所有对 `openai_gateway_service.go`、`openai_account_scheduler.go`、`openai_gateway_handler.go` 的修改**，不再让 Copilot 走 OpenAI Gateway。

### 架构设计

```
/v1/messages (Copilot 分组)
  → routes/gateway.go 判断 platform == PlatformCopilot
    → 主 GatewayHandler.Messages 中的 Copilot 分支
      → GatewayService.ForwardCopilotAsMessages（Anthropic ↔ CC 转换 + 调用 Copilot API）

/v1/chat/completions (Copilot 分组)
  → routes/gateway.go 判断 platform == PlatformCopilot
    → 主 GatewayHandler.ChatCompletions 中的 Copilot 分支
      → GatewayService.ForwardCopilot（直接转发）
```

不经过 `OpenAIGatewayService`，所有 Copilot 逻辑收敛在 `GatewayService` 和 `copilot_*.go` 文件里。

### 具体步骤

#### 1. 回退 OpenAI Gateway 的修改

回退以下文件中所有 `PlatformCopilot` 相关的修改：
- `openai_gateway_service.go` — 回退 `listSchedulableAccounts` 的 platform 参数、`resolveFreshSchedulableOpenAIAccount` 和 `recheckSelectedOpenAIAccountFromDB` 中的 Copilot 检查
- `openai_account_scheduler.go` — 回退 `selectByLoadBalance` 中的 `!account.IsOpenAI()` 修改
- `openai_gateway_handler.go` — 回退 `AllowMessagesDispatch` 的 Copilot 跳过
- `openai_gateway_messages.go` — 回退 `ForwardAsAnthropic` 开头的 Copilot 判断

#### 2. 路由层：独立分发

在 `routes/gateway.go` 中，为 `/v1/messages` 和 `/v1/chat/completions` 添加 Copilot 平台判断，路由到主 Gateway Handler 而不是 OpenAI Gateway Handler。

#### 3. 主 GatewayHandler 添加 Copilot 处理

在 `gateway_handler.go` 的 Messages 方法中，在 Gemini 分支之前添加 Copilot 分支：
- 使用主 `GatewayService.SelectAccountWithLoadAwareness` 进行账号调度（它已经支持 platform 过滤）
- 调用 `GatewayService.ForwardCopilotAsMessages` 处理格式转换和转发

在 `gateway_handler_chat_completions.go` 的 ChatCompletions 方法中，`ForwardAsChatCompletions` 开头的 Copilot 判断保留（这个是在主 GatewayService 上的，不涉及 OpenAI Gateway）。

#### 4. 实现 ForwardCopilotAsMessages

将 `openai_copilot_messages.go` 中的 `forwardCopilotAsAnthropic` 方法迁移到 `GatewayService` 上（改名为 `ForwardCopilotAsMessages`），从 `OpenAIGatewayService` 剥离。

#### 5. 删除不再需要的文件

删除 `openai_copilot_messages.go`（已迁移到 GatewayService）。

### 参考

- `COPILOT_CHANGES.md` — 当前实现的完整修改记录和根因分析
- `~/litellm/litellm/llms/github_copilot/` — LiteLLM 的 Copilot 实现参考
- LiteLLM 通过继承 OpenAIConfig 实现 Copilot，但 sub2api 的多账号调度层决定了不能简单复用 OpenAI Gateway

### 验证

重构完成后需要测试：
1. `curl /v1/chat/completions` — Copilot 分组，gpt-4 模型
2. `curl /v1/messages` — Copilot 分组，claude-sonnet-4.6 模型（Anthropic 格式请求和响应）
3. 确认 OpenAI 分组的 `/v1/messages` 和 `/v1/chat/completions` 功能不受影响
4. API Key: 使用本地测试环境变量中的有效 key，端口 8081
