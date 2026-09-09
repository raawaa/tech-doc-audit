"""KB 写入 orchestrator — 一篇文档从 chunk 到 FAISS 的全部承载者(issue #169 / PR-3)。

为什么单独成模块(issue #165 PR-3):
- 历史上 ``core.index_manager.index_document`` / ``index_documents_batch``
  / ``rebuild_kb_index`` 各自重复一段"分块 → 注入 page-number → 注入
  block-range → embed 重试 → 落盘"的 pipeline —— 单篇与批量两条路径
  共享 40 行相同代码,任何一处漂移都会让"块数对不齐"或"per-doc 隔离
  失效"等 bug 潜入。
- 本模块把整条 pipeline 收归一处,公开**单一入口**
  ``KBIndexWriter.index_documents(docs) -> list[DocResult]``;历史
  ``index_document`` 走 ``index_documents([doc])``、历史
  ``index_documents_batch`` 走 ``index_documents(docs)``,两条路径合并。

公开 API(issue #169 AC #1 / #2):
- :class:`Doc`     — 一篇文档的输入形状(doc_id / text / source_name /
                    by_page / by_layout)。
- :class:`DocResult` — 单篇结果(``done`` / ``failed`` / ``skipped`` +
                    error 字符串)。
- :meth:`KBIndexWriter.index_documents` — 单入口批量入口。
- :meth:`KBIndexWriter.rebuild_kb_index` — 2-phase 编排
                    (cached-vectors fast-path → GPU re-embed fallback)。

依赖模块(每个都有唯一职责):
- :class:`core.chunk_layout_mapper.ChunkLayoutMapper`  —
  T1/P2 chunk→layout 判定。
- :class:`core.kb_index_store.KBIndexStore`           —
  FAISS + sidecar meta + vector cache + per-KB 锁承载者。
- :func:`core.embed_retry.embed_batch_with_retry`      —
  ADR-0007 瞬态失败重试 owner。
- :func:`storage.doc_repo.mark_doc_embedding_failed`   —
  doc ``embedding_status="failed"`` 状态转移唯一公开入口。
- :class:`core.kb_index_status.KbIndexStatusWriter`    —
  KB 检索状态字段唯一写入者。

不变式(全部走这里验证):
- 整批期间 ``kb.index_status`` 一律 ``building``,终态由 writer ``finish``
  一次性写——KBIndexWriter **不**调 ``begin()``(issue #155: caller 拥有
  生命周期)。
- per-doc 失败**不**中止整批(ADR-0007 §3);失败 doc 标 ``failed``,
  其余 doc 继续走完。
- ``embed_batch_with_retry`` 是 ADR-0007 瞬态失败重试的唯一 owner,
  本模块不再另起一层重试。
"""
from __future__ import annotations

import gc
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

from llama_index.core import Document
from llama_index.core.node_parser import MarkdownNodeParser, SentenceSplitter

from core.chunk_layout_mapper import map_chunk_to_blocks, normalize_layout
from core.kb_index_status import KbIndexStatusWriter
from core.kb_index_store import KBIndexStore
from core.logger import get_logger
from core.parse_document import PageLayout, PageText
from core.settings import get_embed_model, get_gpu_inference_lock
import storage.doc_repo as doc_repo


def _get_embed_batch_with_retry():
    """Lazily resolve ``embed_batch_with_retry`` from ``core.index_manager``.

    走 ``core.index_manager.embed_batch_with_retry``(re-export 同一函数对象)
    而非 ``core.embed_retry.embed_batch_with_retry`` 直接 import —— 这样
    现有 tests ``monkeypatch.setattr("core.index_manager.embed_batch_with_retry", ...)``
    仍能拦截 writer 的真实调用路径,无需把 test 改去 patch 不同的模块路径。
    改为 lazy import 是为了打破 ``core.kb_index_writer`` ↔ ``core.index_manager``
    的循环 import(后者从前者 import ``KBIndexWriter`` / ``Doc`` / ``DocResult``)。
    """
    import core.index_manager as _im
    return _im.embed_batch_with_retry

_logger = get_logger(__name__)


# ── 公开数据形状 ──────────────────────────────────────────────────────────────


