#!/bin/bash

API_KEY="${SUB2API_DIAG_API_KEY:?请先设置 SUB2API_DIAG_API_KEY}"
BASE_URL="${SUB2API_DIAG_BASE_URL:-http://localhost:8081}"

echo "=== 1. 检查 API Key 信息 ==="
curl -s "${BASE_URL}/api/v1/auth/me" \
  -H "Authorization: Bearer ${API_KEY}" | jq .

echo -e "\n=== 2. 检查可用模型 ==="
curl -s "${BASE_URL}/v1/models" \
  -H "Authorization: Bearer ${API_KEY}" | jq .

echo -e "\n=== 3. 尝试调用 GPT-4 ==="
curl -s "${BASE_URL}/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${API_KEY}" \
  -d '{
    "model": "gpt-4",
    "messages": [{"role": "user", "content": "Hi"}],
    "max_tokens": 10
  }' | jq .

echo -e "\n=== 4. 检查容器日志（最后 20 行）==="
docker logs sub2api --tail 20 2>&1 | grep -i "copilot\|account\|error" || echo "无相关日志"
