"""``core/pages_store`` 单元测试（PRD #29 / V3）。

覆盖验收：
- save + load round-trip 字段完整
- 不存在返回 None
- delete 后再 load 返回 None
- JSON 损坏时降级返回 None 并 log warning
"""
from __future__ import annotations

import json
import os
import shutil

import pytest

from core import pages_store


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    """每个测试在 tmp_path 下跑，不污染 AUDIT_DATA_DIR。"""
    monkeypatch.setattr(pages_store, "DATA_DIR", tmp_path)
    yield tmp_path


@pytest.fixture
def sample_pages() -> dict:
    """一个最小的 ParseResult-shape dict。"""
    return {
        "by_page": [
            {"page": 0, "text": "第一页内容"},
            {"page": 1, "text": "第二页内容"},
        ],
        "full_text": "第一页内容\n\n第二页内容",
        "layout": [
            {
                "page": 0,
                "width": 1191,
                "height": 1684,
                "blocks": [
                    {
                        "block_label": "text",
                        "block_content": "第一页内容",
                        "bbox_norm": [0.05, 0.05, 0.95, 0.95],
                        "polygon_norm": [[0.05, 0.05], [0.95, 0.05], [0.95, 0.95], [0.05, 0.95]],
                        "block_order": 0,
                    }
                ],
            },
            {"page": 1, "width": 1191, "height": 1684, "blocks": []},
        ],
    }


# ── 1. save + load 往返 ────────────────────────────────────────────────────────


def test_save_and_load_round_trip(sample_pages):
    """save + load 应当完整还原 by_page / full_text / layout。"""
    kb_id = "kb_rt"
    doc_id = "doc_rt"

    path = pages_store.save_pages(
        kb_id, doc_id, sample_pages,
        file_hash="deadbeef", model_version="PaddleOCR-VL-1.6",
    )
    assert path.exists(), "落盘路径应当存在"

    loaded = pages_store.load_pages(kb_id, doc_id)
    assert loaded is not None
    # 保留原字段
    assert loaded["by_page"] == sample_pages["by_page"]
    assert loaded["full_text"] == sample_pages["full_text"]
    assert loaded["layout"] == sample_pages["layout"]
    # 自动写入元字段
    assert loaded["doc_id"] == doc_id
    assert loaded["kb_id"] == kb_id
    assert loaded["file_hash"] == "deadbeef"
    assert loaded["model_version"] == "PaddleOCR-VL-1.6"
    assert "parsed_at" in loaded


def test_save_overwrites_existing_file(sample_pages):
    """再次 save 同 doc_id → 文件被覆盖而非堆积。"""
    kb_id, doc_id = "kb_ow", "doc_ow"
    pages_store.save_pages(kb_id, doc_id, sample_pages)

    new = {**sample_pages, "full_text": "覆盖后的全文"}
    pages_store.save_pages(kb_id, doc_id, new)

    loaded = pages_store.load_pages(kb_id, doc_id)
    assert loaded["full_text"] == "覆盖后的全文"


def test_save_creates_parent_dirs(tmp_path, sample_pages):
    """pages/ 子目录不存在时自动创建。"""
    kb_id = "kb_new"
    doc_id = "doc_new"
    assert not (tmp_path / "kbs" / kb_id / "pages").exists()

    pages_store.save_pages(kb_id, doc_id, sample_pages)

    assert (tmp_path / "kbs" / kb_id / "pages" / f"{doc_id}.json").exists()


# ── 2. 不存在 → None ───────────────────────────────────────────────────────────


def test_load_returns_none_when_file_missing():
    assert pages_store.load_pages("kb_nope", "doc_nope") is None


# ── 3. delete ──────────────────────────────────────────────────────────────────


def test_delete_removes_file(sample_pages):
    kb_id, doc_id = "kb_del", "doc_del"
    pages_store.save_pages(kb_id, doc_id, sample_pages)
    assert pages_store.load_pages(kb_id, doc_id) is not None

    assert pages_store.delete_pages(kb_id, doc_id) is True
    assert pages_store.load_pages(kb_id, doc_id) is None


def test_delete_returns_false_when_no_file():
    assert pages_store.delete_pages("kb_nope", "doc_nope") is False


# ── 4. JSON 损坏 → 降级 None + warning ────────────────────────────────────────


def test_corrupted_json_returns_none(tmp_path, monkeypatch, caplog):
    """损坏 JSON 不抛异常，返回 None，log warning。"""
    from core.logger import get_logger
    import logging

    kb_id, doc_id = "kb_corrupt", "doc_corrupt"
    path = pages_store._pages_file(kb_id, doc_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")

    caplog.set_level(logging.WARNING)
    result = pages_store.load_pages(kb_id, doc_id)
    assert result is None
    # 应记录一条 warning（不是 error）
    assert any("failed to load" in r.message for r in caplog.records)


def test_save_then_load_with_minimal_payload_round_trips():
    """边界：仅必填字段（by_page / full_text / layout）也能 round-trip。"""
    payload = {"by_page": [], "full_text": "", "layout": []}
    pages_store.save_pages("kb_min", "doc_min", payload)
    loaded = pages_store.load_pages("kb_min", "doc_min")
    assert loaded is not None
    assert loaded["by_page"] == []
    assert loaded["full_text"] == ""
    assert loaded["layout"] == []
