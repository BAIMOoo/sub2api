# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

**重要规则：在此项目中工作时，始终使用中文回复用户。**

---

## 项目概述

Sub2API 是一个 AI API 网关平台，将多种 AI 订阅账号（Claude/Copilot/Gemini/OpenAI/Sora 等）统一暴露为标准 Anthropic API，支持账号调度、计费、限速、并发控制。

**Tech Stack:** Go 1.26 + Gin + Ent (backend) / Vue 3 + Vite + TailwindCSS (frontend) / PostgreSQL + Redis

---

## 常用命令

### 后端

```bash
# 本地开发运行
cd backend && go run ./cmd/server

# 编译（本地平台）
cd backend && make build   # → bin/server

# 交叉编译（Apple Silicon → Linux amd64，用于 Docker 部署）
cd backend
export GOPROXY=https://goproxy.cn,direct
GOOS=linux GOARCH=amd64 CGO_ENABLED=0 go build -tags embed -o sub2api-linux ./cmd/server

# 重新生成 Wire 依赖注入（修改 handler/service 后必须执行）
cd backend/cmd/server && ~/go/bin/wire

# 重新生成 Ent ORM（修改 ent/schema 后执行）
cd backend && go generate ./ent

# 运行全部测试
cd backend && go test ./...

# 按 tag 分组运行
cd backend && go test -tags=unit ./...
cd backend && go test -tags=integration ./...
cd backend && go test -tags=e2e -v -timeout=300s ./internal/integration/...

# 运行单个测试文件
cd backend && go test ./internal/service/gateway_service_test.go

# 运行单个测试函数
cd backend && go test -run TestGatewayService_SelectAccount ./internal/service

# 使用 Makefile（推荐）
cd backend && make test          # 运行测试 + golangci-lint
cd backend && make test-unit     # 仅单元测试
cd backend && make test-integration  # 仅集成测试
cd backend && make test-e2e      # E2E 测试（需要 Docker 环境）
```

### 前端

```bash
cd frontend

# 开发服务器
npm run dev

# 生产构建（输出嵌入后端的静态文件）
npm run build

# 类型检查
npm run typecheck

# Lint（自动修复）
npm run lint

# 单元测试
npm run test:run

# 测试覆盖率
npm run test:coverage
```

### 完整部署流程（前端改动后）

```bash
# 1. 构建前端
cd frontend && npm run build

# 2. 交叉编译后端（必须带 -tags embed，否则前端不会嵌入，访问 404）
cd backend
GOOS=linux GOARCH=amd64 CGO_ENABLED=0 go build -tags embed -o sub2api-linux ./cmd/server

# 3. 部署到 Docker 容器
docker cp backend/sub2api-linux sub2api:/app/sub2api
docker restart sub2api
```

---

## 架构

### 整体结构

```
sub2api/
├── backend/          # Go 服务端
│   ├── cmd/server/   # 入口 (main.go, wire_gen.go, VERSION)
│   ├── ent/          # ORM 模型及生成代码 (schema/ 为源)
│   ├── internal/
│   │   ├── config/      # 配置加载 (Viper)
│   │   ├── domain/      # 跨层共享的领域常量/类型
│   │   ├── handler/     # HTTP 处理层 (Gin)
│   │   │   ├── admin/   # 管理后台接口
│   │   │   └── dto/     # 请求/响应 DTO
│   │   ├── middleware/  # Gin 中间件 (认证、限速等)
│   │   ├── pkg/         # 可复用工具包
│   │   ├── repository/  # 数据库访问层 (Ent)
│   │   ├── server/      # HTTP 服务器启动、路由注册
│   │   └── service/     # 业务逻辑层（核心）
│   └── migrations/   # 数据库迁移 SQL
└── frontend/         # Vue 3 前端
    └── src/
        ├── api/         # Axios 接口封装
        ├── components/  # 通用组件
        ├── composables/ # Vue composables
        ├── stores/      # Pinia 状态
        ├── views/       # 页面视图
        └── router/      # 路由配置
```

### 请求处理链路

```
客户端 (Anthropic API 格式)
  → GatewayHandler (handler/gateway_handler.go)
      → 认证 + 限速中间件
      → GatewayService.SelectAccount() — 账号调度
          → 平台网关服务（按平台路由）:
              · GatewayService (gateway_service.go)         — Anthropic/Claude 直连
              · OpenAIGatewayService                        — OpenAI 格式转发
              · CopilotGatewayService (copilot_gateway_messages.go / responses.go)
              · AntigravityGatewayService                   — Antigravity 平台
              · GeminiMessagesCompatService                 — Gemini 兼容层
              · SoraGatewayService                          — Sora 视频生成
      → 流式 SSE 转发 / 非流式响应
      → UsageRecordWorkerPool — 异步计费记录
```

