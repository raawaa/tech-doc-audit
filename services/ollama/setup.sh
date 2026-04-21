#!/bin/bash
# services/ollama/setup.sh
# Ollama 模型下载和配置脚本

set -e

echo "=== Ollama 模型安装脚本 ==="

# 检查 Ollama 是否运行
echo "检查 Ollama 服务状态..."
if ! curl -s http://localhost:11434/api/tags > /dev/null 2>&1; then
    echo "错误: Ollama 服务未运行，请先启动 Ollama"
    echo "启动方式: docker-compose up -d ollama 或 ollama serve"
    exit 1
fi

echo "Ollama 服务正常运行"

# 下载 Qwen3.5-0.8B 模型
echo "下载 Qwen3.5-0.8B 模型..."
ollama pull qwen3.5:0.8b

# 验证模型已安装
echo "验证模型安装..."
if ollama list | grep -q "qwen3.5:0.8b"; then
    echo "✓ Qwen3.5-0.8B 模型已成功安装"
else
    echo "错误: 模型安装失败"
    exit 1
fi

# 测试模型推理
echo "测试模型推理..."
ollama run qwen3.5:0.8b "你好，请回复：模型测试成功"

echo "=== 安装完成 ==="