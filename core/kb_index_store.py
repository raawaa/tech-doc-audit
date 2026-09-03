"""一个 KB 的 FAISS + sidecar meta + vector cache + per-KB 锁的**全部承载者**(issue #168)。

为什么单独成模块(issue #165 拆分 PR-2):
- ``core.index_manager`` 同时持有 FAISS HNSW build/persist、sidecar
  meta 读写、vector cache 落盘、per-KB 锁。Code reviewer 验证"没有并发
  per-KB 写入"要在文件里 grep ``_get_index_lock`` 调用点——契约是隐式的。
- 抽到本类后,**入口只有** ``KBIndexStore.open(kb_id)``。所有写操作
  (``add_doc`` / ``remove_doc`` / ``rebuild_from_vectors``) 内部
  ``with self._lock:``,锁不再以模块私有符号的形式被外部 import。
- 单例化(``open`` 走 ``_instances`` dict)保证同一 ``kb_id`` 在不同调用
  方之间共享同一把锁与同一份 ``VectorStoreIndex`` 缓存。直接 ``KBIndexStore(
  kb_id)`` 构造被 ``__init__`` 显式拒绝——绕开 ``open()`` 会让锁不共享,
  失去唯一性保证。

公开 API(issue #168 AC #2 六方法):
- ``add_doc(doc_id, nodes, vectors)``     — 写向量缓存 + 插 FAISS + 落盘
- ``remove_doc(doc_id)``                  — 优先 ``delete_ref_doc``,降级 rebuild
- ``search(query_embedding, top_k)``      — 单 KB 向量检索
- ``rebuild_from_vectors(doc_ids)``       — 从 .npy 缓存重建 FAISS(CPU only)
- ``get_meta()``                          — 读 ``index.meta.json``
- ``assert_embedding_system_matches()``   — 写入前 meta 断言(防"非 bge-m3
                                              向量混入生产路径")

编排层(``index_document`` / ``index_documents_batch`` / ``rebuild_kb_index``)
仍留在 ``core.index_manager``——它做 chunking / metadata 富化 / page-num /
block-range 注入 / embed 重试,完成后再 ``store.add_doc(doc_id, nodes,
vectors)``。这跟 issue #165 后续 KBIndexWriter ticket 的边界一致
(orchestrator 与 storage 各司其职)。

私有边界:
- ``_lock`` / ``_index_cache`` / ``_vectors_dir()`` 等以下划线开头,但
  ``rebuild_kb_index`` 编排需要跨多个 store 调用持锁,本模块以
  ``acquire_write_lock()`` contextmanager 显式提供这条通道——这是
  issue #165 spec "code reviewer 可读一处验证 lock 语义" 的兑现。
- ``_inject_*`` 系列(chunk→layout 注入)不在本模块——见
  ``core.chunk_layout_mapper`` 后续 ticket。
"""
from __future__ import annotations

import contextlib
import json as _json
import os
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

import numpy as np
from llama_index.core import Settings, StorageContext, VectorStoreIndex
from llama_index.core.schema import TextNode
from llama_index.vector_stores.faiss import FaissVectorStore

import faiss

from core.logger import get_logger

_logger = get_logger(__name__)


def get_data_dir() -> Path:
    """解析数据根目录;每次调用读取 env(issue #137 per-test 隔离)。"""
    return Path(os.environ.get("AUDIT_DATA_DIR", "./data"))


# ── 单例化:同一 kb_id 在不同调用方之间共享同一份 KBIndexStore ──────────────
# open() 路径下:同一 kb_id → 同一实例 → 同一把 RLock + 同一份 _index_cache。
# 直接 KBIndexStore(kb_id) 被 __init__ 拒绝(见下),绕开本表会让锁变成多份,
# 失去"per-KB 唯一持有者"语义。

#: ``index.meta.json`` 的文件名——与 ``default__vector_store.json`` 同级。
#: 外部读 meta 路径的脚本(/scripts/backfill_kb_meta.py)沿用此常量名。
INDEX_META_FILENAME = "index.meta.json"