### 账号调度

- **`OpenAIAccountScheduler`** (`service/openai_account_scheduler.go`)：OpenAI/Copilot 账号的负载感知调度（`IsOpenAI()` 或 `IsCopilot()` 均走此路）
- 粘性会话 (`stickySessionTTL = 1h`)：同一用户在会话内持续命中同一账号
- 并发限制 (`ConcurrencyService`) + RPM 令牌桶 (`RateLimitService`)
- `TokenRefreshService` + 各平台 `*TokenRefresher`：后台定时刷新 OAuth token（30 分钟）

### 依赖注入

项目使用 **Google Wire** 做编译期依赖注入。修改 handler 或 service 构造函数后，需要在 `backend/cmd/server/` 目录运行 `wire` 重新生成 `wire_gen.go`。

**重要：** `wire_gen.go` 是自动生成文件，多分支并行开发时容易产生合并冲突。合并时需按功能归属人工取舍，不能直接 `git checkout` 任一侧。解决冲突后重新运行 `wire` 生成。

### 平台网关文件对应关系

| 平台 | 主要文件 |
|------|---------|
| Anthropic/Claude 直连 | `service/gateway_service.go`, `service/gateway_forward_as_chat_completions.go` |
| OpenAI | `service/openai_gateway_service.go`, `service/openai_gateway_messages.go` |
| Copilot | `service/copilot_gateway_service.go`, `service/copilot_gateway_messages.go`, `service/copilot_gateway_responses.go` |
| Gemini | `service/gemini_session.go`, `service/gemini_messages_compat_service.go` |
| Antigravity | `service/antigravity_gateway_service.go` |
| Sora | `service/sora_gateway_service.go` |

### 数据库与 ORM

使用 **Ent** (`entgo.io/ent`)。Schema 定义在 `backend/ent/schema/`，生成代码在 `backend/ent/`。修改 schema 后执行 `go generate ./ent`。

### 测试策略

项目使用 build tags 区分测试类型：
- **unit** — 单元测试，无外部依赖（数据库/Redis），使用 mock
- **integration** — 集成测试，需要真实数据库/Redis
- **e2e** — 端到端测试，完整环境（通过 Docker Compose）

运行测试前确保相应环境已就绪（integration/e2e 需要 PostgreSQL + Redis）。

---

## 关键实现细节

### Copilot 支持模型列表维护

模型列表文档：[`docs/copilot-models.md`](docs/copilot-models.md)

**更新步骤（需要 Docker 运行中）：**

```bash
# 1. 从数据库取一个有效的 access_token
ACCESS_TOKEN=$(docker exec sub2api-postgres sh -c \
  "psql -U sub2api -d sub2api -t -c \
  \"SELECT credentials->>'access_token' FROM accounts WHERE platform='copilot' AND status='active' ORDER BY id DESC LIMIT 1;\"" \
  | tr -d ' \n')

# 2. 用 access_token 换取新的 Copilot API token
NEW_API_KEY=$(curl -s "https://api.github.com/copilot_internal/v2/token" \
  -H "Authorization: token $ACCESS_TOKEN" \
  -H "Accept: application/json" \
  -H "User-Agent: GitHubCopilotChat/0.26.7" \
  -H "Editor-Version: vscode/1.99.0" \
  -H "Editor-Plugin-Version: copilot-chat/0.26.7" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('token',''))")

# 3. 查询模型列表
curl -s "https://api.githubcopilot.com/models" \
  -H "Authorization: Bearer $NEW_API_KEY" \
  -H "Copilot-Integration-Id: vscode-chat" \
  -H "Accept: application/json" \
  -H "User-Agent: GitHubCopilotChat/0.26.7" \
  | python3 -c "
import sys, json
d = json.load(sys.stdin)
for m in d.get('data', []):
    caps = m.get('capabilities', {})
    limits = caps.get('limits', {})
    print(m['id'], caps.get('type',''), limits.get('max_context_window_tokens',''), limits.get('max_prompt_tokens',''))
"
```

查询结果对照 `docs/copilot-models.md` 手动更新，注意更新文件顶部的日期。

---

### Copilot 集成注意事项
- OAuth scope 必须为 `read:user`（不是 `user:email`）
- token 有效期约 30 分钟，`TokenRefreshService` 自动刷新
- Codex 系列模型走 `/responses` 端点 (`copilot_gateway_responses.go`)；其余走 `/chat/completions`
- `copilotShouldUseResponsesAPI(model)` — 模型名含 "codex" 时路由到 Responses API
- model 名后缀（如 `[1m]`）在 `message_start` 事件返回前通过 `stripModelSuffix()` 去除，避免 Claude Code 误判上下文窗口

