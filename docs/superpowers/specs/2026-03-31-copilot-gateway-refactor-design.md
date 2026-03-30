# Copilot Gateway Refactor: 从 OpenAI Gateway 侵入式修改改为独立路径

**日期：** 2026-03-31
**状态：** Approved

## 背景

当前 Copilot 集成通过在 OpenAI Gateway 调度器和服务中添加 `|| account.Platform != PlatformCopilot` 的方式实现，散落在 `openai_gateway_service.go`、`openai_account_scheduler.go`、`openai_gateway_handler.go`、`openai_gateway_messages.go` 共 4 个文件中。这种侵入式修改有影响现有 OpenAI 功能的风险，且违反单一职责原则。

详细的修改记录和根因分析见 `COPILOT_CHANGES.md`。

## 目标

将 Copilot 从 OpenAI Gateway 中完全剥离，改为通过主 GatewayService 的独立路径处理。回退所有对 OpenAI Gateway 文件的 Copilot hack，使 OpenAI Gateway 恢复为纯 OpenAI 平台专用。

## 架构设计

### 重构后的请求流

```
/v1/messages (Copilot 分组)
  → routes/gateway.go: platform == PlatformCopilot
    → GatewayHandler.Messages (主 handler)
      → Copilot failover 分支
        → GatewayService.SelectAccountWithLoadAwareness (调度)
        → GatewayService.ForwardCopilotAsMessages (Anthropic ↔ CC 转换 + 调用 Copilot API)

/v1/chat/completions (Copilot 分组)
  → routes/gateway.go: 已有逻辑 (不经过 OpenAI Gateway)
    → GatewayHandler.ChatCompletions
      → GatewayService.ForwardAsChatCompletions
        → Copilot 判断 → GatewayService.ForwardCopilot (直接转发，已实现)
```

不经过 `OpenAIGatewayService`，所有 Copilot 逻辑收敛在 `copilot_*.go` 文件里。

### 与 Gemini 的对比

Copilot 分支在 GatewayHandler.Messages 中的位置和模式与 Gemini 分支平行：

| 方面 | Gemini | Copilot |
|------|--------|---------|
| 调度 | `SelectAccountWithLoadAwareness` | `SelectAccountWithLoadAwareness` |
| 转发 | `geminiCompatService.ForwardMessages` | `gatewayService.ForwardCopilotAsMessages` |
| Failover | FailoverState loop | FailoverState loop |

## 具体变更

### 1. 路由层 (`routes/gateway.go`)

**变更：** `/v1/messages` 路由中，Copilot 分组从 `h.OpenAIGateway.Messages(c)` 改为 `h.Gateway.Messages(c)`。

```go
// Before:
if platform == PlatformOpenAI || platform == PlatformCopilot {
    h.OpenAIGateway.Messages(c)
}

// After:
if platform == PlatformCopilot {
    h.Gateway.Messages(c)
    return
}
if platform == PlatformOpenAI {
    h.OpenAIGateway.Messages(c)
    return
}
```

`/v1/chat/completions` 不需要改动——Copilot 已经走 GatewayHandler。

### 2. GatewayHandler.Messages 添加 Copilot 分支 (`gateway_handler.go`)

在 Gemini 分支之前添加 Copilot 分支。复用方法顶部的公共逻辑（Auth、Billing、并发检查），模式与 Gemini 分支一致：

1. `SelectAccountWithLoadAwareness` 调度账号
2. `AcquireAccountSlot` 获取并发槽位
3. `ForwardCopilotAsMessages` 执行格式转换和转发
4. FailoverState 处理重试

不需要新增 handler 构造函数参数——`gatewayService` 已是 GatewayHandler 的字段。

### 3. ForwardCopilotAsMessages (`copilot_gateway_messages.go` — 新建)

将 `openai_copilot_messages.go` 中的 `forwardCopilotAsAnthropic` 方法迁移到 GatewayService：

- 方法接收者：`*OpenAIGatewayService` → `*GatewayService`
- 方法名：`forwardCopilotAsAnthropic` → `ForwardCopilotAsMessages`（public）
- 文件名：`openai_copilot_messages.go` → `copilot_gateway_messages.go`
- 内部逻辑不变：Anthropic Messages → Chat Completions → Copilot API → Anthropic Messages

该方法不依赖 OpenAIGatewayService 特有字段（如 schedulerSnapshot），迁移无阻碍。

### 4. 回退 OpenAI Gateway 的 Copilot hack

| 文件 | 回退内容 |
|------|---------|
| `openai_account_scheduler.go:587` | `(!account.IsOpenAI() && account.Platform != PlatformCopilot)` → `!account.IsOpenAI()` |
| `openai_gateway_service.go:1583` | `resolveFreshSchedulableOpenAIAccount` 同上 |
| `openai_gateway_service.go:1605` | `recheckSelectedOpenAIAccountFromDB` 同上 |
| `openai_gateway_service.go:listSchedulableAccounts` | 移除 `platform` 参数和 Copilot 自动检测逻辑（line 1527-1541），恢复为硬编码 `PlatformOpenAI`；调用处 (line 1135, 1336) 移除空字符串参数 |
| `openai_gateway_handler.go` | `AllowMessagesDispatch` 移除 Copilot 跳过逻辑 |
| `openai_gateway_messages.go:36-38` | `ForwardAsAnthropic` 开头移除 Copilot 判断 |

### 5. 删除不再需要的文件

- 删除 `openai_copilot_messages.go`（已迁移到 `copilot_gateway_messages.go`）

### 6. ChatCompletions 路径 (不变)

`gateway_forward_as_chat_completions.go` 中的 Copilot 路由保留——这已经在主 GatewayService 上，通过 `ForwardCopilot` 直接转发。

## 文件变更总结

```
修改文件 (6):
  backend/internal/server/routes/gateway.go       — Copilot 路由到主 GatewayHandler
  backend/internal/handler/gateway_handler.go      — 添加 Copilot failover 分支
  backend/internal/service/openai_account_scheduler.go — 回退 Copilot 条件
  backend/internal/service/openai_gateway_service.go   — 回退 Copilot 条件 + listSchedulableAccounts
  backend/internal/handler/openai_gateway_handler.go   — 回退 AllowMessagesDispatch
  backend/internal/service/openai_gateway_messages.go  — 回退 ForwardAsAnthropic Copilot 判断

新建文件 (1):
  backend/internal/service/copilot_gateway_messages.go — ForwardCopilotAsMessages

删除文件 (1):
  backend/internal/service/openai_copilot_messages.go  — 已迁移
```

## 验证计划

重构完成后需要测试（在 git worktree 中）：

1. `curl /v1/chat/completions` — Copilot 分组，gpt-4 模型（直接转发）
2. `curl /v1/messages` — Copilot 分组，claude-sonnet-4.6 模型（Anthropic 格式请求和响应）
3. 确认 OpenAI 分组的 `/v1/messages` 和 `/v1/chat/completions` 功能不受影响
4. 编译验证：`go build -tags embed ./cmd/server` 确保无编译错误

## 开发方式

在 git worktree 中隔离开发，不直接修改主分支。完成验证后合并。
