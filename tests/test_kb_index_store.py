"""``core.kb_index_store.KBIndexStore`` 单元测试(issue #168 AC #5)。

覆盖四个核心保证:
1. HNSW build/persist
2. Sidecar meta read/write
3. Vector cache round-trip
4. Concurrent add_doc serialization
"""
import threading
import pytest
from core.kb_index_store import (
    INDEX_META_FILENAME,
    KBIndexStore,
    reset_singletons,
)


@pytest.fixture(autouse=True)
def _use_fake_models(fake_models):
    yield


@pytest.fixture(autouse=True)
def _isolate_kb_state(tmp_path, monkeypatch):
    reset_singletons()
    monkeypatch.setenv("AUDIT_DATA_DIR", str(tmp_path))
    yield
    reset_singletons()


def _seed_kb(kb_id):
    import storage.kb_repo as kb_repo
    from models.knowledge_base import KnowledgeBase
    kb = KnowledgeBase(id=kb_id, name="seed", category="national")
    kb_repo.update(kb)
    kb = kb_repo.get(kb_id)
    kb.index_status = "searchable"
    kb_repo.update(kb)
    KBIndexStore.open(kb_id)._write_index_meta(force=True)


def _make_text_node(text, embedding, *, doc_id):
    from llama_index.core.schema import TextNode
    return TextNode(
        text=text,
        id_=f"{doc_id}_node_{abs(hash(text)) % 99999}",
        metadata={"doc_id": doc_id},
        embedding=embedding,
    )


def test_direct_construction_is_forbidden():
    """Issue #168 AC #1:直接 KBIndexStore(kb_id) 必须 raise。"""
    with pytest.raises(RuntimeError, match="direct construction is forbidden"):
        KBIndexStore("kb_direct_construction")  # noqa: F841


def test_open_returns_singleton_for_same_kb_id():
    """open(kb_id) 单例工厂:同一 kb_id 跨调用方共享同一实例。"""
    store_a = KBIndexStore.open("kb_singleton")
    store_b = KBIndexStore.open("kb_singleton")
    assert store_a is store_b
    reset_singletons()
    store_c = KBIndexStore.open("kb_singleton")
    assert store_c is not store_a


def test_hnsw_build_creates_empty_index():
    """KBIndexStore._create_index() 返回带 HNSW 索引的 VectorStoreIndex。"""
    store = KBIndexStore.open("kb_hnsw_build")
    index = store._create_index()
    faiss_index = index.vector_store._faiss_index
    assert faiss_index.d == 1024
    assert faiss_index.hnsw.efConstruction == 200
    assert faiss_index.hnsw.efSearch == 64
    assert faiss_index.ntotal == 0


def test_hnsw_persist_round_trip():
    """索引写入后 _persist 落盘;再 _load_index 重新读出。"""
    store = KBIndexStore.open("kb_hnsw_persist")
    index = store._create_index()
    store._persist(index)
    store_file = store._vectors_dir() / "default__vector_store.json"
    assert store_file.exists()
    reset_singletons()
    fresh_store = KBIndexStore.open("kb_hnsw_persist")
    reloaded = fresh_store._load_index()
    assert reloaded is not None
    assert reloaded.vector_store._faiss_index.ntotal == 0


def test_add_doc_persists_index_to_disk():
    """add_doc 落盘 FAISS + docstore + .npy + _nodes.json。"""
    _seed_kb("kb_add_persist")
    store = KBIndexStore.open("kb_add_persist")
    nodes = [_make_text_node(
        "测试 add_doc 持久化的内容文本,长度足够通过 20 字符检查。",
        [0.1] * 1024, doc_id="d1",
    )]
    store.add_doc("d1", nodes, [[0.1] * 1024])
    vectors_dir = store._vectors_dir()
    assert (vectors_dir / "default__vector_store.json").exists()
    assert (vectors_dir / "docstore.json").exists()
    assert (vectors_dir / f"d1.npy").exists()
    assert (vectors_dir / f"d1_nodes.json").exists()


def test_get_meta_returns_none_when_missing():
    store = KBIndexStore.open("kb_meta_missing")
    assert store.get_meta() is None


def test_write_then_read_meta_round_trip():
    store = KBIndexStore.open("kb_meta_round")
    store._write_index_meta(force=True)
    meta = store.get_meta()
    assert meta is not None
    assert meta["embedding_model_id"] == "BAAI/bge-m3"
    assert meta["embedding_dim"] == 1024
    assert "created_at" in meta


def test_write_meta_without_force_preserves_created_at():
    store = KBIndexStore.open("kb_meta_preserve")
    store._write_index_meta(force=True)
    first = store.get_meta()["created_at"]
    store._write_index_meta(force=False)
    second = store.get_meta()["created_at"]
    assert second == first


def test_assert_embedding_system_matches_raises_when_missing():
    store = KBIndexStore.open("kb_assert_missing")
    with pytest.raises(RuntimeError, match="缺 index.meta.json"):
        store.assert_embedding_system_matches()


def test_assert_embedding_system_matches_raises_on_mismatch():
    store = KBIndexStore.open("kb_assert_mismatch")
    store._write_index_meta(force=True)
    with pytest.raises(RuntimeError, match="embedding 体系不一致"):
        store.assert_embedding_system_matches(model_id="BAAI/bge-m3", dim=999)