### 前端静态资源嵌入
后端通过 `//go:embed` 将前端构建产物嵌入二进制。本地开发不需要 embed tag；发布/Docker 部署必须加 `-tags embed`。

### 配置文件
配置通过 Viper 加载，路径优先级：`./config.yaml` > `/etc/sub2api/config.yaml`。参考 `deploy/config.example.yaml`。

### 运行模式
- `standard`：完整功能模式（PostgreSQL + Redis），支持完整计费、限速、并发控制
- `simple`：简化模式，适合单机部署，**计费和配额检查被禁用**

### 版本管理
版本号定义在 `backend/cmd/server/VERSION` 文件中，编译时通过 `//go:embed` 嵌入二进制。CI 构建时可通过 `-ldflags` 注入额外信息（commit hash、构建时间等）。

---

## 部署

Docker Compose 文件位于 `deploy/`：
- `docker-compose.yml` — 标准部署（本地构建镜像 `sub2api:latest`，不是直接拉远程应用镜像）
- `docker-compose.standalone.yml` — 单机一体化部署
- `docker-compose.dev.yml` — 本地开发辅助

```bash
# 快速部署
cd deploy && bash install.sh
```

### 更新服务器上 Docker 中的 sub2api 服务

当前仓库的标准部署使用 `deploy/docker-compose.yml`，其中应用服务镜像为本地构建的 `sub2api:latest`，并设置了 `pull_policy: never`。因此更新代码后，正确流程是重新构建镜像，再用 Compose 重建容器，而不是只执行 `docker compose pull`。

```bash
# 1. 更新主干代码
cd ~/sub2api
git checkout main
git pull origin main

# 2. 重新构建应用镜像（会同时构建前端并嵌入后端）
docker build -t sub2api:latest -f deploy/Dockerfile .

# 3. 重建并启动服务
cd deploy
docker compose up -d

# 4. 查看服务日志
docker compose logs -f --tail=100 sub2api
```

注意：
- 不要仅执行 `docker compose pull` 来更新 `deploy/docker-compose.yml` 中的应用服务；该文件不会从远程拉取 `sub2api:latest`
- 不要执行 `docker compose down -v`，否则会删除 PostgreSQL / Redis 数据卷
- 如果实际使用的是 `docker-compose.local.yml` 或 `docker-compose.standalone.yml`（应用镜像为 `weishaw/sub2api:latest`），才使用 `docker compose pull && docker compose up -d` 的更新方式

---

## 已知问题 / 开发笔记

### Copilot 上下文窗口与 autoCompact 问题

**现象：** 使用 Copilot 账号时，上下文达到约 13% 就收到 "prompt is too long" 并中断。

**根因：** Claude Code 从 model 名 `claude-sonnet-4-6[1m]` 中的 `[1m]` 后缀推断上下文窗口为 1M，而 Copilot 实际 `max_prompt_tokens` 为 128K，两者严重不一致。Claude Code 2.1.92 的 reactive compact service 未激活，收到错误后直接退出。

**已实施修复：**
- 后端 `copilot_gateway_messages.go` 新增 `stripModelSuffix()`，在 `message_start` 事件里去掉 model 名的 OMC 后缀（如 `[1m]`），Claude Code 改用 200K 窗口计算
- `~/.claude.json` 中设置 `autoCompactWindow: 155000`，使触发阈值约为 110K（距 Copilot 128K 限制留 18K 余量）

**各 Copilot Claude 模型实际可用 prompt tokens：均为 128,000**（`max_context_window` 显示 200K，但 `max_prompt_tokens` 为 128K）

**切换回 Anthropic 原生 API 时**，需删除 `~/.claude.json` 中的 `autoCompactWindow` 字段。

---

### Copilot OAuth 集成（已完成）

- Device Flow OAuth，scope 必须为 `read:user`（历史教训：用 `user:email` 会导致 token 无法刷新）
- 前端入口：CreateAccountModal.vue → `/api/v1/auth/oauth/copilot/start` + `/complete`
- Token 有效期约 30 分钟，`TokenRefreshService` 自动后台刷新
- `isNonRetryableRefreshError` 已移除 `"unauthorized:"` 条目，401 走重试→临时不可调度流程（而非永久封号）
- 受影响旧账号恢复：`UPDATE accounts SET status='active', error_message='' WHERE id=<id>;`
