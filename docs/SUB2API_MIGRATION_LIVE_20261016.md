# AWS → 腾讯东京：本轮迁移执行记录

用户已于本会话授权开始迁移，目标 `43.165.177.136`。本记录只写本轮事实，不沿用历史演练作为当前验收证据。

## 当前状态

- 客户入口与应用已在腾讯；PostgreSQL、Redis 权威仍在 AWS，数据交接尚未完成。
- 源应用：0.2.14，提交 `c69fdbeb0fa26543cc5bf115d2e5048426041e84`；原镜像 config `sha256:7f83a7aa3937e47473df037a341e48104318f6ad7fa6c5de00504a12cbe87742`。
- 兼容补丁在隔离工作树 `/Users/mima0000/.codex/worktrees/sub2api-live-migration/sub2api` 编写；腾讯兼容应用已承载客户请求，旧 AWS 应用已自然退出。
- 腾讯准备目录 `/home/ubuntu/sub2api-migration-20261016`：实际配置及制品已复制验证；专用应用、入口及 Redis 副本运行；PG 已完成重新备份、校验及安装，备用库恢复 streaming，数据权威仍在 AWS。
- 目标制品的 Docker manifest ID 与源 classic image ID 不同；使用 config + ordered DiffID 验证内容，不使用不同存储后端的 `.Id` 相等作为依据。
- 用户确认目标云安全组已开放 TCP 80/443/22；AWS 与独立本机代理路径已能访问目标。本机直连问题详见以下新证据。

最新检查点（2026-10-17，北京时间04:30前后）：客户入口桥 COMPLETE，Cloudflare A 已指向腾讯43.165.177.136。腾讯新应用 instance `r32ufr8h00vvbf`、PID534792 正常承载新TCP连接；旧腾讯应用仍有少量已建立连接，正在自然排空，尚未发送TERM。两应用均以source/source连接AWS PostgreSQL与Redis，后台任务关闭。新实例各writer failed/dropped为0；旧实例failed1/dropped3及原始证据永久保留，四条缺失warn已在源库补回并独立核验。AWS旧应用已clean stop。

源state仍SOURCE_DATA/attempt3/windowNone；PG/Redis权威AWS/AWS；源guard最新fresh healthy/RUNNING_BOUNDED，无当前trip或fence。全局保守暂停历史14.656846920028329秒不可清零；最大已排空请求cohort为7.082038551568985秒，当前cohort 7b54d1b6384a4d859436856bbbce4dcb使用4.104169294238091秒。尚未建立替代generation的cohort或再次启动main；必须先完成旧连接排空、应用controller接管及新generation真实canary。

腾讯PG已完成隔离pg_basebackup、pg_verifybackup及数据安装，原volume根inode917589不变，旧数据保留。PID509035，recovery/read_only/streaming，sysid7626048206782038050、timeline1，replay覆盖四条诊断恢复WAL fence8/D04ADEC8。Redis仍为AWS副本、link up。历史guard trip/fence及旧备用数据完整保留。

rolling原start UNKNOWN未重发；独立inert-start-resolution在证明容器从未运行后只执行一次start，并记录新的process receipt。随后Docker Mounts顺序被证实不稳定，按destination排序比较保持数量、重复、权限及路径等值检查，未放宽挂载内容约束。修正host已安装；SYN条件精确NAT规则已安装，保护原连接并只转向新连接。源Caddy及Relay未reload或重启。keeper与交接后canary尚未执行，迁移尚未完成。

首次窗口时间线：app.quiesce wire 1.777531秒、pg.arm 0.665932秒、app.quiet-snapshot 0.605944秒，连源PG证据及checkpoint在提交门前约3.55秒，已不足保留5秒正向交接余量，因此在不可逆动作前拒绝。恢复status约0.718秒、cancel约0.560秒、restore约1.799秒，总窗口7.082秒。目标账本331操作约691KB，其中279个runtime.status；不得删除账本来规避慢路径。

## 已取得的新证据

