from typing import Optional

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel
from typing import Literal

import services.kb_service as kb_svc
import storage.doc_repo as doc_repo
from api.deps import get_data_dir
from core import bulk_reparse_report_store
from core.kb_index_status import KbIndexStatusWriter
from core.logger import get_logger
from services.bulk_reparse_service import (
    DEFAULT_CONCURRENCY,
    OcrCostEstimate,
    cache_state,
    estimate_ocr_cost,
    list_target_docs,
    run_bulk_reparse,
)

_logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/knowledge-bases", tags=["knowledge-bases"])


def _get_actually_indexed_doc_ids(kb_id: str) -> set[str]:
    """读取 FAISS docstore，返回实际已索引的 doc_id 集合。

    用于 rebuild 后验证哪些文档成功进入了向量索引。
    不依赖内存中的 index_cache（rebuild 后缓存可能已失效）。

    兼容两种 docstore 格式：
    1. 新格式（rebuild 产物）：docstore/data → 每个节点 metadata.doc_id
    2. 旧格式（增量索引）：docstore/ref_doc_info → {doc_id: {node_ids: [...]}}
    """
    import json as _json

    vectors_dir = get_data_dir() / "kbs" / kb_id / "vectors"
    docstore_path = vectors_dir / "docstore.json"
    if not docstore_path.exists():
        return set()
    try:
        docstore = _json.loads(docstore_path.read_text(encoding="utf-8"))

        # 格式1（新）：docstore/data — 每个节点存储 metadata.doc_id
        data = docstore.get("docstore/data", {})
        if data:
            doc_ids = set()
            for node_data in data.values():
                meta = node_data.get("__data__", {}).get("metadata", {})
                doc_id = meta.get("doc_id", "")
                if doc_id:
                    doc_ids.add(doc_id)
            if doc_ids:
                return doc_ids

        # 格式2（旧）：docstore/ref_doc_info
        ref_info = docstore.get("docstore/ref_doc_info", {})
        return set(ref_info.keys())
    except Exception as e:
        _logger.warning("failed to read docstore for kb %s: %s", kb_id, e)
        return set()


class CreateKBRequest(BaseModel):
    name: str
    description: str = ""
    category: Literal["national", "industry", "enterprise"] = "national"


class KBDocumentResponse(BaseModel):
    id: str
    name: str
    original_name: str
    file_type: str
    page_count: int | None
    embedding_status: str

    @classmethod
    def from_doc(cls, doc):
        return cls(
            id=doc.id,
            name=doc.name,
            original_name=doc.original_name,
            file_type=doc.file_type,
            page_count=doc.page_count,
            embedding_status=doc.embedding_status,
        )


class KBResponse(BaseModel):
    id: str
    name: str
    description: str
    category: str
    created_at: str
    updated_at: str
    document_count: int
    index_status: str
    index_progress: Optional[float] = None
    index_current_doc: str = ""

    @classmethod
    def from_kb(cls, kb):
        return cls(
            id=kb.id,
            name=kb.name,
            description=kb.description,
            category=kb.category,
            created_at=kb.created_at.isoformat() if hasattr(kb.created_at, 'isoformat') else str(kb.created_at),
            updated_at=kb.updated_at.isoformat() if hasattr(kb.updated_at, 'isoformat') else str(kb.updated_at),
            document_count=len(kb.document_ids),
            index_status=kb.index_status,
            index_progress=getattr(kb, 'index_progress', 0.0),
            index_current_doc=getattr(kb, 'index_current_doc', ''),
        )


@router.get("", response_model=list[KBResponse])
def list_kbs(category: Optional[str] = Query(None)):
    """获取知识库列表"""
    kbs = kb_svc.list_kbs(category=category)
    return [KBResponse.from_kb(kb) for kb in kbs]


@router.post("", response_model=KBResponse)
def create_kb(req: CreateKBRequest):
    """创建知识库"""
    kb = kb_svc.create_kb(
        name=req.name,
        description=req.description,
        category=req.category,
    )
    return KBResponse.from_kb(kb)