@dataclass
class Doc:
    """``index_documents`` 的单篇文档输入。

    字段对齐 ``ParseResult`` 的 by_page / layout 平行结构;``by_page`` /
    ``by_layout`` 均可为 ``None``(旧 KB / 非 PDF KB / 文本 KB 走 fallback)。

    向后兼容:``text`` 长度 < 20 字符的 doc 会直接被标 ``skipped``(沿袭
    旧 ``index_document`` 的 "if not text or len(text) < 20: return" 早退)。
    """

    doc_id: str
    text: str
    source_name: str = ""
    by_page: Optional[list] = None
    by_layout: Optional[list] = None


@dataclass
class DocResult:
    """``index_documents`` 的单篇结果。

    - ``status``:
      - ``"done"`` — 成功 chunked + embedded + 落入 FAISS。
      - ``"failed"`` — embedding 抛错(ADR-0007 §3 per-doc 隔离),doc
        已标 ``embedding_status=failed``,``error`` 带原始异常文字。
      - ``"skipped"`` — 输入不达门槛(text 为空 / < 20 字符 / chunk 拆
        不出 node),不进 embed,也不进索引。
    - ``error``:出错时为 ``"ExceptionType: message"`` 形式,人读可定位
      失败原因;非失败时为 ``None``。
    """

    doc_id: str
    status: Literal["done", "failed", "skipped"]
    error: Optional[str] = None


# ── orchestrator ──────────────────────────────────────────────────────────────


