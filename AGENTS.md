# AGENTS.md

此文件提供 Codex 在本仓库工作时需要记住的项目级信息。

## 交流

- 始终使用中文回复用户。

## 服务器访问

- 生产/部署服务器 SSH 别名：`sub2api`
- 连接命令：`ssh sub2api`
- 该别名已配置在本机 `/Users/mima0000/.ssh/config`
- 当前配置指向：
  - HostName: `13.231.143.136`
  - User: `ubuntu`
  - IdentityFile: `/Users/mima0000/.ssh/ec2-key.pem`

不要把 `.pem` 私钥内容写入仓库。需要访问服务器时优先使用 `ssh sub2api`。
