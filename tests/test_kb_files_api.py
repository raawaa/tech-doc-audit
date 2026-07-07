"""``api.routers.kb_files`` 单测（PRD #29 / V7 / Issue #42 V7.1）。

不依赖真实 LLM / OCR；只测 ``GET /api/v1/kb-documents/{doc_id}/layout`` 的契约：
- 404 路径（doc 不存在 / pages 文件缺失 / layout 全空 / 每页 blocks 全空）
- happy path：pages 文件含 OCR layout → 返回 ``{layout: [...], has_layout: true}``
- 清理：空 blocks 页从响应里剔除
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.main import app
from core import pages_store

client = TestClient(app)


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    """每个测试在 tmp_path 下跑，不污染 ``AUDIT_DATA_DIR``。"""
    monkeypatch.setattr(pages_store, "DATA_DIR", tmp_path)
    yield tmp_path


def _create_doc(tmp_path):
    """造一条 KB 文档元数据，返回 (doc_id, kb_id)。"""
    from storage import kb_repo, doc_repo

    kb = kb_repo.create(
        kb_repo.KnowledgeBase(
            id="kb_lay", name="layout 测试库", category="national",
            description="", created_at="2026-07-06T00:00:00Z",
            updated_at="2026-07-06T00:00:00Z",
        )
    )
    doc = doc_repo.save_doc(
        kb_id=kb.id, original_name="x.pdf",
        content=b"%PDF-1.4 dummy", file_type="pdf",
    )
    return doc.id, kb.id


# ── 404 路径 ───────────────────────────────────────────────────────────────


def test_layout_404_when_document_does_not_exist():
    r = client.get("/api/v1/kb-documents/nonexistent_doc/layout")
    assert r.status_code == 404
    assert "不存在" in r.json()["detail"]


def test_layout_404_when_pages_file_missing(tmp_path):
    """doc 存在但 pages 文件不存在 → 404（区分"未解析"）。"""
    doc_id, _kb_id = _create_doc(tmp_path)
    r = client.get(f"/api/v1/kb-documents/{doc_id}/layout")
    assert r.status_code == 404
    assert "解析" in r.json()["detail"]


def test_layout_404_when_pages_have_empty_layout(tmp_path):
    """doc 已 parse 但 layout=[]（pdfplumber fallback 产物） → 404。"""
    doc_id, kb_id = _create_doc(tmp_path)
    pages_store.save_pages(
        kb_id=kb_id, doc_id=doc_id,
        parse_result={
            "by_page": [{"page": 0, "text": "fallback page"}],
            "full_text": "fallback page",
            "layout": [],
        },
        file_hash="h", model_version="m", parsed_at="2026-07-06T00:00:00Z",
    )
    r = client.get(f"/api/v1/kb-documents/{doc_id}/layout")
    assert r.status_code == 404
    assert "layout" in r.json()["detail"].lower()


def test_layout_404_when_all_pages_have_no_blocks(tmp_path):
    """doc 有 layout 但每页 blocks 都空 → 404。"""
    doc_id, kb_id = _create_doc(tmp_path)
    pages_store.save_pages(
        kb_id=kb_id, doc_id=doc_id,
        parse_result={
            "by_page": [{"page": 0, "text": "x"}],
            "full_text": "x",
            "layout": [
                {"page": 0, "width": 1000, "height": 2000, "blocks": []},
                {"page": 1, "width": 1000, "height": 2000, "blocks": []},
            ],
        },
        file_hash="h", model_version="m", parsed_at="2026-07-06T00:00:00Z",
    )
    r = client.get(f"/api/v1/kb-documents/{doc_id}/layout")
    assert r.status_code == 404


# ── Happy path ─────────────────────────────────────────────────────────────


def test_layout_returns_pages_with_blocks_happy_path(tmp_path):
    """doc 有真实 OCR layout（至少一页含 blocks）→ 返回清理后的 layout 数组。"""
    doc_id, kb_id = _create_doc(tmp_path)
    pages_store.save_pages(
        kb_id=kb_id, doc_id=doc_id,
        parse_result={
            "by_page": [{"page": 0, "text": "p0"}, {"page": 1, "text": "p1"}],
            "full_text": "p0\n\np1",
            "layout": [
                {
                    "page": 0,
                    "width": 1000,
                    "height": 2000,
                    "blocks": [
                        {
                            "block_label": "text",
                            "block_content": "第一段",
                            "bbox_norm": [0.05, 0.05, 0.95, 0.1],
                            "polygon_norm": [[0.05, 0.05], [0.95, 0.05], [0.95, 0.1], [0.05, 0.1]],
                            "block_order": 0,
                        },
                    ],
                },
                {
                    "page": 1,
                    "width": 1000,
                    "height": 2000,
                    "blocks": [
                        {
                            "block_label": "text",
                            "block_content": "第二段",
                            "bbox_norm": [0.1, 0.2, 0.9, 0.3],
                            "polygon_norm": [],
                            "block_order": 0,
                        },
                    ],
                },
            ],
        },
        file_hash="h", model_version="m", parsed_at="2026-07-06T00:00:00Z",
    )

    r = client.get(f"/api/v1/kb-documents/{doc_id}/layout")
    assert r.status_code == 200
    body = r.json()
    assert body["has_layout"] is True
    assert len(body["layout"]) == 2

    p0 = body["layout"][0]
    assert p0["page"] == 0
    assert p0["width"] == 1000
    assert p0["height"] == 2000
    assert len(p0["blocks"]) == 1
    b = p0["blocks"][0]
    assert b["block_content"] == "第一段"
    assert b["bbox_norm"] == [0.05, 0.05, 0.95, 0.1]


def test_layout_drops_pages_with_no_blocks(tmp_path):
    """只保留有 blocks 的页（契约：has_layout=True 即至少有 1 个 block）。"""
    doc_id, kb_id = _create_doc(tmp_path)
    pages_store.save_pages(
        kb_id=kb_id, doc_id=doc_id,
        parse_result={
            "by_page": [{"page": 0, "text": "p0"}, {"page": 1, "text": "p1"}],
            "full_text": "p0\n\np1",
            "layout": [
                {"page": 0, "width": 100, "height": 100, "blocks": []},  # 空 → 丢
                {
                    "page": 1,
                    "width": 100,
                    "height": 100,
                    "blocks": [
                        {"block_label": "text", "block_content": "x",
                         "bbox_norm": [0, 0, 1, 1], "polygon_norm": [],
                         "block_order": 0},
                    ],
                },
            ],
        },
        file_hash="h", model_version="m", parsed_at="2026-07-06T00:00:00Z",
    )

    r = client.get(f"/api/v1/kb-documents/{doc_id}/layout")
    assert r.status_code == 200
    body = r.json()
    assert len(body["layout"]) == 1
    assert body["layout"][0]["page"] == 1


def test_layout_strips_unexpected_block_fields(tmp_path):
    """block 字段应当白名单输出，pages 文件里多余的字段不能透传给前端。"""
    doc_id, kb_id = _create_doc(tmp_path)
    pages_store.save_pages(
        kb_id=kb_id, doc_id=doc_id,
        parse_result={
            "by_page": [{"page": 0, "text": "p0"}],
            "full_text": "p0",
            "layout": [{
                "page": 0, "width": 100, "height": 100,
                "blocks": [{
                    "block_label": "text",
                    "block_content": "x",
                    "bbox_norm": [0, 0, 1, 1],
                    "polygon_norm": [],
                    "block_order": 0,
                    "extra_field": "should not leak",
                    "scores": [0.9, 0.1],
                }],
            }],
        },
        file_hash="h", model_version="m", parsed_at="2026-07-06T00:00:00Z",
    )
    r = client.get(f"/api/v1/kb-documents/{doc_id}/layout")
    assert r.status_code == 200
    b = r.json()["layout"][0]["blocks"][0]
    assert set(b.keys()) == {
        "block_label", "block_content", "bbox_norm",
        "polygon_norm", "block_order",
    }
    assert "extra_field" not in b
    assert "scores" not in b
