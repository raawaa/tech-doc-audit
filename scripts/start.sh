#!/bin/bash
# 技术文档审核系统启动脚本

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

echo "=========================================="
echo "技术文档审核系统启动脚本"
echo "=========================================="

# 检查 Ollama
echo ""
echo "[1/4] 检查 Ollama 服务..."
if curl -s http://localhost:11434/api/tags > /dev/null 2>&1; then
    echo "✓ Ollama 服务正常"
else
    echo "✗ Ollama 服务未运行，请先启动: ollama serve"
    echo "  或使用 Docker: docker-compose up -d ollama"
fi

# 检查模型
echo ""
echo "[2/4] 检查 Qwen 模型..."
if ollama list | grep -q "qwen3.5"; then
    echo "✓ Qwen 模型已安装"
else
    echo "⚠ 建议安装 Qwen 模型: ollama pull qwen3.5:0.8b"
fi

# 启动 API 服务
echo ""
echo "[3/4] 启动 FastAPI 服务..."
echo "  后端地址: http://localhost:8000"
echo "  API 文档: http://localhost:8000/docs"
if pgrep -f "uvicorn api.main:app" > /dev/null; then
    echo "✓ API 服务已在运行"
else
    echo "  启动中..."
    nohup uvicorn api.main:app --reload --port 8000 > /tmp/audit-api.log 2>&1 &
    sleep 2
    echo "✓ API 服务已启动"
fi

# 启动前端
echo ""
echo "[4/4] 启动前端服务..."
echo "  前端地址: http://localhost:3000"
if pgrep -f "vite" > /dev/null; then
    echo "✓ 前端服务已在运行"
else
    echo "  启动中... (在 frontend 目录下执行 npm run dev)"
    cd frontend
    nohup npm run dev > /tmp/audit-frontend.log 2>&1 &
    cd ..
    sleep 3
    echo "✓ 前端服务已启动"
fi

echo ""
echo "=========================================="
echo "服务启动完成！"
echo "=========================================="
echo ""
echo "访问地址:"
echo "  前端界面: http://localhost:3000"
echo "  API 文档: http://localhost:8000/docs"
echo ""
echo "日志文件:"
echo "  API: /tmp/audit-api.log"
echo "  前端: /tmp/audit-frontend.log"
echo ""
echo "停止服务:"
echo "  pkill -f 'uvicorn api.main:app'"
echo "  pkill -f 'vite'"
echo ""
