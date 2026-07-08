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


# ── 7. V8 cache defense (issue #57) ────────────────────────────────────────────


def test_save_cached_default_source_is_paddleocr(cache_dir, tmp_path):
    """save_cached 默认 source="paddleocr"，与历史行为兼容。"""
    pdf = tmp_path / "d.pdf"
    pdf.write_bytes(b"%PDF-1.4\nd")
    cache_dir.mkdir(parents=True, exist_ok=True)

    paddleocr_cache.save_cached(str(pdf), {"by_page": [], "full_text": "x", "layout": []})

    # 直接读 entry, 验证 source 字段
    entry_path = cache_dir / f"{paddleocr_cache._file_hash(str(pdf))}_{paddleocr_cache._MODEL_VERSION}.json"
    import json
    entry = json.loads(entry_path.read_text(encoding="utf-8"))
    assert entry["source"] == "paddleocr"


def test_save_cached_records_explicit_source(cache_dir, tmp_path):
    """save_cached 显式传 source="fallback_pdfplumber" 时落盘。"""
    pdf = tmp_path / "e.pdf"
    pdf.write_bytes(b"%PDF-1.4\ne")
    cache_dir.mkdir(parents=True, exist_ok=True)

    paddleocr_cache.save_cached(
        str(pdf), {"by_page": [], "full_text": "x", "layout": []},
        source="fallback_pdfplumber",
    )

    import json
    entry_path = cache_dir / f"{paddleocr_cache._file_hash(str(pdf))}_{paddleocr_cache._MODEL_VERSION}.json"
    entry = json.loads(entry_path.read_text(encoding="utf-8"))
    assert entry["source"] == "fallback_pdfplumber"


def test_get_cached_skips_fallback_pdfplumber_when_paddleocr_available(
    cache_dir, tmp_path, monkeypatch
):
    """V8 cache defense 核心: PaddleOCR 可用时,旧 fallback_pdfplumber 产物视为污染 → 返回 None 触发重解析。

    重现 issue #57 失败场景: 部署时未设 PADDLEOCR_API_TOKEN → 走 pdfplumber 落
    cache (source=fallback_pdfplumber, layout=[]) → 后来补上 token → 旧 cache
    仍命中, V8 _inject_block_range 拿不到 layout。
    """
    pdf = tmp_path / "f.pdf"
    pdf.write_bytes(b"%PDF-1.4\nf")
    cache_dir.mkdir(parents=True, exist_ok=True)

    # 模拟旧 cache: source=fallback_pdfplumber, layout=[]
    paddleocr_cache.save_cached(
        str(pdf),
        {"by_page": [{"page": 0, "text": "old"}], "full_text": "old", "layout": []},
        source="fallback_pdfplumber",
    )

    # 模拟 PaddleOCR 凭证已就位
    monkeypatch.setenv("PADDLEOCR_API_TOKEN", "fake-token")
    monkeypatch.setenv("PADDLEOCR_API_URL", "https://fake.example.com")

    # 关键断言: 命中 cache 但被防御逻辑拦截 → 返回 None
    assert paddleocr_cache.get_cached(str(pdf)) is None, (
        "PaddleOCR 可用时, fallback_pdfplumber 旧 cache 必须被强制失效, "
        "否则 V8 _inject_block_range 拿不到 layout, block_range 永远 None"
    )


def test_get_cached_returns_fallback_pdfplumber_when_paddleocr_unavailable(
    cache_dir, tmp_path, monkeypatch
):
    """PaddleOCR 仍未配置时, fallback_pdfplumber cache 仍命中(不破坏离线场景)。"""
    pdf = tmp_path / "g.pdf"
    pdf.write_bytes(b"%PDF-1.4\ng")
    cache_dir.mkdir(parents=True, exist_ok=True)

    fallback_result = {
        "by_page": [{"page": 0, "text": "fb"}],
        "full_text": "fb",
        "layout": [],
    }
    paddleocr_cache.save_cached(
        str(pdf), fallback_result, source="fallback_pdfplumber",
    )

    # 模拟 PaddleOCR 仍不可用
    monkeypatch.delenv("PADDLEOCR_API_TOKEN", raising=False)
    monkeypatch.delenv("PADDLEOCR_API_URL", raising=False)

    # 应当返回 cache 结果(不强制重解析, 因为 PaddleOCR 不可用, 重解析只会再走 fallback)
    loaded = paddleocr_cache.get_cached(str(pdf))
    assert loaded == fallback_result
