"""``storage.doc_repo`` 的失败态转移测试(issue #167)。

``mark_doc_embedding_failed`` 是 doc ``embedding_status="failed"`` 的**唯一
公开入口**(ADR-0007 §3 每稿隔离)。取代历史上藏在
``core.index_manager._mark_doc_embedding_failed`` 的私有实现——放在 repo 层
后,"谁在写 failed"用一次 grep 就能穷举。

契约要点:best-effort。doc 不在 repo(脚本直调 ``index_documents_batch``)、
读盘/写盘出错,一律 log warning 后返回,**不抛** —— 让批量流程挂在"元数据
写不上"是本末倒置。
"""
import pytest

import storage.doc_repo as doc_repo
from models.document import KBDocument

_KB_ID = "kb_mark_failed"


def _seed_doc(doc_id: str = "doc_1", **kwargs) -> KBDocument:
    """落一个 doc meta 到磁盘(``_per_test_data_dir`` 已把 data 根隔离到 tmp)。"""
    doc = KBDocument(
        id=doc_id,
        kb_id=_KB_ID,
        name="标准.pdf",
        original_name="标准.pdf",
        file_type="pdf",
        file_path=f"/nonexistent/{doc_id}.pdf",
        **kwargs,
    )
    doc_repo._save_doc_meta(doc)
    return doc


def test_marks_status_failed_and_records_error():
    """正路:落盘的 doc 状态转 failed,原因写进 ``metadata['embedding_error']``。"""
    _seed_doc(embedding_status="indexing")

    doc_repo.mark_doc_embedding_failed(_KB_ID, "doc_1", ValueError("向量维度不符"))

    saved = doc_repo.get_doc(_KB_ID, "doc_1")
    assert saved.embedding_status == "failed"
    assert saved.metadata["embedding_error"] == "ValueError: 向量维度不符"


def test_error_string_carries_exception_type():
    """``embedding_error`` 形如 ``TypeName: message``——运维靠类型名分诊。"""
    _seed_doc()

    doc_repo.mark_doc_embedding_failed(_KB_ID, "doc_1", RuntimeError("CUDA OOM"))

    saved = doc_repo.get_doc(_KB_ID, "doc_1")
    assert saved.metadata["embedding_error"] == "RuntimeError: CUDA OOM"


def test_err_none_marks_failed_without_error_detail():
    """``err=None``:仍然转 failed,但不编造原因。"""
    _seed_doc(embedding_status="indexing")

    doc_repo.mark_doc_embedding_failed(_KB_ID, "doc_1")

    saved = doc_repo.get_doc(_KB_ID, "doc_1")
    assert saved.embedding_status == "failed"
    assert "embedding_error" not in saved.metadata


def test_err_none_clears_stale_error_from_previous_failure():
    """``embedding_error`` 描述的是**本次**失败;没有本次原因就不能留上次的。"""
    _seed_doc(metadata={"embedding_error": "APIConnectionError: 上一次的原因"})

    doc_repo.mark_doc_embedding_failed(_KB_ID, "doc_1")

    saved = doc_repo.get_doc(_KB_ID, "doc_1")
    assert "embedding_error" not in saved.metadata


def test_is_idempotent_on_already_failed_doc():
    """已是 failed 再标一次:状态不变,原因更新为最新一次。"""
    _seed_doc()

    doc_repo.mark_doc_embedding_failed(_KB_ID, "doc_1", ValueError("第一次"))
    doc_repo.mark_doc_embedding_failed(_KB_ID, "doc_1", ValueError("第二次"))

    saved = doc_repo.get_doc(_KB_ID, "doc_1")
    assert saved.embedding_status == "failed"
    assert saved.metadata["embedding_error"] == "ValueError: 第二次"


def test_preserves_unrelated_metadata():
    """只碰 ``embedding_error``,doc 上其它 metadata 原样保留。"""
    _seed_doc(metadata={"page_count_source": "paddleocr"})

    doc_repo.mark_doc_embedding_failed(_KB_ID, "doc_1", ValueError("boom"))

    saved = doc_repo.get_doc(_KB_ID, "doc_1")
    assert saved.metadata["page_count_source"] == "paddleocr"
    assert saved.metadata["embedding_error"] == "ValueError: boom"


def test_missing_doc_is_a_noop_and_does_not_raise():
    """doc 不在 repo(脚本直调批量索引)→ warning 后跳过,不抛、不建文件。"""
    doc_repo.mark_doc_embedding_failed(_KB_ID, "doc_absent", ValueError("boom"))

    assert doc_repo.get_doc(_KB_ID, "doc_absent") is None


def test_load_failure_does_not_raise(monkeypatch):
    """读 doc 出错(meta 半截 / 磁盘问题)→ 吞掉,批量流程继续。"""
    _seed_doc()

    def _boom(kb_id, doc_id):
        raise OSError("meta 读到一半")

    monkeypatch.setattr(doc_repo, "get_doc", _boom)
    doc_repo.mark_doc_embedding_failed(_KB_ID, "doc_1", ValueError("boom"))


def test_persist_failure_does_not_raise(monkeypatch):
    """写 doc meta 出错 → 吞掉。失败态写不上不该反过来打断整批。"""
    _seed_doc()

    def _boom(doc):
        raise OSError("磁盘满")

    monkeypatch.setattr(doc_repo, "_save_doc_meta", _boom)
    doc_repo.mark_doc_embedding_failed(_KB_ID, "doc_1", ValueError("boom"))


def test_repairs_non_dict_metadata(monkeypatch):
    """``metadata`` 不是 dict(历史脏数据)→ 就地修成 dict 再写,不抛。"""
    doc = _seed_doc()
    doc.metadata = None
    monkeypatch.setattr(doc_repo, "get_doc", lambda kb_id, doc_id: doc)
    captured = {}
    monkeypatch.setattr(doc_repo, "_save_doc_meta", lambda d: captured.update(doc=d))

    doc_repo.mark_doc_embedding_failed(_KB_ID, "doc_1", ValueError("boom"))

    assert captured["doc"].metadata == {"embedding_error": "ValueError: boom"}
    assert captured["doc"].embedding_status == "failed"