- 目标临时 80/443 HTTP 探针成功绑定，AWS 来源均返回固定文本 HTTP 200。目标 UFW inactive、nft INPUT accept；本机直连 SYN 到达腾讯但本机等待连接超时，不再归因为端口未放行。探针已自动退出，未修改防火墙。
- 本机通过既有本地 HTTP CONNECT 代理 `127.0.0.1:7897`，不使用 AWS ProxyJump，严格核对 host key 并使用本机 `tokyo.pem` 成功认证腾讯 SSH；代理出口独立于当前 AWS。保留本机代理在线为该通道前提；私钥没有复制到跳板或目标。
- 源端只读 PG 查询：活跃账户按平台/类型为 anthropic apikey 1、copilot apikey 1、openai apikey 2、openai oauth 6；最近 24 小时用量 1083 条，记录入口均为 `/v1/responses`。这只用于安排演练优先级，不能据此删减其他既有协议兼容要求。
- 源 PostgreSQL SQL `SHOW data_directory` 确认 `/var/lib/postgresql/data`；同版本隔离冷恢复演练已经通过。不得把配置文件声明的卷路径代替该查询证据。
- 安全启动/运行时子集已通过普通与 race 测试、相关服务回归、cmd/server 测试与构建；服务完整 unit tag 套件被基线已有重复测试 helper 阻挡，尚未把该问题记作迁移回归。
- 新持久计费基础通过真实隔离 PostgreSQL 18 事务测试：金额、配额、用量、幂等与 outbox 原子提交；重复重放不重复扣费；用量插入失败全事务回滚。客户端响应终结边界、所有协议和缓存交接尚未接齐，因此不构成切流就绪。
- PostgreSQL 屏障已通过 lib/pq + PostgreSQL 18 真集成：活动事务、打开的 Rows、固定 sql.Conn/advisory lock 均阻止交接，已接收工作能完成；旧 prepared statement 在放行后重新绑定新端点。该测试没有模拟物理复制或证明 10 秒交接预算。
- 新加密通道已安装并启用源端独立 systemd 单元 `sub2api-migration-channel-20261016.service`；目标只接受源 IP 的专用受限公钥，私钥仅在源本机。源新增 API 桥 `127.0.0.1:18081`，目标回源端口只监听 `127.0.0.1:15432/16379`。目标经通道确认 PG SSL 握手和 Redis PONG；没有向公网发布数据库端口，也没有修改业务路由。
- 目标恢复输入 `136,890,532` bytes，pg_dump 与传输 exit 0、stderr 空；源导出快照会话均已退出。目标无网络容器恢复成功；同快照 101 表、1237 约束、438 索引、19 张关键表计数及 292 行迁移记录指纹一致。29 处 CHECK 文本经 PG 原生解析器逐条证明等价。写入 COMMIT→重启→读取通过，探针表已删除、演练容器已停。备份密文已传回本机，AES-256-GCM 认证及原 dump SHA 校验通过。
- 源只读设置查询：`risk_control_enabled=false`，未配置 `prompt_audit_config`。该事实缩小本轮实际审计任务风险，仍须处理后台保留任务与未来启用兼容性。

- Cloudflare 用户授权凭据已保存在本机受限文件，API 验证 active；只读查询 `api.b4im.com` 为单一 A→13.231.143.136，proxied=false，TTL=自动；原记录快照已保存0600。仅将 TTL 降至60秒；A记录仍指向 AWS，尚未改源站地址。
- 目标初始文件副本已校验7项：config.yaml/.installed/价格JSON及SHA文件、api.b4im.com CRT/KEY/证书元数据。只在准备目录，均0600；源/目标实际应用卷未变。证书域名匹配、密钥配对，2026-11-24到期。仍需最终文件冻结/增量归档。
- 真实定价采集通过 race 回归：渠道区间/时段、response_model选中候选、Free Fast、DeepSeek峰谷及搜索附加；HTTP终结边界正在接线。
- 源只读查询：当前受限 platform quota 行数为0；PG slot WAL默认无限制且磁盘仅6.8G空闲，创建任何物理复制slot前必须落实WAL上限与独立监控。Redis当前无副本。

## 保护条件

- 已开始的流不能被迁移主动中断；新请求首次有效回答最多新增等待 10 秒，不能依赖手工重试。
- 原版启动会清理共享 Redis 的其他实例活跃槽；目标原镜像不可直接连接生产 Redis。首次共享之前须先禁用旧容器自动重启且不重启旧进程。
- 当前采用严格排空路线 `MIGRATION_HANDOVER_MODE=quiet`：关闭新业务 handler admission，保留原 HTTP/SSE/WS 自然结束，连续持有全部后台 writer fence，再分别交接 PG 与 Redis。quiet readiness 要求真实 PG、Redis 订阅/L1 重建、writer 覆盖和控制初始化。usage journal 默认关闭，不作为该路线的切流条件。
- PG/Redis 客户端交接、所有后台写入者隔离、在线复制、10 秒预算及恢复演练尚未全部验收。严格排空路线以所有已接收写入提交完成作为主库切换条件。
- 不重载共享 Caddy、不停止旧应用、不改 DNS、不提升副本，直到对应阶段门槛通过。
- Codex Relay 留在 AWS，实例/IP/共享 Caddy 不属于退役范围。

## 后续验收证据

每次推进须记录：执行时间、唯一权威端、具体变更、验证结果与恢复条件。不在本文件记录口令、私钥、完整环境或用户数据。

## 本轮继续执行检查点

