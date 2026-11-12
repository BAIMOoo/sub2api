# Codex exec 请求对齐差异汇总（2026-11-12）

## 对齐范围

- 基准客户端：官方 Codex CLI 的非交互子命令 `codex exec`。
- 对齐分支：HTTP；WebSocket 是明确的未覆盖边界。
- 对比方向：`codex exec -> 官方上游` 与 `codex exec -> Sub2API -> 官方上游`。
- 不在 live 采样范围内：Codex Desktop、TUI、IDE 插件和 WebSocket；其中可识别的 Codex HTTP harness 由完整 Forward 回归验证会归一化为 `codex_exec` 出站形态。
- 生产来源：`b18f7ff606a61ac87f1db1597154af845e6593f1`。
- 最近生产检测：`20261112T010000Z-88612991`。

## 当前生产差异

| 场景 | 结果 | 差异 |
| --- | --- | --- |
| Codex exec 0.158.0 / Lite / 普通问答 | 严格检测发现差异 | `input[1..6]` 的内部消息元数据缺失；旧报告归一化后显示通过 |
| Codex exec 0.158.0 / Lite / 工具续轮 | 严格检测发现差异 | 多个 message/tool item（`input[1..10]` 范围）的内部消息元数据缺失；续轮另有 `input[7].id` 缺失 |
| Codex exec 0.158.0 / Lite / 压缩续轮 | 严格检测发现差异 | 多个 message/tool item（`input[1..10]` 范围）的内部消息元数据缺失；续轮另有 `input[7].id` 缺失 |
| Codex exec 0.167.2 / Lite / 普通问答 | 严格检测发现差异 | `input[1..6]` 的内部消息元数据缺失；旧报告归一化后显示通过 |
| Codex exec 0.167.2 / Lite / 工具续轮 | 证据不足 | 官方请求采集或响应未完整完成 |
| Codex exec 0.167.2 / Lite / 压缩续轮 | 严格检测发现差异 | 多个 message/tool item（`input[1..10]` 范围）的内部消息元数据缺失；续轮另有 `input[7].id` 缺失 |
| Responses / Chat Completions / Messages 兼容接口 | 通过 | 只验证语义和响应完成，不等同于 codex exec 正文一致 |

生产请求本身没有失败：本轮生产重放 `10/10` 完成，兼容接口 `3/3` 通过。报告中的“差异”表示请求结构不完全一致，不表示模型请求失败。

- 可比请求头顺序：已比较 10 次，未发现差异。
- 请求压缩方式：已比较 10 次，未发现差异。
- 单独测量的 TLS 特征：已比较 10 次，未发现差异。
- 已确认的正文差异一：所有可比原生请求中的 `internal_chat_message_metadata_passthrough` 被生产请求删除。
- 已确认的正文差异二：工具或压缩续轮中的 `reasoning.id` 被生产请求删除。
- 旧监控归一化器会隐藏第一类差异，因此旧报告的“普通问答通过”不等于正文一致。

## 差异原因

官方 `codex exec` 在工具或压缩后的续轮中携带 `reasoning` 条目：

- 保留 `rs_*` 形式的 `id`。
- 保留 `encrypted_content` 和 `summary`。

当前生产运行时对原生 HTTP 请求仍有旧的 OAuth 兼容处理：

- `reasoning` 条目会保留加密上下文，但删除 `id`。
- 输入条目的 `internal_chat_message_metadata_passthrough` 会被删除。

这些逻辑早于本次监控，线上应用容器自 2026-10-18 启动后没有更换。

原生元数据保留依据：2026-11-12 运行 `20261112T010000Z-88612991` 的官方 OAuth
`codex exec` 请求实际携带该字段并完成上游请求。该结论不扩展到 TUI、
Desktop、IDE 或非原生兼容路径。

这个分支只在官方响应实际产生带 `id` 的 `reasoning` 输出时才会暴露，所以早期没有该条目的定时样本会通过，后来出现该条目后才持续报告 `input[7].id:missing`。

## 本地修复

原生 `codex exec` HTTP 路径现在逐字段保留 `reasoning` 条目，包括 `id`、`encrypted_content`、`summary`、`content`、内部消息元数据和其他未知字段。非原生兼容入口仍保留旧兼容行为。

监控同时增加两项约束：

1. 正文差异附带安全的条目类型和字段存在性，例如标记为 `reasoning.id：仅官方 codex exec 有`，不保存请求值；内部消息元数据也不再做归一化隐藏。
2. 工具和压缩场景如果没有观察到同时带 `id` 与 `encrypted_content` 的 `reasoning` 续轮，状态改为 `unverified`，不再静默通过。
3. `input` 条目数量不一致时只报告 count/length 差异，不定位具体是哪一条，由后续开发人员根据原始脱敏证据排查。

## 仍未验证

- 当前生产尚未部署本修复，生产检测仍会继续报告旧的 `reasoning.id` 差异。
- 标准模式没有可用的线上模型。
- WebSocket 不在当前检测范围。
- Codex Desktop、TUI 和 IDE 插件没有独立基准。
