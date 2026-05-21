# 技术文档审核系统

技术文档智能审核系统。

## 项目概述

用户上传技术文档（如招标文件），系统通过知识库（技术标准、规范）对比审核，生成详细的审核报告。

## 当前状态

已完成 LlamaIndex 迁移，向量检索从 numpy 暴力搜索升级为 FAISS ANN，LLM 调用统一为 LlamaIndex Settings。

**核心能力：**
- 知识库管理：上传 PDF/DOCX，自动分块 + embedding（bge-m3），FAISS 索引
- 文档审核：Agent 动态主题选择 + 8 个预定义维度，关键词段落定位，向量检索知识库，LLM 对比判断
- 审核报告：结构化输出（问题列表、严重级别、标准依据、修改建议）

**技术栈：** Python 3.12 + FastAPI + LlamaIndex（VectorStoreIndex/FAISS、LLM、Agent）+ React/Vite/Tailwind CSS

## 快速开始

### 前置要求

- Docker & Docker Compose
- Python 3.10+
- [uv](https://docs.astral.sh/uv/)（包管理器，推荐） 或 pip
- Ollama（本地 LLM）/ MiniMax API Key（云端）

### 安装步骤

#### 快速开始（推荐 — uv 隔离环境）

```bash
# 1. 克隆项目
git clone <repo_url>
cd jishu_shenhe

# 2. 安装 Ollama 并下载模型（如果使用 Ollama）
brew install ollama
ollama serve
ollama pull qwen3.5:0.8b

# 3. 安装依赖（自动创建 .venv）
uv sync

# 4. 激活虚拟环境
source .venv/bin/activate

# 5. 生成示例文档
python scripts/generate_sample_doc.py

# 6. 运行验证测试
python scripts/verify_all.py
```

或使用 `uv run` 避免手动激活：

```bash
uv run python scripts/generate_sample_doc.py
uv run python scripts/verify_all.py
```

#### pip（无隔离环境）

```bash
pip install -r requirements.txt
```

### CLI 使用

```bash
# 知识库管理
python -m cli kb create --name "国家标准库" --category national
python -m cli kb list
python -m cli kb delete --id <kb_id>

# 文档导入
python -m cli doc import --kb-id <kb_id> --file sample_docs/sample_standard.pdf
python -m cli doc list --kb-id <kb_id>

# 索引管理
python -m cli index rebuild --kb-id <kb_id>
python -m cli index status --kb-id <kb_id>
```

### API 服务

```bash
# 启动 API 服务
uvicorn api.main:app --reload --port 8000

# 测试端点
curl http://localhost:8000/api/v1/health
curl http://localhost:8000/api/v1/knowledge-bases
```

### 前端界面

```bash
cd frontend
npm install
npm run dev
# 访问 http://localhost:3000
```

## 目录结构

```
jishu_shenhe/
├── api/                    # FastAPI REST API
│   ├── main.py            # 应用入口
│   └── routers/           # API 路由
├── cli/                   # CLI 工具
│   └── main.py           # Typer CLI
├── frontend/              # React 前端应用
│   ├── src/              # 源代码
│   │   ├── pages/       # 页面组件
│   │   ├── components/  # 通用组件
│   │   └── api/         # API 调用
│   └── package.json
├── services/              # 业务逻辑层
│   ├── kb_service.py      # 知识库服务
│   ├── doc_service.py     # 文档服务
│   └── indexing_service.py # 索引服务
├── storage/               # 存储层
│   ├── kb_repo.py        # 知识库存储
│   ├── doc_repo.py       # 文档存储
│   └── index_repo.py     # 索引存储
├── models/                # 数据模型
├── docker-compose.yml    # Docker 服务编排
├── tests/                 # 测试脚本
├── scripts/               # 工具脚本
├── sample_docs/           # 示例文档
└── docs/                  # 设计文档
```

```
jishu_shenhe/
├── docker-compose.yml      # Docker 服务编排
├── services/               # 各服务配置
│   └── ollama/             # Ollama 配置
├── tests/                  # 测试脚本
├── scripts/                # 工具脚本
├── sample_docs/            # 示例文档
└── docs/                   # 设计文档
    └── superpowers/
        ├── specs/          # 规范文档
        └── plans/          # 实现计划
```

## 技术栈

| 组件 | 技术 |
|------|------|
| LLM | Ollama / MiniMax / OpenAI（通过 `LLM_PROVIDER` 配置） |
| 检索引擎 | 本地向量检索（bge-m3 + numpy） |
| 后端框架 | FastAPI |
| 前端 | React + TypeScript + Vite + Tailwind CSS |
| 存储 | 文件系统 + JSON 元数据 |
| 部署 | Docker Compose（Ollama + API + 前端） |

## 文档

- [设计文档](docs/superpowers/specs/2026-04-21-技术文档审核系统设计.md)
- [阶段一实现计划](docs/superpowers/plans/2026-04-21-阶段一-基础设施搭建.md)