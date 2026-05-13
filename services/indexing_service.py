import os
import sys
from pathlib import Path
from datetime import datetime

from models.document import KBDocument
import storage.doc_repo as doc_repo
import storage.index_repo as index_repo
import storage.kb_repo as kb_repo

# 确保 PageIndex 可导入
# 优先尝试通过 pip 安装的 pageindex；dev 环境下从本地源码加载
try:
    import pageindex  # noqa: F401 — 验证 pip 安装版本
except ImportError:
    # 开发环境回退：从 ../../Code/PageIndex 加载
    _dev_path = Path(__file__).resolve().parent.parent.parent / "Code" / "PageIndex"
    if _dev_path.exists() and str(_dev_path) not in sys.path:
        sys.path.insert(0, str(_dev_path))


def _get_llm_model() -> str:
    """根据当前 LLM_PROVIDER 返回 litellm 兼容的模型名。"""
    provider = os.environ.get("LLM_PROVIDER", "ollama").lower().strip()
    if provider in ("minimax", "minimax-cn"):
        return os.environ.get("MINIMAX_CN_MODEL", "MiniMax-M2.7")
    return os.environ.get("OLLAMA_MODEL", "qwen3.5:0.8b")


def _setup_litellm_for_pageindex():
    """配置 litellm 环境变量以匹配当前 LLM 提供商。"""
    provider = os.environ.get("LLM_PROVIDER", "ollama").lower().strip()
    if provider in ("minimax", "minimax-cn"):
        os.environ.setdefault("OPENAI_API_KEY", os.environ.get("MINIMAX_CN_API_KEY", ""))
        os.environ.setdefault("OPENAI_API_BASE", os.environ.get("MINIMAX_CN_BASE_URL", "https://api.minimaxi.com/v1"))
    elif provider == "ollama":
        base = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        os.environ.setdefault("OPENAI_API_BASE", f"{base}/v1")
        os.environ.setdefault("OPENAI_API_KEY", "ollama")


def build_index_for_doc(doc: KBDocument) -> KBDocument:
    """为单个文档构建 PageIndex 树索引。"""
    doc.index_status = "building"
    doc_repo._save_doc_meta(doc)

    try:
        tree = _generate_pageindex_tree(doc.file_path)
        index_path = index_repo.save_index(doc.kb_id, doc.id, tree)
        doc.tree_index_path = index_path
        doc.index_status = "ready"
    except Exception as e:
        doc.index_status = "failed"
        doc.metadata["index_error"] = str(e)

    doc_repo._save_doc_meta(doc)
    _update_kb_index_status(doc.kb_id)
    return doc


def rebuild_kb_index(kb_id: str) -> None:
    """重建知识库所有文档的索引。"""
    kb = kb_repo.get(kb_id)
    if not kb:
        raise ValueError(f"知识库不存在: {kb_id}")

    kb.index_status = "building"
    kb_repo.update(kb)

    docs = doc_repo.list_docs(kb_id)
    for doc in docs:
        build_index_for_doc(doc)

    _update_kb_index_status(kb_id)


def _update_kb_index_status(kb_id: str) -> None:
    """根据文档索引状态更新知识库整体索引状态。"""
    kb = kb_repo.get(kb_id)
    if not kb:
        return

    docs = doc_repo.list_docs(kb_id)
    if not docs:
        kb.index_status = "none"
    elif all(d.index_status == "ready" for d in docs):
        kb.index_status = "ready"
    elif any(d.index_status == "building" for d in docs):
        kb.index_status = "building"
    else:
        kb.index_status = "failed"
    kb_repo.update(kb)


def _generate_pageindex_tree(file_path: str) -> dict:
    """调用 PageIndex 生成树索引。"""
    _setup_litellm_for_pageindex()

    try:
        from pageindex import page_index_main
        from pageindex.utils import ConfigLoader

        model = _get_llm_model()
        model_str = f"openai/{model}"  # litellm 格式

        opt = ConfigLoader().load({
            "model": model_str,
            "if_add_node_summary": "yes",
            "if_add_node_text": "yes",  # 需要文本做搜索
            "if_add_doc_description": "yes",
            "max_token_num_each_node": 15000,
        })

        # page_index_main 返回树结构
        tree = page_index_main(doc=file_path, opt=opt)
        return tree

    except Exception as e:
        # 降级：返回简化结构
        return _create_fallback_tree(file_path)


def _create_fallback_tree(file_path: str) -> dict:
    """创建降级的树索引结构（当 PageIndex 不可用时）。"""
    content = {
        "title": Path(file_path).stem,
        "fallback": True,
        "note": "PageIndex 树生成失败，使用降级模式",
        "nodes": [],
        "generated_at": datetime.utcnow().isoformat(),
    }
    if file_path.lower().endswith(".pdf"):
        try:
            import pdfplumber
            with pdfplumber.open(file_path) as pdf:
                pages_text = []
                for i, page in enumerate(pdf.pages):
                    text = page.extract_text()
                    if text:
                        pages_text.append({"page": i + 1, "text": text[:500]})
                content["pages"] = pages_text
        except Exception:
            pass
    return content
