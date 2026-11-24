# BAIMOoo Sub2API Fork 合约

最后确认日期：2026-11-24。

本文记录 fork 所有者已经确认的意图，供新会话、评审和上游合并使用。它有意与实现状态分开：写在这里的需求，不代表 `main` 已经实现。

当新请求与本合约冲突时，只有把变更记录在这里，才能将该请求视为所有者的新决策。上游默认值、提交标题或相同的 `VERSION`，都不能覆盖所有者已确认的决策。

## 核心产品需求

对于 Codex 和 ChatGPT 账号通道，任意调用方都应能通过直接 HTTP 入口提交自己的 agent 任务。Sub2API 必须保留调用方的任务、指令、历史、推理状态、工具和工具结果，再发出固定的官方 `codex exec 0.171.0` HTTP 出站约定：

```text
任意 agent harness
  -> Sub2API 直接 HTTP
  -> Codex 和 ChatGPT 账号通道
  -> codex_exec 0.171.0 / Linux x86_64
```

调用方无需自报为 Codex。目标是统一**出站模型请求**，而不是用 Codex 工作区替换调用方的提示词或工具体系。

## 稳定决策

| 范围 | 所有者已确认的需求 |
| --- | --- |
| 入站范围 | 直接 HTTP 的 Responses、Chat Completions、Messages、透传和 compact，包括未被识别的 agent harness，以及没有 Codex User-Agent 的调用方。 |
| 上游范围 | Codex 和 ChatGPT 账号协议：OAuth、PAT 和 SetupToken。直连 OpenAI API Key 与第三方上游继续使用各自约定。 |
| 客户端身份 | 不再将 Codex UA 或 `codex_cli_only` 作为准入条件。保留正常的 API Key 鉴权、额度、账号、分组与权限检查。 |
| 出站身份 | 由服务端决定目标身份。本合约将 HTTP 出站固定为 `codex exec 0.171.0 / Linux x86_64`（Pinned Egress Version）；入站版本、账号手动身份、模型元数据与自动版本发现都不能改变它。 |
| 版本滞后门禁 | Pinned Egress Version 必须由人工显式批准静态升级，同时通过自动化单测与离线对齐 CI 硬门禁强制约束其始终不低于仓内所有内置 Codex 模型清单声明的 Model Minimal Client Version。 |
| 400 防误杀 | 明示客户端版本过旧/不支持的 Codex 上游 `400` 不得触发 30 分钟 per-model 冷却或切号重试；在直接 HTTP 固定出站模式下，当账号已同步元数据或内置模型清单声明的 Model Minimal Client Version 已高于当前 Pinned Egress Version 时，即使 `400` 正文命中套餐受限短语也跳过冷却与切号并记录结构化告警日志，防止号池被误冷却并放大为 `429`。真实套餐受限 `400` 保留原有冷却与切号行为。 |
| 任务语义 | 保留指令、历史、受支持的推理字段、内部元数据、工具声明、工具参数与结果、ID 以及跨轮引用。不得静默删除或截短任务内容；无法无损转换时必须返回明确的不兼容错误。 |
| 生成控制 | 不支持的长度、采样、温度或停止控制可以忽略，但必须记录字段名。该例外不能用于修改提示词、历史、工具或结果。 |
| Standard / Lite | 根据最终账号和模型能力选择，并严格对齐 `codex exec 0.171.0` 正文布局：Standard 模式省略顶层 `instructions`（非空指令作为 `input` 开头的 `developer` message，保留顶层 `tools`），Lite 模式无工具声明时不插入空的 `additional_tools` 载体。能力未知时保留请求声明的模式，未声明则默认 Standard，并记录 `alignment_status=unverified`。结构性回退不代表通过对齐。 |
| 传输 | 校验固定身份、有序 HTTP 头（严格以 `0.171.0` Linux x86_64 实采结果为准；保持 `DisableCompression` 防止 Go 隐式注入 `Accept-Encoding: gzip`，并通过既有响应解压层安全处理 `gzip`/`zstd` 流式 SSE 与 4xx/5xx 错误体）、正文布局、zstd 格式与解压后内容，以及官方原生 TLS 指纹。已知传输不兼容时必须失败关闭，不得静默回退到普通 Go TLS、其他版本或插件传输。 |
| 证据 | 接受分层证据：原生 TLS 可与 HTTP、正文、压缩分开采集。必须记录来源和边界，不得将两次独立采集声称为一次完整原生交易。证据缺失只能是 `unverified`，不能是 `pass`。 |
| 真实账号 | 对每种实际可用的凭据类型做真实验证。OAuth、PAT、SetupToken 仍是必需实现范围；暂时没有可用账号的类型必须自动化测试并标记为未实测。 |
| WebSocket | 完整 WebSocket 入站链路（包括 WS→HTTP 桥接）与上游编译期兜底版本常量（`0.146.0`）使用上游行为。fork 的 Codex HTTP 身份、Cookie、TLS profile 和正文传输不得泄漏进这条链路。上游自身的 WS 问题等待上游修复。 |
| compact | 在保留兼容 HTTP 入口的同时使用官方 `0.171.0` 出站行为（`remote_compaction_v2` + `compaction_trigger`）。不得假设旧 `/responses/compact` 上游端点仍然存在。 |