- 加密备份已完整下载，本机 AES-256-GCM 认证解密与原 dump SHA 一致。目标明文 dump 已在验证后删除。
- 隔离入口桥 R3 成功：原 SSE 24 帧完整，原 WS 24 次 echo 自然 close 1000，新 HTTP/SSE/WS 均到目标；删除规则后回源正常。源生产容器 ID/健康/Caddy PID 前后相同，临时资源与 watchdog 均已清理。证据为 `docs/SUB2API_BRIDGE_REHEARSAL_20261016_R3.json`。只证明新 TCP 转向，已复用生产上游连接仍须自然结束。
- Redis 屏障、六类 PubSub 覆盖清单、真实 ACK 与 L1 重建通过本地双 Redis 8.4 race。生产为 8.6.2，仍需同版本整机演练。先停 Redis 消费者再封 PG，重建顺序相反，避免 PubSub handler 等待 SQL 自锁。
- quiet 控制接线、readiness、复用 keepalive admission 与共享 transition 锁已编写，尚未部署。数据仍 paused 或 writer fence 仍持有时禁止提前开放业务 handler。
- 请求派生 detached goroutine 和内部队列仍在补齐强制覆盖，现有组件清单不能当作完整全局证明。超出预算须保留当前权威，先在原端重建订阅/缓存再放行；不能仅调用 Redis.Resume。
- PG/Redis standby 准备脚本正在审阅；尚未创建源 replication slot，尚未启动复制或提升副本。

## 2026-10-16 后续准备进展

- Cloudflare TTL 已于 14:18:28Z 降为60秒并回读；A记录仍为13.231.143.136。
- 腾讯已暂存并验证源 Caddy 2.6.2、API 专用证书和inactive unit；尚未启动入口。
- 加密通道扩展已完成并回读：源127.0.0.1:18092/25432，172.18.0.1:26379；Redis桥仅允许172.18.0.2经源Docker桥访问，其他来源拒绝。原API桥、PG/Redis回源转发均正常。尚未改业务路由。
- PG准备已安装独立源WAL/磁盘保护，创建专用SCRAM复制身份与物理slot，源WAL上限512MiB reload生效；目标正在basebackup。最近采样保留WAL约1.3MiB、源磁盘约6.39GiB、monitor healthy，无安全触发。源应用与PG均未重启。
- Redis owned standby已在线，源看到172.18.0.1:26379；正在核验首次backlog建立导致的正常复制ID轮换、精确runID、反向桥与持久化。尚未角色交接。
- 独立兼容审查发现3项：quiet后日志导致恢复闭环、3个HTTP异步SQL任务遗漏计数、WS client reader可在HTTP返回后继续数据回调。前两项已补并分别通过race回归；WS正补生命周期hook及回归。未部署兼容镜像。
- 后台恢复私有控制已补：需实际选定target PG/Redis、端点ready、writer lease释放；外部独立runner还须证明旧AWS应用退出。
- 单个请求累计额外等待上限为10秒；不得把分离PG/Redis窗口解释为可累计20秒。正式commit前仍须完整演练和预算验证。

- PG basebackup+manifest验证与连续3次状态检查通过：standby_ready=true，sysid7626048206782038050，18.3，target25432 loopback recovery=true，WAL receiver streaming；源slot active reserved，monitor两端healthy，采样lag5544 bytes。尚未提升，准备容器的readonly/standby.signal限制需先受控移交为promotion-ready规格。

## 2026-10-16 15:45Z 继续执行检查点

- 兼容镜像最终 binary SHA `0bdcaa371aa45abfa1eb61c763d2da10f133d718c4ad93251587ca0cc6191724`，image ID `sha256:73945bb5447cceaa5a301e0ddbf6fd026ef7cd27a9ad0596a542de691e29ce6a`，保持生产 0.2.14 基线。新增持久启动端点选择，同一进程 PG/Redis 使用一次性相同快照；挂载目录支持原子替换。
- PG role-aware authority 准备已执行：新容器 `ac9f494b2fd0ec87257a9a7308a6ad7b270051d1dc4da33b33fea9af2885418c`，原数据卷/系统标识/时间线不变；recovery=true、transaction_read_only=on、default_transaction_read_only=off，receiver streaming。两次真实正常退出/重启均恢复 standby；旧 target monitor 已持久 mask，新 role-aware monitor healthy。未提升、未重启源库。
- Redis standby 在线，复制与反向桥身份已核实。腾讯宿主仅持久设置 `vm.overcommit_memory=1`，原值0已记录；未重启 Redis。
- 隔离应用演练已证明同源中止会重建订阅/缓存再恢复（实测入口 pause 1.784秒），以及真实 PG/Redis 一次性交接（3.567秒），两轮 health 额外等待分别 1.692/3.480秒。该证据尚不构成生产提交许可：应用重启验收被演练 namespace holder 随旧 PG 退出的问题阻挡，正在改用独立隔离网络 holder；还需实际独立 runner 的端到端故障验收与真实模型计费 canary。
- 三次最旧失败演练的完整 inspect/logs/report 已归档本机0600并核SHA；精确删除18个已停自有容器、15个自有临时卷，没有 prune、force 或删除备份。腾讯可用磁盘由约6.58GiB恢复至17.58GiB；cold 输入、在线 standby 和所有原报告保留。
- 独立控制器代码正在准备：AWS source operations、target固定RPC、durable backend、core数据窗口、独立WAL/recovery guard、inert应用准备器。恢复测试修复了 PG交接凭据随新观察时间戳变化及旧Redis退出后目标进程run ID核验；80项core/backend测试通过。尚未把这些控制器安装成生产切换服务。
- 源端部署路径只读盘点：原Compose `/home/ubuntu/sub2api/deploy/docker-compose.yml`；未发现watchtower、deployment timer或host自动compose启动unit。原应用仍为unless-stopped，首次共享Redis前必须无重启地禁用。完成切换前还须冻结旧Compose路径，防止误建旧主。
- AWS 实例、IP、共享 Caddy 和 Codex Relay 均保留。观察期至少7天；本次完成指应用和数据权威迁走，不释放承载Relay的实例。

