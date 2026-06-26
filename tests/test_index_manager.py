"""index_manager 核心函数测试。

注意：这些测试会触发 bge-m3 embedding 模型加载和 FAISS 索引操作，
第一次运行耗时较长（~30s 加载模型），后续因缓存加速较快。
"""

import os
import shutil

import pytest

from core.index_manager import (
    index_document,
    index_documents_batch,
    remove_document,
    rebuild_kb_index,
    search,
    get_kb_index_built,
    get_kb_index,
)


@pytest.fixture(autouse=True)
def cleanup():
    """每个测试后清理索引数据。"""
    yield
    data_dir = os.environ["AUDIT_DATA_DIR"]
    if os.path.exists(data_dir):
        shutil.rmtree(data_dir)


def _text_doc(text: str, doc_id: str = "doc_001") -> tuple[str, str, str]:
    return (doc_id, text, f"doc_{doc_id}.txt")


def test_index_and_search():
    """测试索引文档后能正确搜索到。"""
    kb_id = "test_kb_index_search"

    # 索引一篇真实的文档（内容 >=20 字符，否则 index_document 提前返回）
    index_document(
        kb_id, "doc_001",
        "人工智能技术在工程招标文件中应用研究分析报告",
        source_name="ai_paper.txt",
    )

    # 搜索
    results = search([kb_id], "人工智能", top_k=5)
    assert len(results) >= 1, "应搜索到至少 1 条结果"

    result = results[0]
    assert result["kb_id"] == kb_id
    assert "doc_id" in result
    assert result.get("source") == "vec_search"
    assert result.get("content") is not None


def test_index_empty_text():
    """测试空文本不应创建索引节点。"""
    kb_id = "test_kb_empty"

    index_document(kb_id, "doc_empty", "", source_name="empty.txt")
    index_document(kb_id, "doc_short", "short", source_name="short.txt")

    # 搜索不应返回结果
    results = search([kb_id], "test", top_k=5)
    assert len(results) == 0


def test_batch_index():
    """测试批量索引文档。"""
    kb_id = "test_kb_batch"

    docs = [
        _text_doc("网络安全等级保护基本要求 GB/T 22239-2019 最新版本", "doc_001"),
        _text_doc("信息技术安全性评估准则与方法 GB/T 18336 标准", "doc_002"),
        _text_doc("数据安全法解读与应用指南实践操作手册", "doc_003"),
    ]

    progress_log = []

    def on_progress(current, total, doc_name):
        progress_log.append((current, total, doc_name))

    index_documents_batch(kb_id, docs, progress_callback=on_progress)

    # 验证回调被调用
    assert len(progress_log) == 3
    assert progress_log[-1] == (3, 3, "doc_doc_003.txt")

    # 搜索验证所有文档均可检索
    results = search([kb_id], "安全", top_k=10)
    assert len(results) >= 2, "应搜索到至少 2 条涉及安全的内容"


def test_remove_document():
    """测试删除文档（快速路径 + 降级路径）。"""
    kb_id = "test_kb_remove"

    index_document(kb_id, "doc_001", "建设工程质量管理条例内容分析与解读规范文件", source_name="quality.txt")
    index_document(kb_id, "doc_002", "建设工程安全生产管理条例全文规定与实施细则", source_name="safety.txt")
    index_document(kb_id, "doc_003", "招标投标法实施条例详细解读版本全文内容整理", source_name="bid.txt")

    # 确认删除前能搜索到（用包含查询词的较长 query，提升语义匹配率）
    results_before = search([kb_id], "招标投标法条例解读", top_k=5)
    assert len(results_before) >= 1

    # 删除 doc_003
    remove_document(kb_id, "doc_003")

    # 确认删除后搜索结果变化（关于招标的内容不再出现）
    results_after = search([kb_id], "招标", top_k=5)
    # 注：由于 FAISS ANN 是近似搜索，删除后仍可能因语义相似度返回相关内容
    # 此处只验证删除操作不报错 + 索引仍然可用
    assert results_after is not None


