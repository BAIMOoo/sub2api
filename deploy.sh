#!/bin/bash
set -e

echo "🚀 Sub2API 部署脚本 (with Copilot support)"
echo "=========================================="

# 检查 Docker
if ! command -v docker &> /dev/null; then
    echo "❌ Docker 未安装或未启动"
    echo "请先启动 Docker Desktop 应用"
    exit 1
fi

# 检查 Docker daemon
if ! docker info &> /dev/null; then
    echo "❌ Docker daemon 未运行"
    echo "请启动 Docker Desktop 应用"
    exit 1
fi

echo "✅ Docker 已就绪"

# 构建镜像
echo ""
echo "📦 构建 Docker 镜像..."
cd /Users/mima0000/sub2api
docker build -t sub2api:copilot -f Dockerfile .

if [ $? -ne 0 ]; then
    echo "❌ 镜像构建失败"
    exit 1
fi

echo "✅ 镜像构建成功"

# 启动服务
echo ""
echo "🚀 启动服务..."
cd deploy
docker-compose up -d

if [ $? -ne 0 ]; then
    echo "❌ 服务启动失败"
    exit 1
fi

echo "✅ 服务启动成功"

# 等待服务就绪
echo ""
echo "⏳ 等待服务启动..."
sleep 10

# 检查健康状态
echo ""
echo "🔍 检查服务状态..."
docker-compose ps

echo ""
echo "✅ 部署完成！"
echo ""
echo "📍 访问地址: http://localhost:8080"
echo "👤 管理员邮箱: admin@sub2api.local"
echo "🔑 管理员密码: admin123456"
echo ""
echo "📋 查看日志: docker-compose logs -f sub2api"
echo "🛑 停止服务: docker-compose down"
echo ""
echo "🧪 测试 Copilot OAuth:"
echo "curl -X POST http://localhost:8080/api/v1/auth/oauth/copilot/start -H 'Content-Type: application/json'"
