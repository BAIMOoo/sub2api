# Codex 导航

- 在修改、评审、合并、测试或规划 fork 独有的 Codex/ChatGPT 行为、上游同步、传输隔离、数据库迁移、监控、更新来源或部署前，先读取 [FORK_CONTRACT.md](FORK_CONTRACT.md)。
- 将该合约视为所有者已确认的需求，并区分当前 `main` 的实际实现；下游技术文档只提供证据，不构成相互冲突的另一套需求。
- 本机 Go 验证须先确认执行环境的 CPU 与内存硬上限。复用缓存，每次只跑一个单包定向测试（`GOMAXPROCS=1`、`-p 1 -parallel 1`，用 `-run` 指定用例）。全量测试、集成测试和 lint 交给候选分支 CI；`GOMEMLIMIT` 不能代替硬上限。

## Agent skills

### Issue tracker

Specs and work tickets live in GitHub Issues for `BAIMOoo/sub2api`. See `docs/agents/issue-tracker.md`.

### Triage labels

This repo uses the canonical five-role triage vocabulary. See `docs/agents/triage-labels.md`.

### Domain docs

Use the single-context root glossary and ADR layout. See `docs/agents/domain.md`.

### Codex HTTP alignment

For pinned Codex direct HTTP baseline upgrades, fixture capture, `codexlive` verification, and host alignment monitor maintenance, see `.agents/skills/align-codex-http/SKILL.md`.
