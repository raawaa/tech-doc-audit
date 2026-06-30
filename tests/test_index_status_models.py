"""模型 schema 与迁移路径测试。

独立文件以最快速度反馈 schema 改动；不依赖任何外部 fixture。
所有现有引用 doc.index_status / kb.index_status 的测试都迁到这里的等价表述。
"""

import pytest
from pydantic import ValidationError

from models.document import KBDocument
from models.knowledge_base import KnowledgeBase


# ── 知识库模型：终态从 ready 改为 searchable（ADR-0003）────────────────────


def test_kb_searchable_terminal_state_valid():
    """KB 终态词为 'searchable'；'ready' 不在合法值之内。"""
    kb = KnowledgeBase(
        name="kb",
        index_status="searchable",
    )
    assert kb.index_status == "searchable"


def test_kb_rejects_ready_value():
    """旧 'ready' 词不应被 schema 接受——属术语债，需在迁移后消失。"""
    with pytest.raises(ValidationError):
        KnowledgeBase(name="kb", index_status="ready")


def test_kb_loads_legacy_ready_as_searchable():
    """升级迁移：磁盘上旧 kb.json index_status='ready' → 加载为 'searchable'。"""
    legacy = {
        "name": "旧库",
        "description": "",
        "category": "national",
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
        "document_ids": [],
        "index_status": "ready",  # 旧词
    }
    kb = KnowledgeBase.from_dict(legacy)
    assert kb.index_status == "searchable"


# ── 文档模型：拆分出 embedding_status 字段（ADR-0003）────────────────────────────


def test_doc_default_embedding_status_is_none():
    """新 doc 实例默认 embedding_status='none'；不再使用 'index_status' 字段。"""
    doc = KBDocument(
        kb_id="kb1",
        name="x.md",
        original_name="x.md",
        file_type="md",
        file_path="/tmp/x.md",
    )
    assert doc.embedding_status == "none"
    assert not hasattr(doc, "index_status") or "index_status" not in doc.model_dump(
        exclude={"metadata", "file_path", "page_count", "tree_index_path",
                 "content_hash", "created_at", "updated_at", "id", "kb_id",
                 "name", "original_name", "file_type"}
    )


def test_doc_embedded_terminal_state_valid():
    """新终态词 'embedded' 应被 schema 接受。"""
    doc = KBDocument(
        kb_id="kb1",
        name="x.md",
        original_name="x.md",
        file_type="md",
        file_path="/tmp/x.md",
        embedding_status="embedded",
    )
    assert doc.embedding_status == "embedded"


def test_doc_loads_legacy_index_status_ready_as_embedded():
    """升级迁移：磁盘上旧 doc.json index_status='ready' → embedding_status='embedded'。

    'indexing' / 'pending_index' / 'failed' / 'none' 五个旧值映射不变。
    """
    base = {
        "kb_id": "kb1",
        "name": "x.md",
        "original_name": "x.md",
        "file_type": "md",
        "file_path": "/tmp/x.md",
    }

    # ready → embedded（终态词分裂）
    d = KBDocument.from_dict({**base, "index_status": "ready"})
    assert d.embedding_status == "embedded"

    # 其余值原样迁移
    for old, expected in [
        ("pending_index", "pending_index"),
        ("indexing", "indexing"),
        ("failed", "failed"),
        ("none", "none"),
    ]:
        d = KBDocument.from_dict({**base, "index_status": old})
        assert d.embedding_status == expected, (
            f"legacy {old!r} should migrate to {expected!r}, got {d.embedding_status!r}"
        )


def test_doc_prefers_explicit_embedding_status_on_load():
    """迁移幂等：若磁盘上已有新字段 embedding_status（重写后），不会被旧 index_status 覆盖。"""
    base = {
        "kb_id": "kb1",
        "name": "x.md",
        "original_name": "x.md",
        "file_type": "md",
        "file_path": "/tmp/x.md",
    }
    d = KBDocument.from_dict({
        **base,
        "index_status": "ready",       # 旧字段（将被忽略/处理）
        "embedding_status": "failed",  # 新字段已存在
    })
    assert d.embedding_status == "failed"
