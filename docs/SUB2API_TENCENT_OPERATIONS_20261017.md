# 腾讯东京迁移后的运维交接

应用、数据和公网入口均已在腾讯运行，32 项后台已启用，运行守护已 active/enabled。**2026-10-17 北京时间 21:57:57 已停止并禁用 AWS 迁移通道；sub2api 运行不再依赖该 AWS 服务器。** 域名、API Key 和业务基线保留；腾讯当前公网 IP 为 `43.165.182.52`。完成证据见 [脱离 AWS 记录](SUB2API_AWS_INDEPENDENCE_20261017.md)。

本次切换的新增等待窗口实测 **16.38194985128939 秒**，超过约定 10 秒；状态为 `COMPLETE_WITH_WAIT_BREACH`，不能将本次迁移描述为全部无中断目标验收通过。后续真实模型流、精确计费及当前服务健康验证已通过，详情以 `SUB2API_MIGRATION_LIVE_20261016.md` 为准。

## 运行位置与入口

| 项目 | 位置或约定 |
| --- | --- |
| 用户入口 | `https://api.b4im.com` → 腾讯 `43.165.182.52`；A 记录 TTL60、proxied=false，客户端直接连接腾讯 |
| 腾讯服务器 | `43.165.182.52`，旧公网 IP `43.165.177.136` 已更换；同一台实例 |
| 业务基线 | `0.2.14` / `c69fdbeb`，加必要的迁移兼容补丁 |
| 活跃应用容器 | `sub2api-migration-app-20261016`，迁移时接管的新实例 |
| 应用业务端口 | 腾讯本机 `8083`；入口使用受限规则将新连接从 `8082` 转到该端口 |
| PostgreSQL / Redis | 交接后使用腾讯本机 `25432` / `26379`；不向公网开放 |
| 入口服务 | `sub2api-tencent-entry.service` |
| 运行守护 | `sub2api-tencent-keeper-20261016.service`，已 active/enabled |
| 受限状态与凭据目录 | `/var/lib/sub2api-migration-20261016`，root 私有 |
| 固定控制程序 | `/usr/local/libexec/sub2api-migration-20261016` |

应用必要文件位于 `/var/lib/sub2api-migration-20261016/rolling-recovery/app/data`。持久端点记录位于同一迁移根目录的 `app` 子目录。控制器同时校验容器、卷、程序及配置身份，后续部署不能只替换容器或更改端口。

## 日常检查

在腾讯服务器上执行以下只读检查：

```bash
curl -fsS https://api.b4im.com/health
sudo systemctl status sub2api-tencent-keeper-20261016.service --no-pager
sudo systemctl status sub2api-tencent-entry.service --no-pager
sudo docker ps --format '{{.Names}}\t{{.Status}}'
sudo journalctl -u sub2api-tencent-keeper-20261016.service -n 50 --no-pager
```

健康接口用于确认入口可达。完整验收还应核对模型流的结束事件和 EOF、精确请求对应的用量和计费去重记录、PostgreSQL 主库身份、Redis 主库角色及应用持久端点。迁移执行记录保留这些独立证据。

应用 Docker healthcheck 仍指向旧的 `localhost:8080/health`，会显示 unhealthy；实际应用为 8083，keeper 与公网核验正常。不要仅为清除该陈旧探针状态重建或重启生产应用；后续修订部署定义时单独处理。电脑刚切换后若仍解析 AWS，可刷新系统 DNS；不要把 DNS 指回旧桥。

入口沿用 Caddy 自动管理证书配置和已验证的 ACME 存储。生产环境没有通过重启验证冷启动；启动顺序和路由恢复仅作过隔离测试。守护程序在接管时必须保持现有业务 PID，不用生产重启代替检查。

## 重启、升级和故障处理

守护程序按 PostgreSQL、Redis、应用、入口的依赖次序管理固定资源，持久记录每次启动意图。每个组件有启动次数限制；遇到身份不符、结果不确定或次数耗尽，应检查其私有账本及日志，保留原始记录后制定恢复动作。

本次迁移没有顺带升级业务版本。未来升级前须按当前守护配置、端点持久化及路由规则重新制定部署步骤。原 AWS 的通用应用更新脚本和旧 Compose 文件不适用于腾讯当前布局。不要通过删除守护状态、重建同名容器或改用其他镜像来绕过身份校验。

## AWS 保留范围与回退

AWS `13.231.143.136` 上的 Codex Relay、共享 Caddy、实例和 IP 保留，用于独立的远程服务。sub2api 迁移通道、恢复守护、主控已 inactive/disabled，专用转发规则已删除；腾讯旧反向监听和到 AWS 的 TCP 连接均已消失。共享 Caddy 的历史 sub2api 站点配置保留，已经没有可用迁移 upstream，不属于可用服务入口。不要重启旧迁移单元或将 DNS 指回 AWS。

