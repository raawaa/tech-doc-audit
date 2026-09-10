"""PaddleOCR 超限 PDF 分块解析（issue #173 / #176 — T03）。

PaddleOCR SaaS 对 > ``PADDLEOCR_PAGE_LIMIT`` 的源 PDF **静默截断**（issue #160）:
服务端只识别前 100 页、剩余静默丢弃,但 job ``state=done`` —— 调用方从响应里
完全无法与正常成功区分。本模块在 **文件层** 把超限 PDF 切成若干 ≤
``PDF_SPLIT_CHUNK_PAGES`` 的子 PDF,顺序逐块 OCR,再按源 PDF 物理页号缝合
成一份完整的 ``ParseResult``,对调用方透明。

对外三符号（spec #173 §B）:

- :func:`chunk_ranges` —— 纯函数:``page_count → [(start, end)]`` 0-based 闭区间。
  ``bulk_reparse_service`` 预检也会调它算 ``chunks_planned`` —— **唯一**实现
  （不在第二处再写一份 ``ceil`` 除法）。
- :func:`parse_split` —— 主流程:取页数 → pymupdf ``insert_pdf`` 写出子 PDF →
  顺序逐块 :func:`core.parse_document.parse_document` →
  对账 + 缝合页号 → 整篇 ``_normalize_headings`` → 成功 ``rmtree`` 临时目录,
  失败留给 :func:`reap_scratch`。
- :func:`reap_scratch` —— 删除 ``mtime`` 超过 ``PDF_SPLIT_SCRATCH_TTL_HOURS``
  的 ``{run_uuid}`` 顶层目录,由 ``run_bulk_reparse`` 入口调用一次。

调用入口在 :mod:`core.parse_document` 的 :func:`_paddleocr_parse` —— **不**
接管 :func:`_parse_pdf` 路由（文字层 PDF 仍走 PyMuPDF 零配额路径）。
"""
from __future__ import annotations

import shutil
import time
import uuid
from pathlib import Path
from typing import Optional

from core.data_dir import get_data_dir
from core.hashutil import file_sha256_short
from core.logger import get_logger
from core.parse_document import (
    ParseResult,
    _normalize_headings,
    pdf_page_count,
)
from core.settings import (
    PADDLEOCR_PAGE_LIMIT,
    PDF_SPLIT_CHUNK_PAGES,
    PDF_SPLIT_SCRATCH_TTL_HOURS,
)

_logger = get_logger(__name__)

# 不变量(spec #173 §B):单块页数上限 < SaaS 截断点 → 子块不会再被服务端二次截断。
# 这条保证 ``parse_split`` 的递归终止性(每块走完整 ``parse_document`` 入口时会
# 再次 ``pdf_page_count``;若 ``PDF_SPLIT_CHUNK_PAGES >= PADDLEOCR_PAGE_LIMIT``,
# 每次分发都判定"超限"→ 无限递归)。env 错误配置时直接抛,而不是悄无声息地爆栈。
assert PDF_SPLIT_CHUNK_PAGES < PADDLEOCR_PAGE_LIMIT, (
    f"PDF_SPLIT_CHUNK_PAGES ({PDF_SPLIT_CHUNK_PAGES}) must be strictly less "
    f"than PADDLEOCR_PAGE_LIMIT ({PADDLEOCR_PAGE_LIMIT}) — otherwise "
    f"parse_split would recurse infinitely."
)

__all__ = ["chunk_ranges", "parse_split", "reap_scratch", "split_scratch_root"]


# ── 公共符号 ──────────────────────────────────────────────────────────────────