def test_assert_embedding_system_matches_passes_on_consistent_meta():
    store = KBIndexStore.open("kb_assert_ok")
    store._write_index_meta(force=True)
    store.assert_embedding_system_matches()


def test_index_meta_filename_constant_matches_disk():
    store = KBIndexStore.open("kb_filename")
    store._write_index_meta(force=True)
    assert (store._vectors_dir() / INDEX_META_FILENAME).exists()
    assert INDEX_META_FILENAME == "index.meta.json"


def test_save_doc_vectors_writes_npy_and_nodes_json():
    _seed_kb("kb_cache_save")
    store = KBIndexStore.open("kb_cache_save")
    nodes = [
        _make_text_node(f"chunk {i}", [float(i)] * 1024, doc_id="d_cache")
        for i in range(3)
    ]
    embeddings = [[float(i)] * 1024 for i in range(3)]
    store._save_doc_vectors("d_cache", nodes, embeddings)
    vectors_dir = store._vectors_dir()
    assert (vectors_dir / "d_cache.npy").exists()
    assert (vectors_dir / "d_cache_nodes.json").exists()
    import numpy as np
    arr = np.load(str(vectors_dir / "d_cache.npy"))
    assert arr.dtype == np.float32
    assert arr.shape == (3, 1024)


def test_rebuild_from_vectors_round_trips_nodes():
    _seed_kb("kb_cache_rebuild")
    store = KBIndexStore.open("kb_cache_rebuild")
    original_text = "向量缓存重建 round-trip 测试文本,验证重建后内容可还原。"
    original_node = _make_text_node(original_text, [0.42] * 1024, doc_id="d_rt")
    store.add_doc("d_rt", [original_node], [[0.42] * 1024])
    store._index_cache.pop("kb_cache_rebuild", None)
    progress_log = []
    store.rebuild_from_vectors(
        ["d_rt"],
        progress_callback=lambda i, t, n: progress_log.append((i, t, n)),
    )
    rebuilt = store._get_index()
    # 注:rebuilt docstore 的 key 是 node_id,不是 doc_id;用 metadata["doc_id"]
    # 找回原 doc。ref_doc_id 在 insert_nodes 时未设置(LlamaIndex 仅当节点
    # 有 ``source_document`` 时设 ref_doc_id)。
    ref_doc_ids = {
        node.metadata.get("doc_id") for node in rebuilt.docstore.docs.values()
    }
    assert "d_rt" in ref_doc_ids
    assert len(progress_log) == 1


def test_rebuild_from_vectors_writes_meta_when_missing():
    store = KBIndexStore.open("kb_meta_autobackfill")
    assert store.get_meta() is None
    store.rebuild_from_vectors([])
    assert store.get_meta() is not None
    assert store.get_meta()["embedding_model_id"] == "BAAI/bge-m3"


def test_cleanup_doc_vectors_removes_both_files():
    _seed_kb("kb_cleanup")
    store = KBIndexStore.open("kb_cleanup")
    nodes = [_make_text_node("clean me", [0.5] * 1024, doc_id="d_clean")]
    store._save_doc_vectors("d_clean", nodes, [[0.5] * 1024])
    vectors_dir = store._vectors_dir()
    assert (vectors_dir / "d_clean.npy").exists()
    assert (vectors_dir / "d_clean_nodes.json").exists()
    store._cleanup_doc_vectors("d_clean")
    assert not (vectors_dir / "d_clean.npy").exists()
    assert not (vectors_dir / "d_clean_nodes.json").exists()


def test_concurrent_add_doc_serializes():
    _seed_kb("kb_concurrent")
    store = KBIndexStore.open("kb_concurrent")

    def _add_doc(doc_id, n_chunks):
        nodes = [
            _make_text_node(
                f"{doc_id} chunk {i} — 足够长的文本,通过 20 字符检查。",
                [float(i)] * 1024, doc_id=doc_id,
            )
            for i in range(n_chunks)
        ]
        embeddings = [[float(i)] * 1024 for i in range(n_chunks)]
        store.add_doc(doc_id, nodes, embeddings)

    errors = []

    def _runner(doc_id, n):
        try:
            _add_doc(doc_id, n)
        except BaseException as e:
            errors.append(e)

    t1 = threading.Thread(target=_runner, args=("d_concurrent_a", 3))
    t2 = threading.Thread(target=_runner, args=("d_concurrent_b", 3))
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert not errors
    store._index_cache.pop("kb_concurrent", None)
    rebuilt = store._get_index()
    ref_doc_ids = [node.metadata.get("doc_id") for node in rebuilt.docstore.docs.values()]
    n_a = sum(1 for d in ref_doc_ids if d == "d_concurrent_a")
    n_b = sum(1 for d in ref_doc_ids if d == "d_concurrent_b")
    assert n_a == 3
    assert n_b == 3


def test_acquire_write_lock_releases_on_exception():
    store = KBIndexStore.open("kb_ctxmgr")
    with pytest.raises(ValueError):
        with store.acquire_write_lock():
            raise ValueError("test")
    with store.acquire_write_lock():
        pass
