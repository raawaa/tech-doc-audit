"""文档文本提取 — PDF / DOCX → 纯文本。

优先使用 MinerU（API 常驻模式或子进程模式），降级到 pdfplumber / python-docx。
设置 MINERU_API_URL 环境变量可启用 API 常驻模式（推荐）：
  export MINERU_API_URL=http://127.0.0.1:35005
  mineru-api --host 127.0.0.1 --port 35005 &

API 模式优势：模型只加载一次，反复请求复用，大幅提升批量处理速度。
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


def _get_mineru_api_url() -> Optional[str]:
    """获取 MinerU API 地址（通过环境变量配置）。"""
    url = os.environ.get("MINERU_API_URL", "").strip()
    return url.rstrip("/") if url else None


def extract_text(file_path: str) -> str:
    """从文件提取纯文本。

    优先使用 MinerU（API 常驻模式 → 子进程），降级到 pdfplumber 流式提取。
    """
    ext = Path(file_path).suffix.lower()
    try:
        if ext == ".pdf" and _mineru_available():
            api_url = _get_mineru_api_url()
            if api_url:
                text = _extract_with_mineru_api(file_path, api_url)
            else:
                text = _extract_with_mineru(file_path)
            if text:
                return text
            from core.degradation import record as _deg_record
            _deg_record("text_extraction", "mineru_empty_output",
                         f"MinerU returned empty text for {file_path}, falling back to pdfplumber")
        if ext == ".pdf":
            return _extract_pdf_streaming(file_path)
        if ext in (".docx", ".doc"):
            from docx import Document
            parts = [p.text for p in Document(file_path).paragraphs if p.text.strip()]
            return "\n".join(parts) if parts else ""
        return Path(file_path).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def _call_mineru_api(pdf_path: str, api_url: str) -> Optional[dict]:
    """调用 MinerU API 解析 PDF，返回结果字典。"""
    import requests
    with open(pdf_path, "rb") as f:
        resp = requests.post(
            f"{api_url}/file_parse",
            files={"files": f},
            data={
                "backend": "pipeline",
                "parse_method": "auto",
                "return_md": "true",
                "return_content_list": "true",
            },
            timeout=600,
        )
    resp.raise_for_status()
    data = resp.json()
    results = data.get("results", {})
    if not results:
        return None
    # 获取第一个（也是唯一的）文件的结果
    for _name, result in results.items():
        return result
    return None


def _extract_with_mineru_api(file_path: str, api_url: str) -> str:
    """用 MinerU API 常驻服务解析 PDF（模型常驻内存，速度更快）。"""
    result = _call_mineru_api(file_path, api_url)
    if not result:
        return ""

    md_content = result.get("md_content", "")
    content_list_str = result.get("content_list", "")

    # 有 content_list 时做标题层级修复
    if content_list_str:
        try:
            import json
            from core.heading_processor import HeadingProcessor
            data = json.loads(content_list_str)
            # content_list_v2 格式返回在 content_list 中
            if isinstance(data, list) and data and isinstance(data[0], list):
                items = [it for page in data for it in page]
            elif isinstance(data, list):
                items = data
            else:
                items = []
            if items:
                processor = HeadingProcessor()
                text = processor.rebuild_markdown(items)
                if len(text) > 20:
                    return text
        except Exception:
            pass

    # 降级：MD 文本直接返回
    return md_content if len(md_content) > 20 else ""


def _extract_with_mineru(file_path: str) -> str:
    """用 MinerU 解析 PDF（子进程，内存隔离），从 ModelScope 下载模型。

    返回修复标题层级后的 Markdown 文本（自动应用 HeadingProcessor）。
    """
    import tempfile
    _bin = _MINERU_BIN or _find_mineru()
    if not _bin:
        return ""
    env = os.environ.copy()
    env["MINERU_MODEL_SOURCE"] = "modelscope"
    base = Path(file_path).stem
    with tempfile.TemporaryDirectory() as tmpdir:
        result = subprocess.run(
            [_bin, "-p", file_path, "-o", tmpdir, "-b", "pipeline"],
            capture_output=True, text=True, timeout=600, env=env,
        )
        if result.returncode != 0:
            return ""

        text = _process_mineru_output(Path(tmpdir), base)
        return text if len(text) > 20 else ""


def _process_mineru_output(out_dir: Path, base: str) -> str:
    """处理 MinerU 输出目录，应用标题层级修复。"""
    # 优先 content_list_v2.json
    json_files = list(out_dir.rglob(f"{base}_content_list_v2.json"))
    if json_files:
        try:
            import json
            from core.heading_processor import HeadingProcessor
            data = json.loads(json_files[0].read_text(encoding="utf-8"))
            if isinstance(data, list) and data and isinstance(data[0], list):
                items = [it for page in data for it in page]
            else:
                items = data
            processor = HeadingProcessor()
            text = processor.rebuild_markdown(items)
            if len(text) > 20:
                return text
        except Exception:
            pass

    # 降级：直接读 MD
    candidates = list(out_dir.rglob(f"{base}.md"))
    if not candidates:
        return ""
    md_text = candidates[0].read_text(encoding="utf-8", errors="ignore")
    if len(md_text) > 20:
        from core.heading_processor import HeadingProcessor
        processor = HeadingProcessor()
        md_text = processor.rebuild_from_md(md_text)
    return md_text if len(md_text) > 20 else ""


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