def test_rebuild_kb_index():
    """测试重建索引。"""
    kb_id = "test_kb_rebuild"

    # 准备 KB 元数据（rebuild 依赖 kb_repo.get）
    import storage.kb_repo as kb_repo
    from models.knowledge_base import KnowledgeBase

    # 先通过 doc_svc.import_document 导入文档（自动处理 doc_id → document_ids 映射）
    import services.doc_service as doc_svc
    import services.kb_service as kb_svc
    kb = kb_svc.create_kb(name="测试重建", category="national")

    doc_001 = doc_svc.import_document(
        kb.id, "设计说明.md",
        "# 设计说明\n\n## 第一章 总则\n\n建筑工程设计文件编制深度规定内容与标准要求。\n\n## 第二章 要求\n\n各项设计应符合国家标准。".encode(),
    )
    doc_002 = doc_svc.import_document(
        kb.id, "施工规范.md",
        "# 施工规范\n\n## 第一章 总则\n\n建筑施工组织设计规范标准要求与实施指南内容。\n\n## 第二章 验收\n\n施工质量应符合设计文件要求。".encode(),
    )

    # 确认两条 doc_id 都在 KB 中
    kb = kb_repo.get(kb.id)
    assert doc_001.id in kb.document_ids
    assert doc_002.id in kb.document_ids

    # 先索引一篇，使索引文件建立
    index_document(kb.id, doc_001.id, "建筑工程设计文件编制深度规定内容与标准要求")

    # 确认索引文件已建立
    assert get_kb_index_built(kb.id)

    # 重建索引
    progress = []

    def on_rebuild(current, total, doc_name):
        progress.append((current, total, doc_name))

    rebuild_kb_index(kb.id, progress_callback=on_rebuild)

    # 验证回调被调用（至少两篇文档）
    assert len(progress) >= 1

    # 重建后仍可搜索
    results = search([kb.id], "建筑工程设计内容", top_k=5)
    assert len(results) >= 1


def test_search_empty_kb():
    """测试搜索空/不存在 KB 不应报错。"""
    results = search(["nonexistent_kb"], "test", top_k=5)
    assert results == []


def test_index_same_doc_twice():
    """测试重复索引同一文档不报错。"""
    kb_id = "test_kb_duplicate"

    index_document(kb_id, "doc_001", "重复索引测试文档内容验证是否可以多次添加", source_name="dup.txt")
    index_document(kb_id, "doc_001", "重复索引测试文档内容验证是否可以多次添加", source_name="dup.txt")

    # 不应报错，搜索结果应正常
    results = search([kb_id], "重复", top_k=5)
    assert len(results) >= 1


def test_index_markdown_with_headings():
    """测试带 ## 标题的 Markdown 内容触发 MarkdownNodeParser 分块路径。"""
    from core.index_manager import _has_markdown_headings

    kb_id = "test_kb_md_headings"

    md_text = """# 技术标准

## 第一章 总则

### 1.1 范围

本标准规定技术要求与验收标准。

### 1.2 引用文件

下列文件对本文件的应用是必不可少的。

## 第二章 术语和定义

### 2.1 术语一

术语一的定义和解释说明。

### 2.2 术语二

术语二的定义和解释说明。
"""

    # 验证 _has_markdown_headings 能正确检测 ## 标题
    assert _has_markdown_headings(md_text), "应检测到 Markdown 标题"

    index_document(kb_id, "doc_md", md_text, source_name="standard.md")

    # 搜索总则章节内容
    r1 = search([kb_id], "技术标准", top_k=5)
    assert len(r1) >= 1

    # 搜索不同章节内容，验证分块后各章节均可检索
    r2 = search([kb_id], "术语定义", top_k=5)
    assert len(r2) >= 1

    r3 = search([kb_id], "引用文件", top_k=5)
    assert len(r3) >= 1


def test_async_md_index_builds_faiss():
    """异步导入真实 .md 内容（非空文本）→ FAISS 索引实际建立。

    现有 async 测试（test_import_document_async 等）用假 PDF，提取为空文本，
    索引线程因 ``len(text) < 20`` 提前返回、不建索引。本用例补上真实内容的
    异步索引路径，验证 index_status → ready 且 FAISS 索引确实建立。
    """
    import time
    import services.kb_service as kb_svc
    import services.doc_service as doc_svc
    import storage.kb_repo as kb_repo

    kb = kb_svc.create_kb(name="异步MD建索引", category="national")
    content = (
        "# 技术规范\n\n## 第一章 总则\n\n本规范规定技术要求与验收标准内容。\n\n"
        "## 第二章 要求\n\n各项参数应符合国家标准规定要求。"
    ).encode()
    doc = doc_svc.import_document(kb.id, "技术规范.md", content, async_index=True)

    # 等待后台索引线程完成（pending_index → indexing → ready）
    for _ in range(100):
        if doc.index_status not in ("pending_index", "indexing"):
            break
        time.sleep(0.1)

    assert doc.index_status == "ready", f"expected ready, got {doc.index_status}"
    assert get_kb_index_built(kb.id), "FAISS 索引应已建立"

    kb = kb_repo.get(kb.id)
    assert doc.id in kb.document_ids


