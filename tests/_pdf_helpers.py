"""共享 PDF 测试 helper。

``make_blank_pdf(path, page_count)`` 在 ``test_pdf_splitter`` 与
``test_parse_document`` (issue #177 AC1-AC4) 都需要 —— 历史两处各写一份,
抽到本模块统一,免去私有 helper 跨文件复制。

调用方需自行保证 ``pymupdf`` 可用(测试用 ``@pytest.mark.requires_pymupdf``
+ ``pytest.skip(...)`` 表达)。
"""
from __future__ import annotations

from pathlib import Path


def make_blank_pdf(path: Path, page_count: int) -> Path:
    """生成 ``page_count`` 页空白 PDF(不插文字 → 文字层空 → 走 PaddleOCR 路径)。

    与 ``test_pdf_splitter`` 既有 helper 同语义。
    """
    import pymupdf
    doc = pymupdf.open()
    for _ in range(page_count):
        doc.new_page(width=595, height=842)
    doc.save(str(path))
    doc.close()
    return path