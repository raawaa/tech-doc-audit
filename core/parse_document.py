"""PDF / DOCX 文档解析的唯一入口（PRD #29 / V2）。

``parse_document(file_path) -> ParseResult`` 是 KB 文档导入流水线的起点。
一次解析返回 {by_page, full_text, layout}，所有下游（按页文本存储 / 向量索引 / 文本搜索 / PDF 跳转）
从同一份数据消费——避免历史上双解析器（``extract_text`` + ``extract_text_by_page``）导致的不一致。

降级链（P1 数据层 #32 V1 已落 cache）：
  1. PDF: PaddleOCR-VL-1.6（带缓存，命中即跳过 OCR 配额）
  2. PDF 缓存未命中 → PaddleOCR 重新推理 → 落缓存
  3. PaddleOCR 不可用/失败 → 提取页面 markdown 聚合到 full_text + by_page=单页
  4. PaddleOCR/MinerU 全失败 → pdfplumber 流式逐页抽取
  5. 非 PDF（DOCX / MD）→ 单页 full_text，无 OCR
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

from core.logger import get_logger

_logger = get_logger(__name__)


# ── 数据类 ──────────────────────────────────────────────────────────────────────


@dataclass
class PageText:
    """按页纯文本。page 编号 0-based。"""
    page: int
    text: str


@dataclass
class Block:
    """PaddleOCR ``prunedResult.parsing_res_list`` 一项的归一化坐标视图。

    bbox / polygon 以归一化坐标 ``[x1/W, y1/H, x2/W, y2/H]`` 存储（0-1 浮点），
    与 PDF 渲染分辨率解耦；page 级别同时存原始 width / height 便于换算。
    """
    block_label: str = ""
    block_content: str = ""
    bbox_norm: list[float] = field(default_factory=list)
    polygon_norm: list[list[float]] = field(default_factory=list)
    block_order: int = 0


@dataclass
class PageLayout:
    """单个 PDF 页面的版面信息（含归一化坐标 blocks）。"""
    page: int
    width: int = 0
    height: int = 0
    blocks: list[Block] = field(default_factory=list)


@dataclass
class ParseResult:
    """一次解析产出的结构化结果。"""
    by_page: list[PageText]
    full_text: str
    layout: list[PageLayout]

    def to_dict(self) -> dict:
        """序列化供 cache / pages_store 落盘。"""
        return {
            "by_page": [asdict(p) for p in self.by_page],
            "full_text": self.full_text,
            "layout": [
                {
                    "page": pl.page,
                    "width": pl.width,
                    "height": pl.height,
                    "blocks": [asdict(b) for b in pl.blocks],
                }
                for pl in self.layout
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ParseResult":
        """从 cache / pages_store 反序列化。"""
        by_page = [PageText(**p) for p in data.get("by_page", [])]
        layout = [
            PageLayout(
                page=pl.get("page", 0),
                width=pl.get("width", 0),
                height=pl.get("height", 0),
                blocks=[Block(**b) for b in pl.get("blocks", [])],
            )
            for pl in data.get("layout", [])
        ]
        return cls(
            by_page=by_page,
            full_text=data.get("full_text", ""),
            layout=layout,
        )


# ── 解析入口 ────────────────────────────────────────────────────────────────────


_PADDLEOCR_API_URL = os.environ.get("PADDLEOCR_API_URL", "").rstrip("/")
_PADDLEOCR_API_TOKEN = os.environ.get("PADDLEOCR_API_TOKEN", "").strip()
_PADDLEOCR_MODEL = os.environ.get("PADDLEOCR_MODEL", "PaddleOCR-VL-1.6")


def _paddleocr_available() -> bool:
    return bool(_PADDLEOCR_API_TOKEN and _PADDLEOCR_API_URL)


def parse_document(file_path: str, *, use_cache: bool = True) -> ParseResult:
    """解析单份文档，返回 ParseResult。

    PDF: 缓存命中 → 直接反序列化；未命中 → PaddleOCR → 缓存。
    非 PDF: 单页 ParseResult。
    失败: 返回 by_page=[PageText(0, full_text)] 的兜底 ParseResult，full_text 可能为空。

    Args:
        file_path: 文档绝对路径。
        use_cache: True（默认）→ 查/写 ``core.paddleocr_cache``；False → 强制重新解析。
    """
    if not file_path or not Path(file_path).exists():
        return _empty_result()

    ext = Path(file_path).suffix.lower()

    if ext in (".docx", ".doc"):
        return _parse_docx(file_path)
    if ext in (".md", ".markdown", ".txt"):
        return _parse_plain_text(file_path)
    if ext != ".pdf":
        # 其他格式：视作文本读取
        return _parse_plain_text(file_path)

    return _parse_pdf(file_path, use_cache=use_cache)


# ── PDF 路径（含缓存）────────────────────────────────────────────────────────────


def _parse_pdf(file_path: str, *, use_cache: bool) -> ParseResult:
    cached: Optional[dict] = None
    if use_cache:
        from core.paddleocr_cache import get_cached
        cached = get_cached(file_path)

    if cached is not None:
        return ParseResult.from_dict(cached)

    result = _paddleocr_parse(file_path)

    if use_cache and result.by_page:
        try:
            from core.paddleocr_cache import save_cached
            save_cached(file_path, result.to_dict())
        except Exception as e:
            _logger.warning("parse_document: cache save failed for %s: %s", file_path, e)

    return result


def _paddleocr_parse(file_path: str) -> ParseResult:
    """调 PaddleOCR API → 落 ParseResult；不可用 / 失败 → 降级。"""
    if not _paddleocr_available():
        return _pdf_fallback(file_path)

    try:
        return _paddleocr_call(file_path)
    except Exception as e:
        _logger.warning("paddleocr_parse failed for %s: %s", file_path, e)
        return _pdf_fallback(file_path)


def _paddleocr_call(file_path: str) -> ParseResult:
    """实际的 PaddleOCR-VL-1.6 调用流程。失败时抛 Exception。"""
    import requests  # 延迟 import，允许离线测试

    headers = {"Authorization": f"bearer {_PADDLEOCR_API_TOKEN}"}

    # 提交 job
    data = {
        "model": _PADDLEOCR_MODEL,
        "optionalPayload": json.dumps({
            "useDocOrientationClassify": False,
            "useDocUnwarping": False,
            "useChartRecognition": False,
        }),
    }
    with open(file_path, "rb") as f:
        resp = requests.post(
            _PADDLEOCR_API_URL, headers=headers, data=data,
            files={"file": f}, timeout=120,
        )
    resp.raise_for_status()
    job_id = resp.json()["data"]["jobId"]

    # 轮询
    deadline = time.monotonic() + 600
    jsonl_url = ""
    while time.monotonic() < deadline:
        try:
            r = requests.get(f"{_PADDLEOCR_API_URL}/{job_id}", headers=headers, timeout=30)
            r.raise_for_status()
            j = r.json()["data"]
            state = j["state"]
            if state == "done":
                jsonl_url = j["resultUrl"]["jsonUrl"]
                break
            if state == "failed":
                raise RuntimeError(f"paddleocr job failed: {j.get('errorMsg', '?')}")
        except RuntimeError:
            raise
        except Exception:
            pass
        time.sleep(5)

    if not jsonl_url:
        raise RuntimeError("paddleocr timeout")

    # 取 JSONL
    r = requests.get(jsonl_url, timeout=120)
    r.raise_for_status()

    return _paddleocr_jsonl_to_parse_result(r.text)


def _paddleocr_jsonl_to_parse_result(jsonl_text: str) -> ParseResult:
    """解析 PaddleOCR JSONL 响应 → ParseResult。

    每行对应一页的 layoutParsingResults；从 ``markdown.text`` 取页面文本，
    从 ``prunedResult.parsing_res_list`` 取归一化 block 坐标。
    标题层级修复（HeadingProcessor）应用到 full_text。
    """
    page_texts: list[PageText] = []
    page_layouts: list[PageLayout] = []
    page_order = 0

    for line in jsonl_text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        result = payload.get("result") or {}
        for res in result.get("layoutParsingResults", []):
            md_text = (res.get("markdown", {}) or {}).get("text", "") or ""
            page_texts.append(PageText(page=page_order, text=md_text.strip()))

            blocks = _extract_blocks(res.get("prunedResult") or {})
            page_layouts.append(PageLayout(
                page=page_order,
                width=int(res.get("width") or 0),
                height=int(res.get("height") or 0),
                blocks=blocks,
            ))
            page_order += 1

    full_md = "\n\n".join(p.text for p in page_texts if p.text)
    if full_md:
        full_md = _normalize_headings(full_md)

    return ParseResult(by_page=page_texts, full_text=full_md, layout=page_layouts)


def _extract_blocks(pruned: dict) -> list[Block]:
    """``prunedResult.parsing_res_list`` → ``[Block]``，bbox / polygon 归一化到 0-1。"""
    raw_blocks = pruned.get("parsing_res_list") or []
    page_size = pruned.get("image_size")
    W, H = _page_dims(page_size)

    blocks: list[Block] = []
    for i, b in enumerate(raw_blocks):
        bbox = _coerce_bbox(b.get("block_bbox") or b.get("bbox"))
        polygon = b.get("block_polygon") or b.get("polygon") or []

        if W and H:
            norm_bbox = [bbox[0] / W, bbox[1] / H, bbox[2] / W, bbox[3] / H] if len(bbox) == 4 else []
            norm_polygon = [
                [(float(p[0]) / W), (float(p[1]) / H)]
                for p in polygon if isinstance(p, (list, tuple)) and len(p) >= 2
            ]
        else:
            norm_bbox, norm_polygon = [], []

        blocks.append(Block(
            block_label=str(b.get("block_label", "") or b.get("label", "")),
            block_content=str(b.get("block_content", "") or b.get("content", "")),
            bbox_norm=norm_bbox,
            polygon_norm=norm_polygon,
            block_order=int(b.get("block_order", i)),
        ))
    return blocks


def _page_dims(image_size) -> tuple[int, int]:
    """``prunedResult.image_size`` → (W, H)。"""
    if isinstance(image_size, (list, tuple)) and len(image_size) >= 2:
        try:
            return int(image_size[0]), int(image_size[1])
        except (TypeError, ValueError):
            pass
    return 0, 0


def _coerce_bbox(raw) -> list:
    """Accept [x1,y1,x2,y2] / (x1,y1,x2,y2) / None → flat list[float]."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        try:
            return [float(v) for v in raw[:4]]
        except (TypeError, ValueError):
            return []
    return []


