#!/bin/bash

# 测试 Copilot API 调用
# 使用方法: ./test_copilot.sh YOUR_API_KEY

API_KEY="${1:-sk-your-api-key-here}"
BASE_URL="http://localhost:8080"

echo "测试 Copilot GPT-4 调用..."
curl -X POST "${BASE_URL}/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${API_KEY}" \
  -d '{
    "model": "gpt-4",
    "messages": [{"role": "user", "content": "Say hello in Chinese"}],
    "max_tokens": 50
  }' | jq .

echo -e "\n\n测试 Copilot Claude 调用..."
curl -X POST "${BASE_URL}/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${API_KEY}" \
  -d '{
    "model": "claude-opus-4.6",
    "messages": [{"role": "user", "content": "Say hello in Chinese"}],
    "max_tokens": 50
  }' | jq .
