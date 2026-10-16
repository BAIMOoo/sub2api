# 新 TCP 连接桥：隔离原型与演练

本目录的 `bridge_rehearsal.py` 默认只输出计划，不连接服务器、不启动容器、不操作 iptables。当前交付是待根代理审查的原型，尚未取得远端演练证据。`prepare_channel.py` 不受本原型修改。

## 固定资源边界

| 资源 | 本演练使用 | 限制 |
| --- | --- | --- |
| AWS 独立测试 Caddy 2.6.2 | `127.0.0.1:28080` | 独立 transient unit，UID/GID 为 `caddy`，`admin off` |
| AWS 测试 Docker 后端 | `127.0.0.1:28081` → 容器 `8080` | 新名字、新标签、静态 Go probe、只读挂载 |
| 已有专用 SSH 桥 | AWS `127.0.0.1:18081` → 腾讯 `127.0.0.1:8082` | 只消费现有桥，不创建、重启或改桥 |
| 腾讯测试 Docker 后端 | `127.0.0.1:8082` → 容器 `8080` | 端口必须为空，不能与真实迁移实例同时运行 |
| 生产入口/服务 | `80`、`443`、`8081`、`8787` | 代码不对这些端口创建规则或变更监听 |

生产 `caddy.service` 仅通过 `systemctl show` 读取 MainPID/状态；不会 reload、stop、restart。测试 Caddy 的 HOME、XDG_CONFIG_HOME、XDG_DATA_HOME 都放入自己的 scope，避免覆盖生产 Caddy 的 autosave、数据或证书目录。测试容器关闭继承镜像的 healthcheck、默认 entrypoint 和自动重启，限制为 64 MiB、0.5 CPU、64 PID，无 capabilities。

## 候选规则

脚本查询当前 `caddy` 的数值 UID，拒绝 UID 0。规则仅为：

```text
iptables -w 5 -t nat -I OUTPUT 1 \
  -p tcp -d 127.0.0.1/32 --dport 28081 \
  -m owner --uid-owner <caddy UID> \
  -m conntrack --ctstate NEW \
  -m comment --comment sub2api-bridge-<随机 run id> \
  -j REDIRECT --to-ports 18081
```

插入后检查其位于所有 OUTPUT 跳转之前，包括 Docker OUTPUT。清理只使用同一完整规则的 `-C` / `-D`，不使用链序号删除、不 flush、不批量重置规则、不改 sysctl、不清 conntrack。规则对 UID 为 root 的演练控制客户端不生效；被改写的是独立 Caddy 发出的 upstream TCP。

本原型验证的是 **新 TCP**。测试 Caddy 专门关闭 upstream keepalive，确保新 HTTP 请求建立新 upstream TCP。生产 Caddy 可能重用旧连接；已有 TCP 不会被 NAT 重新选择目的地。因此本演练成功不能证明所有新 HTTP 请求立即迁到腾讯，也不能证明原生产 Caddy 的连接复用、健康检查、HTTP/2、TLS、流量负载或数据交接已安全。生产推广仍需单独评审和演练，不能直接把 `28081` 改成 `8081`。

## 自动演练顺序

1. 为本次运行生成 16 位随机 run id。只接受两个明确给出的、已经存在的测试镜像；通过 image ID 固定镜像，禁止 pull。
2. 按服务器实际架构构建 `CGO_ENABLED=0` 的静态 probe。Go 依赖来自现有 backend module，不改 go.mod。
3. 两端各创建全新 root-owned `/var/tmp/sub2api-bridge-rehearsal-<run id>`。记录所有既有容器的名称、不可变 ID、运行状态和 health，以及生产 Caddy MainPID/状态；输出不包含容器环境变量。
4. **先启动独立 cleanup watchdog，再启动任何测试容器。** watchdog 有绝对截止时间、systemd RuntimeMaxSec 和 ExecStopPost。对资源创建和清理使用同一文件锁，防止超时清理后再创建孤儿资源。
5. 腾讯在空闲 `8082` 启动 probe，AWS 在空闲 `28081` 启动另一个 probe，再启动独立测试 Caddy。确认测试 Caddy 的真实 PID、UID 和 `/proc/<pid>/cmdline` 指向本次配置。
6. 经测试 Caddy 建立 AWS 长 SSE 与标准 WebSocket echo。两条流都取得第一帧/echo 后才插入规则。
7. 插入精确 NAT。新 HTTP、SSE 和 WS 必须识别为腾讯 probe；之前建立的两条 AWS 流必须按序完整结束，WS 必须完成 1000 close 握手。报告保存规则插入时间与两条流首末时间。
8. 确认旧测试流已经结束，再停止 **仅本次拥有的** AWS 测试容器。Docker 的旧端口发布消失后，规则仍须让新请求到腾讯。
9. 重启同一个 AWS 测试容器。删除本次精确规则后，新 HTTP、SSE 和 WS 都须回到 AWS probe。
10. finally 与两端 watchdog 分别清理自己的测试资源，记录生产容器 ID/health 与生产 Caddy MainPID 的前后比较。任何变化都会使演练不通过；脚本不擅自修复或重启生产。

