from pathlib import Path
from dotenv import load_dotenv

# 加载 .env 配置
env_path = Path(__file__).resolve().parent.parent / ".env"
if env_path.exists():
    load_dotenv(env_path)

import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routers import knowledge_bases, documents, audit_docs, audit_tasks, kb_search_chat

app = FastAPI(
    title="技术文档审核系统 API",
    description="技术文档智能审核系统",
    version="0.1.0",
)

# CORS — 从环境变量读取允许的 origin 列表（逗号分隔）
# 默认开发端口：Vite 3000 / 5173
_allowed_origins = os.environ.get(
    "ALLOWED_ORIGINS",
    "http://localhost:3000,http://localhost:5173",
).split(",")
_allowed_origins = [o.strip() for o in _allowed_origins if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(knowledge_bases.router)
app.include_router(documents.router)
app.include_router(audit_docs.router)
app.include_router(audit_tasks.router)
app.include_router(kb_search_chat.router)


@app.get("/")
def root():
    return {"message": "技术文档审核系统 API", "version": "0.1.0"}


@app.get("/api/v1/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)