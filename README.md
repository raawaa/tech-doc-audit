# 技术文档审核系统

基于 Dify + PageIndex 的技术文档智能审核系统。

## 项目概述

用户上传技术文档（如招标文件），系统通过知识库（技术标准、规范）对比审核，生成详细的审核报告。

## 当前阶段

**阶段一：基础设施搭建与技术验证** ✓

已验证：
- Ollama + Qwen3.5-0.8B 本地 LLM 推理
- PageIndex 文档索引与检索
- PDF/Word 文档解析

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

## 目录结构

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