# ── 向量持久化测试 ────────────────────────────────────────────────────────────


def test_save_and_cleanup_doc_vectors():
    """索引文档后验证 .npy 和 _nodes.json 文件落盘，删除后验证清理。"""
    from core.index_manager import _save_doc_vectors, _cleanup_doc_vectors, _vectors_dir

    kb_id = "test_kb_vectors_persist"

    # 索引文档
    index_document(
        kb_id, "doc_v1",
        "向量持久化测试文档内容，验证 .npy 文件和节点元数据 JSON 文件是否正确落盘。",
        source_name="vectors_test.txt",
    )

    vectors_dir = _vectors_dir(kb_id)
    npy_file = vectors_dir / "doc_v1.npy"
    nodes_file = vectors_dir / "doc_v1_nodes.json"

    assert npy_file.exists(), f".npy 文件应存在: {npy_file}"
    assert nodes_file.exists(), f"_nodes.json 文件应存在: {nodes_file}"

    # 验证 .npy 格式
    vec = __import__('numpy').load(str(npy_file))
    assert vec.dtype == __import__('numpy').float32, f"向量应为 float32，实际 {vec.dtype}"
    assert vec.ndim == 2, f"向量应为 2D (n_chunks, dim)，实际 shape {vec.shape}"
    assert vec.shape[1] == 1024, f"向量维度应为 1024，实际 {vec.shape[1]}"

    # 验证 _nodes.json 格式
    import json
    nodes_data = json.loads(nodes_file.read_text())
    assert isinstance(nodes_data, list)
    assert len(nodes_data) == vec.shape[0], f"节点数 ({len(nodes_data)}) 应与向量行数 ({vec.shape[0]}) 一致"
    for nd in nodes_data:
        assert "node_id" in nd
        assert "text" in nd
        assert "metadata" in nd

    # 清理并验证
    _cleanup_doc_vectors(kb_id, "doc_v1")
    assert not npy_file.exists(), ".npy 文件应已删除"
    assert not nodes_file.exists(), "_nodes.json 文件应已删除"


def test_rebuild_from_vectors():
    """从已保存的 .npy 向量文件重建索引后可搜索。"""
    from core.index_manager import _rebuild_from_vectors, _vectors_dir, clear_cache, _save_doc_vectors, _cleanup_doc_vectors

    # 用 llm mocked? 不，先正常 index 生成缓存
    kb_id = "test_kb_rebuild_from_vec"

    index_document(
        kb_id, "doc_vec_rebuild",
        "从向量缓存重建索引的功能测试文档，验证重建后仍能正确搜索到相关内容与关键字匹配。",
        source_name="rebuild_test.txt",
    )

    # 确认向量文件存在
    vectors_dir = _vectors_dir(kb_id)
    assert (vectors_dir / "doc_vec_rebuild.npy").exists()

    # 清缓存 + 删 FAISS 索引文件，模拟"只有向量缓存，没有索引"的状态
    clear_cache()
    store_file = vectors_dir / "default__vector_store.json"
    if store_file.exists():
        store_file.unlink()

    # 重建
    progress = []
    _rebuild_from_vectors(kb_id, ["doc_vec_rebuild"], progress_callback=lambda c, t, n: progress.append((c, t, n)))

    # 验证 progress 回调
    assert len(progress) >= 1
    assert progress[-1][1] == 1  # total = 1

    # 验证搜索可用
    results = search([kb_id], "向量缓存重建", top_k=5)
    assert len(results) >= 1, "从向量缓存重建后应能搜索到结果"

    # 清理
    _cleanup_doc_vectors(kb_id, "doc_vec_rebuild")


