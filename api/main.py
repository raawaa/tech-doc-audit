from pathlib import Path
from dotenv import load_dotenv

# 加载 .env 配置
env_path = Path(__file__).resolve().parent.parent / ".env"
if env_path.exists():
    load_dotenv(env_path)

import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routers import knowledge_bases, documents, audit_docs, audit_tasks, kb_search_chat, qa

app = FastAPI(
    title="技术文档审核系统 API",
    description="技术文档智能审核系统",
    version="0.1.0",
)

# ── 启动恢复：上次中断残留的状态清除 ────────────────────────────
# 如果服务器在后台索引进行时重启/崩溃，index_status 会永远卡在
# "building"（daemon 线程被强制终止，无法执行 set to "ready"）。
# 这里在启动时自动恢复 ― 将 stuck 状态的 KB/Doc 重置为 "none"。
import storage.kb_repo as kb_repo
import storage.doc_repo as doc_repo

_stuck_kbs = [kb for kb in kb_repo.list_all() if kb.index_status == "building"]
for kb in _stuck_kbs:
    kb.index_status = "none"
    kb.index_progress = None
    kb.index_current_doc = ""
    kb_repo.update(kb)
    print(f"[startup] 恢复卡住的 KB: {kb.name} ({kb.id}) → index_status=none")

for kb_dir in (kb_repo.KBS_DIR.iterdir() if kb_repo.KBS_DIR.exists() else []):
    if not kb_dir.is_dir():
        continue
    stuck_docs = [d for d in doc_repo.list_docs(kb_dir.name) if d.index_status == "pending_index"]
    for doc in stuck_docs:
        doc.index_status = "none"
        doc_repo._save_doc_meta(doc)
        print(f"[startup] 恢复卡住的文档: {doc.original_name} ({doc.id}) → index_status=none")

del _stuck_kbs

# 审核任务：将因上次服务器重启中断的 processing 任务标记为 failed
import storage.audit_task_repo as audit_task_repo
from datetime import datetime

_stuck_tasks = [t for t in audit_task_repo.list_tasks() if t.status == "processing"]
for task in _stuck_tasks:
    task.status = "failed"
    task.error_message = "审核任务因服务重启中断"
    task.completed_at = datetime.utcnow()
    audit_task_repo.save_task(task)
    print(f"[startup] 恢复卡住的审核任务: {task.document_name} ({task.id}) → failed")

del _stuck_tasks

# ─────────────────────────────────────────────────────────────

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
app.include_router(qa.router)


@app.get("/")
def root():
    return {"message": "技术文档审核系统 API", "version": "0.1.0"}


@app.get("/api/v1/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)