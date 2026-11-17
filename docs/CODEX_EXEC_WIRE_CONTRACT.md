# Codex exec 0.158.0 HTTP 出站约定

用户确认的完整范围见 [需求记录](CODEX_EXEC_EGRESS_REQUIREMENTS.md)。本次基准与实际出站共同固定为官方 `codex exec 0.158.0`、Linux x86_64，参考运行环境为 Alpine Linux 3.24.4。后台版本自动同步、客户端 UA、账号自定义身份与旧传输开关不能改变目标通道的这个基准。

## 适用范围

普通 HTTP 入站的 Responses、Chat Completions、Messages、透传及 compact，最终连接 Codex 账号协议的 OAuth、PAT、SetupToken。整个 WebSocket 入站链路（含 WS→HTTP 桥接）、OpenAI API Key 和第三方上游继续沿用旧行为。目标通道取消 `codex_cli_only` 身份门槛；正常鉴权、额度和分组权限保留。

## 正文与调用方语义

| 项目 | 普通 Responses | Responses Lite |
| --- | --- | --- |
| 调用方的顶层基础指令 | 非空时保留 `instructions` | 转为带 base-instructions 标记的 developer message |
| 原 system 消息 | 原位变为 developer，内容保留 | 同左 |
| 工具声明 | 顶层 `tools`，空集合为 `[]` | 一个 input 前缀 `additional_tools`，保留 namespace 与工具定义；空集合也有载体 |
| parallel_tool_calls | 使用请求值，未声明时采用 exec 默认 true | false |
| reasoning.context | 保留受支持设置 | all_turns |
| store / stream | false / true | false / true |
| include | 获取 reasoning.encrypted_content，保留其他受支持声明 | 同左 |

模式按最终账号、最终模型的已知能力选择。持久化账号模型信息优先；否则复用已知模型默认。能力未知时沿用入站声明，未声明则普通模式，并记录 `alignment_status=unverified`。已知模式记录 `baseline_selected`，这不代表完成实包验收。推理热路径不联网刷新模型目录。

不注入官方 Codex 提示词或工具集，不使用强制模板、todo guard、Spark 限制提示或自动图像桥接，也不从历史自动补搜索工具。Chat/Messages 采用保留内容的转换；不支持的内容类型、工具语义或歧义明确返回 400。工具别名与关联 ID 只能进行语义等价的往返转换，不能把 tool_choice 放宽为 auto。

Codex 无法兑现的长度上限、采样和停止设置按用户决定忽略，入口日志只记录参数名；最终报文仍按固定基准构造。模型默认 effort/verbosity 有证据时才补齐，未知模型不臆造这些默认值。

重试使用实际发出的、压缩前的正文定位字段；不会因上游拒绝而删除 reasoning、改变工具 namespace 或静默截断历史。作用域化与生成的会话标识在正文和请求头中保持关联，原始入站请求不受账号切换影响。非 Codex 的任意会话字符串在既有账号/用户作用域化后转换为 UUIDv7；这种合成映射在同一服务进程内稳定，重启可能改变合成会话标识，原生 UUIDv7 不受影响。

## compact 兼容入口

官方 0.158.0 的 OpenAI 路径只支持 Responses V2 压缩。`features.remote_compaction_v2=false` 已被移除并忽略；[源码与二进制实测](../backend/internal/service/testdata/codex_exec_0158_linux_amd64/legacy-compact-check/README.md)证明不会请求旧 `/responses/compact` 上游端点。

因此本地 `/v1/responses/compact` 是兼容入口：保留输入与工具，在末尾加入 `compaction_trigger`，使用最终模型的普通/Lite 布局，经 `/backend-api/codex/responses` 发起 SSE 请求，并将完整 compaction 输出还原为客户端需要的 unary `response.compaction` JSON。请求 metadata 的 request_kind 为 compaction。其他上游和 WS 桥接的旧 compact 行为不变。

## 身份、头序与传输

固定 UA 为：

```text
codex_exec/0.158.0 (Alpine Linux 3.24.4; x86_64) unknown (codex_exec; 0.158.0)
```

`originator=codex_exec`、`version=0.158.0`。主 Responses 和 V2 压缩共用采集确认的 HTTP/1.1 头序。TLS 使用采集确认的原生 Linux ClientHello（无 ALPN/GREASE，JA3 `0b85eb0d4981e69064e40753e4f0ac5f`）。

目标 HTTP 请求强制使用固定 profile，忽略账号级关闭/自定义 profile。直连、HTTP CONNECT、SOCKS5 使用这一路径；现有 HTTPS 代理回退和不能证明保持该约定的插件明确返回配置不兼容，禁止静默换成普通 Go TLS。共享 WS 路径不受此标记影响。

请求使用 zstd 流式帧：2 MiB 窗口、无内容长度字段、无字典、无校验和。验收比较这些格式特征和解压后正文，不要求压缩字节或分块完全相同。

## 证据与验证

[Linux amd64 基准](../backend/internal/service/testdata/codex_exec_0158_linux_amd64/README.md)保存校验过发行摘要的原始二进制采集：标准/Lite/V2 压缩完整正文、有序头、原始压缩数据、独立原生 TLS ClientHello、固定源码引用与逐文件摘要。合成 OAuth 通过自建 CA 采集 HTTP；该层的 TLS 不被当作官方原生 TLS。两层证据不宣称是同一次完整直连抓包。

旧 `codex_exec_0158` arm64 样本保留为历史正文回归，当前运行身份以新的 x86_64 证据为准。fixture 内的官方 agent 内容只用于回归，不是网关注入模板。

核心回归：`TestForward_CodexExec0158LinuxAMD64WireLayouts`、`TestCodexHTTPOrderedHeadersMatch0158Reference`、`TestCodexHTTPZstdMatchesOfficial0158Frame`、`TestCodex0158LinuxProfileMatchesNativeReference`、`TestCodexHTTPEgress_*`，以及 Chat/Messages 的内容与 reasoning 往返测试。

真实验证通过独立 `codexlive` 测试程序复用实际可用账号，临时运行，不部署应用、不写数据库；私有配置不进入仓库。PAT/SetupToken 没有真实账号时仅报告自动化覆盖。未知模型模式和未实测认证类型不会被计为已验证。

目标通道对返回的工具名保留调用方语义，不执行旧的 read_file→read 等客户端专用改名。非流式调用即使遇到上游 SSE 正文与 Content-Type 不符，也按正文识别并返回 JSON；缺失完成事件明确报错。
