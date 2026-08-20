"""KB VectorStoreIndex 生命周期管理。

每个知识库（KB）对应一个独立的 FAISS 索引文件。
索引加载后缓存在内存中，避免重复读盘。
"""

import gc
import json as _json
import os
import threading
import shutil
from pathlib import Path
from typing import Optional

import numpy as np

from core.logger import get_logger

_logger = get_logger(__name__)

import faiss
from llama_index.core import VectorStoreIndex, StorageContext, Document, Settings
from llama_index.core.node_parser import SentenceSplitter, MarkdownNodeParser
from llama_index.core.schema import TextNode
from llama_index.vector_stores.faiss import FaissVectorStore

from core.settings import get_embed_model, get_gpu_inference_lock
from core.parse_document import PageText, PageLayout
from core.embed_retry import embed_batch_with_retry
from core.text_norm import _block_matches_chunk, norm
from core.kb_index_status import KbIndexStatusWriter
import storage.doc_repo as doc_repo


def get_data_dir() -> Path:
    """解析数据根目录；每次调用读取 env（issue #137 per-test 隔离）。"""
    return Path(os.environ.get("AUDIT_DATA_DIR", "./data"))


# 内存缓存: kb_id -> VectorStoreIndex
_index_cache: dict[str, VectorStoreIndex] = {}

# per-KB 可重入锁：防止并发索引同一 KB 导致 FAISS 死锁
# 使用 RLock 因为 rebuild_kb_index/remove_document 会递归调用 index_document
_index_locks: dict[str, threading.RLock] = {}
_index_locks_lock = threading.Lock()


def _get_index_lock(kb_id: str) -> threading.RLock:
    """获取 KB 对应的可重入锁（线程安全创建）。"""
    with _index_locks_lock:
        if kb_id not in _index_locks:
            _index_locks[kb_id] = threading.RLock()
        return _index_locks[kb_id]


# ── 内部路径 ────────────────────────────────────────────────────────────────────

def _vectors_dir(kb_id: str) -> Path:
    return get_data_dir() / "kbs" / kb_id / "vectors"


def _ensure_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)


# ── 索引元数据 sidecar：存放 embedding 体系元信息 ────────────────────────────
# 由 issue #144 acceptance criteria 第 1 项 + 第 2 项引入。
# 每个 KB 索引目录必须有 ``index.meta.json``，含 ``embedding_model_id`` +
# ``embedding_dim`` + ``created_at``；写入新 chunk 前断言一致，防止
# ``repro_kb/d1.npy`` 这种"非 bge-m3 向量混入生产路径"的事件再次发生
# (T4 §5.1 spike 已观察到一次)。
#
# 不变量：
# - ``embedding_model_id``:当前生产路径写 ``"BAAI/bge-m3"``(无论 local / SF,
#   模型本身相同,字面 ID 一致);
# - ``embedding_dim``:1024(SF 与本机 bge-m3 实测一致,T3 §1.2);
# - ``created_at``:meta 首次落盘时间,AtomicWrite,不可回改。
#
# 旧 KB(144 之前生产索引)在 #144 验收前由一次性脚本
# ``scripts/backfill_kb_meta.py`` 写入；新写入的 KB 由 ``_save_index_meta``
# 自动创建。

#: ``index.meta.json`` 的文件名——与 ``default__vector_store.json`` 同级。
INDEX_META_FILENAME = "index.meta.json"


def _index_meta_path(kb_id: str) -> Path:
    return _vectors_dir(kb_id) / INDEX_META_FILENAME


def _read_index_meta(kb_id: str) -> Optional[dict]:
    """读取 KB 的 ``index.meta.json``；缺失返回 None(backfill 待补)。"""
    p = _index_meta_path(kb_id)
    if not p.exists():
        return None
    try:
        return _json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        _logger.warning("failed to read index.meta.json for kb %s: %s", kb_id, e)
        return None