class KBIndexStore:
    """一个 KB 的 FAISS 索引 + sidecar meta + vector cache + per-KB 锁的承载者。

    单例: ``KBIndexStore.open(kb_id)`` 跨调用方共享同一实例(锁 + 缓存)。
    直接构造 ``KBIndexStore(kb_id)`` 会被 ``__init__`` 拒绝——issue #168 AC #1
    "Direct KBIndexStore(kb_id) construction is forbidden"。
    """

    # 单例表(类级别):key=kb_id, value=KBIndexStore 实例。
    # 类方法 + 类级锁,确保多线程同时 ``open`` 同一 kb_id 也只构造一次。
    _instances: dict[str, "KBIndexStore"] = {}
    _instances_lock = threading.Lock()

    def __init__(self, kb_id: str) -> None:
        """直接构造被拒绝——必须经 ``KBIndexStore.open(kb_id)``。

        ``__new__`` 路径下 ``__init__`` 仍会被调一次,显式 raise 防止
        任何途径的"绕过 open"。
        """
        raise RuntimeError(
            "KBIndexStore(kb_id) direct construction is forbidden; "
            "use KBIndexStore.open(kb_id) to share lock + cache across callers."
        )

    @classmethod
    def open(cls, kb_id: str) -> "KBIndexStore":
        """单例工厂:同一 ``kb_id`` 永远返回同一实例(锁 + 缓存都共享)。

        直接 ``KBIndexStore(kb_id)`` 会 raise(见 ``__init__``),所以本方法是
        唯一构造路径——也是 issue #168 AC "no external symbol exposes the lock"
        的硬兑现:外部拿不到 ``self._lock`` 的引用入口,只能通过 ``open`` 拿到
        单例化的实例。
        """
        with cls._instances_lock:
            inst = cls._instances.get(kb_id)
            if inst is not None:
                return inst
            # 绕过 __init__(它会 raise),手工设置属性——这是单例工厂的标准模式。
            inst = cls.__new__(cls)
            inst._kb_id = kb_id
            # RLock 原因:rebuild_kb_index 在编排层需要持锁期间多次
            # 调 ``add_doc`` / ``rebuild_from_vectors``;同一线程重入必须不死锁。
            inst._lock = threading.RLock()
            inst._index_cache: dict[str, VectorStoreIndex] = {}
            inst._ready = True
            cls._instances[kb_id] = inst
            return inst

    # ── 公开 API(issue #168 AC #2 六方法) ─────────────────────────────

    def add_doc(self, doc_id: str, nodes: list, vectors: list) -> None:
        """写一篇文档的向量 + 节点元数据,插入 FAISS,落盘。

        Contract:
          - ``nodes``: 已是 TextNode-like,带 ``node.text`` / ``node.metadata`` /
            ``node.embedding``(由编排层在调本方法前设置好)。
          - ``vectors``: 与 ``nodes`` 一一对应的 embedding 数组(列表形式),
            用于落 ``.npy`` 缓存。冗余但显式——避免本方法再从 ``node.embedding``
            拆 list。
          - 写入前断言 ``index.meta.json`` 与当前 provider 一致(issue #144
            AC #3,防 repro_kb/d1.npy 那种非 bge-m3 向量混入)。
          - 整个流程在 ``self._lock`` 内,串行化 FAISS 操作。
        """
        with self._lock:
            self.assert_embedding_system_matches()
            self._save_doc_vectors(doc_id, nodes, vectors)
            index = self._get_index()
            index.insert_nodes(nodes)
            self._persist(index)

    def remove_doc(self, doc_id: str) -> None:
        """从 KB 索引中删除指定文档。

        优先 ``index.delete_ref_doc`` 快速路径(FAISS 直接按 ref_doc 删除);
        失败时降级到 ``rebuild_from_vectors`` 从 .npy 缓存重建(无需 GPU)。
        降级路径里,无剩余文档时清理 ``vectors/`` 目录但保留 ``index.meta.json``
        (issues/144 AC #3:meta 是 production KB 元数据,不能随向量被物理删除)。
        """
        with self._lock:
            index = self._get_index()
            # 快速路径:delete_ref_doc 直接从索引删除
            try:
                if (
                    hasattr(index.vector_store, "_faiss_index")
                    and hasattr(index.vector_store._faiss_index, "remove_ids")
                ):
                    index.delete_ref_doc(doc_id, delete_from_docstore=True)
                    self._persist(index)
                    self._cleanup_doc_vectors(doc_id)
                    _logger.info(
                        "removed doc %s from kb %s via delete_ref_doc",
                        doc_id, self._kb_id,
                    )
                    return
            except Exception as e:
                _logger.warning(
                    "vector-level deletion failed for %s/%s (%s), "
                    "fallback to rebuild from cached vectors",
                    self._kb_id, doc_id, e,
                )

            # 降级路径:从已保存的 .npy 向量重建索引(无需 GPU)
            _logger.info(
                "rebuilding kb %s index from cached vectors after removing doc %s",
                self._kb_id, doc_id,
            )
            # 缓存置空:避免重建时仍从内存旧索引读
            self._index_cache.pop(self._kb_id, None)

            import storage.kb_repo as kb_repo
            kb = kb_repo.get(self._kb_id)
            if not kb:
                return

            remaining_ids = [did for did in kb.document_ids if did != doc_id]
            vectors_dir = self._vectors_dir()

            if not remaining_ids:
                meta_p = self._index_meta_path()
                meta_existed = meta_p.exists()
                if vectors_dir.exists():
                    shutil.rmtree(str(vectors_dir))
                if meta_existed:
                    # 重建空 vectors/(已经在 rmtree 里被删);只保留 meta
                    vectors_dir.mkdir(parents=True, exist_ok=True)
                    self._write_index_meta(force=True)
                return

            # 从向量缓存重建(成功后 _persist 会覆盖旧 FAISS 文件)
            self.rebuild_from_vectors(remaining_ids)
            self._cleanup_doc_vectors(doc_id)

    def search(self, query_embedding: list, top_k: int) -> list:
        """单 KB 向量检索;返回 ``[NodeWithScore]``(供 ``core.index_manager.search``
        在多 KB 合并后统一格式化 hit dict)。

        在 ``self._lock`` 内——保留旧 ``_get_index_lock`` 的"读也持锁"语义,
        防止读路径撞上正在 persist 的半完成 FAISS。

        Args:
            query_embedding: 已经由编排层(``core.index_manager.search``)编码好
                的查询向量——本层不做 query embedding,与"查询路径零附加重试"
                的 ADR-0007 §2 语义一致。
        """
        from llama_index.core.schema import QueryBundle

        with self._lock:
            index = self._get_index()
            retriever = index.as_retriever(similarity_top_k=top_k)
            # ``QueryBundle(embedding=...)`` 让 retriever 直接用预先编码好的
            # 向量,不再走 embedder——等价于旧 ``retriever.retrieve(query)``
            # 内部 ``_get_query_embedding`` 看到 ``embedding`` 已设就跳过编码
            # 的分支(见 ``BaseRetriever._get_query_embedding_batch``)。
            bundle = QueryBundle(query_str="", embedding=query_embedding)
            return retriever.retrieve(bundle)

    def rebuild_from_vectors(
        self, doc_ids: list[str], progress_callback=None,
    ) -> None:
        """从已落盘的 ``.npy`` 向量文件重建 FAISS 索引(CPU only,无需 GPU)。

        用于 rebuild 的 cached-vectors fast path:从 ``{doc_id}.npy`` 加载向量,
        从 ``{doc_id}_nodes.json`` 加载节点文本/元数据,重建 ``TextNode`` 并插入
        新索引。重建后 ``index.meta.json`` 留用现有 meta;无 meta 时(纯裸重建)
        写一份给生产体系 = bge-m3,防 issues/144 AC #3 的断言挂掉。

        在 ``self._lock`` 内,且会替换 ``_index_cache[kb_id]`` 为新建的空索引
        ——配合外部 ``acquire_write_lock()`` 使用,避免与并发 ``add_doc`` 撞车。
        """
        with self._lock:
            vectors_dir = self._vectors_dir()
            new_index = self._create_index()
            self._index_cache[self._kb_id] = new_index

            total = len(doc_ids)
            loaded = 0
            for i, doc_id in enumerate(doc_ids, 1):
                vec_file = vectors_dir / f"{doc_id}.npy"
                nodes_file = vectors_dir / f"{doc_id}_nodes.json"

                if not vec_file.exists():
                    _logger.warning(
                        "vector cache missing for doc %s, will need re-embedding",
                        doc_id,
                    )
                    if progress_callback:
                        progress_callback(i, total, doc_id)
                    continue

                vectors = np.load(str(vec_file))

                # 加载节点元数据(文本 + metadata)
                if not nodes_file.exists():
                    _logger.error(
                        "vector cache incomplete for doc %s: "
                        ".npy exists but _nodes.json missing. "
                        "This doc will be skipped in rebuild. "
                        "Run `index rebuild --kb-id %s` to re-embed.",
                        doc_id, self._kb_id,
                    )
                    if progress_callback:
                        progress_callback(i, total, doc_id)
                    continue

                try:
                    nodes_data = _json.loads(nodes_file.read_text())
                except Exception as e:
                    _logger.error(
                        "failed to load nodes metadata for doc %s (%s). "
                        "This doc will be skipped in rebuild.",
                        doc_id, e,
                    )
                    if progress_callback:
                        progress_callback(i, total, doc_id)
                    continue

                if len(nodes_data) != len(vectors):
                    _logger.error(
                        "node count mismatch for doc %s: "
                        "%d nodes in _nodes.json vs %d vectors in .npy. "
                        "This doc will be skipped in rebuild.",
                        doc_id, len(nodes_data), len(vectors),
                    )
                    if progress_callback:
                        progress_callback(i, total, doc_id)
                    continue

                nodes = []
                for j, vec in enumerate(vectors):
                    nd = nodes_data[j]
                    nodes.append(TextNode(
                        text=nd.get("text", ""),
                        id_=nd.get("node_id", f"{doc_id}_{j}"),
                        metadata=nd.get("metadata", {}),
                        embedding=vec.tolist() if hasattr(vec, "tolist") else list(vec),
                    ))

                new_index.insert_nodes(nodes)
                loaded += 1

                if progress_callback:
                    progress_callback(i, total, doc_id)

            self._persist(new_index)

            # issues/144 AC #3:rebuild 路径不写新 chunk,但必须确保
            # ``index.meta.json`` 存在,后续 ``index_document`` 写入前断言可过。
            # 留用现有 meta;若没有(极端:纯裸重建)写一份(给生产体系 = bge-m3)。
            if self.get_meta() is None:
                self._write_index_meta(force=True)
            _logger.info(
                "rebuilt index for kb %s from %d/%d docs (cached vectors)",
                self._kb_id, loaded, len(doc_ids),
            )

    def get_meta(self) -> Optional[dict]:
        """读 ``index.meta.json``;缺失返回 ``None``(backfill 待补)。

        不持锁——只是 read 文件,与 FAISS 操作正交;若调用方处于写流程中,
        外部编排(``add_doc`` 等)已经持锁,本方法不会破坏不变式。
        """
        p = self._index_meta_path()
        if not p.exists():
            return None
        try:
            return _json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            _logger.warning(
                "failed to read index.meta.json for kb %s: %s",
                self._kb_id, e,
            )
            return None

    def assert_embedding_system_matches(
        self, *, model_id: str = "BAAI/bge-m3", dim: int = 1024,
    ) -> None:
        """断言 KB 索引体系的 ``embedding_model_id`` / ``embedding_dim`` 与
        当前 provider 给定的一致(issues/144 AC #3,写入新 chunk 前必调)。

        Contract:
          - meta 文件缺失:**raise RuntimeError**("需先跑 backfill 写入")——
            缺失意味着当前 KB 还没建立"该用什么向量"的明确立场,任何"看起来
            安全"的隐式假定都是错的(spec 措辞"否则 raise,不入库")。
            推荐先跑 ``scripts/backfill_kb_meta.py``。
          - 已有 meta 但 ``model_id`` / ``dim`` 不符:**raise RuntimeError**,
            不入库。这是防止"非 bge-m3 向量混入生产路径"的硬关
            (T4 §5.1 spike 复盘)。
        """
        meta = self.get_meta()
        if meta is None:
            raise RuntimeError(
                f"kb {self._kb_id} 缺 index.meta.json;add_doc 不入库。"
                f"请先跑 scripts/backfill_kb_meta.py 一次性回填"
                f"(model_id={model_id}, dim={dim})."
            )
        existing_id = meta.get("embedding_model_id")
        existing_dim = meta.get("embedding_dim")
        if existing_id != model_id or existing_dim != dim:
            raise RuntimeError(
                f"kb {self._kb_id} embedding 体系不一致:"
                f"index.meta.json 记录 ({existing_id!r}, dim={existing_dim}),"
                f"当前 provider 要求 ({model_id!r}, dim={dim})."
                f"可能混入非 {model_id} 向量(T4 §5.1 spike 复盘);禁止入库。"
            )

    # ── 编排层通道:跨多次 store 调用的持锁 ─────────────────────────────

    @contextlib.contextmanager
    def acquire_write_lock(self) -> Iterator[None]:
        """持锁一段时间——给编排层(rebuild_kb_index)跨多次 store 调用持锁用。

        单次 store 调用(``add_doc`` / ``remove_doc`` / ``rebuild_from_vectors``
        / ``search``)自己 ``with self._lock:``,不需要本 contextmanager。
        编排层需要"先清缓存,再 rebuild,再 add_doc(在缓存里),全程串行"
        时,用 ``with store.acquire_write_lock():`` 包外层;内部调用再 ``with
        self._lock:`` 因为是 RLock 不会死锁。

        为什么是 contextmanager 而不是直接暴露 ``self._lock``:
        - ``self._lock`` 是 RLock 实例,外部拿到后可以 ``lock.acquire()``
          但配对 ``release()`` 在异常路径上易漏——contextmanager 强制配对。
        - contextmanager 也屏蔽了"我能不能拿 RLock?"的判断——给编排层一个
          唯一、显式的入口。
        """
        with self._lock:
            yield

    # ── 内部:路径 / meta / FAISS HNSW build+persist ─────────────────────

    def _vectors_dir(self) -> Path:
        return get_data_dir() / "kbs" / self._kb_id / "vectors"

    def _index_meta_path(self) -> Path:
        return self._vectors_dir() / INDEX_META_FILENAME

    def _write_index_meta(
        self, *, model_id: str = "BAAI/bge-m3", dim: int = 1024,
        created_at: Optional[str] = None,
        force: bool = False,
    ) -> None:
        """原子写入 ``index.meta.json``(``scripts/backfill_kb_meta.py`` 也调用)。

        Args:
            model_id / dim: 来自 provider 的当前标识(``BAAI/bge-m3`` / 1024)。
            created_at: ISO8601 字符串;``None`` 时自动生成当前时间。
            force: ``True`` 时强制覆盖(给 backfill 脚本用);``False`` 时
                已存在则保留原 ``created_at`` 不变(只更新 ``updated_at``)。
        """
        p = self._index_meta_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        now = created_at or datetime.now(timezone.utc).isoformat()
        if force:
            payload = {
                "embedding_model_id": model_id,
                "embedding_dim": dim,
                "created_at": now,
            }
        else:
            existing = self.get_meta() or {}
            payload = {
                "embedding_model_id": model_id,
                "embedding_dim": dim,
                "created_at": existing.get("created_at", now),
            }
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(
            _json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.rename(p)

    def _create_index(self, dim: int = 1024) -> VectorStoreIndex:
        """创建新的空 FAISS 索引(HNSW,支持高效 ANN 搜索)。

        注意:不套 ``IndexIDMap``,因为 llama-index 的 ``FaissVectorStore.add()``
        只使用 faiss ``add()`` 而非 ``add_with_ids()``,IDMap 会导致崩溃。
        向量级删除(``remove_doc``)降级到全量重建路径,代码已支持。
        """
        from core.settings import get_embed_model
        get_embed_model()
        # HNSW: 高效的近似最近邻索引,O(log n) 搜索
        hnsw_index = faiss.IndexHNSWFlat(dim, 32)
        hnsw_index.hnsw.efConstruction = 200  # 建图质量(越大越准)
        hnsw_index.hnsw.efSearch = 64         # 搜索精度
        vector_store = FaissVectorStore(faiss_index=hnsw_index)
        storage_context = StorageContext.from_defaults(vector_store=vector_store)
        return VectorStoreIndex(
            nodes=[],
            storage_context=storage_context,
            embed_model=Settings.embed_model,
        )

    def _load_index(self) -> Optional[VectorStoreIndex]:
        """从磁盘加载已有 FAISS 索引;失败返回 ``None``(让 ``_get_index``
        走新建空索引路径)。"""
        vectors_dir = self._vectors_dir()
        store_file = vectors_dir / "default__vector_store.json"
        if not store_file.exists():
            return None
        try:
            from core.settings import get_embed_model
            get_embed_model()
            faiss_index = faiss.read_index(str(store_file))
            vector_store = FaissVectorStore(faiss_index=faiss_index)

            from llama_index.core.storage.docstore import SimpleDocumentStore
            from llama_index.core.storage.index_store import SimpleIndexStore
            docstore = SimpleDocumentStore.from_persist_dir(str(vectors_dir))
            index_store = SimpleIndexStore.from_persist_dir(str(vectors_dir))

            storage_context = StorageContext.from_defaults(
                vector_store=vector_store,
                docstore=docstore,
                index_store=index_store,
            )

            index_struct = None
            for is_ in index_store.index_structs():
                index_struct = is_
                break

            return VectorStoreIndex(
                nodes=[],
                index_struct=index_struct,
                storage_context=storage_context,
                embed_model=Settings.embed_model,
            )
        except Exception as e:
            _logger.warning(
                "failed to load index for kb %s: %s", self._kb_id, e,
            )
            return None

    def _get_index(self) -> VectorStoreIndex:
        """获取 KB 的 ``VectorStoreIndex``(加载或创建,带内存缓存)。

        调用方必须在 ``self._lock`` 内——读 + 写 cache 替换都要原子。
        """
        cached = self._index_cache.get(self._kb_id)
        if cached is not None:
            return cached
        index = self._load_index() or self._create_index()
        self._index_cache[self._kb_id] = index
        return index

    def _persist(self, index: VectorStoreIndex) -> None:
        """持久化 FAISS 索引 + docstore 到磁盘。"""
        vectors_dir = self._vectors_dir()
        vectors_dir.mkdir(parents=True, exist_ok=True)
        index.storage_context.persist(persist_dir=str(vectors_dir))

    def _save_doc_vectors(
        self, doc_id: str, nodes: list, embeddings: list,
    ) -> None:
        """保存文档的 embedding 向量和节点元数据到磁盘(``.npy`` + ``_nodes.json``)。

        每个文档保存两个文件:
        - ``{doc_id}.npy``: float32 向量矩阵 (n_chunks, 1024)
        - ``{doc_id}_nodes.json``: 节点元数据列表 ``[{node_id, text, metadata}, ...]``

        先写 ``_nodes.json`` 再写 ``.npy``:``.npy`` 存在 ⇔ 向量缓存完整,
        重建时以此判断。写入顺序保证崩溃后不会出现"``.npy`` 存在但
        ``_nodes.json`` 缺失"的半完成状态。

        这些文件使索引重建时无需重新 embedding(纯 CPU 操作)。
        """
        vectors_dir = self._vectors_dir()
        vectors_dir.mkdir(parents=True, exist_ok=True)

        # 先写节点元数据(非原子写入可能崩溃残留,但 .npy 不存在时不会触发重建)
        nodes_data = []
        for node in nodes:
            nodes_data.append({
                "node_id": node.node_id,
                "text": node.text or "",
                "metadata": node.metadata or {},
            })
        nodes_file = vectors_dir / f"{doc_id}_nodes.json"
        nodes_tmp = vectors_dir / f"{doc_id}_nodes.json.tmp"
        nodes_tmp.write_text(
            _json.dumps(nodes_data, ensure_ascii=False),
            encoding="utf-8",
        )
        nodes_tmp.rename(nodes_file)

        # 后写向量(np.save 内部写临时文件 + rename,原子操作)
        vec_array = np.array(embeddings, dtype=np.float32)
        np.save(str(vectors_dir / f"{doc_id}.npy"), vec_array)

    def _cleanup_doc_vectors(self, doc_id: str) -> None:
        """删除文档的向量缓存文件(``.npy`` + ``_nodes.json``)。"""
        vectors_dir = self._vectors_dir()
        for suffix in (".npy", "_nodes.json"):
            f = vectors_dir / f"{doc_id}{suffix}"
            if f.exists():
                f.unlink()


# ── 单例化辅助:测试 / 集成场景需要"清空单例表"时用 ────────────────────────────
# ``clear_cache()`` 在 ``core.index_manager`` 的语义是"清空内存里的
# ``VectorStoreIndex`` 缓存"。在 KBIndexStore 里,缓存是实例属性,所以
# 清除必须重置单例表本身——否则旧单例仍持有旧缓存。
# ``scripts/backfill_kb_meta.py`` 之类的脚本在 import 后会自然落到新的
# ``KBIndexStore.open(kb_id)`` 路径上,不需要单独清缓存。


def reset_singletons() -> None:
    """清空 ``KBIndexStore._instances``——测试隔离 / 进程重启场景。

    不公开到 ``core.index_manager``(那里是 ``clear_cache``);仅本模块自用,
    ``core.index_manager.clear_cache`` 会调到这里。
    """
    with KBIndexStore._instances_lock:
        KBIndexStore._instances.clear()