## 2026-10-17 继续执行：真实验收与控制准备

- 完整隔离演练 `3988afe9342082db` 成功：同权威中止并重建恢复时入口 pause 2.163秒、排队 health 等待2.068秒；真实 PG+Redis 交接时入口 pause3.753秒、health等待3.662秒。持久端点 target/target，旧双端口均拒绝连接，应用重启1.668秒后 SQL/Redis 与六类订阅均正常；SQL标记和Redis确认值保留。输入冷备份前后 treeSHA 相同，7个自有演练容器均已停止，未修改生产。
- 腾讯惰性安装 `INSTALLED_INERT_VERIFIED`：应用 ID `cd5c29cf86b2595fffc41fc068e024d3f790f69bb8bdaaacc63eaba1ac9bcd82`，running=false/PID0/restart=no；root固定RPC及guard已安装，RPC ledger epoch0/operations0。PG/Redis角色和源进程未改变。AWS→root强制RPC通道仍须实测。
- 真实源模型 canary 通过：首次文本1.603秒、总1.635秒，13个SSE事件、response.completed及干净EOF；24输入/8输出。精确客户端计费UUID对应1条用量与1条billing_dedup，费用USD0.000128×0.2=USD0.0000256。没有以并发余额变化冒充单请求扣费证据。腾讯模型canary等待目标应用就绪。
- 控制器审查发现实际Go runtime在ingress暂停时ready=false，而Python后端误把ready=true当quiet条件；已阻止上线，正在修正并补真实schema回归。源guard已补无窗口/初始态主unit健康验证，43项离线测试通过；尚未远程安装源控制器。
- 源原应用仅执行restart=no更新，PID2260633和完整Config保持不变，health正常。随后精确启动腾讯兼容应用PID257639，ready=true，四项readiness均true，后台关闭，端点source/source。
- 腾讯API专用Caddy已安装并启动。AWS直连目标443以api.b4im.com作SNI/Host验证证书链/域名与leaf pin，health200；实际WS101、matching pong、1000关闭，无模型帧。该无首条模型消息的WS探针产生1条`openai.websocket_read_first_message_failed`，不得误报成迁移业务故障。
- 腾讯唯一真实TLS模型canary通过：首次文本1.916秒、总2.036秒、13个SSE事件、正常终结与EOF；24输入/8输出、1条usage和1条billing_dedup，精确费用与源canary相同。报告canonicalSHA `a7e04495c3efd7f39de6dafc266967cf5f08e550bb3ed6e4c34d72dd0ca5bdb1`。仅证明腾讯应用通过原数据库链工作，不代表数据主权已迁移。
- 源四个`docker-compose*.yml`原件已逐字节归档到root0700 `/var/lib/sub2api-migration-20261016/deployment-archive`，原路径替换为root0444显式封存文件；四次`docker compose config --services`均拒绝且没有服务输出。没有改动任何现有容器、卷、Caddy或Relay。
- 源应用数据卷仅四个已复制业务文件和约69.8MB日志，无其他必要文件；旧应用退出后仍须最终归档日志并复核四文件。
- AWS 控制器安装成功：main inactive，独立 recovery/WAL guard 已启用且 healthy，guard 初始探针 0.142 秒；固定 root RPC 认证从 AWS 实际通过。未启动正式控制器、未安装客户 bridge。
- 首次恢复自测在任何 arm/pause/mainstart 前因真实 systemd timer 不提供 MainPID 被拒绝；仅固定 timer 允许省略并补 0，service PID 仍严格检查。失败证据保留，未重置生产状态。
- 只读采样提速已安装：`app.quiet-snapshot` 一次固定 RPC 完成 runtime/ingress/writers/Redis/PG 状态，前后实例一致及双方 2 秒新鲜度保持；目标 RPC SHA `dffaf4274a6d92f7d745a7ae5c1be268a7cb4e890820cbdbf4c80029073ec947`，目标配置 rawSHA `99b9d4bd51bbc9c46035ddb9ae0b782c9ebc221a2e26f457352e7ab04a5c1bcb`。源 backend SHA `7904f16e12ef9893b910961c2d51d5156207a7463c8c82950ceb40f64b46638d`、source operations SHA `8c96ae9234701022ef84914b1856fcc1ea9b76fcf0401a711a2dab83b2073818`。旧字节已在两端 root 私有目录归档；原 target ledger 和所有 PID 保持。
- 暖 RPC 实测普通读取约 0.19–0.48 秒；原双窗口即使加入上述只读提速仍需约 72 次串行调用，仅 RPC 下界约 13.68 秒，不能当作可上线方案。正在缩短控制路径；不扩大用户 10 秒 SLA，不切换入口规避该门槛。
- 腾讯持久运行 keeper 已完成本地 32 项故障/权限测试。实机只读预检 0.240 秒通过：PG/Redis/app 原 ID、Config、image、卷、挂载目录及既有入口 unit/Caddy/config 身份匹配；PG PID180813、Redis PID133623、app PID257639、entry PID259990。该检查未安装、arm、启动、停止或重启任何资源；正式迁完后仍须 root 新鲜 COMPLETE 与源退役证据才能接管。

