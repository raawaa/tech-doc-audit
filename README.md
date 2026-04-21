# 技术文档审核系统

基于 Dify + PageIndex 的技术文档智能审核系统。

## 项目概述

用户上传技术文档（如招标文件），系统通过知识库（技术标准、规范）对比审核，生成详细的审核报告。

## 当前阶段

**阶段四：审核核心流程** ✓

已完成：
- 审核任务管理（创建/查询/取消）
- 知识库检索服务
- LLM 审核分析
- 结果生成与汇总
- API + CLI 双入口

**阶段三：文档处理** ✓
- 待审核文档上传、解析、结构识别

**阶段二：知识库管理** ✓
- 知识库 CRUD API
- 文档导入与索引

**阶段一：基础设施搭建** ✓
- Ollama + Qwen3.5-0.8B
- PageIndex 验证

## 快速开始

### 前置要求

- Docker & Docker Compose
- Python 3.10+
- Ollama（本地安装）

### 安装步骤

1. 克隆项目
```bash
git clone <repo_url>
cd jishu_shenhe
```

2. 安装 Ollama 并下载模型
```bash
# macOS
brew install ollama
ollama serve

# 下载模型
ollama pull qwen3.5:0.8b
```

3. 安装 Python 依赖
```bash
pip install -r requirements.txt
```

4. 生成示例文档
```bash
python scripts/generate_sample_doc.py
```

5. 运行验证测试
```bash
python scripts/verify_all.py
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

## 目录结构

```
jishu_shenhe/
├── api/                    # FastAPI REST API
│   ├── main.py            # 应用入口
│   └── routers/           # API 路由
├── cli/                   # CLI 工具
│   └── main.py           # Typer CLI
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
│   ├── ollama/             # Ollama 配置
│   └── pageindex/          # PageIndex 配置
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
| LLM | Ollama + Qwen3.5-0.8B |
| 检索引擎 | PageIndex |
| 应用平台 | Dify（后续阶段） |
| 后端 | FastAPI（后续阶段） |
| 前端 | React/Vue（后续阶段） |
| 数据库 | PostgreSQL（后续阶段） |

## 文档

- [设计文档](docs/superpowers/specs/2026-04-21-技术文档审核系统设计.md)
- [阶段一实现计划](docs/superpowers/plans/2026-04-21-阶段一-基础设施搭建.md)