SSE 每条旧流共 24 帧，间隔 250 ms；WebSocket 使用 RFC 6455 Upgrade、masked client frames、原样 text echo、ping/pong 和正常 close。probe 服务器使用已有 `github.com/coder/websocket` 库，不自行实现服务端 WS。

## 本地校验（无需 SSH）

```bash
python3 scripts/migration/bridge_rehearsal.py
python3 -m unittest discover -s scripts/migration -p bridge_rehearsal_test.py
cd backend
go test -race ../scripts/migration/bridge_probe.go ../scripts/migration/bridge_probe_test.go -count=1
```

Python 测试把所有远端/系统变更命令替换为 mock。Go 测试只使用本地 httptest 的临时端口。

## 根代理审查后方可执行的入口

此命令在当前交付阶段**尚未执行**。镜像须先由根代理只读核实后填写；不能假定机器已经缓存某个 Alpine 标签。

```bash
python3 scripts/migration/bridge_rehearsal.py --apply \
  --source-image '<AWS 上已存在的审核后镜像 ID>' \
  --target-image '<腾讯上已存在的审核后镜像 ID>' \
  --timeout 240 \
  --report /absolute/path/bridge-rehearsal.json
```

AWS 使用现有 SSH alias `sub2api`；腾讯连接方式沿用现有日本机器的 key、严格 host key 检查和本地代理。脚本使用 `sudo -n`，不修改 sudoers、sshd 或密钥。如果独立 unit、已有镜像、架构或权限条件不满足，就停止演练并清理本次资源。

每端的 watchdog 从 bootstrap 开始计时，默认 240 秒；本地 SSH 失联不影响远端清理。`ExecStopPost` 是进程意外退出或 RuntimeMaxSec 的第二条清理路径，每次停止阶段最多留 120 秒进行有界清理，各外部命令也有单独超时。若整台主机关机、systemd/内核故障或 Docker/iptables 持续失败，无法承诺软件清理成功；报告会保留错误。本次精确规则只匹配测试端口，故障也不应触及生产入口。

## 独立故障收尾

主报告记录 run id 和两端 scope。每端保留 `state.json`、`exercise.json`（AWS）和 `cleanup.json` 作为取证；自动清理不递归删除目录。必要时通过原 SSH 通道手动调用**这次 scope 内的脚本**，无需本会话状态：

```text
sudo -n python3 /var/tmp/sub2api-bridge-rehearsal-<run id>/bridge_rehearsal.py \
  --remote=cleanup --scope=/var/tmp/sub2api-bridge-rehearsal-<run id>
```

清理会检查容器 run 标签后按不可变 ID 删除，只停止描述符匹配的测试 Caddy unit，拒绝与原生产 Caddy PID 相同的进程。它不会对名称模式执行 kill。随后可调用相同脚本的 `--remote=stop-watchdog` 结束本次 watchdog；这仍会触发幂等清理。

## 通过标准与不能代替的验收

通过要求主报告 `success=true`，两端 `cleanup.errors=[]`、`production_unchanged=true`；旧 SSE/WS 跨规则完整结束；新流到腾讯；停止旧测试容器后桥仍工作；去规则后恢复 AWS。新增或变化的既有容器 health、生产 Caddy PID 变化均需人工查明，不能当作通过。

原型没有读写业务数据，不证明计费持久化、PostgreSQL/Redis 主权切换、插件/审计任务排空、真实 provider 出口、DNS 缓存、最大并发或 10 秒等待预算。也不证明 HTTP/3/QUIC、未经过本机 OUTPUT 的连接、其他 Caddy UID、其他目的地址/端口可使用同一规则。它仅消除入口桥在指定 Docker/iptables/Caddy 组合下的一组具体不确定性。