## 2026-10-17 单窗口主控恢复实测

- 只读资源fences与Redis pair检查优化已安装，source_operations SHA `f6c90b5d5069fde8a1cdce9ad3f6d750327bb64b935cc77473d7f8491ea4e86b`；不改变mutation、角色或连接。
- 新固定宏app.quiesce/app.restore/pg.route/redis.preroute保留每子步持久intent、epoch及未知结果观察；完整quiet、writer/实例/新鲜度与唯一权威校验未删减。正常单窗口13次wire RPC，取代不能满足预算的原72次路径。两独立审查无真实阻断，575项迁移Python测试中573通过/2跳过。
- `upgrade_control_fastpath.py --apply` 两端CAS升级成功，root保留旧字节；target ledger、容器PID、source checkpoint不变，source guard未重启。
- `controller_selftest.py --apply` 真正暂停目标、故意挂起AWS主控；独立guard杀主cgroup并恢复原AWS端点。health HTTP200等待6.714271秒，计入窗口7.227569秒，deadline_breached=false；主PID0、AWS三资源完全相同、PG/Redis权威AWS/AWS、目标背景off且ready。总驱动耗时11.482秒包括窗前/窗后核验，不等于客户等待。
- `close_controller_selftest.py --apply` 成功封存6文件、解除armed，恢复无人客户流量的演练初始checkpoint。未删除RPC/transport/audit，无生产预算重置。证据在本机受限 `~/.codex/backups/sub2api-source-controller-20261016/` 与AWS root私有passed-selftest目录。
- 客户bridge工具仅惰性安装，规则尚未应用；keeper尚未安装或arm。恢复演练证明主控挂起路径，不声称任意故障瞬间、未知POST结果或生产满负载均10秒内。

## 2026-10-17 最终只读预检发现与修复

- 最终预检证据 `final-preflight-20261017.json` 原字节SHA `44ce0dcd06ed4379616bc4746f778e30fa5c185b86054d74af8b3fbb89dd8c2f`：四必要文件源目标一致；四Compose原件归档与精确root0444封存均通过；腾讯TLS health200/桥health200；源与目标全部业务PID、AWS Caddy/Relay/channel PID保持。
- 发现并在本地修复目标INFO解析：Redis8.6.2 Modules节合法重复module行，不能做全INFO全局去重。新预路由仅解析Server/Replication并在这两节严格拒绝重复，所有身份/角色/唯一复制链校验保持。
- 发现并在本地修复PG18实际目录格式：`client_addr::text`输出含/32，改固定SQL为`host(client_addr)`；内部backend白名单精确增加`io worker`。新代码已在源仅内存执行只读采样两轮，确认唯一owned sender、streaming及lag0；当前unknown仅原app的5个idle客户端，原app尚运行，quiet=false符合要求。未知客户端、未知backend、外来sender仍拒绝。
- 快路径进一步减少为11次窗口内RPC：正常PG commit依靠同ID严格arm/authority/fence校验省去冗余status，恢复仍新鲜观察；源Redis已完成coordinated failover、target CONFIG持久化/精确runID和端点持久化后恢复入口，再在窗口外完成旧replica退休和新鲜master验证，全部三源cleanstop后才激活后台。
- 最终代码/固定guard预算修正与第二轮真实恢复自测正在进行，尚未把上述修复作为已切流事实。

## 2026-10-17 最终升级在保护前置处停住