### 2026-11-19 与 2026-11-24 评审决策

| 问题 | 最终决策 |
| --- | --- |
| 固定出站范围 | 所有 Codex/ChatGPT 账号通道的直接 HTTP 入口都统一使用官方 `codex_exec 0.171.0` 契约，不保留 Responses-only 例外。 |
| `codex_cli_only` | fork 可以移除 Codex/ChatGPT 直接 HTTP 路径上的 `codex_cli_only` 准入逻辑，任何 agent harness 都不再因 UA/originator 被拒绝；正常 API Key、额度、分组及权限校验仍保留。 |
| 传输策略 | Codex/ChatGPT 直接 HTTP 强制使用固定 TLS 指纹、实采 HTTP 头序（含 `0.171.0` 实采确认的 `accept-encoding` 策略，同时保持 `DisableCompression` 禁用 Go 隐式 gzip）、zstd 和相关传输规则。账号旧覆盖、关闭开关或插件只要不能证明保持该契约，就明确返回配置不兼容错误，不得静默回退。 |
| Standard / Lite | 由 Sub2API 根据最终账号和模型能力自动选择请求排版并对齐 `0.171.0` 正文布局（Standard 省略顶层 `instructions` 并将非空指令置于 `input` 开头 `developer` message、保留顶层 `tools`；Lite 无工具时不插入空 `additional_tools` 载体）。能力未知时沿用已有声明或默认 Standard 继续发送，并记录 `alignment_status=unverified`；不得将该结构性回退视为对齐通过。 |
| 版本滞后门禁与 400 防误杀 | Pinned Egress Version 维持人工静态批准升级，不允许运行时根据模型元数据或版本自动发现静默抬升；新增单测与离线对齐 CI 硬门禁阻断 Pinned Egress Version 低于仓内任一内置模型 `minimal_client_version`。同时对明示客户端版本过旧的 `400` 及固定直接 HTTP 下已知版本滞后的套餐短语 `400` 免除 30 分钟 per-model 冷却与切号，防止全池误冷却放大成 `429`。 |
| compact | 保留客户端 `/v1/responses/compact` 兼容入口，但最终出站只使用官方 `codex exec 0.171.0` 支持的远程压缩流程；不得依赖或假设旧 `/responses/compact` 上游端点仍然存在。 |
| 内容注入 | Codex/ChatGPT 直接 HTTP 路径删除强制提示模板、todo guard 及其他服务端自动追加的任务内容或工具；最终只转换并发送调用方明确提交的语义。 |
| 无损转换 | 任务内容、工具参数及跨轮 ID 无法无损转换时必须返回明确的 400 不兼容错误。只有 Codex 不支持的长度、采样、温度、停止词等生成控制可忽略并记录字段名。 |
| WebSocket 边界 | 完整客户端 WS 入口（包括 WS→HTTP 桥接）与上游编译期兜底版本常量（`0.146.0`）全部使用上游实现和上游问题策略；固定 HTTP 出站逻辑、HTTP 账号 Cookie、TLS profile 及正文处理不得进入 WS 链路。 |
| 认证类型验收 | OAuth、PAT、SetupToken 均属于必需实现范围。真实验收覆盖当前实际可用的凭据类型；没有真实账号的类型保留自动化测试，并在报告中明确标记“未实测”，不得声称已完成真实验证。 |
| 证据边界 | CI、单元测试、离线 HTTP 对齐与独立原生 TLS ClientHello 采集通过只证明各自检查通过；真实供应商、生产数据库升级（保留 `schema_migrations=306`）、部署验收必须分别取得独立证据，不得用其中一种结果替代其他结论。 |
| 三节点发版与监控切换 | 严禁覆写或重打已发布的 `v0.2.24` tag；合入 `main`、发布新版本 tag（如 `v0.2.25`）与停机从 `v0.2.23`（`schema_migrations=306`）升级生产为三个独立人工批准节点。宿主机私有 `codex-alignment` 配置中的 Approved Alignment Baseline 仅在生产部署验收通过后显式切换至 `0.171.0`。 |
| 扣费合并队列 | 该资源安全问题已确认来自固定上游实现 `2af3f1e15`（上游提交 `2101f4f94`），当前 fork `main` 未做额外改动。按上游问题处理，等待上游修复，不在 fork 中单独改；合入上游修复时必须重新验证有界队列、过载行为、关闭流程和并发安全。此决策只说明归属和处理顺序，不表示风险已解决。 |

