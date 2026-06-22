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

# CLI 工具（与 API 功能一一对应）
uv run python -m cli kb create --name "xxx" --category national
uv run python -m cli kb list
uv run python -m cli doc import --kb-id <id> --file sample_docs/sample.pdf
uv run python -m cli audit upload --file sample_docs/sample.pdf
uv run python -m cli audit-task create --doc-id <id> --kb-ids <ids>
uv run python -m cli qa ask --kb-ids <ids> "问题"            # 知识库问答
uv run python scripts/import_docs.py --kb-id <id>              # 批量导入（默认 data/kb_sources/）
uv run python scripts/import_docs.py --kb-id <id> --dir <dir>  # 从指定目录导入
uv run python scripts/eval_qa.py --kb-ids <ids>              # RAG 评估（检索+答案质量）
uv run python -m benchmark.cli run --kb-ids <ids>            # 检索 benchmark
uv run python -m benchmark.cli sweep --kb-ids <ids>          # 参数扫描

# 生成示例文档
uv run python scripts/generate_sample_doc.py

# 前端
cd frontend && npm run dev           # 开发服务器
cd frontend && npm run build         # 生产构建

# Docker 一键启动
docker-compose up                    # Ollama + API + 前端
docker-compose --profile with-nginx up  # 含 Nginx 反向代理
```

## Architecture Overview

项目是一个**技术文档智能审核系统**：用户上传招标文件等技术文档，系统通过知识库（技术标准/规范）对比审核，生成审核报告。

### 四层架构（API → Service → Core/Infra → Storage）

```
api/routers/          → FastAPI 路由层（知识库、文档、审核任务、审核文档、知识库搜索 Chatbox）
services/             → 业务逻辑层（审核管线、文档处理、Agent 动态审核）
core/                 → 基础设施层（LlamaIndex Settings、FAISS 索引管理、文本提取）
storage/              → 存储层（文件系统 + JSON 元数据，无外部数据库）
models/               → Pydantic 数据模型
cli/                  → Typer CLI（覆盖全部 API 功能）
frontend/             → React + Vite + Tailwind CSS SPA
benchmark/            → 检索质量评估与参数扫描
docs/retrospectives/  → 开发复盘系列文档（`YYYY-MM-DD-title.md`，含 draw.io 导出的配图）
```

### 数据流（审核管线）

1. **上传** `audit_doc_service.upload_document()` → 存入 `data/audit_docs/`
2. **解析** `audit_doc_service.parse_document()` → MinerU 优先，pdfplumber/python-docx 降级
3. **结构提取** `structure_service.analyze_document_structure()` → 零 LLM，正则降级链（regex → docx styles → 单章节兜底）
4. **Agent 选主题** `agent_audit.determine_audit_topics()` → LLM 分析文档自主决定审核维度（降级到 8 个固定主题）
5. **主题审核** `topic_audit.audit_topic()` → 关键词定位段落 → FAISS 向量检索 → 每主题 1 次 LLM 调用
6. **生成报告** → AuditResult（含 issues、summary、standard_reference）

### 关键设计

- **检索引擎**: LlamaIndex VectorStoreIndex + FAISS（bge-m3 embedding，ANN 近似搜索），失败降级到 ripgrep-all 纯文本搜索
- **LLM 调用**: `core/settings.get_llm()` 统一封装（LlamaIndex LLM），支持 Ollama / MiniMax / OpenAI / DeepSeek 四种 provider，通过 `LLM_PROVIDER` 环境变量切换。参见 `.env.example` 了解配置项。
- **两个域**: 知识库文档（`models/document.py` — KBDocument）和待审核文档（`models/audit_document.py` — AuditDocument）相互独立
- **存储**: 所有数据存为 JSON 文件在 `data/` 目录下，向量索引存为 FAISS 单文件。无外部数据库。
- **异步审核（两级并发）**: 任务级 `audit_task_service.run_audit_async()` 用 `threading.Thread(daemon=True)` 后台执行；主题级用 `ThreadPoolExecutor(max_workers=min(8, len(topics)))` 并行审核，每完成一个主题即更新进度。Daemon 线程在服务重启时被强杀，FastAPI 启动时有重置卡住任务的恢复逻辑。
- **审核方式**: Agent 动态审核（`agent_audit.py`）或 8 个预定义审核主题（税率合规、品牌限制、支付条款等），每主题用关键词在全文定位相关段落，结合 FAISS 检索结果提交 LlamaIndex LLM 审核

### 核心降级链（Graceful Degradation）

几乎所有核心组件都有降级逻辑，修改时需保持：

| 组件 | 优先路径 | 降级路径 |
|------|---------|---------|
| 文档解析 | MinerU | pdfplumber → python-docx |
| 向量检索 | FAISS ANN | ripgrep-all 纯文本搜索 |
| 审核主题 | LLM Agent 动态选择 | 8 个预定义固定主题 |
| 结构提取 | docx heading styles | markdown regex → plain text regex → 单章节兜底 |

### 测试模式

测试通过 `AUDIT_DATA_DIR` 环境变量隔离数据目录（每个测试用 `tempfile.mkdtemp()`），避免污染生产数据。fixture 中 `autouse=True` 负责测试后清理。

### LLM 配置

- Embedding 模型（bge-m3 ~2GB）和 LLM 实例均为延迟加载 + 全局缓存（`core/settings.py` 中 `_embed_model` / `_llm` 单例）
- FAISS 索引按 KB 缓存在内存中（`core/index_manager.py` 中 `_index_cache`）
- Ollama 使用自定义 `_SafeOllama` 子类绕过 SOCKS 代理问题
- 所有 LLM prompt 均为中文，匹配中文企业治理文档领域

### 并发与 GPU 资源

修改 embedding / reranker / 索引 / 主题审核并发相关代码前必读（曾因并发 GPU 推理导致 OOM kill）：

- **全局 GPU 锁** `core/settings.get_gpu_inference_lock()` 返回一个 `threading.RLock`。HuggingFace embedding 与 reranker 的 forward 非线程安全，所有 GPU 推理（含 `index_manager` 的批量 embedding）必须先持有该锁——多线程同时前向会各自分配完整激活张量，撑爆显存。
- **两个 GPU 模型**: bge-m3 embedding + `BAAI/bge-reranker-v2-m3` reranker（`RERANKER_TOP_N` 默认 5），均延迟加载 + 全局缓存，均在 GPU 锁下推理。
- **入口线程上限**: `core/settings.py` 顶部用 `os.environ.setdefault` 设 `OMP_NUM_THREADS=2` / `MKL_NUM_THREADS=2`，限制 CPU 线程数以降低内存峰值，勿删除。
- **两级并发**: 见上文"异步审核"。LLM 走 HTTP API 不占 GPU，线程在等 GPU 锁时 LLM 调用仍可进行。

### 几个尚未校准的魔数（已知债）

- 向量搜索接受阈值 `relevance > 0.35`（`vector_search.search_by_keywords()`），低于此值降级到文本搜索——未经 benchmark 系统校准。
- Agent 选主题只送文档前 **8000 字符**给 LLM（`agent_audit.determine_audit_topics()`），长文档后半段可能被遗漏。
- 关键词段落定位取关键词前后各 **1500 字符**（`topic_audit.KEYWORD_CONTEXT_CHARS`），未按文档类型自适应。

### 项目状态

已完成 LlamaIndex 迁移（向量检索 + LLM 调用 + Agent 动态审核）。后续方向：审核报告交互追问（ChatEngine）、检索质量评估（eval）、外部数据源 Tool。

> 架构全貌、降级链、并发模型与完整技术债清单见 `docs/retrospectives/2026-06-18-architecture-and-debt.md`（含配图），是比本文件更详细的架构参考。