def _normalize_headings(text: str) -> str:
    """标题层级修复（保留 ``core.heading_processor.HeadingProcessor`` 失败兜底）。"""
    try:
        from core.heading_processor import HeadingProcessor
        return HeadingProcessor().rebuild_from_md(text)
    except Exception:
        return text


# ── PDF 降级：pdfplumber 流式逐页 ───────────────────────────────────────────────


def _pdf_fallback(file_path: str) -> ParseResult:
    """PaddleOCR 不可用 / 失败：走 pdfplumber 流式逐页抽取，落 ParseResult。"""
    try:
        import pdfplumber
    except ImportError:
        return _empty_result()

    page_texts: list[PageText] = []
    try:
        with pdfplumber.open(file_path) as pdf:
            for i, page in enumerate(pdf.pages):
                t = page.extract_text() or ""
                page_texts.append(PageText(page=i, text=t))
                page.flush_cache()
    except Exception as e:
        _logger.warning("pdfplumber fallback failed for %s: %s", file_path, e)
        return _empty_result()

    full_text = "\n\n".join(p.text for p in page_texts if p.text)
    full_text = _normalize_headings(full_text)
    return ParseResult(by_page=page_texts, full_text=full_text, layout=[])


# ── 非 PDF 路径 ────────────────────────────────────────────────────────────────


def _parse_docx(file_path: str) -> ParseResult:
    try:
        from docx import Document as DocxDocument
    except ImportError:
        return _empty_result()
    try:
        parts = [p.text for p in DocxDocument(file_path).paragraphs if p.text and p.text.strip()]
    except Exception as e:
        _logger.warning("docx parse failed for %s: %s", file_path, e)
        return _empty_result()
    text = "\n".join(parts)
    return ParseResult(
        by_page=[PageText(page=0, text=text)] if text else [],
        full_text=text,
        layout=[],
    )


def _parse_plain_text(file_path: str) -> ParseResult:
    try:
        text = Path(file_path).read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        _logger.warning("plain text read failed for %s: %s", file_path, e)
        return _empty_result()
    return ParseResult(
        by_page=[PageText(page=0, text=text)] if text else [],
        full_text=text,
        layout=[],
    )


def _empty_result() -> ParseResult:
    """PaddleOCR + pdfplumber 都失败时的兜底：空结构。"""
    return ParseResult(by_page=[], full_text="", layout=[])
