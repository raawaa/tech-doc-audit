"""共享测试夹具与配置。

AUDIT_DATA_DIR 必须在任何 storage 模块被 import 之前设置——
``storage/kb_repo.py``、``storage/audit_doc_repo.py`` 等在 import 时通过
``os.environ.get("AUDIT_DATA_DIR")`` 绑定 ``DATA_DIR`` 路径对象，之后再改
环境变量不会生效（见 review_report.md #5 的 import 顺序依赖问题）。

因此在 conftest 模块级（pytest 收集阶段最先执行）统一设置，取代各测试文件里
脆弱的模块级 ``os.environ`` 赋值。
"""

import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest
from llama_index.core import Settings as _LISettings
from llama_index.core.embeddings import BaseEmbedding


@pytest.fixture(autouse=True)
def _clear_degradation_log():
    """每个测试前清空线程级降级日志，防止交叉污染。"""
    try:
        from core.degradation import drain
        drain()
    except Exception:
        pass


# ── 测试数据目录（模块级，确保早于 storage 模块 import）──────────────────────────
_TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="jishu_shenhe_test_"))
os.environ["AUDIT_DATA_DIR"] = str(_TEST_DATA_DIR)


def pytest_sessionfinish(session, exitstatus):
    """整个测试会话结束后清理临时数据目录。"""
    shutil.rmtree(_TEST_DATA_DIR, ignore_errors=True)


# ── 共享 mock_llm 夹具 ─────────────────────────────────────────────────────────


@pytest.fixture
def mock_llm():
    """返回一个预配置的 MagicMock LLM，供需要 mock ``get_llm()`` 的测试使用。

    默认配置 ``as_structured_llm`` 路径返回 ``raw=None``；具体测试可通过
    ``monkeypatch`` 覆盖 ``.as_structured_llm.return_value.chat.return_value.raw``
    等属性来定制返回值。绝不触发真实模型加载。
    """
    llm = MagicMock()
    structured = MagicMock()
    structured.chat.return_value.raw = None
    llm.as_structured_llm.return_value = structured
    llm.chat.return_value.message.content = ""
    return llm


# ── fake_models：注入假 LLM/embedder，取代 core.settings 单例 ────────────────────


class _FakeEmbedder(BaseEmbedding):
    """确定性 embedder：md5(text) → seed RNG → dim 维单位向量。仅供测试。

    向量无语义意义，但维度/类型/批量接口与真 bge-m3 兼容，足以驱动 LlamaIndex
    FAISS 建索引 + 查询，让测试无需加载 ~2GB bge-m3。
    """

    dim: int = 1024

    def _vec(self, text: str) -> list[float]:
        h = hashlib.md5((text or "").encode()).digest()
        rng = np.random.default_rng(np.frombuffer(h * 4, dtype=np.uint32))
        v = rng.standard_normal(self.dim).astype(np.float32)
        n = np.linalg.norm(v)
        return (v / n).tolist()

    def _get_query_embedding(self, query: str) -> list[float]:
        return self._vec(query)

    def _get_text_embedding(self, text: str) -> list[float]:
        return self._vec(text)

    def _get_text_embeddings(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    async def _aget_query_embedding(self, query: str) -> list[float]:
        return self._vec(query)

    async def _aget_text_embedding(self, text: str) -> list[float]:
        return self._vec(text)


@pytest.fixture
def fake_models(monkeypatch):
    """opt-in：注入假 LLM/embedder，让测试零模型加载（不载 bge-m3、不连 LLM API）。

    覆盖 core.settings 单例的**双重真值源**：
    - ``get_embed_model``/``get_llm`` → 返回假模型。patch 各顶层 import 处
      （core.settings、core.index_manager、services.qa_service）+ 源模块，
      因为 ``from core.settings import get_embed_model`` 会在 import 处绑定名字。
    - ``Settings.embed_model``/``Settings.llm`` → 同步设为假模型
      （``_create_index`` 等走 LlamaIndex 全局 Settings 的路径）。
    - ``run_reranker`` → 原样返回 nodes（不载真 cross-encoder）。

    Returns ``{"embed_model", "llm"}``；teardown 还原 Settings。
    """
    import importlib

    embed = _FakeEmbedder(dim=1024, model_name="fake-deterministic")
    llm = MagicMock(name="fake_llm")

    # patch 所有顶层 import 了 getter 的模块 + 源模块
    for mod_name in ("core.settings", "core.index_manager", "services.qa_service"):
        try:
            mod = importlib.import_module(mod_name)
        except Exception:
            continue
        if hasattr(mod, "get_embed_model"):
            monkeypatch.setattr(mod, "get_embed_model", lambda: embed)
        if hasattr(mod, "get_llm"):
            monkeypatch.setattr(mod, "get_llm", lambda: llm)
        if hasattr(mod, "run_reranker"):
            monkeypatch.setattr(mod, "run_reranker", lambda nodes, query, config=None: nodes)

    def _peek(attr):
        # Settings.embed_model 是惰性 property：未设置时读取会触发 resolve
        # → 回落 OpenAI → 报错。用 try/except 安全捕获旧值，不触发 resolve。
        try:
            return getattr(_LISettings, attr)
        except Exception:
            return None

    prev_embed, prev_llm = _peek("embed_model"), _peek("llm")
    _LISettings.embed_model = embed
    # Settings.llm 必须是 LLM 实例（LlamaIndex 类型校验）；None → MockLLM。
    # get_llm() 另返 MagicMock（供直接调用 llm.chat 的模块，如 agentic_audit）。
    _LISettings.llm = None
    try:
        yield {"embed_model": embed, "llm": llm}
    finally:
        _LISettings.embed_model = prev_embed
        _LISettings.llm = prev_llm


# ── 知识库元数据播种：让直接调用 index_document / search 的测试也能跑 ──────


@pytest.fixture
def seed_searchable_kb():
    """创建 KB 元数据并标记 index_status='searchable'。

    ADR-0002 后，``core.index_manager.search()`` / ``get_kb_index_built()``
    直接读 ``kb.index_status``。生产路径经 doc_svc 自然维护这个状态，
    单元测试若绕过 doc_svc 直接调底层 ``index_document``，需要手工 seed。
    """
    seeded: list[str] = []

    def _seed(kb_id: str):
        kb = KnowledgeBase(id=kb_id, name="seed", category="national")
        kb_repo.update(kb)
        kb = kb_repo.get(kb_id)
        kb.index_status = "searchable"
        kb.document_ids = []
        kb_repo.update(kb)
        seeded.append(kb_id)
        return kb_id

    yield _seed


@pytest.fixture(autouse=True)
def _wait_for_async_rebuild_threads():
    """测试结束后等 _ensure_kb_index 启动的后台 rebuild 线程全部完成。

    原因：_ensure_kb_index 慢路异步分支以 daemon 线程触发 rebuild；
    pytest 测试 body 结束后 cleanup 立刻跑 rmtree，若线程还在写
    kb.json 就会撞见 JSONDecodeError / 文件被删导致异常。
    """
    yield
    # 把 core.index_manager 中落盘过的后台线程 join 完
    import threading
    main_thread = threading.current_thread()
    for t in threading.enumerate():
        if t is main_thread or not t.is_alive() or not t.daemon:
            continue
        # daemon 线程通常是 QA 异步降级触发的 rebuild
        try:
            t.join(timeout=5)
        except Exception:
            pass


# 在 conftest 模块级导入，避免顶级 import 顺序问题
from models.knowledge_base import KnowledgeBase
import storage.kb_repo as kb_repo
