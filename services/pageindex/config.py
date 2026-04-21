# services/pageindex/config.py
"""PageIndex 服务配置"""

import os

# LLM 配置（使用 Ollama）
LLM_BASE_URL = os.getenv("OPENAI_API_BASE", "http://localhost:11434/v1")
LLM_API_KEY = os.getenv("OPENAI_API_KEY", "sk-dummy")
LLM_MODEL_NAME = os.getenv("LLM_MODEL_NAME", "qwen3.5:0.8b")

# PageIndex 工作路径
PAGEINDEX_WORK_PATH = os.getenv("PAGEINDEX_WORK_PATH", "/root/.pageindex")

# 文档存储路径
DOCUMENT_PATH = os.getenv("DOCUMENT_PATH", "/data/documents")