- 本轮最新版617项迁移Python测试通过（2跳过），最终跨云控制/预算代码及升级器独立复核通过。
- `upgrade_control_verified.py --apply` 腾讯阶段成功：RPC SHA `2b4fa88abb3a35c3ba1349ed7370d624cb5c3ebdd2fcbc121fd9704c4a3e196c`，config rawSHA `9eb33e0f44ac2e6b8c9e743a2bd7593548967aa2eca2233548391a1581df2169` / canonicalSHA `23a0df42c6e48af322776eaa920cfb5be0230a1158f60254275da797068ba74d`；ledger、PID、角色完全不变。
- AWS阶段在写任何源程序/归档之前因guard前置失败退出，`verified-upgrade-20261017`源目录不存在。源程序仍为上一轮版本、初始checkpoint/业务PID不变。不得重跑整个升级器；目标已升级，应只读对账后精确恢复源阶段。
- 实际guard已latched `GUARD_SAMPLE_TIMEOUT`，sample仍显示RUNNING_BOUNDED、retained_bytes0、free约6.83GB、WAL cap512MiB；healthy=false/tripped=true。正只读核验trip时间/guard日志/角色及slot和复制，以判断独立fence副作用。未重置trip、未修改角色/slot、未执行r2真实演练。客户入口/DNS/应用/数据权威仍AWS，无客户bridge。


## 2026-10-17 guard 超时只读诊断

- trip 时间为北京时间01:24:02.387，原因 `GUARD_SAMPLE_TIMEOUT`；原始 trip SHA `a915bb07f40825a61149463a9ae3b66ec15abbed881bf1e1ed76ea06fe5dfde4`。源 checkpoint 保持初始态、原业务 PID 不变。
- 旧 guard 的 `client_addr::text` 地址条件与 PG18 实际含 `/32` 的输出不匹配：NOLOGIN 已提交，复制连接未被终止，slot 没有删除，无 guard-fence 文件。腾讯仍 recovery/streaming，sysid/timeline 与源一致。
- 连续15轮已安装采样成功，耗时0.122–0.214秒；无 OOM/磁盘错误。trip 与旧 monitor 的一轮1.183秒慢采样重叠，但旧 monitor 不使用共享采样锁，缺少历史分项 trace，不能确定瞬时慢点。
- 发现已完成 worker 用 supervisor 轮询时刻判断耗时/新鲜度的问题，正作最小修复；1.25秒执行超时及恢复预算保持。恢复前须停止旧 guard、确认 cgroup 与容器内旧 SQL 均退出、归档现场，再仅恢复严格归属的复制角色 LOGIN。绝不在携带 active trip 时启动修正了隔离 SQL 的新 guard。
- 三份只读 evidence 位于本机受限备份目录，SHA 分别为 snapshot `9df1d9fec9ebaa5bd450b79d0e1302c832c56469c9f18ff880c10850400588e1`、measurement `d6be030caf6361e67cd30d2b84c8a11d77c3c6d36fc247650bd1c21307476dcf`、replication `34048574c09c6761f74b6f128e6ef9c1792a6665b992919a89a0b89664f1b1a2`。


## 2026-10-17 受控恢复、第二轮实测与入口切换

- source-only恢复工具SHA `1555bec645077fcea3e59d32dc0957ef3feddea573cca44e6e9a73a8de60267d`完成独立审阅。停止旧guard后证明cgroup/容器psql/旧guard SQL均已退出，两次精确role/slot/sender稳定；只恢复owned角色LOGIN，不重建slot、不杀sender。归档原trip/status后CAS移走trip，安装最终5文件，启动新guard并核验源角色/slot与目标receiver/PID不变。证据目录 `guard-trip-recovery-20261017`，complete=true。
- 新guard SHA `704b171cc50feccf28f873320437886e8ea15fc7b76f9a8dbf36ed964dfd9345`按worker真实完成时刻判断1.25秒超时及新鲜度，保留真实超时与7.5秒接管边界。源其余最终SHA与verified NEW一致；腾讯RPC仍2b4fa88a…/config9eb33e0f…。
- r2真实恢复实测：故意挂起主控PID2804454，由独立guard结束主cgroup并恢复AWS端点；窗口9.176471587秒，health8.476825437秒后200/bodyok，deadline_breached=false。源三PID、唯一权威及原数据未变。
- r2首次close在任何归档/解除armed之前，读取guard-status时因UNTRUSTED_FILE安全拒绝；随后只读核验state/report精确一致、archive不存在、guard文件root0600/nlink1且freshhealthy。保留失败receipt后以同一REMOTE、新独立receipt完成close；原始失败证据不覆盖。归档为passed-selftest-20261017-r2，production pause0、RPC/transport/audit均保留。
- 最终只读预检工具SHA `06decf938b394d683b3cfb0cdc8de67419a30ec1b391dfd1116e9b584fcebdc0`实机通过，证据 `final-live-preflight-20261017-1792172859195787000.json`：源码摘要、r2闭环、原进程、PG/Redis复制、四Compose封锁/原件、四业务文件、腾讯TLS及18081桥全部通过。迁移Python全套639项/2跳过；源0644/目标0700模式修正后另5项预检测试通过。
- root accepted gates已写，rawSHA `ae7dec1090fc84135fb6bdc589e02a0b786b16127fe3ee97981d4b4dbebf131d`。
- 入口桥应用结果COMPLETE/VERIFIED，固定规则SHA `61925fd96c25a75f2fbcd84ca1c6ac1a0ce7c3646d83066a973c13bb380d7e92`；没有reload Caddy、删除conntrack或主动断连接。
- Cloudflare仅一次PATCH修改api.b4im.com A→43.165.177.136，17:48:44.760Z生效，readback确认且其它属性摘要不变。证据目录 `sub2api-cloudflare-cutover-3ee3d5dd9f9b4b0e`，reportSHA `78277fd0e552e314aa23e7ef0c21e7c821eb03385bc27ee18548014254f5d9b9`。
- 17:48:56Z启动独立main，PID2832094。已观察150秒，仍在自然排空，state ENTRY_PREPARATION/route_fenced=true/attempt0/windowNone/pause0；不把入口切换宣称为数据迁完。正在只读核实唯一残留旧连接，绝不强断。