class KBIndexWriter:
    """KB 写入 orchestrator — 隐藏 chunking / metadata 富化 / page-number
    注入 / block-range 注入 / embed 重试派发。

    单例工厂:同 PR-2 的 ``KBIndexStore`` 类似,本类也以 classmethod 工厂
    实例化,以便未来需要"per-KB 私有状态"(如 per-KB 临时 chunk 缓存)
    时能扩展。直接 ``KBIndexWriter(kb_id)`` 仍允许(无特殊锁需求,
    orchestrator 不持跨调用方共享资源)——但更推荐 ``KBIndexWriter.open(kb_id)``
    以便与 KBIndexStore 风格对齐。
    """

    def __init__(self, kb_id: str) -> None:
        self._kb_id = kb_id

    # ── 公开 API:单入口批量 ────────────────────────────────────────

    def index_documents(
        self,
        docs: list[Doc],
        *,
        kb_status_writer: Optional[KbIndexStatusWriter] = None,
    ) -> list[DocResult]:
        """单入口批量索引 —— 旧 ``index_document`` 与 ``index_documents_batch``
        两条路径合并。

        调用契约:
          - **caller 拥有 KB 生命周期**:本方法**不**调 ``kb_status_writer.begin()``,
            仅当 ``kb_status_writer`` 注入时才调 ``note_in_flight`` / ``advance``
            / ``fail_doc``(issue #155: caller 独家承担 ``begin()``,避免
            批量路径 N+1 次 begin 把 ``index_progress`` 清零)。
          - **per-doc 隔离**(ADR-0007 §3):任一 doc 抛错,``DocResult.status
            = "failed"``,doc 标 ``embedding_status=failed``,**其余 doc
            继续走完**;整批不抛。
          - **嵌入重试**(ADR-0007 §2):``embed_batch_with_retry`` 是连接层
            重试唯一 owner,本方法不另起一层;HTTP 层重试由 OpenAI SDK 负责。

        Args:
            docs: 一批 ``Doc`` 实例。每篇独立 chunk + embed + 写 FAISS。
            kb_status_writer: 可选 ``KbIndexStatusWriter``;注入时 per-doc
                进度走 ``note_in_flight`` / ``advance`` / ``fail_doc``
                通道(``total=1`` 的 writer 自动 ``finish(failed=[...])``
                写终态)。``None`` = 纯编排、不写 KB 状态(供 CLI / 脚本调用)。

        Returns:
            与 ``docs`` 一一对应的 ``DocResult`` 列表,顺序与 ``docs`` 一致。
        """
        results: list[DocResult] = []
        embed_model = get_embed_model()
        if embed_model is None:
            raise RuntimeError("Embedding model not loaded, cannot index documents")

        # 本地计数器:per-doc ``advance(done)`` 不再从 ``kb.index_progress``
        # 反推(那个反推在 multi-doc 路径上会因 KB 状态被外部读而错位)。
        # Writer 自己维护 done 计数,issue #155 不变式 —— 单调非递减
        # 由 ``KbIndexStatusWriter._write`` 内的 max() 守卫负责。
        done_count = 0

        for doc in docs:
            by_page = _coerce_by_page(doc.by_page)
            by_layout = doc.by_layout

            # 早退:< 20 字符不入库(沿袭旧 index_document 的早退,避免
            # 无意义 chunk + embed + FAISS 占位)。
            if not doc.text or len(doc.text) < 20:
                results.append(DocResult(doc_id=doc.doc_id, status="skipped"))
                continue

            if kb_status_writer is not None:
                kb_status_writer.note_in_flight(doc.source_name or doc.doc_id)

            # 整篇切 chunk(V4:不再按页硬切;跨页章节不被腰斩)
            llama_doc = Document(
                text=doc.text,
                id_=doc.doc_id,
                metadata={"doc_id": doc.doc_id, "source": doc.source_name or doc.doc_id},
            )
            nodes = _split_document(llama_doc)
            _enrich_chunk_metadata(nodes, doc.doc_id, doc.source_name or doc.doc_id)
            _inject_page_number(nodes, by_page)
            # V8-S2:chunk → KB layout block 区间注入(无 by_layout → 全 None)
            _inject_block_range(nodes, by_layout)
            del llama_doc

            if not nodes:
                results.append(DocResult(doc_id=doc.doc_id, status="skipped"))
                continue

            # 嵌入 + 重试:连接层失败自动重试(ADR-0007 §2)。
            # per-doc 隔离:异常被 catch 后该稿记 failed,其余稿继续。
            node_texts = [node.text or "" for node in nodes]
            try:
                with get_gpu_inference_lock():
                    embeddings = _get_embed_batch_with_retry()(embed_model, node_texts)
            except Exception as e:
                _logger.error(
                    "embedding failed for doc %s after retries: %s",
                    doc.doc_id, e,
                )
                doc_repo.mark_doc_embedding_failed(self._kb_id, doc.doc_id, e)
                results.append(DocResult(
                    doc_id=doc.doc_id,
                    status="failed",
                    error=f"{type(e).__name__}: {e}",
                ))
                if kb_status_writer is not None:
                    kb_status_writer.fail_doc(
                        doc.source_name or doc.doc_id, str(e),
                    )
                del nodes
                continue

            for node, emb in zip(nodes, embeddings):
                node.embedding = emb

            # 存储层:per-KB 锁内做 meta 断言 + .npy 落盘 + FAISS insert +
            # persist。委派给 KBIndexStore.add_doc,锁由 store 内部封装。
            KBIndexStore.open(self._kb_id).add_doc(doc.doc_id, nodes, embeddings)

            results.append(DocResult(doc_id=doc.doc_id, status="done"))

            if kb_status_writer is not None:
                done_count += 1
                kb_status_writer.advance(done_count)

            del nodes, embeddings

        if kb_status_writer is not None and kb_status_writer._total == 1:
            # 单篇路径:writer 自己 finish 终态。多篇路径由 caller 收尾。
            # KBIndexWriter 不调 begin() —— caller 已经 begin() 过。
            any_failed = [r for r in results if r.status == "failed"]
            if any_failed:
                kb_status_writer.finish(failed=[
                    (r.doc_id, r.error or "未知失败") for r in any_failed
                ])
            else:
                kb_status_writer.finish()

        return results

    # ── 公开 API:2-phase rebuild ────────────────────────────────────

    def rebuild_kb_index(
        self,
        *,
        progress_callback=None,
    ) -> None:
        """重建 KB 索引 —— 2-phase 编排(cached-vectors fast-path →
        GPU re-embed fallback)。

        Phase 1:对有 ``.npy`` 缓存的 doc,直接从向量缓存重建 FAISS(CPU only,
        无需 GPU)。
        Phase 2:对无 ``.npy`` 缓存的 doc,重新 ``parse_document`` +
        embed(GPU 路径)。

        不变式:
          - **整批期间 KB 状态字段由 writer 独占**:KB 检索状态字段
            (``index_status`` / ``index_progress`` / ``index_current_doc``)
            通过注入 ``kb_status_writer`` 写。Writer ``begin()`` 仍由
            caller 独家承担(issue #155)。
          - **失败 → failed 终态**:任一 phase 抛异常 → ``finish(failed=...)``
            把 KB 写成 ``failed`` 并保留错误信息。
          - **空 KB 也是合法 searchable**:无文档 / 全部 doc 都无缓存
            → ``finish()`` 直接 ``searchable``(与旧 ``rebuild_kb_index``
            同款约定)。

        Args:
            progress_callback: 可选 ``(current, total, doc_name)`` 回调,
                外部汇报进度用。Phase 1 调用一次 / 篇;Phase 2 也调用
                一次 / 篇(进度计数跨 phase)。

        Returns:
            None。KB 状态字段是副作用,经由 ``kb_status_writer`` 写入。

        Raises:
            RuntimeError:KB 不存在 / 任一 phase 抛错(``finish(failed=...)``
                仍被调用后再 raise,确保 KB 字段被写)。
        """
        store = KBIndexStore.open(self._kb_id)
        kb_writer = KbIndexStatusWriter(self._kb_id)
        # caller 已 begin() 过,本函数不再 begin()(issue #155 防御)。
        # writer._total 默认 1,但 rebuild 内部不使用 advance —— 用作
        # fail_doc 的"是否单篇路径"开关。

        try:
            import storage.kb_repo as kb_repo
            kb = kb_repo.get(self._kb_id)
            if not kb:
                return

            vectors_dir = Path(store._vectors_dir())
            doc_ids = list(kb.document_ids)

            with_vectors: list[str] = []
            without_vectors: list[str] = []
            for doc_id in doc_ids:
                if (vectors_dir / f"{doc_id}.npy").exists():
                    with_vectors.append(doc_id)
                else:
                    without_vectors.append(doc_id)

            # Phase 1:从向量缓存快速重建(无需 GPU)
            if with_vectors:
                _logger.info(
                    "rebuilding kb %s from %d cached vectors (fast path)",
                    self._kb_id, len(with_vectors),
                )
                # 删除旧的 llama-index 持久化文件(rebuild_from_vectors
                # 成功后 _persist 写回新的)
                _cleanup_llama_persist_files(vectors_dir)
                store.rebuild_from_vectors(
                    with_vectors, progress_callback=progress_callback,
                )

            # Phase 2:重新提取文本 + embedding(向量缓存缺失的 doc)
            if without_vectors:
                _logger.info(
                    "rebuilding kb %s: %d docs need re-embedding (slow path)",
                    self._kb_id, len(without_vectors),
                )
                from storage.doc_repo import get_doc
                total_phase2 = len(without_vectors)
                for i, doc_id in enumerate(without_vectors, 1):
                    _doc_obj = get_doc(self._kb_id, doc_id)
                    doc_name = (
                        _doc_obj.original_name
                        if _doc_obj and _doc_obj.original_name
                        else doc_id
                    )
                    if progress_callback:
                        progress_callback(i, total_phase2, doc_name)
                    if _doc_obj and _doc_obj.file_path and Path(_doc_obj.file_path).exists():
                        try:
                            from core.parse_document import parse_document as _parse_document
                            parse_result = _parse_document(_doc_obj.file_path)
                            text = parse_result.full_text
                            if text:
                                self.index_documents([Doc(
                                    doc_id=doc_id,
                                    text=text,
                                    source_name=doc_name,
                                    by_page=parse_result.by_page,
                                    by_layout=parse_result.layout,
                                )])
                        except Exception as e:
                            _logger.warning("  [skip] %s: %s", doc_id, e)

            if not with_vectors and not without_vectors:
                # 空 KB:清目录 + 直接 searchable(空库也是合法状态)
                if vectors_dir.exists():
                    shutil.rmtree(str(vectors_dir))
                kb_writer.finish()
                return

            kb_writer.finish()
        except Exception as e:
            # 内置契约:失败 → 字段 failed(保留错误信息在 current_doc)。
            kb_writer.finish(failed=[("重建", str(e))])
            raise


