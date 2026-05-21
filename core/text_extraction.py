"""文档文本提取 — PDF / DOCX → 纯文本。

优先使用 MinerU（子进程，内存隔离），降级到 pdfplumber / python-docx。
"""

import os
import subprocess
from pathlib import Path
from typing import Optional

_MINERU_BIN: Optional[str] = None


def _find_mineru() -> Optional[str]:
    import shutil
    try:
        path = shutil.which("mineru")
        if path:
            result = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                return path
    except Exception:
        pass
    return None


def _mineru_available() -> bool:
    global _MINERU_BIN
    if _MINERU_BIN is None:
        _MINERU_BIN = _find_mineru()
    return _MINERU_BIN is not None


def extract_text(file_path: str) -> str:
    """从文件提取纯文本。

    优先使用 MinerU（子进程，内存隔离），降级到 pdfplumber 流式提取。
    """
    ext = Path(file_path).suffix.lower()
    try:
        if ext == ".pdf" and _mineru_available():
            text = _extract_with_mineru(file_path)
            if text:
                return text
        if ext == ".pdf":
            return _extract_pdf_streaming(file_path)
        if ext in (".docx", ".doc"):
            from docx import Document
            parts = [p.text for p in Document(file_path).paragraphs if p.text.strip()]
            return "\n".join(parts) if parts else ""
        return Path(file_path).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def _extract_with_mineru(file_path: str) -> str:
    """用 MinerU 解析 PDF（子进程，内存隔离），从 ModelScope 下载模型。"""
    import tempfile
    _bin = _MINERU_BIN or _find_mineru()
    if not _bin:
        return ""
    env = os.environ.copy()
    env["MINERU_MODEL_SOURCE"] = "modelscope"
    with tempfile.TemporaryDirectory() as tmpdir:
        result = subprocess.run(
            [_bin, "-p", file_path, "-o", tmpdir, "-b", "pipeline"],
            capture_output=True, text=True, timeout=600, env=env,
        )
        if result.returncode != 0:
            return ""
        base = Path(file_path).stem
        candidates = list(Path(tmpdir).rglob(f"{base}.md"))
        if not candidates:
            return ""
        text = candidates[0].read_text(encoding="utf-8", errors="ignore")
        return text if len(text) > 20 else ""


def _extract_pdf_streaming(file_path: str) -> str:
    """流式提取 PDF 文本，每 50 页 flush 一次释放内存。"""
    import pdfplumber
    parts = []
    with pdfplumber.open(file_path) as pdf:
        for i, page in enumerate(pdf.pages):
            t = page.extract_text() or ""
            if t:
                parts.append(t)
            page.flush_cache()
            if i > 0 and i % 50 == 0:
                parts = ["\n\n".join(parts)]
    return "\n\n".join(parts) if parts else ""
