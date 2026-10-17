# sub2api 脱离 AWS：完成记录

北京时间 2026-10-17 **21:57:57**，AWS 迁移入口和控制通道已停止并禁用。随后在通道关闭状态下，腾讯公网网页、真实模型流及对应计费记录全部通过验证。

当前路径为 `客户端 → api.b4im.com → 腾讯 43.165.182.52 → 腾讯本机应用 / PostgreSQL / Redis`。Cloudflare 仅提供 DNS，A 记录为 `43.165.182.52`、TTL 60、proxied=false；业务请求不经过 AWS。

## 本次完成的操作

1. 从国内 Mac 无代理验证腾讯新 IP 的 HTTPS、真实 Chrome 首页/登录页、初始化接口和模型 SSE；确认腾讯仍为唯一数据权威。
2. 只执行一次 DNS content PATCH，将临时 AWS 入口改为腾讯新 IP，其余记录属性保持。Cloudflare、Google、阿里公共 DNS 读回新 IP。
3. 发现 macOS 系统解析仍缓存 AWS，而公共 DNS、路由器和新的 Chrome 已返回腾讯。普通用户缓存清理未奏效；用户执行管理员级 DNS 刷新后，系统解析及普通无代理 curl 均直连腾讯。没有重启代理、修改系统 hosts 或断开连接。
4. 等待 AWS 桥上连接自然结束；最终三个空闲样本间隔约 33.30 / 36.15 秒，总观察约 69.45 秒。新鲜腾讯运行状态和普通域名 HTTPS 证明通过后，执行唯一一次入口退役。
5. 停止并禁用 AWS 的 `sub2api-migration-recovery-20261016.service`、`sub2api-migration-runner-20261016.service`、`sub2api-migration-channel-20261016.service`；仅删除一条专用入口 NAT 和两条专用 Redis 迁移防火墙规则，逐步比对其余规则未变。
6. 将余额通知的充值 URL 从旧 AWS HTTP 地址改为 `https://api.b4im.com`，保留完整 path/query/fragment。腾讯主库按原值、行 ID、xmin 做单行 CAS，提交后独立读回；未重启应用、未发送通知。

## 通道关闭后的证据

| 验证项 | 结果 |
| --- | --- |
| 公共入口 | Cloudflare A → `43.165.182.52`，TTL60，proxied=false |
| 普通 DNS、无代理真实 Chrome | 首页/登录页、JS/CSS、`/setup/status`、`/api/v1/settings/public` 均 200；所有站点响应对端为腾讯，无失败请求 |
| 真实模型 SSE | 腾讯 TLS/SNI/证书验证通过，HTTP200，13 个事件、`response.completed`、正常 EOF |
| 模型响应时间 | 首文本约 1.613 秒，完整 EOF 约 1.665 秒；这是验收探针时间，不是原数据交接暂停时间 |
| 精确请求计费 | input24 / output8；usage 和 billing_dedup 各一条，费用 USD0.00002560 |
| PostgreSQL / Redis | TENCENT / TENCENT；PG system ID `7626048206782038050`、timeline2、主库；运行和持久端点均 target/target |
| 腾讯应用和后台 | instance `r32ufr8h00vvbf`、PID534792；ready/serving，32 项后台启用 |
| 腾讯回源残留 | 15432/16379 旧反向监听消失；只读全 TCP 快照中没有 AWS IP 连接 |
| 腾讯 keeper / 入口 | PID1198987 / PID259990，保持运行；keeper enabled，入口由 keeper 排序管理，独立 unit 仍 disabled |
| AWS 迁移服务 | 三个 unit 均 inactive、disabled、PID0；相关 cgroup 已空 |
| AWS 无关服务 | Caddy PID1284574、Codex Relay PID1307343 保持运行，没有重启或重载 |
| AWS 旧业务 | 应用/PG/Redis stopped、restart=no；exit0 / 0 / 127 原事实不变 |

精确 dedup 行与业务扣费使用同一事务；该探针不存在独立逐请求余额账本行，未用并发共享余额变化归因该次扣费。

## AWS 保留范围与恢复边界

AWS 实例及共享 Caddy 继续承载 Codex Relay，与 sub2api 业务运行独立。旧业务容器、卷、历史备份和迁移证据继续保留至少七天，不自动删除。共享 Caddy 的旧 sub2api 站点配置仍保留为历史配置，已无可用迁移 upstream，也不再是服务入口；不能将 DNS 指回 AWS，或启动过时的旧数据库。

如需业务回迁，须以腾讯最新数据为权威重新同步、演练、排空和交接。已执行的 DNS、迁移、恢复、备份和本次退役 one-shot 均不得重跑；失败或结果不确定时先读证据，不重发写操作。迁移专用受限 SSH 公钥及历史程序保留，不代表存在运行中的跨云连接。

## 历史失败与未验证项

- 原数据交接新增等待 **16.38194985128939 秒**，超过 10 秒目标。原状态 `COMPLETE_WITH_WAIT_BREACH` 及累计保守暂停历史 46.66268306039274 秒不改；本次脱离 AWS 成功不抵销该失败。
- 网络诊断会话在更换旧公网 IP 后恢复了国内直连。其回执记录 12:54:48Z 首次观察失败、13:14:20Z 恢复，约 **19 分 32 秒**。换 IP 后恢复支持地址/路径相关，不能单独证明旧 IP 被封禁或指定丢弃设备。该事件原始证据继续保留。
- 本次早期人工只读观测出现本地格式化异常，未改变生产；首次通道关闭后目标观测只保留通用失败结果，具体异常未确定。随后独立只读核验成功，原失败文件没有覆盖。
- 应用 Docker healthcheck 仍访问旧端口 8080，因此显示 unhealthy；实际业务端口为 8083，运行控制和公网验证正常。本次没有为修复这个陈旧探针而重建或重启应用。
- 生产冷启动和最终备份完整恢复仍未实测；现有 PG、Redis、文件备份分别取样，不是跨服务原子快照，也不能代替持续备份。
- 账号凭据中发现一个包含旧 IP 的不透明 API Key 字段，不是已识别的路由 URL；未修改或公开凭据。代理配置无旧 IP 引用，设置表中的旧 IP URL 已清除。

## 证据与工具

本机私密目录 `~/.codex/backups/sub2api-aws-independent-entry-20261017`：

- `dns/`、`final-dns-readback.json`：唯一 DNS 写入和最终读回。
- `retirement-orchestrator-intent.json`、`retirement-source.json`：新鲜目标证明及 AWS 退役结果。
- `canary-before-dns.json`、`canary-after-aws-stop.json`：两次不同阶段的真实模型探针，每阶段只执行一次。
- `web-after-aws-stop.json`：普通 DNS、无代理真实浏览器结果。
- `target-after-retirement-confirmed.json`：腾讯端点、进程、回源端口及 AWS socket 核验；首次失败另存 `target-after-retirement.json`。
- `recharge-link.json`：充值链接安全回执；原值仅在腾讯 root 私有目录。

AWS root 私密目录：`/var/lib/sub2api-migration-20261016/retire-aws-entry-20261017`，保存全部样本、before、intent、phase 和 result。腾讯充值链接证据位于同一迁移根下 `recharge-link-independent-20261017`。旧公网 IP 更换记录位于本机 `sub2api-tencent-direct-network-20261017/ip-change-result-20261017T1314Z`。

本次新增 DNS 安全测试 16 项、退役安全测试 21 项、充值链接安全测试 15 项。完整迁移 Python 套件 **1178 项，1176 通过、2 跳过、0 失败**。新增脚本默认离线；实际生产验证和故障测试证据分别保留。