def chunk_ranges(page_count: int) -> list[tuple[int, int]]:
    """把 ``page_count`` 切成 ``[(start, end)]`` 0-based 闭区间,每块 ``≤ PDF_SPLIT_CHUNK_PAGES``。

    - ``page_count <= 0`` → 空 list（无页不切）。
    - ``page_count <= PDF_SPLIT_CHUNK_PAGES`` → 单块 ``(0, page_count - 1)``。
    - 否则按 ``PDF_SPLIT_CHUNK_PAGES`` 等切；最后一块可能不满。

    纯函数；不读文件、不调 OCR。被 :func:`parse_split` 与
    ``services.bulk_reparse_service`` 的 OCR 成本预检共用 —— **第二处不许
    出现 ``ceil(page_count / PDF_SPLIT_CHUNK_PAGES)``**（spec #173 AC）。
    """
    if page_count <= 0:
        return []
    step = PDF_SPLIT_CHUNK_PAGES
    if page_count <= step:
        return [(0, page_count - 1)]
    return [
        (start, min(start + step - 1, page_count - 1))
        for start in range(0, page_count, step)
    ]


# ── 临时目录布局 ──────────────────────────────────────────────────────────────


def split_scratch_root() -> Path:
    """``{AUDIT_DATA_DIR}/.scratch/split_pdfs/`` —— 顶层按 ``run_uuid`` 切。

    每次调用重新读 ``AUDIT_DATA_DIR``（同 :func:`core.data_dir.get_data_dir`
    的 per-test 隔离语义,issue #137）。不在 import 时定死。
    """
    return get_data_dir() / ".scratch" / "split_pdfs"


def _doc_scratch_dir(file_path: str, run_uuid: str) -> Path:
    """``{run_uuid}/{源文件 sha256[:12]}/`` —— 单次 ``parse_split`` 调用的工作目录。

    按源文件 sha256 取前 12 hex 而非用原文件名：避免中文 / 空格 / 超长路径
    触发 OS 限制；同一份 doc 在同一次 run 里复用同一目录（多次进 parse_split
    时分块写到同目录再清）。并发跑批时 run_uuid 不同 → 不互相覆盖。
    """
    return split_scratch_root() / run_uuid / file_sha256_short(file_path)


def reap_scratch() -> int:
    """删除 ``mtime`` 超过 :data:`PDF_SPLIT_SCRATCH_TTL_HOURS` 的 ``{run_uuid}`` 子树。

    由 ``run_bulk_reparse`` 入口调一次；**不**删单个 ``{run_uuid}`` 之外的
    子树（即便超时），避免误删其他活跃 run 的临时目录。返回删除的子树数。

    异常 swallow：单个子树删不动不阻塞其它；``scratch_root`` 不存在直接返回 0。
    """
    root = split_scratch_root()
    if not root.exists():
        return 0
    cutoff = time.time() - PDF_SPLIT_SCRATCH_TTL_HOURS * 3600
    removed = 0
    for run_dir in root.iterdir():
        if not run_dir.is_dir():
            continue
        try:
            mtime = run_dir.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            try:
                shutil.rmtree(run_dir)
                removed += 1
            except OSError as e:
                _logger.debug("reap_scratch: failed to remove %s: %s", run_dir, e)
    return removed


# ── 主流程 ────────────────────────────────────────────────────────────────────


