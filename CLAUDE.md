# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# 依赖安装
uv sync                              # 安装项目依赖（自动创建 .venv）

# 运行测试
uv run python scripts/verify_all.py  # 运行所有 pytest 测试
uv run pytest tests/ -v --tb=short   # 运行全部测试
uv run pytest tests/test_xxx.py -v   # 运行单个测试文件

# 启动 API 服务
uv run uvicorn api.main:app --reload --port 8000

# CLI 工具
uv run python -m cli kb create --name "xxx" --category national
uv run python -m cli kb list
uv run python -m cli doc import --kb-id <id> --file sample_docs/sample.pdf
uv run python -m cli audit upload --file sample_docs/sample.pdf
uv run python -m cli audit-task create --doc-id <id> --kb-ids <ids>

# 生成示例文档
uv run python scripts/generate_sample_doc.py

# 前端
cd frontend && npm run dev           # 开发服务器
cd frontend && npm run build         # 生产构建
```

## Architecture Overview

项目是一个**技术文档智能审核系统**：用户上传招标文件等技术文档，系统通过知识库（技术标准/规范）对比审核，生成审核报告。

### 三层架构（API → Service → Storage）

```
api/routers/          → FastAPI 路由层（知识库、文档、审核任务、审核文档、知识库搜索 Chatbox）
services/             → 业务逻辑层
storage/              → 存储层（文件系统 + JSON 元数据，无数据库）
models/               → Pydantic 数据模型
cli/                  → Typer CLI（覆盖全部 API 功能）
frontend/             → React + Vite + Tailwind CSS SPA
```

### 数据流（审核管线）

1. **上传** `audit_doc_service.upload_document()` → 存入 `data/audit_docs/`
2. **解析** `audit_doc_service.parse_document()` → MinerU 优先，pdfplumber/python-docx 降级
3. **结构提取** `structure_service.analyze_document_structure()` → 零 LLM，正则降级链（regex → docx styles → 单章节兜底）
4. **主题审核** `audit_task_service.run_audit()` → 预定义关键词定位段落 → 向量 KB 检索 → 每主题 1 次 LLM 调用
5. **生成报告** → AuditResult（含 issues、summary、standard_reference）

### 关键设计

- **检索引擎**: 本地向量检索 bge-m3 + numpy（文档分块 → embedding → cosine similarity），失败降级到纯文本关键词搜索
- **LLM 客户端**: `services/llm_client.py` 统一封装，支持 Ollama / MiniMax / OpenAI 三种 provider，通过 `LLM_PROVIDER` 环境变量切换
- **两个域**: 知识库文档（`models/document.py` — KBDocument）和待审核文档（`models/audit_document.py` — AuditDocument）相互独立
- **存储**: 所有数据存为 JSON 文件在 `data/` 目录下，无外部数据库依赖
- **异步审核**: `audit_task_service.run_audit_async()` 通过 `threading.Thread` 实现后台执行
- **审核方式**: 主题式审核（`topic_audit.py`），8 个预定义审核主题（税率合规、品牌限制、支付条款等），每主题用关键词在全文定位相关段落，结合知识库检索结果提交 LLM 审核

### 项目状态

已完成六个阶段：基础设施搭建 → 知识库管理 → 文档处理 → 审核核心流程 → 前端界面 → 集成测试与部署。当前在持续优化审核算法和向量检索。
