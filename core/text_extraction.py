"""文档文本提取 — PDF / DOCX → 纯文本。

PDF 提取优先级：
  1. PaddleOCR-VL-1.6 在线 API（需 PADDLEOCR_API_TOKEN，无需本地 GPU）
  2. MinerU API 常驻服务（需 MINERU_API_URL）
  3. MinerU 子进程（需 mineru CLI）
  4. pdfplumber 流式提取（纯 Python，最终降级）

DOCX 使用 python-docx，MD/TXT 直接读取。
"""

import os
import subprocess
import time
from pathlib import Path
from typing import Optional

# ── PaddleOCR-VL-1.6 在线 API 配置 ─────────────────────────────────────────────

_PADDLEOCR_API_URL = os.environ.get(
    "PADDLEOCR_API_URL",
    "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs",
).rstrip("/")
_PADDLEOCR_API_TOKEN = os.environ.get("PADDLEOCR_API_TOKEN", "").strip()
_PADDLEOCR_MODEL = os.environ.get("PADDLEOCR_MODEL", "PaddleOCR-VL-1.6")

_MINERU_BIN: Optional[str] = None


def _paddleocr_available() -> bool:
    """检查 PaddleOCR-VL-1.6 在线 API 是否可用（需配置 Token）。"""
    return bool(_PADDLEOCR_API_TOKEN)


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

    PDF: PaddleOCR → MinerU API → MinerU 子进程 → pdfplumber
    DOCX: python-docx
    其他: 直接读取
    """
    ext = Path(file_path).suffix.lower()
    try:
        if ext == ".pdf":
            # ① PaddleOCR-VL-1.6 在线 API（最高优先级）
            if _paddleocr_available():
                text = _extract_with_paddleocr(file_path)
                if text:
                    return text
                from core.degradation import record as _deg_record
                _deg_record("text_extraction", "paddleocr_empty_output",
                            f"PaddleOCR returned empty text for {file_path}, falling back to MinerU")

            # ② MinerU API 常驻服务
            if _mineru_available():
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

            # ③ pdfplumber 流式提取（最终降级）
            return _extract_pdf_streaming(file_path)

        if ext in (".docx", ".doc"):
            from docx import Document
            parts = [p.text for p in Document(file_path).paragraphs if p.text.strip()]
            return "\n".join(parts) if parts else ""
        return Path(file_path).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def _extract_with_paddleocr(file_path: str) -> str:
    """用 PaddleOCR-VL-1.6 在线 API 解析 PDF。

    流程：提交文件 → 轮询 job 状态 → 下载 JSONL 结果 → 拼接 Markdown → 标题层级修复。
    返回修复后的 Markdown 文本，失败返回 ""。
    """
    import json
    import requests

    headers = {"Authorization": f"bearer {_PADDLEOCR_API_TOKEN}"}

    # ── 提交 job ──
    data = {
        "model": _PADDLEOCR_MODEL,
        "optionalPayload": json.dumps({
            "useDocOrientationClassify": False,
            "useDocUnwarping": False,
            "useChartRecognition": False,
        }),
    }
    try:
        with open(file_path, "rb") as f:
            resp = requests.post(
                _PADDLEOCR_API_URL,
                headers=headers,
                data=data,
                files={"file": f},
                timeout=120,
            )
        resp.raise_for_status()
        job_id = resp.json()["data"]["jobId"]
    except Exception as e:
        from core.degradation import record as _deg_record
        _deg_record("text_extraction", "paddleocr_submit_failed",
                     f"PaddleOCR job submission failed: {e}")
        return ""

    # ── 轮询 job 状态 ──
    deadline = time.monotonic() + 600
    jsonl_url = ""
    while time.monotonic() < deadline:
        try:
            job_resp = requests.get(f"{_PADDLEOCR_API_URL}/{job_id}", headers=headers, timeout=30)
            job_resp.raise_for_status()
            job_data = job_resp.json()["data"]
            state = job_data["state"]

            if state == "done":
                jsonl_url = job_data["resultUrl"]["jsonUrl"]
                break
            elif state == "failed":
                error_msg = job_data.get("errorMsg", "unknown error")
                from core.degradation import record as _deg_record
                _deg_record("text_extraction", "paddleocr_job_failed",
                             f"PaddleOCR job {job_id} failed: {error_msg}")
                return ""
            # pending / running → continue polling
        except Exception:
            pass
        time.sleep(5)

    if not jsonl_url:
        from core.degradation import record as _deg_record
        _deg_record("text_extraction", "paddleocr_timeout",
                     f"PaddleOCR job {job_id} timed out after 600s")
        return ""

    # ── 下载 JSONL 结果并拼接 Markdown ──
    try:
        jsonl_resp = requests.get(jsonl_url, timeout=120)
        jsonl_resp.raise_for_status()
    except Exception as e:
        from core.degradation import record as _deg_record
        _deg_record("text_extraction", "paddleocr_download_failed",
                     f"Failed to download PaddleOCR result: {e}")
        return ""

    parts: list[str] = []
    for line in jsonl_resp.text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            result = json.loads(line)["result"]
            for res in result.get("layoutParsingResults", []):
                md_text = res.get("markdown", {}).get("text", "")
                if md_text.strip():
                    parts.append(md_text.strip())
        except Exception:
            continue

    if not parts:
        return ""

    full_md = "\n\n".join(parts)

    # ── 标题层级修复 ──
    try:
        from core.heading_processor import HeadingProcessor
        processor = HeadingProcessor()
        full_md = processor.rebuild_from_md(full_md)
    except Exception:
        pass

    return full_md if len(full_md) > 20 else ""


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