旧应用、PostgreSQL 和 Redis 均 stopped/restart=no；旧 Compose 入口已封存。旧应用与 PostgreSQL 为 exit0；旧 Redis 自身完成最终快照和正常关机后，旧 shell 包装脚本返回127。原 signal 操作保持 pending，关机证明独立保留。不要为“修复”这份历史记录启动旧容器、重发 signal 或改账本。旧数据、容器和迁移证据至少保留至 2026-10-24 21:57:57（北京时间）之后；七天到期不自动删除任何资源。

数据交接前，只有确认两端账本仍为 AWS 权威、没有不可逆交接意图时，才允许按已验证恢复路径继续使用 AWS。

腾讯开始接收写入后，旧 AWS 数据已经落后。不能将入口指向使用旧数据的 AWS 应用，或启动旧主库。此前临时入口恢复仅通过 AWS→腾讯桥转发，没有回退数据；该桥现已退役，不再是恢复路径。若需要迁回业务数据，必须先以腾讯为权威同步最新数据、完成一致性校验，再安排请求排空及唯一主库交接。现有 AWS 实例因 Codex Relay 仍在使用，不属于可释放资源。

## 证据和备份

- 两端 root 私有迁移目录保存原始操作账本、失败与 UNKNOWN 记录、请求排空证明、等待预算链和交接回执。
- 本机 `/Users/mima0000/.codex/backups/sub2api-source-controller-20261016` 保存控制器恢复与验收证据。
- 本机 `/Users/mima0000/.codex/backups/sub2api-retired-source-final-20261017` 保存最终 AWS 应用文件与日志的加密归档；已验证解密和内容摘要。
- AWS 停止的原数据库、原卷以及腾讯保留的旧备用数据不删除。原始冷备份和隔离恢复演练证据继续保留。

私密目录中的 API Token、数据库凭据、控制令牌、证书私钥和备份密钥不进入 Git 或公开报告。迁移完成后的新增业务数据也需要持续备份；旧 AWS 副本不能作为腾讯后续写入的备份。


## 运行收尾的特殊记录

目标 keeper 的 `arm.json` 同时保留等待超时事实和固定旧 Redis 的 shell 异常退出证明；异常范围不能扩大到其他容器。`keeper/route-compatibility-20261017` 保存升级前 keeper、route、arm 和原证明，`keeper/installed-adoption-20261017` 保存最终采用意图与核验。RPC 优化记录位于 `rpc/read-optimization-20261017`，是原 adoption 配置证据链的必要部分，不可删除。

应用已使用腾讯本机的持久数据端点。旧 AWS 数据仅为保留证据及恢复参考，不是业务数据权威；新写入的权威在腾讯。跨云通道已停止并禁用，业务入口直接位于腾讯。回迁业务数据需要以腾讯最新数据重新同步和演练。


## 本机最终备份与恢复边界

- `~/.codex/backups/sub2api-tencent-final-20261017`：新主 PostgreSQL custom dump、应用配置、端点和 keeper/RPC 证据的 AES-256-GCM 备份。已验证落盘解密摘要和 PG 归档目录。
- `~/.codex/backups/sub2api-tencent-redis-final-20261017`：当前腾讯主 Redis 实时快照，已验证 RDB 结构/CRC 和落盘解密摘要。原始含协议尾标的复制流另存为加密证据，恢复只使用 `redis.rdb`。
- `~/.codex/backups/sub2api-tencent-bootstrap-20261017`：50 个固定路径的运行程序、PG/Redis 配置、控制及采用证据、入口/TLS 和 systemd 文件，以及三个容器 inspect、systemd 状态和两个显式 mask 链接证明。归档 15,058,104 bytes，逐文件摘要及独立落盘解密校验通过；采集没有写入或重启远端服务。该归档包含凭据与证书私钥，只能保存在私密目录中。
- 各目录独立的 `backup.key` 为 0600，归档的 AAD、明/密文摘要和状态在该目录 `verification.json`。恢复时按报告 AAD 解密；不要把密钥或明文放入仓库。

这些备份是迁移后的单次基线，不是持续备份策略。PG/Redis 分别取样，不保证跨服务同一时刻一致性；最终备份没有完整恢复演练。补充归档没有导出容器镜像本体，不能直接覆盖到新主机后照搬启动；重建须准备匹配镜像并评审主机、容器、卷、程序及证据中的身份约束。恢复须在隔离环境先验证，并以腾讯当前数据为权威。持续备份和异地保管须覆盖迁移之后的新写入，旧 AWS 数据不能替代。