## 其他 Fork 不变量

1. **账号隔离：** 按账号隔离上游 Cookie、连接状态、会话值、模型模式、重试和故障切换。重连或重启后，也不得让一个账号复用另一个账号的状态。
2. **Codex 监控：** 保留严格的正文、头、会话和 TLS 漂移检测、脱敏、隐私限制、证据保留以及中文可读报告。监控只报告结果，不得自动合并、部署或批准基线。
3. **更新与回滚来源：** 在线更新和回滚版本查询必须指向 `BAIMOoo/sub2api`，不得静默切换到 `Wei-Shaw/sub2api`。
4. **安全下限：** 吸收上游依赖时保留 fork 已提高的依赖最低版本和审计策略。不得通过放宽 fork 安全检查来解决上游冲突。
5. **Desktop 上下文：** 保留首轮、多页分片、历史与工具结果语义。只有在同时满足 fork 和上游用例后才选一种实现，不得保留两套转换器。
6. **数据库升级：** 保留已安装 fork 数据库中的存储设置和 fork 自有迁移。尤其是迁移 241 仍归 fork 所有；现有 Gemini 或 Antigravity 分组显式保存的生图开关为准，不得批量开启原本关闭的分组。
7. **上游例外：** 新建分组的生图默认值问题归上游，允许等待上游修复。没有新的所有者决策，不在 fork 中单独改变。
8. **运维规模：** 当前小规模服务允许直接停机更新。除非用户要求，不增加请求排空、HA、双实例或零停机机制。合入 `main`、发布版本和部署是三个独立批准节点。

## 上游合并规则

