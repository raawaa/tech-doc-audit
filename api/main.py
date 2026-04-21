from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routers import knowledge_bases, documents

app = FastAPI(
    title="技术文档审核系统 API",
    description="基于 Dify + PageIndex 的技术文档智能审核系统",
    version="0.1.0",
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(knowledge_bases.router)
app.include_router(documents.router)


@app.get("/")
def root():
    return {"message": "技术文档审核系统 API", "version": "0.1.0"}


@app.get("/api/v1/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)