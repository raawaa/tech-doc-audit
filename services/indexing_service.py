import os
from pathlib import Path
from datetime import datetime

from models.document import KBDocument
import storage.doc_repo as doc_repo
import storage.index_repo as index_repo
import storage.kb_repo as kb_repo


def build_index_for_doc(doc: KBDocument, model: str = "qwen3.5:0.8b") -> KBDocument:
    """为单个文档构建 PageIndex 树索引。"""
    doc.index_status = "building"
    doc_repo._save_doc_meta(doc)

    try:
        tree = _generate_pageindex_tree(doc.file_path, model)
        index_path = index_repo.save_index(doc.kb_id, doc.id, tree)
        doc.tree_index_path = index_path
        doc.index_status = "ready"
    except Exception as e:
        doc.index_status = "failed"
        doc.metadata["index_error"] = str(e)

    doc_repo._save_doc_meta(doc)
    _update_kb_index_status(doc.kb_id)
    return doc


def rebuild_kb_index(kb_id: str, model: str = "qwen3.5:0.8b") -> None:
    """重建知识库所有文档的索引。"""
    kb = kb_repo.get(kb_id)
    if not kb:
        raise ValueError(f"知识库不存在: {kb_id}")

    kb.index_status = "building"
    kb_repo.update(kb)

    docs = doc_repo.list_docs(kb_id)
    for doc in docs:
        build_index_for_doc(doc, model)

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


def _generate_pageindex_tree(file_path: str, model: str) -> dict:
    """调用 PageIndex 生成树索引。"""
    try:
        from pageindex import PageIndexTree

        tree_builder = PageIndexTree(
            pdf_path=file_path,
            model=model,
            max_pages_per_node=5,
            max_tokens_per_node=15000
        )
        tree = tree_builder.generate()
        return tree
    except ImportError:
        # PageIndex 未安装时返回占位结构
        import json
        with open(file_path, "rb") as f:
            pass
        return _create_fallback_tree(file_path, model)
    except Exception as e:
        # 降级为简单结构
        return _create_fallback_tree(file_path, model)


def _create_fallback_tree(file_path: str, model: str) -> dict:
    """创建降级的树索引结构（当 PageIndex 不可用时）。"""
    # 尝试用 pdfplumber 提取文本作为基本信息
    content = {"title": Path(file_path).stem, "model": model, "nodes": []}
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

    content["generated_at"] = datetime.utcnow().isoformat()
    content["fallback"] = True
    content["note"] = "PageIndex 未安装，使用降级模式"
    return content