# ── 私有 helper ──────────────────────────────────────────────────────────────


def _coerce_by_page(by_page) -> Optional[list[PageText]]:
    """``list[PageText]`` / ``list[str]`` / ``None`` → ``list[PageText] | None``。

    兼容旧 API 残留:tests / 序列化路径可能传 ``list[str]``。归一后下游
    只读 ``PageText.text`` 属性,不再做类型判断。
    """
    if not by_page:
        return by_page
    if not isinstance(by_page[0], PageText):
        return [PageText(page=i, text=t) for i, t in enumerate(by_page)]
    return by_page


def _split_document(doc: Document):
    """根据文档内容选择分块器(MarkdownNodeParser / SentenceSplitter)。

    Markdown 标题层级数 >= 2 → MarkdownNodeParser;否则 SentenceSplitter
    (默认 chunk_size=512, overlap=50)。
    """
    if _has_markdown_headings(doc.text):
        splitter = MarkdownNodeParser()
    else:
        splitter = SentenceSplitter(chunk_size=512, chunk_overlap=50)
    return splitter.get_nodes_from_documents([doc])


def _has_markdown_headings(text: str) -> bool:
    """快速检测文本是否包含 Markdown 标题层级。"""
    import re
    return bool(re.search(r"^#{1,6}\s+\S", text, re.MULTILINE))


