"""共享测试夹具与配置。

AUDIT_DATA_DIR 必须在任何 storage 模块被 import 之前设置——
``storage/kb_repo.py``、``storage/audit_doc_repo.py`` 等在 import 时通过
``os.environ.get("AUDIT_DATA_DIR")`` 绑定 ``DATA_DIR`` 路径对象，之后再改
环境变量不会生效（见 review_report.md #5 的 import 顺序依赖问题）。

因此在 conftest 模块级（pytest 收集阶段最先执行）统一设置，取代各测试文件里
脆弱的模块级 ``os.environ`` 赋值。
"""

import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest


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
