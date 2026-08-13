"""共享测试夹具与配置。

数据目录双层隔离（issue #137）：
1. 模块级兜底：``AUDIT_DATA_DIR`` 在 conftest 模块级指向 session 临时目录。
   历史上一度要求"先于 storage 模块 import"（模块 import 时绑定 ``DATA_DIR``
   常量）；storage/core/api 已全部改为每次调用 ``get_data_dir()`` 解析 env，
   这里保留模块级赋值是为了防止尚未迁移的 services 层在 import 时读到生产
   ``./data``。
2. per-test 隔离：``_per_test_data_dir`` autouse fixture 把每条用例的
   ``AUDIT_DATA_DIR`` 指到 pytest ``tmp_path``，存储层按调用解析 →
   异步索引线程只能写自己用例的目录，teardown 竞态（Errno 39）结构上消失。
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
    """整个测试会话结束后清理临时数据目录。

    issue #137：per-test env 还原后，仍存活的 daemon 索引线程下次调用
    ``get_data_dir()`` 会解析回 session 目录，若它恰好在 rmtree 期间写文件，
    ``os.rmdir`` 抛 Errno 39，被 ``ignore_errors`` 吞掉后留下孤儿目录
    （/tmp/jishu_shenhe_test_*）。因此先 join 完所有 daemon 线程再删，
    仍不死心的线程由重试兜底。
    """
    import threading
    import time as _t

    main_thread = threading.current_thread()
    for t in threading.enumerate():
        if t is main_thread or not t.is_alive() or not t.daemon:
            continue
        try:
            t.join(timeout=60)
        except Exception:
            pass
    for _ in range(3):
        shutil.rmtree(_TEST_DATA_DIR, ignore_errors=True)
        if not _TEST_DATA_DIR.exists():
            break
        _t.sleep(1)


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


# ── PaddleOCR 网络守卫（issue #136）──────────────────────────────────────────


@pytest.fixture(autouse=True)
def _block_live_paddleocr_calls(monkeypatch):
    """autouse：测试期间禁止真实 PaddleOCR HTTP 调用（issue #136）。

    背景：fake/corrupt PDF 在 ``core.parse_document`` 里会落到 PaddleOCR 分支
    （文字层检测失败 → OCR 路由），一旦开发者 ``.env`` 配了 OCR 凭证，测试就会
    真的向第三方 OCR 服务发 HTTP（120s 提交超时 + 600s 轮询上限）——套件结果
    取决于 ``.env``，这就是 test_import_document_async 间歇失败的环境根因。

    本 fixture 把 ``_paddleocr_call``（提交 job / 轮询 / 取 JSONL 的 HTTP seam）
    替换为直接抛错的守卫：任何没 opt-in 的测试一碰到 OCR 调用就带着守卫名
    失败，而不是发起网络请求。

    可覆盖性：monkeypatch 撤销是 LIFO —— 测试体内自己的
    ``monkeypatch.setattr(pd_module, "_paddleocr_call", fake)``（test_parse_document
    等既有惯例）在测试执行期间覆盖本守卫。

    为什么不是清 env：``_PADDLEOCR_API_TOKEN`` / ``_PADDLEOCR_API_URL`` 在
    ``core.parse_document`` import 时已绑定为模块常量，fixture 里清环境变量
    不生效——必须替换调用 seam 本身。
    """
    import core.parse_document as _pd

    def _guard(*args, **kwargs):
        raise RuntimeError(
            "PaddleOCR 网络守卫（issue #136）：测试期间禁止真实 OCR HTTP 调用。"
            "需要 OCR 行为请在测试里 opt-in：monkeypatch "
            "core.parse_document._paddleocr_call / _paddleocr_parse"
            "（或 stub core.parse_document.parse_document）。"
        )

    monkeypatch.setattr(_pd, "_paddleocr_call", _guard)


# ── 知识库元数据播种：让直接调用 index_document / search 的测试也能跑 ──────


@pytest.fixture
def seed_searchable_kb():
    """创建 KB 元数据并标记 index_status='searchable'。

    ADR-0002 后，``core.index_manager.search()`` / ``get_kb_index_built()``
    直接读 ``kb.index_status``。生产路径经 doc_svc 自然维护这个状态，
    单元测试若绕过 doc_svc 直接调底层 ``index_document``，需要手工 seed。

    ``index_document`` 现在还会断言 ``vectors/index.meta.json``（issues/144
    AC#3）；本 fixture 同步写一份 BAAI/bge-m3 + dim=1024 的 meta 让旧测试
    不需要再为每个 KB 显式 seed meta。生产索引由 ``scripts/backfill_kb_meta.py``
    一次性回填。
    """
    from core.index_manager import _write_index_meta

    seeded: list[str] = []

    def _seed(kb_id: str):
        kb = KnowledgeBase(id=kb_id, name="seed", category="national")
        kb_repo.update(kb)
        kb = kb_repo.get(kb_id)
        kb.index_status = "searchable"
        kb.document_ids = []
        kb_repo.update(kb)
        seeded.append(kb_id)
        # issues/144 写入前的硬关（issues/144 AC#3）—— 测试 fixture 同步
        # 提供生产体系元数据（生产路径由 doc_svc 落，或由 backfill 回填）。
        _write_index_meta(
            kb_id, model_id="BAAI/bge-m3", dim=1024, force=True,
        )
        return kb_id

    yield _seed


@pytest.fixture(autouse=True)
def _per_test_data_dir(tmp_path, monkeypatch):
    """每个测试独立的 AUDIT_DATA_DIR（issue #137）。

    之前整个套件共享一个 session 级目录 + 各测试文件自己的 rmtree cleanup，
    daemon 索引线程与 teardown 清理会撞（OSError Errno 39）。现在每条用例的
    目录由 pytest ``tmp_path`` 派生并 monkeypatch 到 env —— 存储层
    ``get_data_dir()`` 每次调用解析 env，异步线程只能写自己用例的目录，
    跨用例竞态从结构上消失；测试体里读 ``os.environ["AUDIT_DATA_DIR"]``
    看到的就是本用例的 ``tmp_path``。

    必须声明在 ``_wait_for_async_rebuild_threads`` **之前**：pytest 按声明顺序
    setup、逆序 teardown，这样 join 先于 env 还原 / 目录清理，防再引入
    "清理跑在线程写完之前"。
    """
    monkeypatch.setenv("AUDIT_DATA_DIR", str(tmp_path))
    yield tmp_path


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