def _enrich_chunk_metadata(nodes: list, doc_id: str, source_name: str) -> None:
    """从 chunk 文本中检测条款编号与章节标题,写进 ``node.metadata``。

    使 FAISS 搜索结果能追溯到标准(如 ``CJJ101-2016 第 3.2.1 条``)。
    不在 text 中注入元数据 —— 避免稀释 embedding 语义信号。
    """
    import re
    clause_re = re.compile(r"(\d+(?:\.\d+)*)")
    for node in nodes:
        text = node.text or ""
        if not text:
            continue
        nums = clause_re.findall(text)
        if nums:
            clause = max(nums, key=lambda n: n.count("."))
            node.metadata["clause_number"] = clause
        sec_match = re.search(r"^(#{1,6})\s+(.+)", text, re.MULTILINE)
        if sec_match:
            node.metadata["section_path"] = sec_match.group(2).strip()
        node.metadata.setdefault("doc_id", doc_id)
        node.metadata.setdefault("source", source_name)


def _chunk_prefix(text: str, max_chars: int = 200) -> str:
    """用于页号定位的 chunk 前缀。

    取 chunk 首段非空连续字符(前 ``max_chars`` 字),足以在 ``by_page[*].text``
    找到匹配;跨页章节的前缀会落在章节首字符所在页。
    """
    if not text:
        return ""
    return text.strip()[:max_chars]


def _inject_page_number(nodes: list, by_page) -> None:
    """把 chunk 起始文本所在的页号写进 ``node.metadata["page_number"]``。

    - 输入:已经分块好的 nodes;``by_page``(可选)按页文本列表。
    - 对每个 node,取 ``_chunk_prefix(node.text)`` 在每页文本里 ``find``;
      首个命中页写入 ``metadata["page_number"]``。
    - 找不到 / 没传 → 写 ``None``,不阻塞。
    - 纯函数:原地改 metadata。
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


def _inject_block_range(nodes: list, by_layout=None) -> list:
    """把 chunk 覆盖的 KB layout block 区间写进 ``node.metadata["block_range"]``。

    这是 "for-all-nodes inject" 循环(issue #165 PR-3 决策:留在 Writer,
    它写的是 metadata 而不是 mapping)。映射判定走
    :func:`core.chunk_layout_mapper.map_chunk_to_blocks`,归一走
    :func:`core.chunk_layout_mapper.normalize_layout`。

    Contract:
      - 输入:已经 :func:`_inject_page_number` 过的 nodes
        (``node.metadata["page_number"]`` 已是 0-based 页号)。
      - ``by_layout``:``list[PageLayout]`` / ``list[dict]`` / ``None``。
        ``None`` 或缺页 → ``chunk.block_range = None``(非 PDF / 旧 KB /
        异常 layout 走 fallback)。
      - 输出:原 nodes(原地改 ``metadata``),便于调用方链式接住。
      - 找不到任何命中(罕见,OCR 重排 / 字符差异大)→ 写 ``None``,不抛、
        不阻塞索引。
      - 跨页 chunk:仅记录起始页的 block 区间(与 ``page_number`` 同语义,
        MVP 限制)。
    """
    if not nodes:
        return nodes
    normalized_layout = normalize_layout(by_layout)
    for node in nodes:
        if normalized_layout is None:
            node.metadata["block_range"] = None
            continue
        page_number = node.metadata.get("page_number")
        if (
            page_number is None
            or page_number < 0
            or page_number >= len(normalized_layout)
        ):
            node.metadata["block_range"] = None
            continue
        page_layout = normalized_layout[page_number]
        chunk_text = node.text or ""
        node.metadata["block_range"] = map_chunk_to_blocks(chunk_text, page_layout)
    return nodes


def _cleanup_llama_persist_files(vectors_dir: Path) -> None:
    """删除旧的 llama-index 持久化文件,为 phase 1 的 ``rebuild_from_vectors``
    腾出空间。``rebuild_from_vectors`` 成功后 ``_persist`` 会写回新的。
    """
    store_file = vectors_dir / "default__vector_store.json"
    if store_file.exists():
        store_file.unlink()
    for pattern in ("docstore.json", "index_store.json", "graph_store.json"):
        p = vectors_dir / pattern
        if p.exists():
            p.unlink()