@router.get("/{kb_id}", response_model=KBResponse)
def get_kb(kb_id: str):
    """获取知识库详情"""
    kb = kb_svc.get_kb(kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    return KBResponse.from_kb(kb)


@router.delete("/{kb_id}")
def delete_kb(kb_id: str):
    """删除知识库"""
    success = kb_svc.delete_kb(kb_id)
    if not success:
        raise HTTPException(status_code=404, detail="知识库不存在")
    return {"message": "删除成功"}


@router.post("/{kb_id}/reindex")
def reindex_kb(kb_id: str):
    """重建知识库索引（异步：立即返回，后台运行，进度通过 GET 查询）。

    KB 检索状态字段（``index_status`` / ``index_progress`` /
    ``index_current_doc``）由 ``KbIndexStatusWriter`` 独占写入（issue #148 /
    #147 / #151）：开头预写 ``building``，``_on_progress`` 回调走
    ``note_in_flight`` + ``advance``；终态由 ``rebuild_kb_index`` 内置契约
    （issue #149）经同一 writer 类的另一实例写 —— router 这层不二次收尾，
    只剩 setup 阶段异常的兜底 ``failed``。
    """
    kb = kb_svc.get_kb(kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")

    if kb.index_status == "building":
        raise HTTPException(status_code=409, detail="索引正在重建中")

    # 标记所有文档为 pending_index，先于 begin() —— begin() 一调，外部 GET
    # 立刻看到 building，reindex / bulk-reparse 互斥也立刻生效
    all_docs = doc_repo.list_docs(kb_id)
    for doc in all_docs:
        doc.embedding_status = "pending_index"
        doc_repo._save_doc_meta(doc)

    kb_writer = KbIndexStatusWriter(kb_id, total=len(all_docs))
    kb_writer.begin()

    def _on_progress(current: int, total: int, doc_name: str):
        """每索引完一篇文档的回调 —— 走 writer，不直写字段。

        ``total`` 参数由 ``rebuild_kb_index`` 传过来（无向量缓存需重跑的那批
        doc 的篇数），router 这层不依赖它：进度分母用 writer 构造时的
        ``total=len(all_docs)``，整体保持在 [0, 1] 内即可。
        """
        kb_writer.note_in_flight(doc_name)
        kb_writer.advance(current)

    def _run():
        """后台执行重建。成功 / 失败终态由 ``rebuild_kb_index`` 内部 writer
        写（ADR-0002 / #149），router 这层不在 success 分支二次收尾；
        但 setup / post-work（FAISS 核实、doc 元数据落盘）的异常仍走
        writer 兜底 failed —— 保留旧版"任何步骤出错 KB 都进 failed"的契约
        （issue #151 AC："保持外部行为不变"）。"""
        import services.vector_search as vs
        try:
            vs.rebuild_kb_index(kb_id, progress_callback=_on_progress)
            # 重建完成后，检查实际索引结果并更新各文档的向量化状态
            # （rebuild_kb_index 内部可能因节点不匹配等原因跳过某些文档，
            #   所以需要核实 FAISS 索引中实际包含哪些文档）
            indexed_doc_ids = _get_actually_indexed_doc_ids(kb_id)
            for doc in all_docs:
                if doc.id in indexed_doc_ids:
                    doc.embedding_status = "embedded"
                else:
                    doc.embedding_status = "failed"
                    _logger.warning(
                        "reindex: doc %s (%s) not found in FAISS index after rebuild, marked as failed",
                        doc.id, doc.original_name,
                    )
                doc_repo._save_doc_meta(doc)
        except Exception as e:
            # rebuild_kb_index 自己已写过 failed（issue #149）；这里对 setup
            # / post-work 异常也兜底（最后一次写覆盖前一次，行为与旧版
            # "任何步骤出错 KB 都进 failed" 一致）；错误信息格式收敛到
            # writer 一处（#151），不再有 "错误: <msg>" 这种私有前缀。
            kb_writer.finish(failed=[("reindex", str(e))])
            for doc in all_docs:
                if doc.embedding_status == "pending_index":
                    doc.embedding_status = "failed"
                    doc_repo._save_doc_meta(doc)
        # success 分支：KB 检索状态由 rebuild_kb_index 内部 writer 在锁内
        # 按 ADR-0002 收尾，router 这层不再二次 fetch+update。

    import threading
    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    return {"message": "索引重建已启动"}


@router.get("/{kb_id}/documents", response_model=list[KBDocumentResponse])
def list_kb_documents(kb_id: str):
    """获取知识库内的文档列表"""
    kb = kb_svc.get_kb(kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    docs = doc_repo.list_docs(kb_id)
    return [KBDocumentResponse.from_doc(doc) for doc in docs]


@router.delete("/{kb_id}/documents/{doc_id}")
def delete_kb_document(kb_id: str, doc_id: str):
    """删除知识库中的文档"""
    import services.doc_service as doc_svc
    success = doc_svc.delete_document(kb_id, doc_id)
    if not success:
        raise HTTPException(status_code=404, detail="文档不存在")
    return {"message": "删除成功"}


# ── 批量重新解析 (Bulk Reparse) — issue #111 ──────────────────────────────────
#
# 把一次性脚本 ``scripts/bulk_reparse.py`` 升级为产品入口。三个端点：
#   - GET  /bulk-reparse/preflight?force=...  — 无副作用 dry-run
#   - POST /bulk-reparse                       — 异步触发（接受 concurrency / force）
#   - GET  /bulk-reparse/report                — 上次运行的报告
#
# 三条选取规则与成本估算只有一份实现（``services.bulk_reparse_service``），
# CLI 与 API 共用。KB 状态机由 ``run_bulk_reparse`` 内的 ``_KbIndexStatus``
# 统一接管，HTTP 层只做"已 building 则 409"这一道关卡 —— reindex 端点也复用
# 同一字段，互斥天然成立。


class BulkReparsePreflightResponse(BaseModel):
    """预检返回：无副作用 dry-run 的成本估算 + 每篇入选原因。"""

    kb_id: str
    force: bool
    target_count: int
    cached_docs: int
    uncached_docs: int
    # 历史 ``source=fallback_pdfplumber`` 条目（V8 cache defense 标的"污染"）
    # 在 #99/05 后不再判废，按命中计费；这里仍独立列出，运维清理时方便点名。
    polluted_cached_docs: int = 0
    cached_pages: int
    uncached_pages: int
    estimated_ocr_pages: int
    targets: list[dict]
    over_page_limit: list[dict]


class BulkReparseTriggerRequest(BaseModel):
    concurrency: int = DEFAULT_CONCURRENCY
    force: bool = False


class BulkReparseTriggerResponse(BaseModel):
    """202 响应：AC 明确要求"返回 202 时 KB 处于 ``building``"，把状态塞进响应体，
    让客户端不必再发一次 GET 就能立即确认；同时携带目标数，便于 UI 立刻渲染。"""

    kb_id: str
    target_count: int
    index_status: str = "building"


def _build_preflight_payload(kb_id: str, *, force: bool) -> BulkReparsePreflightResponse:
    """预检核心：选目标 + 估算成本，纯函数 + 无副作用（issue #111 AC 2）。"""
    targets = list_target_docs(kb_id, force=force)
    cost: OcrCostEstimate = estimate_ocr_cost(targets)
    return BulkReparsePreflightResponse(
        kb_id=kb_id,
        force=force,
        target_count=len(targets),
        cached_docs=cost.cached,
        uncached_docs=cost.uncached,
        polluted_cached_docs=0,  # V8 cache defense 已删（#99/05），运维清理单独 ticket
        cached_pages=cost.pages_cached,
        uncached_pages=cost.pages_uncached,
        # OCR 配额只烧在未命中 → 预估 OCR 页数 = 未命中页数
        estimated_ocr_pages=cost.pages_uncached,
        targets=[
            {
                "doc_id": t.doc.id,
                "original_name": t.doc.original_name,
                "page_count": t.estimated_page_count,
                "reason": t.reason,
                # ``cache_state(doc)`` 已直接返 ``"cached" | "uncached"`` —— 不再做中间换算。
                "cache_state": cache_state(t.doc),
            }
            for t in targets
        ],
        over_page_limit=[
            {
                "doc_id": s.doc.id,
                "original_name": s.doc.original_name,
                "page_count": s.page_count,
                "reason": s.reason,
            }
            for s in cost.over_page_limit
        ],
    )


@router.get(
    "/{kb_id}/bulk-reparse/preflight",
    response_model=BulkReparsePreflightResponse,
)
def bulk_reparse_preflight(kb_id: str, force: bool = Query(False)):
    """批量重新解析预检 —— 无副作用 dry-run（spec #102 story 6）。

    保证零副作用的边界：仅调用 ``list_target_docs`` + ``estimate_ocr_cost``，
    两者只读 ``doc_repo`` / ``pages_store`` / ``paddleocr_cache``，不触发解析、
    不写 pages/、不写缓存。AC 2 由 ``tests/test_api_bulk_reparse.py::test_preflight_has_zero_side_effects``
    + ``..._does_not_invoke_parse_document`` 双向验证。
    """
    kb = kb_svc.get_kb(kb_id)
    if kb is None:
        raise HTTPException(status_code=404, detail="知识库不存在")
    return _build_preflight_payload(kb_id, force=force)


@router.post(
    "/{kb_id}/bulk-reparse",
    response_model=BulkReparseTriggerResponse,
    status_code=202,
)
def bulk_reparse_trigger(
    kb_id: str,
    req: BulkReparseTriggerRequest = Body(default_factory=BulkReparseTriggerRequest),
):
    """触发一次批量重新解析 —— 异步执行，立即返回 202。

    互斥由 ``kb.index_status == "building"`` 统一守门（spec #102 story 17/18）：
    reindex 与 bulk 共享同一字段，in-flight 任一边都会挡住另一边，无需新 mutex。

    KB 状态机由 ``KbIndexStatusWriter`` 独占（issue #148 / #147 / #151 /
    #154）：本路由预写 ``building`` 让客户端 GET 立刻看到 + reindex 互斥立刻
    生效，``run_bulk_reparse`` 用自己的 writer 实例完成批次期间的进度推进与
    终态收尾 —— 两份 writer 各管各的写，不读 cross-instance 私有锁。两份
    ``begin()`` 落到同一组（building / 0 / ""）的值，幂等。

    **空批次短路**：没有可跑的目标（全被页数上限拦下 / 全 healthy）= 没发生
    任何重解析，``run_bulk_reparse`` 的 ``if total:`` 分支根本不进，KB 状态
    也不会被改写。若在这里也预写 building，KB 会**永远卡在 building** —— 前端
    会一直轮询一个永不落地的批次，比触发失败更糟。直接返回 200（0 目标）即可。

    Body 可省：缺省时 ``concurrency=DEFAULT_CONCURRENCY``、``force=False``；
    前端从确认对话框直接 POST 即可，不必先序列化一份默认值。
    """
    kb = kb_svc.get_kb(kb_id)
    if kb is None:
        raise HTTPException(status_code=404, detail="知识库不存在")
    if kb.index_status == "building":
        raise HTTPException(status_code=409, detail="索引正在重建中")

    # 算目标数（不缓存，spawn 后线程里也会再算；这里只是为了让响应携带这个数）
    targets = list_target_docs(kb_id, force=req.force)

    # 空批次：不预写 building、不 spawn 线程，run_bulk_reparse 自己也不会改 KB。
    if not targets:
        return BulkReparseTriggerResponse(
            kb_id=kb_id, target_count=0, index_status=kb.index_status,
        )

    # 预写 building —— 由 KbIndexStatusWriter 独占写入（issue #148 /
    # #147 / #151）。``run_bulk_reparse`` 内部还会再用它自己的 writer 实例
    # 写一次（同 idempotent 的 begin()），两份都落到同一组字段值
    # （building / 0 / ""）。
    kb_writer = KbIndexStatusWriter(kb_id, total=len(targets))
    kb_writer.begin()

    def _run():
        try:
            run_bulk_reparse(
                kb_id, targets,
                concurrency=req.concurrency,
                forced=req.force,
            )
        except Exception as e:
            # 编排层自身抛错：把 KB 落在 failed 而不是让它永远卡在 building。
            # （run_bulk_reparse 的 ``except BaseException`` 已经写过 failed；
            # 这里是双保险 —— 比如线程根本没启起来 / 列表生成出错。）
            # 走 writer，错误信息格式收敛到 writer 一处（issue #151）。
            kb_writer.finish(
                failed=[("bulk_reparse", f"{type(e).__name__}: {e}")],
            )

    import threading
    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    return BulkReparseTriggerResponse(
        kb_id=kb_id,
        target_count=len(targets),
        index_status="building",
    )


@router.get("/{kb_id}/bulk-reparse/report")
def bulk_reparse_report(kb_id: str):
    """读取上一次批量运行的报告（#110 落盘的 JSON）；从未跑过 → 404。"""
    kb = kb_svc.get_kb(kb_id)
    if kb is None:
        raise HTTPException(status_code=404, detail="知识库不存在")
    report = bulk_reparse_report_store.load_report(kb_id)
    if report is None:
        raise HTTPException(status_code=404, detail="尚未运行过批量重新解析")
    return report