def _write_index_meta(kb_id: str, *, model_id: str, dim: int,
                     created_at: Optional[str] = None,
                     force: bool = False) -> None:
    """原子写入 ``index.meta.json``。

    Args:
        model_id / dim: 来自 provider 的当前标识(``BAAI/bge-m3`` / 1024)。
        created_at: ISO8601 字符串;None 时自动生成当前时间。
        force: True 时强制覆盖(给 backfill 脚本用);False 时已存在则保留原
               ``created_at`` 不变(只更新 ``updated_at``)。
    """
    from datetime import datetime, timezone
    p = _index_meta_path(kb_id)
    _ensure_dir(p.parent)
    now = (created_at or datetime.now(timezone.utc).isoformat())
    if force:
        payload = {
            "embedding_model_id": model_id,
            "embedding_dim": dim,
            "created_at": now,
        }
    else:
        existing = _read_index_meta(kb_id) or {}
        payload = {
            "embedding_model_id": model_id,
            "embedding_dim": dim,
            "created_at": existing.get("created_at", now),
        }
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(_json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.rename(p)


def _assert_kb_embedding_system_matches(kb_id: str, *, model_id: str,
                                        dim: int) -> None:
    """断言 KB 索引体系的 ``embedding_model_id`` / ``embedding_dim`` 与
    当前 provider 给定的一致(写入新 chunk 前必调,issues/144 AC#3)。

    Contract:
      - meta 文件缺失:**raise RuntimeError**("需先跑 backfill 写入")——
        缺失意味着当前 KB 还没建立"该用什么向量"的明确立场,任何"看起来
        安全"的隐式假定都是错的(spec 措辞"否则 raise,不入库")。
        推荐先跑 ``scripts/backfill_kb_meta.py``。
      - 已有 meta 但 model_id / dim 不符:**raise RuntimeError**,不入库。
        这是防止"非 bge-m3 向量混入生产路径"的硬关(T4 §5.1 spike 复盘)。
    """
    meta = _read_index_meta(kb_id)
    if meta is None:
        raise RuntimeError(
            f"kb {kb_id} 缺 index.meta.json;index_document 不入库。"
            f"请先跑 scripts/backfill_kb_meta.py 一次性回填"
            f"(model_id={model_id}, dim={dim})."
        )
    existing_id = meta.get("embedding_model_id")
    existing_dim = meta.get("embedding_dim")
    if existing_id != model_id or existing_dim != dim:
        raise RuntimeError(
            f"kb {kb_id} embedding 体系不一致:index.meta.json 记录 "
            f"({existing_id!r}, dim={existing_dim}),"
            f"当前 provider 要求 ({model_id!r}, dim={dim})."
            f"可能混入非 {model_id} 向量(T4 §5.1 spike 复盘);禁止入库。"
        )


# ── 索引创建 / 加载 / 缓存 ─────────────────────────────────────────────────────

def _create_index(dim: int = 1024) -> VectorStoreIndex:
    """创建新的空 FAISS 索引（HNSW，支持高效 ANN 搜索）。

    注意：不套 IndexIDMap，因为 llama-index 的 FaissVectorStore.add()
    只使用 faiss `add()` 而非 `add_with_ids()`，IDMap 会导致崩溃。
    向量级删除（remove_document）降级到全量重建路径，代码已支持。
    """
    # 确保 embedding 模型已初始化
    get_embed_model()
    # HNSW: 高效的近似最近邻索引，O(log n) 搜索
    hnsw_index = faiss.IndexHNSWFlat(dim, 32)
    hnsw_index.hnsw.efConstruction = 200  # 建图质量（越大越准）
    hnsw_index.hnsw.efSearch = 64         # 搜索精度
    vector_store = FaissVectorStore(faiss_index=hnsw_index)
    storage_context = StorageContext.from_defaults(vector_store=vector_store)
    # 直接用 VectorStoreIndex 构造，传入空 nodes + storage_context（含 FaissVectorStore）
    index = VectorStoreIndex(
        nodes=[],
        storage_context=storage_context,
        embed_model=Settings.embed_model,
    )
    return index


def _load_index(kb_id: str) -> Optional[VectorStoreIndex]:
    """从磁盘加载已有 FAISS 索引。"""
    vectors_dir = _vectors_dir(kb_id)
    store_file = vectors_dir / "default__vector_store.json"
    if not store_file.exists():
        return None
    try:
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

        # 从已加载的 index_store 中获取已有的 index_struct
        index_struct = None
        for is_ in index_store.index_structs():
            index_struct = is_
            break

        index = VectorStoreIndex(
            nodes=[],
            index_struct=index_struct,
            storage_context=storage_context,
            embed_model=Settings.embed_model,
        )
        return index
    except Exception as e:
        _logger.warning("failed to load index for kb %s: %s", kb_id, e)
        return None


def get_kb_index(kb_id: str) -> VectorStoreIndex:
    """获取 KB 的 VectorStoreIndex（加载或创建，带内存缓存）。"""
    if kb_id in _index_cache:
        return _index_cache[kb_id]
    index = _load_index(kb_id) or _create_index()
    _index_cache[kb_id] = index
    return index


def _persist(kb_id: str, index: VectorStoreIndex):
    """持久化 FAISS 索引 + docstore 到磁盘。"""
    vectors_dir = _vectors_dir(kb_id)
    _ensure_dir(vectors_dir)
    index.storage_context.persist(persist_dir=str(vectors_dir))


def clear_cache():
    """清空索引缓存（用于测试）。"""
    _index_cache.clear()


# ── 向量持久化辅助函数 ──────────────────────────────────────────────────────────

def _save_doc_vectors(kb_id: str, doc_id: str, nodes: list, embeddings: list):
    """保存文档的 embedding 向量和节点元数据到磁盘。

    每个文档保存两个文件：
    - {doc_id}.npy: float32 向量矩阵 (n_chunks, 1024)
    - {doc_id}_nodes.json: 节点元数据列表 [{node_id, text, metadata}, ...]

    先写 _nodes.json 再写 .npy：.npy 存在 ⇔ 向量缓存完整，重建时以此判断。
    写入顺序保证崩溃后不会出现「.npy 存在但 _nodes.json 缺失」的半完成状态。

    这些文件使索引重建时无需重新 embedding（纯 CPU 操作）。
    """
    vectors_dir = _vectors_dir(kb_id)
    _ensure_dir(vectors_dir)

    # 先写节点元数据（非原子写入可能崩溃残留，但 .npy 不存在时不会触发重建）
    nodes_data = []
    for node in nodes:
        nodes_data.append({
            "node_id": node.node_id,
            "text": node.text or "",
            "metadata": node.metadata or {},
        })
    nodes_file = vectors_dir / f"{doc_id}_nodes.json"
    nodes_tmp = vectors_dir / f"{doc_id}_nodes.json.tmp"
    nodes_tmp.write_text(_json.dumps(nodes_data, ensure_ascii=False))
    nodes_tmp.rename(nodes_file)

    # 后写向量（np.save 内部写临时文件 + rename，原子操作）
    vec_array = np.array(embeddings, dtype=np.float32)
    np.save(str(vectors_dir / f"{doc_id}.npy"), vec_array)


def _cleanup_doc_vectors(kb_id: str, doc_id: str):
    """删除文档的向量缓存文件。"""
    vectors_dir = _vectors_dir(kb_id)
    for suffix in [".npy", "_nodes.json"]:
        f = vectors_dir / f"{doc_id}{suffix}"
        if f.exists():
            f.unlink()


# ── 文档索引 ────────────────────────────────────────────────────────────────────

def _chunk_prefix(text: str, max_chars: int = 200) -> str:
    """用于页号定位的 chunk 前缀。

    取 chunk 首段非空连续字符（前 ``max_chars`` 字），足以在 by_page[*].text 找到匹配；
    跨页章节的前缀会落在章节首字符所在页。
    """
    if not text:
        return ""
    return text.strip()[:max_chars]


def _inject_page_number(nodes: list, by_page) -> None:
    """把 chunk 起始文本所在的页号写进 ``node.metadata["page_number"]``。

    - 输入：已经分块好的 nodes；by_page（可选）按页文本列表（page=0-based, text=str）。
    - 对每个 node，取 ``_chunk_prefix(node.text)`` 在每页文本里 ``find``；
      首个命中页写入 ``metadata["page_number"]``。
    - 找不到（页级粒度退化、文本修复后字符变化）→ 写 ``None``，不阻塞。
    - 没传 by_page 或为空 → 直接写 ``None``。
    - 纯函数：原地改 metadata。
    """
    if not nodes:
        return
    pages_text: list[str] = []
    if by_page:
        pages_text = [(p.text or "") for p in by_page if p.text is not None]

    for node in nodes:
        prefix = _chunk_prefix(node.text or "")
        page_num = None
        if prefix and pages_text:
            for i, pt in enumerate(pages_text):
                if pt.find(prefix) != -1:
                    page_num = i  # 0-based
                    break
        node.metadata["page_number"] = page_num


def _find_chunk_block_range(
    chunk_text: str,
    page_blocks: list,
) -> tuple[int, int] | None:
    """在单页 layout blocks 中找 chunk 覆盖的 (start_block_order, end_block_order) 区间。

    按 block_order 升序遍历,记录所有命中 block 的 block_order;
    至少一个命中才返回闭区间 ``(min, max)``,全无命中返回 ``None``。
    跨 block 的 chunk(OCR 拆散场景)→ 区间 ``(min, max)``,``max > min``。
    """
    if not chunk_text or not page_blocks:
        return None
    chunk_norm = norm(chunk_text)
    if not chunk_norm:
        return None

    sorted_blocks = sorted(page_blocks, key=lambda b: getattr(b, "block_order", 0) or 0)
    matched_orders: list[int] = []
    for b in sorted_blocks:
        block_content = getattr(b, "block_content", "") or ""
        if _block_matches_chunk(chunk_norm, norm(block_content)):
            order = getattr(b, "block_order", 0) or 0
            matched_orders.append(int(order))
    if not matched_orders:
        return None
    return (min(matched_orders), max(matched_orders))


def _inject_block_range(nodes: list, by_page=None, by_layout=None) -> list:
    """[V8-S2] 把 chunk 覆盖的 KB layout block 区间写进 ``node.metadata["block_range"]``。

    Contract:
      - 输入:已经 ``_inject_page_number`` 过的 nodes(node.metadata["page_number"]
        已是 0-based 页号)。
      - by_page:``list[PageText]``,与 ``_inject_page_number`` 同构(预留接口、
        当前未使用——分块落点已在 ``page_number`` 字段)。
      - by_layout:``list[PageLayout]`` 或 ``list[dict]``(0-based 与 by_page 平行)。
        旧 API 残留 dict 形式由本函数内自动归一为 PageLayout。
        ``None`` 或缺失页 → chunk.block_range = None(非 PDF KB / 旧 KB / 异常
        layout 走 fallback)。
      - 输出:原 nodes(原地修改 metadata),便于调用方链式接住。
      - 找不到任何命中(罕见,OCR 重排/字符差异大)→ 写 ``None``,不抛、不阻塞索引。
      - 跨页 chunk:仅记录起始页的 block 区间(与 ``page_number`` 同语义,MVP 限制)。

    匹配规则与 ``frontend/src/lib/layoutMatch.ts:matchHighlightToBlocks`` 对齐:
    NFKC + casefold + 去空白 + 去中英标点 → T1 双向 includes → P2 LCS 兜底
    (短串 < 4 字符不跑 LCS)。这保证:
      - KB 索引阶段写出的 block_range 区间在前端 fallback 字符串匹配下也能
        命中(语义一致)。
      - 反向:前端 fallback 路径不再误匹配相邻段落,因为后端算法已经过滤了
        弱匹配。
    """
    if not nodes:
        return nodes
    # 兼容 list[dict] / list[PageLayout] 混合
    normalized_layout = _normalize_layout(by_layout) if by_layout is not None else None
    for node in nodes:
        if normalized_layout is None:
            node.metadata["block_range"] = None
            continue
        page_number = node.metadata.get("page_number")
        if page_number is None or page_number < 0 or page_number >= len(normalized_layout):
            node.metadata["block_range"] = None
            continue
        page_layout = normalized_layout[page_number]
        page_blocks = getattr(page_layout, "blocks", None) or []
        chunk_text = node.text or ""
        result = _find_chunk_block_range(chunk_text, page_blocks)
        node.metadata["block_range"] = result
    return nodes


def _normalize_layout(by_layout) -> list | None:
    """``list[PageLayout]`` / ``list[dict]`` / ``None`` → ``list[PageLayout] | None``。

    旧 API 残留:tests / 序列化路径可能传 ``list[dict]``。归一后下游只读
    ``PageLayout.blocks`` / ``PageLayout.page`` 属性,不再做类型判断。
    """
    if by_layout is None:
        return None
    if not by_layout:
        return []
    if not isinstance(by_layout[0], PageLayout):
        from core.parse_document import Block as _Block
        normalized = []
        for p in by_layout:
            if isinstance(p, dict):
                blocks_raw = p.get("blocks") or []
                blocks = [_Block(**b) if isinstance(b, dict) else b for b in blocks_raw]
                normalized.append(PageLayout(
                    page=p.get("page", 0),
                    width=p.get("width", 0),
                    height=p.get("height", 0),
                    blocks=blocks,
                ))
            else:
                normalized.append(p)
        return normalized
    return list(by_layout)


def index_document(kb_id: str, doc_id: str, text: str, source_name: str = "",
                   by_page=None, by_layout=None):
    """对文档文本分块 → embedding → 写入 KB 索引 + 持久化向量（V6 单一入口）。

    V4（PRD #29）chunking 与页码解耦：整篇 ``text`` 走一套分块器
    （MarkdownNodeParser / SentenceSplitter），事后 ``_inject_page_number`` 把页号
    写回 ``node.metadata["page_number"]``，跨页章节不被腰斩。

    V8-S2 增 ``by_layout``：把每个 chunk 归一化后与该页 OCR layout blocks 匹配，
    把覆盖区间 ``(start_block_order, end_block_order)`` 写入
    ``node.metadata["block_range"]``。``None`` → block_range = None 走 fallback。

    Args:
        by_page: ``ParseResult.by_page`` 同构（list[PageText|str]）。
        by_layout: ``ParseResult.layout`` 同构（list[PageLayout]）。``None`` → 跳过
            block_range 注入（旧 KB / 非 PDF 走 fallback 高亮）。
    """
    if not text or len(text) < 20:
        return

    # 兼容 list[str]：旧 API 残留，pages_store / reparse 全部传 PageText 实例
    if by_page and not isinstance(by_page[0], PageText):
        by_page = [PageText(page=i, text=t) for i, t in enumerate(by_page)]
    # 兼容 list[dict]: 旧 API 残留的 layout dict 形式由 _inject_block_range 内
    # _normalize_layout 统一归一
    with _get_index_lock(kb_id):
        embed_model = get_embed_model()
        if embed_model is None:
            raise RuntimeError("Embedding model not loaded, cannot index document")

        # issues/144 AC#3：写入新 chunk 前断言 KB 索引体系的
        # ``embedding_model_id`` / ``embedding_dim`` 与当前 provider 一致,
        # 否则 raise 不入库(防 repro_kb 那种"非 bge-m3 向量混入"事件)。
        # KB 当前生产 provider 是 bge-m3(无论走 local SF,模型字面 ID 一致),
        # 所以这里直接读 provider 常量。
        _assert_kb_embedding_system_matches(
            kb_id, model_id="BAAI/bge-m3", dim=1024,
        )

        index = get_kb_index(kb_id)

        # 整篇切 chunk（V4：不再按页硬切）
        doc = Document(
            text=text,
            id_=doc_id,
            metadata={"doc_id": doc_id, "source": source_name or doc_id},
        )
        all_nodes = _split_document(doc)
        _enrich_chunk_metadata(all_nodes, doc_id, source_name or doc_id)
        # 事后注入页号
        _inject_page_number(all_nodes, by_page)
        # V8-S2: 注入 chunk 覆盖的 layout block 区间（无 by_layout → 全 None,走 fallback）
        all_nodes = _inject_block_range(all_nodes, by_page, by_layout)
        del doc

        if not all_nodes:
            return
        # 预 embedding：拿到向量引用后再插入索引，避免重复推理
        node_texts = [node.text or "" for node in all_nodes]
        with get_gpu_inference_lock():
            embeddings = embed_batch_with_retry(embed_model, node_texts)
        for node, emb in zip(all_nodes, embeddings):
            node.embedding = emb

        # 持久化向量（索引重建时无需 GPU）
        _save_doc_vectors(kb_id, doc_id, all_nodes, embeddings)

        # 插入索引（节点已有 embedding，不会重复推理）
        index.insert_nodes(all_nodes)

        del all_nodes, embeddings
        gc.collect()

        _persist(kb_id, index)


def _enrich_chunk_metadata(
    nodes: list,
    doc_id: str,
    source_name: str,
):
    """从文档分块文本中检测条款编号和章节标题，注入 node.metadata。

    使 FAISS 搜索结果能追溯到标准的某个具体条款（如 "CJJ101-2016 第 3.2.1 条"）。
    不在 text 中注入元数据，避免稀释 embedding 语义信号。
    """
    import re
    clause_re = re.compile(r"(\d+(?:\.\d+)*)")

    for node in nodes:
        text = node.text or ""
        if not text:
            continue

        # 检测条款编号（取最长的数字段，如 3.2.1 而非 3）
        nums = clause_re.findall(text)
        if nums:
            clause = max(nums, key=lambda n: n.count("."))
            node.metadata["clause_number"] = clause

        # 检测章节标题
        sec_match = re.search(r"^(#{1,6})\s+(.+)", text, re.MULTILINE)
        if sec_match:
            node.metadata["section_path"] = sec_match.group(2).strip()

        # 保证 doc_id / source 完整
        node.metadata.setdefault("doc_id", doc_id)
        node.metadata.setdefault("source", source_name)


def index_documents_batch(
    kb_id: str,
    docs: list,  # [(doc_id, text, source_name, by_page?, by_layout?)] — 兼容 3/4/5 元组
    progress_callback=None,
):
    """批量索引文档：分块 → 批量 embedding → 保存向量 → 写入 FAISS。

    每篇文档内部的所有 chunk 批量 embedding（利用 embed_batch_size 加速），
    embedding 结果持久化为 .npy 文件供后续快速重建。

    V4（PRD #29）跨页章节不再被腰斩：整篇 ``text`` 走一套分块器，
    事后 ``_inject_page_number`` 把页号写回 node.metadata。

    V8-S2 增 ``by_layout``：每篇 doc 携带自身 layout 数据（与 by_page 平行）；
    ``_inject_block_range`` 用其写入 chunk 覆盖的 block_range。

    Args:
        kb_id: 知识库 ID。
        docs: 3/4/5 元组列表:
              (doc_id, text, source_name) 或
              (doc_id, text, source_name, by_page) 或
              (doc_id, text, source_name, by_page, by_layout)。
              by_page 是 ``ParseResult.by_page`` 同构（list[PageText]）。
              by_layout 是 ``ParseResult.layout`` 同构（list[PageLayout]）。
        progress_callback: 可选回调 (current, total, doc_name) → None。
    """
    with _get_index_lock(kb_id):
        embed_model = get_embed_model()
        if embed_model is None:
            raise RuntimeError("Embedding model not loaded, cannot index documents")

        # issues/144 AC#3：批量写入前断言 KB 索引体系一致(同上 index_document)。
        _assert_kb_embedding_system_matches(
            kb_id, model_id="BAAI/bge-m3", dim=1024,
        )

        index = get_kb_index(kb_id)
        total = len(docs)

        for idx, item in enumerate(docs, 1):
            doc_id, text, source_name = item[0], item[1], item[2]
            by_page = item[3] if len(item) > 3 else None
            by_layout = item[4] if len(item) > 4 else None
            # 兼容 list[str]：旧 API 残留
            if by_page and not isinstance(by_page[0], PageText):
                by_page = [PageText(page=i, text=t) for i, t in enumerate(by_page)]
            # 兼容 list[dict]: 旧 API 残留的 layout dict 形式由 _inject_block_range 内
            # _normalize_layout 统一归一
            if progress_callback:
                progress_callback(idx, total, source_name or doc_id)

            if not text or len(text) < 20:
                continue

            # 整篇切 chunk（V4：不再按页硬切）
            doc = Document(
                text=text,
                id_=doc_id,
                metadata={"doc_id": doc_id, "source": source_name or doc_id},
            )
            all_nodes = _split_document(doc)
            _enrich_chunk_metadata(all_nodes, doc_id, source_name or doc_id)
            # 事后注入页号（接受页级粒度退化，None 不阻塞）
            _inject_page_number(all_nodes, by_page)
            # V8-S2: 注入 chunk 覆盖的 layout block 区间（无 by_layout → 全 None）
            all_nodes = _inject_block_range(all_nodes, by_page, by_layout)
            del doc

            if not all_nodes:
                continue

            # 批量 embedding 本稿件所有 chunk（GPU 锁内，连接层失败自动重试）
            # ADR-0007 §3：单稿 embedding 失败不中止整批(每稿隔离)。失败稿
            # 标 ``embedding_status=failed``,原因 best-effort 落到 doc_repo,
            # 其余稿继续。追跑走 reparse 通道(OCR 缓存命中零配额)。
            node_texts = [node.text or "" for node in all_nodes]
            try:
                with get_gpu_inference_lock():
                    embeddings = embed_batch_with_retry(embed_model, node_texts)
            except Exception as e:
                _logger.error(
                    "embedding failed for doc %s after retries: %s", doc_id, e,
                )
                doc_repo.mark_doc_embedding_failed(kb_id, doc_id, e)
                del all_nodes
                continue

            for node, emb in zip(all_nodes, embeddings):
                node.embedding = emb

            # 持久化向量（索引重建时无需 GPU）
            _save_doc_vectors(kb_id, doc_id, all_nodes, embeddings)

            # 插入索引（节点已有 embedding）
            index.insert_nodes(all_nodes)

            del all_nodes, embeddings

        _persist(kb_id, index)


def _split_document(doc: Document):
    """根据文档内容选择分块器。"""
    if _has_markdown_headings(doc.text):
        splitter = MarkdownNodeParser()
    else:
        splitter = SentenceSplitter(chunk_size=512, chunk_overlap=50)
    return splitter.get_nodes_from_documents([doc])


def _has_markdown_headings(text: str) -> bool:
    """快速检测文本是否包含 Markdown 标题层级。"""
    import re
    # 检查是否包含至少 2 个带层级的 Markdown 标题（# 或 ## 或 ###）
    return bool(re.search(r"^#{1,6}\s+\S", text, re.MULTILINE))


def remove_document(kb_id: str, doc_id: str):
    """从 KB 索引中删除指定文档的所有节点。

    优先尝试向量级删除（delete_ref_doc），降级到从已保存的 .npy 向量
    重建索引（无需 GPU 重新 embedding）。
    """
    with _get_index_lock(kb_id):
        # 快速路径：通过 delete_ref_doc 直接从索引删除
        try:
            index = get_kb_index(kb_id)
            if hasattr(index.vector_store, '_faiss_index') and hasattr(index.vector_store._faiss_index, 'remove_ids'):
                index.delete_ref_doc(doc_id, delete_from_docstore=True)
                _persist(kb_id, index)
                _cleanup_doc_vectors(kb_id, doc_id)
                _logger.info("removed doc %s from kb %s via delete_ref_doc", doc_id, kb_id)
                return
        except Exception as e:
            _logger.warning("vector-level deletion failed for %s/%s (%s), fallback to rebuild from cached vectors", kb_id, doc_id, e)

        # 降级路径：从已保存的 .npy 向量重建索引（无需 GPU）
        _logger.info("rebuilding kb %s index from cached vectors after removing doc %s", kb_id, doc_id)
        _index_cache.pop(kb_id, None)

        import storage.kb_repo as kb_repo
        kb = kb_repo.get(kb_id)
        if not kb:
            return

        remaining_ids = [did for did in kb.document_ids if did != doc_id]
        vectors_dir = _vectors_dir(kb_id)

        if not remaining_ids:
            # 无剩余文档，清理所有索引文件。但 ``index.meta.json`` 是
            # production KB 元数据,问题描述"该 KB 用什么 embedding"
            # 不能随向量被物理删除(issues/144 AC#3);留 meta,
            # 等下次 ``index_document`` 写入前断言可过。
            if vectors_dir.exists():
                meta_p = _index_meta_path(kb_id)
                meta_existed = meta_p.exists()
                shutil.rmtree(str(vectors_dir))
                if meta_existed:
                    # 重建空 vectors/(已经在 rmtree 里被删);只保留 meta
                    _ensure_dir(vectors_dir)
                    _write_index_meta(
                        kb_id, model_id="BAAI/bge-m3", dim=1024,
                        force=True,
                    )
            return

        # 从向量缓存重建（_rebuild_from_vectors 成功后 _persist 会覆盖旧 FAISS 文件；
        # 失败则旧文件仍在，不会丢失索引）
        _rebuild_from_vectors(kb_id, remaining_ids)
        _cleanup_doc_vectors(kb_id, doc_id)


def _rebuild_from_vectors(kb_id: str, doc_ids: list[str], progress_callback=None):
    """从已保存的 .npy 向量文件重建 FAISS 索引（纯 CPU，无需 GPU）。

    从 {doc_id}.npy 加载向量，从 {doc_id}_nodes.json 加载节点文本/元数据，
    重建 TextNode 并插入新索引。

    Args:
        progress_callback: 可选回调 (current, total, doc_name) → None。
    """
    vectors_dir = _vectors_dir(kb_id)

    new_index = _create_index()
    _index_cache[kb_id] = new_index

    total = len(doc_ids)
    loaded = 0
    for i, doc_id in enumerate(doc_ids, 1):
        vec_file = vectors_dir / f"{doc_id}.npy"
        nodes_file = vectors_dir / f"{doc_id}_nodes.json"

        if not vec_file.exists():
            _logger.warning("vector cache missing for doc %s, will need re-embedding", doc_id)
            if progress_callback:
                progress_callback(i, total, doc_id)
            continue

        vectors = np.load(str(vec_file))

        # 加载节点元数据（文本 + metadata）
        if not nodes_file.exists():
            _logger.error(
                "vector cache incomplete for doc %s: .npy exists but _nodes.json missing. "
                "This doc will be skipped in rebuild. Run `index rebuild --kb-id %s` to re-embed.",
                doc_id, kb_id,
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
                "node count mismatch for doc %s: %d nodes in _nodes.json vs %d vectors in .npy. "
                "This doc will be skipped in rebuild.",
                doc_id, len(nodes_data), len(vectors),
            )
            if progress_callback:
                progress_callback(i, total, doc_id)
            continue

        nodes = []
        for j, vec in enumerate(vectors):
            nd = nodes_data[j]
            node = TextNode(
                text=nd.get("text", ""),
                id_=nd.get("node_id", f"{doc_id}_{j}"),
                metadata=nd.get("metadata", {}),
                embedding=vec.tolist() if hasattr(vec, 'tolist') else list(vec),
            )
            nodes.append(node)

        new_index.insert_nodes(nodes)
        loaded += 1

        if progress_callback:
            progress_callback(i, total, doc_id)

        if loaded % 20 == 0:
            gc.collect()

    _persist(kb_id, new_index)
    # issues/144 AC#3：rebuild 路径不写新 chunk(向量从 .npy 重用),但必须确保
    # ``index.meta.json`` 存在,后续 ``index_document`` 写入前断言可过。
    # 留用现有 meta;若没有(极端:纯裸重建)写一份(给生产体系 = bge-m3)。
    meta = _read_index_meta(kb_id)
    if meta is None:
        _write_index_meta(
            kb_id, model_id="BAAI/bge-m3", dim=1024, force=True,
        )
    _logger.info("rebuilt index for kb %s from %d/%d docs (cached vectors)", kb_id, loaded, len(doc_ids))


def rebuild_kb_index(kb_id: str, progress_callback=None):
    """重建 KB 索引。

    优先从已保存的 .npy 向量重建（纯 CPU，秒级），
    向量缺失时降级到重新提取文本 + embedding（需要 GPU）。

    内置契约（ADR-0002 §决策 2）：在 per-KB 锁内、根据本函数结果写回
    ``kb.index_status``：
    - 至少有一篇文档被成功索引 → 'searchable'（同时清 progress / current_doc）
    - 重建过程抛异常 → 'failed'（保留 current_doc 为错误信息）
    - KB 不存在 → 静默返回

    不依赖任何调用方记得写字段——这是为何把"写回字段"内置在重建函数里，
    而不是分散在 reindex 按钮 / auto-rebuild / 批量导入等调用方。

    Issue #149：本函数是首批持有 per-KB 锁（``_get_index_lock``，保护 FAISS
    资源）的写入者之一——FAISS 锁继续保留。KB 检索状态字段（``index_status``
    / ``index_progress`` / ``index_current_doc``）则统一交给
    ``KbIndexStatusWriter``（issue #148），本函数不再直接赋值这三个字段。
    两种锁是不同资源：FAISS 锁保护 ``_load_index`` / ``_persist`` / 缓存
    失效，writer 的 per-instance 锁保护 KB 状态字段的 read-modify-write。

    Args:
        kb_id: 知识库 ID。
        progress_callback: 可选回调 (current_index, total, doc_name) → None，
                           每处理完一篇文档后调用，用于外部汇报进度。
    """
    with _get_index_lock(kb_id):
        _index_cache.pop(kb_id, None)

        import storage.kb_repo as kb_repo
        kb = kb_repo.get(kb_id)
        if not kb:
            return

        # 内置契约：开锁前先标记 building，让 UI/其他线程看见的中间态。
        # 这一写也在锁内，免与并发 _ensure_kb_index 交错。
        # Issue #149：KB 检索状态字段交给 KbIndexStatusWriter 写。
        kb_writer = KbIndexStatusWriter(kb_id)
        kb_writer.begin()

        try:
            vectors_dir = _vectors_dir(kb_id)
            doc_ids = kb.document_ids

            # 区分有/无向量缓存的文档
            with_vectors = []
            without_vectors = []
            for doc_id in doc_ids:
                if (vectors_dir / f"{doc_id}.npy").exists():
                    with_vectors.append(doc_id)
                else:
                    without_vectors.append(doc_id)

            # 阶段 1：从向量缓存快速重建（无需 GPU）
            if with_vectors:
                _logger.info("rebuilding kb %s from %d cached vectors (fast path)", kb_id, len(with_vectors))
                # 删除旧的 llama-index 持久化文件（_rebuild_from_vectors 成功后会 _persist 写回新的）
                old_store = vectors_dir / "default__vector_store.json"
                if old_store.exists():
                    old_store.unlink()
                for pattern in ["docstore.json", "index_store.json", "graph_store.json"]:
                    p = vectors_dir / pattern
                    if p.exists():
                        p.unlink()

                _rebuild_from_vectors(kb_id, with_vectors, progress_callback=progress_callback)

            # 阶段 2：重新提取文本 + embedding（向量缓存缺失的文档）
            if without_vectors:
                _logger.info("rebuilding kb %s: %d docs need re-embedding (slow path)", kb_id, len(without_vectors))
                from storage.doc_repo import get_doc
                total = len(without_vectors)
                for i, doc_id in enumerate(without_vectors, 1):
                    doc = get_doc(kb_id, doc_id)
                    doc_name = doc.original_name if doc and doc.original_name else doc_id
                    if progress_callback:
                        progress_callback(i, total, doc_name)
                    if doc and doc.file_path and Path(doc.file_path).exists():
                        try:
                            from core.parse_document import parse_document as _parse_document
                            parse_result = _parse_document(doc.file_path)
                            text = parse_result.full_text
                            if text:
                                index_document(
                                    kb_id, doc_id, text,
                                    by_page=parse_result.by_page,
                                    by_layout=parse_result.layout,
                                )
                        except Exception as e:
                            _logger.warning("  [skip] %s: %s", doc_id, e)
            if not with_vectors and not without_vectors:
                # 无文档，清理 + 仍标 searchable（空库也是合法"无文档"状态）
                if vectors_dir.exists():
                    shutil.rmtree(str(vectors_dir))
                kb_writer.finish()
                return

            # 内置契约：重建成功 → 字段 searchable（无需调用方再写）
            kb_writer.finish()
        except Exception as e:
            # 内置契约：失败 → 字段 failed（保留错误信息在 current_doc）。
            # Issue #149：失败摘要走 writer 的 ``finish(failed=...)`` 通道，
            # 不再直接写 ``kb.index_current_doc``。KB 已删 → writer 的
            # ``_write`` 内部对 ``kb_repo.get`` 返 ``None`` 静默 return。
            #
            # 已知语义小漂移：writer 的 ``_format_failure_summary`` 字面前缀
            # 仍是 ``"批量重新解析失败 N/M 篇（…）"``(#148 ship 时未实现
            # 单篇 ``"失败: <err>"`` 格式,#147 §User Stories 第 14 条
            # 列入后续 ticket);此处重建失败单事件会被套上批量前缀,
            # 见 #149 PR description 备注。contract test 的 ``"disk" in
            # current_doc`` 断言仍通过(异常文本里就含该子串)。
            kb_writer.finish(failed=[("重建", str(e))])
            raise


def get_kb_index_built(kb_id: str) -> bool:
    """检查 KB 是否可被向量检索。

    ADR-0002 单真相：本函数只读 KB 元数据中的 ``kb.index_status`` 字段。
    旧的依据 ``default__vector_store.json`` 文件是否存在的判定已弃用——
    FAISS 文件（含 .npy 文档向量缓存）仅为可从字段与文档重生的缓存，
    不再是状态真相。

    取值映射：
    - ``searchable`` → True（可向量检索）
    - ``building`` / ``none`` / ``failed`` → False（自愈路径触发条件）

    删除同名函数旧签名是为了避免静默回退到文件判定——任何路径失效都立刻报错。
    """
    import storage.kb_repo as _kb_repo
    kb = _kb_repo.get(kb_id)
    return kb is not None and kb.index_status == "searchable"


# ── 搜索 ────────────────────────────────────────────────────────────────────────

def search(kb_ids: list[str], query: str, top_k: int = 5, use_reranker: bool = True) -> list[dict]:
    """跨 KB 向量搜索。

    返回格式与旧版 vec_search() 兼容：
    [{source, kb_id, doc_id, content, doc_source, relevance}, ...]

    当 reranker 可用时，用 cross-encoder 对候选结果重排序提升精度。
    """
    if not query or not kb_ids:
        return []

    # 确保 embed_model 已加载，防止 LlamaIndex 默认解析到 OpenAI
    get_embed_model()

    from core.settings import get_gpu_inference_lock
    gpu_lock = get_gpu_inference_lock()

    # 将整个 GPU 相关操作置于锁内。HuggingFaceEmbedding 和
    # SentenceTransformerRerank 的 forward 非线程安全，并发调用会各自
    # 分配完整激活张量撑爆显存。此锁确保同时只有一个进程执行模型前向传播，
    # LLM 调用（DeepSeek API 不走 GPU）不受影响仍可并行。
    with gpu_lock:
        # 先收集 NodeWithScore 对象，保留完整 score 信息
        from llama_index.core.schema import NodeWithScore
        all_nodes: list[NodeWithScore] = []
        for kb_id in kb_ids:
            if not get_kb_index_built(kb_id):
                continue
            try:
                with _get_index_lock(kb_id):
                    index = get_kb_index(kb_id)
                    retriever = index.as_retriever(similarity_top_k=top_k)
                    nodes = retriever.retrieve(query)
                for node in nodes:
                    node.node.metadata["kb_id"] = kb_id
                    all_nodes.append(node)
            except Exception as e:
                _logger.warning("vector search failed for kb %s: %s", kb_id, e)
                continue

        if not all_nodes:
            return []

        all_nodes.sort(key=lambda n: n.score or 0, reverse=True)
        all_nodes = all_nodes[: top_k * 2]  # 多留候选给 reranker

        # ── Reranker 重排序（按需加载→推理→卸载）────────────────────────
        if use_reranker:
            try:
                from core.settings import run_reranker
                reranked = run_reranker(all_nodes, query)
                if reranked:
                    all_nodes = reranked
            except Exception as e:
                _logger.warning("reranker failed in search, using raw ranking: %s", e)

    # 转换为 dict 返回格式
    hits = []
    for node in all_nodes[:top_k]:
        meta = node.metadata or {}
        hits.append({
            "source": "vec_search",
            "kb_id": meta.get("kb_id", ""),
            "doc_id": meta.get("doc_id", ""),
            "content": node.text,
            "doc_source": meta.get("source", ""),
            "section_path": meta.get("section_path", ""),
            "clause_number": meta.get("clause_number", ""),
            "page_number": meta.get("page_number"),  # int or None, 0-based
            # V8-S3: 从 node.metadata 透传 block_range;None/缺失 → 字段仍暴露(None),
            # 调用方按字段是否存在决定显示——_tool_flag_issue / standard_linker /
            # IssueResponse 都按"非 None 才使用"消费。
            "block_range": meta.get("block_range"),
            "relevance": round(node.get_score() or 0, 4),
        })

    return hits
