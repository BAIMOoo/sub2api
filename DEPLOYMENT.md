# Sub2API 部署指南 (macOS)

## 当前状态

✅ 已完成 GitHub Copilot 集成代码
✅ 已配置环境变量文件 (`deploy/.env`)
⏳ 需要安装 Docker 来构建和运行

## 快速部署步骤

### 1. 安装 Docker Desktop for Mac

```bash
# 方法 1: 使用 Homebrew (推荐)
brew install --cask docker

# 方法 2: 手动下载
# 访问 https://www.docker.com/products/docker-desktop/
# 下载并安装 Docker Desktop for Mac
```

安装后启动 Docker Desktop 应用。

### 2. 构建镜像

```bash
cd /Users/mima0000/sub2api

# 构建包含 Copilot 支持的镜像
docker build -t sub2api:copilot -f Dockerfile .
```

构建时间约 5-10 分钟（首次构建）。

### 3. 启动服务

```bash
cd /Users/mima0000/sub2api/deploy

# 使用 docker-compose 启动
docker-compose up -d
```

这将启动：
- sub2api 应用 (端口 8080)
- PostgreSQL 数据库
- Redis 缓存

### 4. 验证部署

```bash
# 查看容器状态
docker-compose ps

# 查看日志
docker-compose logs -f sub2api

# 检查健康状态
curl http://localhost:8080/health
```

### 5. 访问应用

打开浏览器访问：http://localhost:8080

**管理员账号：**
- 邮箱: admin@sub2api.local
- 密码: admin123456

## 测试 Copilot OAuth

### 启动设备流

```bash
curl -X POST http://localhost:8080/api/v1/auth/oauth/copilot/start \
  -H "Content-Type: application/json"
```

响应示例：
```json
{
  "device_code": "xxx",
  "user_code": "ABCD-1234",
  "verification_uri": "https://github.com/login/device",
  "expires_in": 900,
  "interval": 5
}
```

### 完成授权

1. 访问 `verification_uri`
2. 输入 `user_code`
3. 授权后调用：

```bash
curl -X POST http://localhost:8080/api/v1/auth/oauth/copilot/complete \
  -H "Content-Type: application/json" \
  -d '{
    "device_code": "xxx",
    "interval": 5
  }'
```

## 环境变量配置

已生成的配置文件：`/Users/mima0000/sub2api/deploy/.env`

关键配置：
- **数据库密码**: b76fd0ffa544aefb6656952d1107ee87
- **Redis 密码**: 797ec9a37112ea7bf13aa903c5627e10
- **JWT Secret**: fde654b042cd9ddf10c47aebf764374a35a4c0ebd477d9bb823ce1522b66bf3f
- **管理员密码**: admin123456

## 常用命令

```bash
# 停止服务
docker-compose down

# 停止并删除数据
docker-compose down -v

# 重启服务
docker-compose restart

# 查看日志
docker-compose logs -f

# 进入容器
docker-compose exec sub2api sh

# 更新镜像
docker-compose pull
docker-compose up -d
```

## 故障排查

### 端口冲突

如果 8080 端口被占用，修改 `.env` 文件：
```bash
SERVER_PORT=8081
```

### 查看详细日志

```bash
docker-compose logs -f sub2api
```

### 重置数据库

```bash
docker-compose down -v
docker-compose up -d
```

## 生产环境建议

1. **修改默认密码**：
   - 编辑 `.env` 中的 `ADMIN_PASSWORD`
   - 重启服务

2. **配置反向代理**：
   - 使用 Nginx/Caddy 提供 HTTPS
   - 配置域名

3. **数据备份**：
   ```bash
   docker-compose exec postgres pg_dump -U sub2api sub2api > backup.sql
   ```

4. **监控日志**：
   - 日志位置：`/app/data/logs/sub2api.log`
   - 使用 `docker-compose logs` 查看

## 下一步

安装 Docker 后，运行：

```bash
cd /Users/mima0000/sub2api
docker build -t sub2api:copilot .
cd deploy
docker-compose up -d
```

然后访问 http://localhost:8080 开始使用！
