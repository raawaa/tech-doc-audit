"""PaddleOCR 缓存层单元测试。

覆盖 (#32 V1) 验收点：
- 未命中返回 None
- 同 PDF 同版本二次调用返回缓存
- 同 PDF 不同版本自动失效
- PDF 内容变更（hash 变）自动失效
- clear_cache() 清空目录
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from core import paddleocr_cache


@pytest.fixture
def cache_dir(monkeypatch, tmp_path):
    """重定向缓存根目录到临时目录。"""
    root = tmp_path / "paddleocr_cache"
    monkeypatch.setattr(paddleocr_cache, "CACHE_DIR", root)
    return root


# ── 1. 未命中路径 ─────────────────────────────────────────────────────────────


def test_get_cached_returns_none_when_cache_dir_missing(tmp_path, monkeypatch):
    """缓存目录不存在 → get_cached 返回 None，不抛异常。"""
    monkeypatch.setattr(paddleocr_cache, "CACHE_DIR", tmp_path / "nope")
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4\nfake")

    assert paddleocr_cache.get_cached(str(pdf)) is None


def test_get_cached_returns_none_when_file_not_in_cache(cache_dir, tmp_path):
    """缓存目录存在但无此文件缓存 → 返回 None。"""
    pdf = tmp_path / "y.pdf"
    pdf.write_bytes(b"%PDF-1.4\nfake")

    assert paddleocr_cache.get_cached(str(pdf)) is None


# ── 2. save + get 往返 ───────────────────────────────────────────────────────


def test_save_then_get_returns_cached_result(cache_dir, tmp_path):
    """save_cached 落盘后 get_cached 命中 → 返回 result。"""
    pdf = tmp_path / "z.pdf"
    pdf.write_bytes(b"%PDF-1.4\nfake")
    result = {"layoutParsingResults": [{"page": 0, "markdown": {"text": "hello"}}]}

    paddleocr_cache.save_cached(str(pdf), result)
    loaded = paddleocr_cache.get_cached(str(pdf))

    assert loaded == result


# ── 3. 版本隔离 ──────────────────────────────────────────────────────────────


def test_save_with_different_versions_writes_separate_files(cache_dir, tmp_path):
    """同 PDF 用不同 model_version 落盘 → 落两份独立文件。"""
    pdf = tmp_path / "v.pdf"
    pdf.write_bytes(b"%PDF-1.4\nv")
    r_v1 = {"model": "PaddleOCR-VL-1.5"}
    r_v2 = {"model": "PaddleOCR-VL-1.6"}

    p1 = paddleocr_cache.save_cached(str(pdf), r_v1, model_version="PaddleOCR-VL-1.5")
    p2 = paddleocr_cache.save_cached(str(pdf), r_v2, model_version="PaddleOCR-VL-1.6")

    assert p1 != p2
    assert p1.exists() and p2.exists()


def test_stale_version_cache_treated_as_miss_for_current_version(cache_dir, tmp_path):
    """当前 env 升到新版本时，旧版本缓存自动失效（get_cached 返回 None）。"""
    pdf = tmp_path / "v2.pdf"
    pdf.write_bytes(b"%PDF-1.4\nv2")
    r_v1 = {"model": "old"}

    # 假设之前用旧版本解析过
    paddleocr_cache.save_cached(str(pdf), r_v1, model_version="PaddleOCR-VL-1.5")

    # 当前 env 是 PaddleOCR-VL-1.6（默认）→ 旧缓存视为未命中
    assert paddleocr_cache.get_cached(str(pdf)) is None



# ── 4. 文件内容变更失效 ──────────────────────────────────────────────────────


def test_pdf_content_change_invalidates_cache(cache_dir, tmp_path):
    """PDF 内容改了（hash 变），旧缓存条目视为未命中。"""
    pdf = tmp_path / "changes.pdf"
    pdf.write_bytes(b"%PDF-1.4\nversion-A")
    paddleocr_cache.save_cached(str(pdf), {"v": "A"})

    # 命中
    assert paddleocr_cache.get_cached(str(pdf)) == {"v": "A"}

    # 改文件
    pdf.write_bytes(b"%PDF-1.4\nversion-B-with-more-content")

    # 旧缓存失效 → 未命中
    assert paddleocr_cache.get_cached(str(pdf)) is None


# ── 5. 容错：JSON 损坏 / 缺字段 ──────────────────────────────────────────────


def test_corrupted_cache_file_treated_as_miss(cache_dir, tmp_path):
    """缓存 JSON 损坏（不是合法 JSON）→ get_cached 返回 None，不抛。"""
    pdf = tmp_path / "c.pdf"
    pdf.write_bytes(b"%PDF-1.4\nc")
    cache_dir.mkdir(parents=True, exist_ok=True)
    # 手工写一份合法文件名但内容损坏的条目
    bad = cache_dir / f"{paddleocr_cache._file_hash(str(pdf))}_{paddleocr_cache._MODEL_VERSION}.json"
    bad.write_text("{ this is not json", encoding="utf-8")

    assert paddleocr_cache.get_cached(str(pdf)) is None


# ── 6. 运维工具：clear_cache ──────────────────────────────────────────────────


def test_clear_cache_removes_all_entries(cache_dir, tmp_path):
    """clear_cache() 清空缓存目录。"""
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "a.json").write_text("{}")
    (cache_dir / "b.json").write_text("{}")

    paddleocr_cache.clear_cache()

    assert not cache_dir.exists() or not any(cache_dir.iterdir())