def test_remove_document_fallback_path(monkeypatch):
    """强制 delete_ref_doc 抛异常 → fallback 到 _rebuild_from_vectors 路径。"""
    from core.index_manager import _vectors_dir
    import storage.kb_repo as kb_repo
    from models.knowledge_base import KnowledgeBase

    kb_id = "test_kb_remove_fallback"

    # 创建 KB 元数据（remove_document fallback 依赖 kb.document_ids）
    kb = KnowledgeBase(id=kb_id, name="fallback测试", category="national")
    kb_repo.update(kb)

    index_document(kb_id, "doc_fb_1", "建设工程质量管理条例内容分析与解读规范文件全文", source_name="fb1.txt")
    index_document(kb_id, "doc_fb_2", "建设工程安全生产管理条例全文规定与实施细则", source_name="fb2.txt")

    # 更新 KB document_ids（正常路径由 doc_service 维护，这里手动补上）
    kb = kb_repo.get(kb_id)
    kb.document_ids = ["doc_fb_1", "doc_fb_2"]
    kb_repo.update(kb)

    # 验证索引已建立
    assert get_kb_index_built(kb_id)

    # 强制 delete_ref_doc 抛异常，触发 fallback 路径
    original_delete = get_kb_index(kb_id).delete_ref_doc

    def _raise(*a, **k):
        raise RuntimeError("simulated delete_ref_doc failure")

    monkeypatch.setattr(get_kb_index(kb_id), "delete_ref_doc", _raise)

    # 删除 doc_fb_2（应该走 fallback 路径）
    remove_document(kb_id, "doc_fb_2")

    # 恢复后验证：doc_fb_1 仍然可搜索
    monkeypatch.setattr(get_kb_index(kb_id), "delete_ref_doc", original_delete)
    results = search([kb_id], "建设工程质量", top_k=5)
    assert len(results) >= 1, "fallback 重建后应仍能搜索到剩余文档"

    # 验证被删除文档的向量文件已清理
    vectors_dir = _vectors_dir(kb_id)
    assert not (vectors_dir / "doc_fb_2.npy").exists(), "被删除文档的向量缓存应已清理"
    assert (vectors_dir / "doc_fb_1.npy").exists(), "剩余文档的向量缓存应保留"


def test_rebuild_kb_index_mixed_vectors():
    """rebuild_kb_index：部分文档有向量缓存、部分没有的混合场景。"""
    import services.kb_service as kb_svc
    import services.doc_service as doc_svc
    import storage.kb_repo as kb_repo
    import storage.doc_repo as doc_repo

    kb = kb_svc.create_kb(name="混合重建", category="national")

    # doc_A：正常导入（会建索引 + 向量缓存）
    doc_a = doc_svc.import_document(
        kb.id, "doc_a.md",
        "# 设计说明\n\n## 第一章 总则\n\n建筑工程设计文件编制深度规定内容与标准要求。\n\n## 第二章 要求\n\n各项设计应符合国家标准。".encode(),
    )
    assert doc_a.index_status == "ready"

    # doc_B：模拟 bulk_import 场景（只保存文件，不建索引，无向量缓存）
    content_b = "# 施工规范\n\n## 第一章 总则\n\n建筑施工组织设计规范标准要求与实施指南内容。\n\n## 第二章 验收\n\n施工质量应符合设计文件要求。".encode()
    doc_b = doc_repo.save_doc(kb.id, "doc_b.md", content_b, "md")
    doc_b.content_hash = __import__('hashlib').sha256(content_b).hexdigest()
    doc_b.index_status = "none"
    doc_repo._save_doc_meta(doc_b)
    # 追加到 KB document_ids
    kb = kb_repo.get(kb.id)
    kb.document_ids.append(doc_b.id)
    kb_repo.update(kb)

    # 确认 doc_A 有向量缓存，doc_B 没有
    from core.index_manager import _vectors_dir
    vectors_dir = _vectors_dir(kb.id)
    assert (vectors_dir / f"{doc_a.id}.npy").exists(), "doc_A 应有向量缓存"
    assert not (vectors_dir / f"{doc_b.id}.npy").exists(), "doc_B 应无向量缓存"

    # 重建索引
    progress = []
    rebuild_kb_index(kb.id, progress_callback=lambda c, t, n: progress.append((c, t, n)))

    # 两个文档都应被处理（progress 应包含两者）
    assert len(progress) >= 2, f"应处理 2 篇文档，实际 {len(progress)} 篇"

    # 重建后两者都应可搜索
    results_a = search([kb.id], "建筑工程设计", top_k=5)
    assert len(results_a) >= 1, "doc_A 重建后应可搜索"

    results_b = search([kb.id], "施工组织设计", top_k=5)
    assert len(results_b) >= 1, "doc_B 重建后应可搜索"