def parse_split(file_path: str, *, run_uuid: Optional[str] = None) -> ParseResult:
    """PaddleOCR 超限 PDF 拆分解析主流程（issue #176）。

    Args:
        file_path: 源 PDF 绝对路径（已由 :func:`_paddleocr_parse` 判定为
                   物理页数 > :data:`core.settings.PADDLEOCR_PAGE_LIMIT`）。
        run_uuid: 本次流程顶层 UUID4 字符串；``None`` 则现场生成。
                  并发跑批时由 ``run_bulk_reparse`` 顶层生成、传入,避免
                  互相删对方临时目录。

    Returns:
        缝合后的完整 :class:`ParseResult`,``len(by_page) == page_count``,
        ``[p.page for p in by_page] == list(range(page_count))``,
        ``full_text`` 已做整篇 :func:`core.parse_document._normalize_headings`
        —— 与不拆路径的 ``full_text`` **逐字节等价**（spec #173 AC#44）。

    Raises:
        RuntimeError: 任意一块 ``parse_document`` 返回的 ``len(by_page)``
            与该块物理页数不一致 —— 上抛给 :func:`_parse_pdf`,
            由其转抛给 :func:`parse_document` 调用方；缓存不写（解析失败
            路径不触发 ``save_cached``）。
        FileNotFoundError / pymupdf exceptions: 源 PDF 不可读（损坏 / 加密）,
            传播不 swallow。
    """
    if run_uuid is None:
        run_uuid = uuid.uuid4().hex

    # 延迟 import：pdf_splitter.parse_split 被 _paddleocr_parse 调用,而
    # _paddleocr_parse 在 parse_document 模块顶层定义 —— 模块级 from-import
    # 会反向触发未完全初始化的 parse_document。
    from core.parse_document import parse_document as _parse_document
    import pymupdf

    page_count = pdf_page_count(file_path)
    if page_count is None:
        # 调用方已用 pdf_page_count 判过 > limit；理论上走不到这里。
        # 防御性：源 PDF 在两次读取之间被损坏 / 加密 → 不假装成功,直接抛。
        raise RuntimeError(
            f"pdf_splitter.parse_split: cannot read page_count of {file_path}"
        )

    ranges = chunk_ranges(page_count)
    doc_scratch = _doc_scratch_dir(file_path, run_uuid)
    doc_scratch.mkdir(parents=True, exist_ok=True)

    by_page: list = []
    layout: list = []

    # 注:此处**不**用 try/except 把异常吞掉 —— 异常路径必须让 doc_scratch
    # 保留给 :func:`reap_scratch` 接管(spec:"失败保留给 reaper"),所以不能
    # 让 rmtree 在异常时执行;直接让异常自然上抛即可。
    with pymupdf.open(file_path) as src:
        for i, (start, end) in enumerate(ranges):
            chunk_pages = end - start + 1
            chunk_doc = pymupdf.open()
            chunk_doc.insert_pdf(src, from_page=start, to_page=end)
            chunk_path = doc_scratch / f"chunk_{i:03d}_{start}-{end}.pdf"
            chunk_doc.save(str(chunk_path))
            chunk_doc.close()

            # 走完整 parse_document 入口:自动复用"_paddleocr_parse"内部的
            # 空结果 → orientation_classify=True 重试。use_cache=False 保证
            # 子块 OCR 产物**不**写缓存(规范:缓存只挂在源 PDF sha256 槽位)。
            chunk_result = _parse_document(str(chunk_path), use_cache=False)

            # 页数对账(spec #173 AC#40/#41/#43: 唯一实现,失败就抛)。
            actual = len(chunk_result.by_page)
            if actual != chunk_pages:
                raise RuntimeError(
                    f"pdf_splitter: page-count reconciliation failed at "
                    f"chunk {i} (pages {start}-{end}): expected "
                    f"{chunk_pages} pages, got {actual}"
                )

            # 缝合页号:by_page[i].page / layout[i].page 都按源 PDF 物理页号
            # 写。block_order **不**重编号 —— per-page-local 是 OCR 解析器
            # 输出约定,跨块重编号会破坏按块索引语义。
            for j, pt in enumerate(chunk_result.by_page):
                pt.page = start + j
            for j, pl in enumerate(chunk_result.layout):
                pl.page = start + j
            by_page.extend(chunk_result.by_page)
            layout.extend(chunk_result.layout)

    # 成功路径:full_text 整篇重建 + 一次 _normalize_headings(spec AC#44:
    # 拆 / 不拆产出的 full_text 逐字节等价)。
    #
    # 注:每块本身在 _paddleocr_jsonl_to_parse_result 内部也调过
    # _normalize_headings —— 我们**丢弃**那次的结果,改用 ``by_page.text``
    # 重新拼接后再 normalize 一次。该等价性依赖
    # ``HeadingProcessor.rebuild_from_md`` 是**逐行幂等**的(line-local,
    # 不累积跨行上下文);若日后变成 context-sensitive,AC#4 会静默破。
    full_text = "\n\n".join(p.text for p in by_page if p.text)
    if full_text:
        full_text = _normalize_headings(full_text)

    try:
        shutil.rmtree(doc_scratch)
    except OSError as e:
        _logger.debug("pdf_splitter: failed to clean %s: %s", doc_scratch, e)

    return ParseResult(by_page=by_page, full_text=full_text, layout=layout)
