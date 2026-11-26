# Sub2API

Shared terminology for Codex and ChatGPT outbound client versioning, Model Minimal Client Version requirements, alignment monitoring baselines, model catalog queries, and operations monitoring in this fork. Owner decisions live exclusively in `FORK_CONTRACT.md`.

## Language

### Codex Egress

**Pinned Egress Version**:
The single human-approved official `codex exec` client version fixed by this fork for all direct HTTP Codex and ChatGPT outbound requests.
_Avoid_: Dynamic version, auto-discovered version, caller version

**Model Minimal Client Version**:
The minimum official Codex client version required by an upstream or bundled Codex model descriptor before that model can be served.
_Avoid_: Model version floor, client floor, required version

**Approved Alignment Baseline**:
The immutable official Codex release version configured on the alignment monitor as the reference target for drift detection, changed only after production deployment acceptance.
_Avoid_: Latest stable, live baseline, discovered target

**Catalog Client Version**:
The client version parameter sent when querying Codex model catalog manifests so callers see models gated up to that release.
_Avoid_: Manifest version, frontend version, query version

### 运维监控

**输出 TPS（Output TPS）**
一次请求的输出 Token 数除以请求总耗时（秒），单位为 tok/s。总耗时包含首字等待，输出 Token 可能包含推理 Token。

**请求模型（Requested Model）**
客户端在请求中指定的模型名称。路由映射可以为该请求选择另一个上游模型。