## 2026-10-17 入场并发恢复与再启动

- 同一实例的排空证明已落 root 不可变 cohort 记录，全局7.082038551568985秒历史不变，本cohort起始0秒。首次重启仅等待，排查入场竞态后已在无窗口状态停止main。
- 所有restore在取消quiesce或发resume前验证实际数据端点；只有入口暂停、数据组件开放且authority已确认时，允许原SSE/WS活动期间恢复入口。fresh GET observer不写任一端的操作账本；已完成quiet采样与所有mutation仍持久记录。独立复核通过。
- 662项/1.41MB历史的隔离真实loopback HTTP+fsync演练通过，完整调用/恢复0.585秒、新请求等待0.354秒、旧SSE连续30帧且保持连接；仅入口pause/resume，旧历史不改。该演练使用Python控制模型，不能声称真实Go或生产跨云测时。
- admission安装完成：target_rpc SHA4681f2e7203623b39d8f861811cb00cace10dd17f61bc447a3821196587acb9c，target config rawSHA3aed65e1a5c69093d4c3b72f616974fffab75b18dc31cffe0a42765e02dc0152；源backend SHA8d1f7e6064e51c2392e977603e47ba2684db5c36096bc3e944041b1d76218bbd、service SHA7318557010904f289e05b8974f0ff6e23cffdcb3f372f82763824c9fc0eaa5a1。所有PID、ledger、state/cohort不变，三次跨云observer0.743/0.304/0.266秒，source transport逐字节不变。
- 18:58:47Z启动main后在无窗口保护检查退出。系统日志证明其已完成retire操作仍重发daemon-reload（284ms），随后guard前置失败；guard后来freshhealthy，无trip/fence。不能断言保留了瞬时guard失败的完整原因。已将complete-only路径改为重新验证freshguard、资源、全部accepted证据、mask/idle后只读返回，非complete流程字节保持；SHA28ff80ddfa89e71e4f91d217ddeb4d1ca90aee3028fe6523af0dd1ff2f15fbb6。独立73项测试通过，完整719项通过（2跳过）。
- 单文件CAS安装后，实机以禁止systemctl mutation的封闭probe验证complete退役路径，源state/transport/operations逐字节不变。19:02:49Z再次启动main PID3288279，观察仍attempt1/windowNone/AWS-AWS，guardhealthy，无新暂停预算消费。
- 已最终归档AWS应用卷72,635,662原字节（4必要文件+历史日志），压缩4,363,351字节，SHA52d00c5b8213cb492e697e1c5dd04c19112b242a92d8e2639a0002e35fdb88e7。本机0600及腾讯root0700独立source-application-archive目录均保留，未覆盖目标实时日志；4文件与目标现状逐个哈希一致。


## 2026-10-17 03:20后：日志与复制阻塞的恢复分支

- 719项迁移Python测试通过（2跳过），admission race恢复、fresh无账本observer及已完成monitor retirement只读协调已安装。真实attempt2没有进入任何pause子步；attempt3仅入口暂停后writers.pause因历史ops日志失败拒绝，4.104秒内恢复，数据权威未改变。
- 当前target ledger epoch14，旧PG arm CANCELLED，promotion_intent=null，所有operation COMPLETE；runtime ingress epoch10/open/queued0，原app PID257639。
- 03:12源guard新trip已真实完成fence；源PG PID972839仍primary/sysid7626048206782038050/timeline1，Redis PID972827仍primary；API及目标app运行正常。现不能宣称guard healthy或standby streaming。
- 四条缺失日志为openai.responses.stream_diag、openai.ws_bind_response_account_failed、openai.http_bind_response_owner_failed、content_moderation.runtime_snapshot_refresh_failed，均warn且非audit组件。候选SQL只操作ops_system_logs，COMMIT前逐事件唯一性断言、synchronous_commit=on、独立读回与WAL flush fence必需。旧failed1/dropped3永久保留。
- 恢复新实例、备用库及controller身份以前禁止数据交接；不重跑任何旧一次性安装器，不删除账本，不重置历史预算。


## 2026-10-16 19:48Z（北京时间17日03:48）前后：诊断修复与副本重建执行