- 合并前固定 fork 与上游的完整 SHA。版本号相同不能证明两棵树等价。
- 在候选分支合并，并以 fork 为第一父提交。不得 squash 掉上游祖先，也不得强制更新 `main`。
- 审查双方都修改过的每个路径，包括没有文本冲突的重叠文件。Git 冲突数本身不是风险评估。
- 按可观察约定保留 fork 行为，不机械地一律选 `ours`；只在等价测试证明约定成立后采用上游实现。
- 同时运行 fork 回归与上游回归，不得通过弱化或改写旧断言让候选通过。
- 使用相同的确定性输入、模型能力数据、时钟、价格表、数据库起点和 mock，对比旧 fork 与候选。
- 在已安装 fork 数据库的副本上演练数据库升级，不能只测试空库。保留配置、账单证据及回滚边界。
- 只有实际通过验证的准确候选 SHA 才有资格进入 `main`。后续任何新提交或目标变化，都必须重跑受影响的验证。
- CI 和离线对齐通过，只证明这些检查通过。真实供应商、部署或生产数据库结论必须另有证据。

## 实现状态

本节仅记录状态，不构成需求。

| 项目 | 2026-11-24 状态 |
| --- | --- |
| 当前 `main` | 已获所有者批准合入固定直接 HTTP `codex_exec 0.171.0 / Linux x86_64` 出站、版本滞后门禁与 400 防误杀（来源分支 `codex/egress-baseline-0171` @ `e3fa0e805`，已通过全套远端 CI 与 24/24 真机 OAuth 实测）；保留历史 tag `v0.2.24`（`2950874a3`）不变，进入新版本 `v0.2.25` 发布流程。 |
| 当前生产运行态 | 因旧 `v0.2.24` 的 `0.158.0` 低于 `gpt-6.2-sol` / `gpt-6.2-astra` 要求的 `minimal_client_version: 0.166.0` 且 `400` 触发 30 分钟号池冷却放大为 `429`，生产仍维持临时回退的 `v0.2.23`（PostgreSQL `schema_migrations=306`），等待所有者单独批准停机升级。 |
| fork 更新源、监控、Cookie、HTTP 回归、Desktop 覆盖与依赖加固 | 已在 `main`；后续合并继续保护。 |
| 宿主机私有 `codex-alignment` Approved Alignment Baseline | 保持原有 `0.158.0` 配置，待 `v0.2.25` 生产部署验收通过后再由所有者显式切换至 `0.171.0`。 |

只有实现真正集成，且最终 `main` 的同一 SHA 已通过相关检查，才更新上表。

## 新会话检查清单

1. 在请所有者重复说明 fork 约定前，先读取本文与根目录 [GLOSSARY.md](GLOSSARY.md)。
2. 判断请求路径：直接 HTTP、WebSocket 或其他供应商，然后应用对应决策。
3. 对比需求与当前 Git 状态，明确区分“目标是什么”“`main` 已有什么”“生产实际运行什么”“哪些只在候选分支上”。
4. 处理上游冲突前，先列出必须保护的行为及其测试或证据，再修改文件。
5. 只有实际行为确实不同于上述稳定决策时才询问新决策；确认后及时记录在这里。

## 来源

所有者决策来自以下确认记录：

- [更新规划与 Codex 出站需求澄清](codex://threads/01a1e760-b850-7160-b314-5473afc69b68)
- [上游合并风险与 fork 保留决策](codex://threads/01a1ed84-6182-7f70-8e2f-f01120984e3b)
- 固定出站实现分支 `codex/codex-exec-egress` 与 `codex/fork-contract-codex-http-egress`（已进入 `v0.2.24`）
- [BAIMOoo/sub2api#13](https://github.com/BAIMOoo/sub2api/issues/13)（Spec: upgrade fixed Codex HTTP egress baseline to 0.171.0）

术语定义见根目录 [GLOSSARY.md](GLOSSARY.md)；技术报文细节与监控操作继续分别记录在 `docs/CODEX_EXEC_WIRE_CONTRACT.md`、`docs/CODEX_ALIGNMENT_MONITOR.md` 和 `docs/CODEX_ALIGNMENT_RUNBOOK.md`。
