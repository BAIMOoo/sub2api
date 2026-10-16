# 腾讯实际应用：仅创建 inert 容器

`prepare_live_app.py` 为独立、固定资源的准备器。默认无参数完全离线；只在显式 `--create-inert` 时读取腾讯本机已批准的快照并执行 Docker inspect/create。没有 SSH、docker run/start/restart/exec、数据库/Redis连接、业务端口监听、路由、源服务或DNS修改路径。本次交付尚未在远端 apply。

固定容器为 `sub2api-migration-app-20261016`。固定兼容镜像：

```text
tag       sub2api:migration-compat-20261016-0bdcaa371aa4
image ID  sha256:73945bb5447cceaa5a301e0ddbf6fd026ef7cd27a9ad0596a542de691e29ce6a
binary    0bdcaa371aa45abfa1eb61c763d2da10f133d718c4ad93251587ca0cc6191724
baseline  c69fdbeb0fa26543cc5bf115d2e5048426041e84
```

创建前核验 image ID、linux/amd64、binary/baseline标签、entrypoint/Cmd与有序RootFS.Layers。使用实际 image ID 和 `--pull never`，不通过可变tag选择新镜像。该 binary SHA来自之前完成的镜像构建/校验记录；准备器本身不运行镜像再次读取binary。

## root 输入

root installer 先将本脚本放入不可由非root修改的安装目录，用隔离Python执行；创建以下 root0700 私有目录与root0600配置。所有宿主祖先无symlink、不可group/other写入；root路径所有者必须root。

```text
/var/lib/sub2api-migration-20261016/app-prepare/config.json
```

closed schema：

```json
{
  "v": 1,
  "run": "20261016",
  "owner": "<32 lowercase hex, unique to this preparation>",
  "machine_id_sha256": "<SHA256 of /etc/machine-id bytes.strip()>",
  "source_runtime_sha256": "5570f29f0954706128798f2d4575be4302a0508739d2e21c39e364c4d1dc16fd",
  "initial_manifest_sha256": "<freshly approved SHA256 of fixed staged manifest bytes>"
}
```

程序不接受任意输入文件位置或容器参数。固定读取：

```text
/home/ubuntu/sub2api-migration-20261016/app-runtime/source-runtime.json
/home/ubuntu/sub2api-migration-20261016/initial-api-files-verification.json
/home/ubuntu/sub2api-migration-20261016/app-initial/config.yaml
/home/ubuntu/sub2api-migration-20261016/app-initial/.installed
/home/ubuntu/sub2api-migration-20261016/app-initial/model_pricing.json
/home/ubuntu/sub2api-migration-20261016/app-initial/model_pricing.sha256
```

暂存文件必须uid1000、0600、regular/单hardlink/无symlink。其祖先只允许root/uid1000且不可group/other写入。虽然暂存材料由ubuntu保管，其内容必须匹配root已批准的快照/manifest SHA。四文件再核验manifest记录的逐文件SHA与size，每项最多1MiB；manifest中的其他certificate项不复制进appdata。准备器不改暂存输入。

source-runtime只有container_id/image_id/environment/command/entrypoint五项；固定核验源实际image ID `sha256:7f83a7aa3937e47473df037a341e48104318f6ad7fa6c5de00504a12cbe87742`，源command `/app/sub2api`、entrypoint `/app/docker-entrypoint.sh`。不采用Compose的.env替代实际环境。

## 创建的固定资源

```text
/var/lib/sub2api-migration-20261016/app-prepare/        root0700
  app.env / app-control.token                         root0600，秘密
  operation.lock / state.json / adoption.json         root0600
/var/lib/sub2api-migration-20261016/app/                root0755
  endpoints.json                                     root0644，无秘密
  data/                                              uid1000:gid1000 0700
    config.yaml/.installed/model_pricing.*            uid1000:gid1000 0600
```

appdata是独立副本，仅复制上述四文件。初始endpoints严格为 `{"v":1,"postgres":"source","redis":"source"}`；任何已有不同文件均拒绝覆盖。准备器不用于恢复已开始迁移后的环境。

- host network，restart=no，512MiB memory与swap上限，1CPU、pids=256、日志轮转8m×2；no-new-privileges与drop NET_RAW。
- 原entrypoint/Cmd保持，独立data读写bind到 `/app/data`，整个root-owned `app/`目录只读bind到 `/migration-state`。挂载目录能够观察之后atomic replace后的endpoint文件。
- 原业务环境键逐项保留，包括JWT、TOTP key、数据库password、业务选项和proxy值。移除旧MIGRATION_*后只设置本次固定迁移参数。
- 业务地址 `127.0.0.1:8082`；私控 `127.0.0.1:18092`；`GOMEMLIMIT=400MiB`，AUTO_SETUP=false。
- 初始Postgres `127.0.0.1:15432`；target DSN `127.0.0.1:25432`，原sub2api user/dbname/password、sslmode=disable。密码按libpq conninfo引用，始终只在root0600 env文件中保存。
- 初始Redis `127.0.0.1:16379`；target `127.0.0.1:26379`；无username/password/TLS，原DB选项保持。
- quiet handover、shared Redis开启、Postgres/Redis动态连接器开启、后台启动关闭、usage journal关闭；`MIGRATION_ENDPOINT_STATE_FILE=/migration-state/endpoints.json`。

secret env通过Docker的固定 `--env-file`路径传入，绝不放到shell/argv/返回值。Docker inspect可被宿主root/Docker权限者读取，这是宿主既有权限边界；公共结果只有摘要。

## 持久意图与重复运行

写 `PREPARING` checkpoint后准备文件，再fsync `CREATING` intent才执行create。create回复丢失不证明未执行：返回DOCKER_OUTCOME_UNKNOWN，资源保留，重新运行只检查固定name。若匹配本次owner、镜像、完整env、命令、mount、资源限制且status严格为created/Running=false/Pid=0，可adopt并产出结果；不再次create。

Docker inspect失败只表明未知。程序使用成功的全量固定name列表确认该容器确实不存在后才允许create。已有无本次checkpoint/owner的容器、变更过的已记录container ID、曾启动后exited的容器或Running容器都拒绝。程序从不删除、停止、重启或接管它们。

成功产出root0600 `adoption.json`：container ID、image ID、binary SHA、**容器Config的compact sorted JSON SHA**、有序RootFS DiffID、control_port=18092、私有token文件位置、env SHA与四文件SHA。RPC installer使用这些字段构造app配置，并把token安全复制到固定RPC目录的root0600 `app-control.token`。metadata没有环境变量值或token值。

准备器只证明容器尚未开始运行，不能证明正式启动已满足生产安全条件。namespace/channel、source原app restart=no、数据authority、旧后台停止、源/目标IP白名单与上游连通性，由root installer/runner在显式启动和切流前分别验证。任何兼容binary变化都必须先同步固定tag/image ID/binary SHA并重做构建/测试/演练；本准备器不自动追随新镜像。

## 离线验证

```text
python3 -m py_compile scripts/migration/prepare_live_app.py scripts/migration/prepare_live_app_test.py
python3 -m unittest discover -s scripts/migration -p prepare_live_app_test.py -q
```

12项隔离测试覆盖默认offline、closed schema、exact source/image身份、业务env保留与引用、固定create参数、secret不进argv/metadata、私有文件读取、精确inert adoption、create丢回复恢复及已启动/未知容器拒绝。测试的完整准备流程在临时目录模拟root/uid1000写入，并以FakeDocker替代真实Docker；未执行远端apply。