- 四条诊断warn已精确补回源ops_system_logs，仅INSERT该表及序列；提交前唯一性断言、synchronous_commit=on，提交后四行独立SHA读回均匹配。原进程failed1/dropped3、原日志、恢复SQL与receipt永久保留。
- guard修正程序SHA72cb81b27bcf8c4f316511ca0460f44af453ab51276d53cda82ba55e2cee8d05已安装，三份递增fresh健康状态及unitPID验证通过；旧trip及全部原配置/账本已归档。暂态SQL传输失败立即撤销健康，连续失败门槛及4秒失联期限才隔离；身份/权限/WAL/磁盘违规仍立即trip，8秒预算监督独立保留。
- reseed首次安装在load阶段因原记录误用base-image ID而拒绝，没有stop或scope副作用；真实inspect确认目标PG为authority wrapper image9a529633…，与RPC config一致。仅修正此pin、CAS更新脚本后prepare成功：旧standby单次SIGINT，clean exit0/PID0/OOMfalse，旧数据尚未移动。
- source_reseed_ready独立slot创建intent/读回后，另LOGIN intent/读回，guard始终运行；原fence字节CAS归档，源主库/Redis/app状态与state/transport/source-op字节不变。新slot reserved且restart_lsn非空，WAL cap512MiB。新鲜proof在1.504秒内复制并启动备份，不经过模型等待。
- 备份helper a1e3fd64c05d64bf0d5498b7cd3fde48a07220efc6c148d629ef6887da6254ab，独立new-data、8MiB/s、256MiB/0.5CPU/restartno，pg_basebackup --no-clean、manifestSHA256，随后pg_verifybackup。旧数据将完整保留，原Docker volume根inode/容器身份不变；校验成功前禁止启动新standby。
- 当前线上仍source/source、API health正常，未开新窗口、未切换数据权威。rolling新app及controller代际接管尚未执行。

## 2026-10-16 20:02Z 之后：备用库恢复与新应用准备

- 隔离 pg_basebackup 与 pg_verifybackup 均 clean exit0；新备用数据已安装，原 PG 容器/volume 根 inode917589 保持，旧数据完整保留。新 PG PID509035，recovery/read_only/streaming 正确，replay 已覆盖四条恢复日志 WAL fence。
- 精确 NAT 规则补充 SYN 限制后，独立 cold/warm Linux namespace 演练均通过：旧 SSE60帧完整、空闲连接仍复用旧后端、新UID1000连接进入新后端，其它规则与连接保持。此为合成连接演练，不冒充生产模型或跨云延时证据。
- 新应用容器17e3bfacbd442beff52427e003e58f4970e613203356b8bb8bde0e98b9229bce已创建，业务镜像及必要文件与旧应用一致，仅 API/control端口及私密token变化。rolling准备原intent经只读核验完成；启动原intent结果UNKNOWN，容器仍Created/PID0/从未启动。未重发原intent，正准备独立一次性root恢复证明。
- 当前入口/DNS/应用仍由腾讯承接，PG/Redis权威AWS/AWS；source main PID0，windowNone，历史pause14.656846920028329及cohort链不变，freshguard healthy/RUNNING_BOUNDED，未切换新app路由或数据权威。

## 2026-10-17 04:30 前后：新应用已接收连接

- 新应用CID17e3bfacbd442beff52427e003e58f4970e613203356b8bb8bde0e98b9229bce、instance r32ufr8h00vvbf、PID534792，ready/serving true、background false。应用数据目录为rolling-recovery/app/data，端点仍AWS/AWS。
- 精确SYN NAT安装后rolling处于DRAINING。只读观察旧实例连接由4减至2，仍等待自然结束；HTTP/业务计数曾为0，不能把瞬时空闲误当TCP已排空。未发TERM，未变更controller身份。
- 新generation验收脚本已独立审阅，19项故障测试通过；v2 delta声明涵盖10模块375项测试，通过且程序/测试摘要匹配。仅为后续受监督交接准备，不构成数据迁移完成证据。

## 2026-10-17 04:38 后：等待最后一条网页保活连接

- 旧实例已自然从4条连接降至1条；最后一条为AWS Caddy→源18081桥→腾讯sshd→旧app8082，腾讯端tuple为127.0.0.1:49528→8082。不关闭该连接，不reload源Caddy或Relay。
- 受限只读观测仅输出请求方法和路径摘要，确认该连接每分钟调用GET /api/v1/auth/me；与frontend/src/stores/auth.ts的60秒自动刷新一致。没有输出或存储Authorization、Cookie或请求正文。
- 已请求用户暂时关闭各设备上的sub2api网页标签页，保留当前Codex会话。网页停止轮询后，旧upstream keepalive可按既有超时自然关闭；仍须连续3次、静稳至少2秒证明所有TCP/HTTP/WS/写任务均归零，才单次TERM。
- 本机独立路径：DNS A解析43.165.177.136，公网HTTPS /health返回200、status ok、TLS校验0。923项migration Python测试通过，2项跳过。PG/Redis权威仍为AWS，main未重启；等待连接结束不计入请求暂停窗口。
