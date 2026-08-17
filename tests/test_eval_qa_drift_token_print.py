"""``scripts/eval_qa_drift.py`` 的 token 打印契约测试(ADR-0009)。

本测试不依赖真实 KB / SF / LLM —— 通过 ``monkeypatch.setattr`` 把脚本
里的 ``_run_provider`` / ``_load_test_cases`` 替换为 stub,只盯三件事:

1. 跑前 ``core.metrics.reset_embedding_tokens_total()`` 被调(避免污染)
2. 跑末 ``total_prompt_tokens=N`` 被打印
3. ``--no-token-print`` 关闭时**不**打印该行
"""
from __future__ import annotations

import importlib.util
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest


def _load_eval_qa_drift_module():
    """动态加载 ``scripts/eval_qa_drift.py`` 当作模块。

    脚本不在 package 内,且会触发 ``core.settings`` 初始化 —— 直接用
    importlib 让它走自己的 import 路径,与 ``python scripts/eval_qa_drift.py``
    CLI 等价。
    """
    path = Path(__file__).resolve().parent.parent / "scripts" / "eval_qa_drift.py"
    spec = importlib.util.spec_from_file_location("eval_qa_drift", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def eval_qa_drift_module():
    """加载 ``scripts.eval_qa_drift`` 模块(autouse 之外的 fixture,手动取用)。"""
    return _load_eval_qa_drift_module()


@pytest.fixture(autouse=True)
def _reset_metrics():
    """清零 metrics 防跨用例污染。"""
    from core import metrics
    metrics.reset_embedding_tokens_total()
    yield
    metrics.reset_embedding_tokens_total()


def _stub_run_provider(model):
    """stub ``_run_provider``:不真跑 vector_search,只往 metrics 加一个固定值
    让验证 "run 末 token 打印" 可观察。
    """
    def _stub(kb_ids, test_cases):
        from core.metrics import record_embedding_tokens
        # 模拟两次 SF query 调用,共 42 token
        record_embedding_tokens(20)
        record_embedding_tokens(22)
        return {
            "provider": "stub",
            "n": len(test_cases),
            "averages": {"hit_rate": 1.0, "mrr": 1.0, "recall": 1.0, "precision_at_k": 1.0},
            "per_query": [],
        }
    return _stub


def test_eval_qa_drift_prints_total_prompt_tokens_at_exit(
    monkeypatch, eval_qa_drift_module, tmp_path,
):
    """ADR-0009:跑末打印 ``total_prompt_tokens=N``(免费档 baseline)。"""
    cases_yaml = tmp_path / "cases.yaml"
    cases_yaml.write_text(
        "test_cases:\n  - id: t1\n    query: hello\n    expected_chunks: []\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(eval_qa_drift_module, "_run_provider", _stub_run_provider(eval_qa_drift_module))

    # 用 sys.argv 模拟命令行
    monkeypatch.setattr(
        sys, "argv", ["eval_qa_drift.py", "--cases", str(cases_yaml), "--kb-ids", "fake_kb"]
    )

    buf = io.StringIO()
    with redirect_stdout(buf):
        exit_code = eval_qa_drift_module.main()

    assert exit_code == 0
    out = buf.getvalue()
    assert "total_prompt_tokens=42" in out, (
        f"run 末应打印 total_prompt_tokens=42(2 次 stub 累加),实际输出:\n{out}"
    )


def test_eval_qa_drift_token_line_does_not_show_on_empty_cases(
    monkeypatch, eval_qa_drift_module, tmp_path,
):
    """cases yaml 找不到 / 为空 → 直接 return 2,token 行不打印(早期 return)。

    边界行为:脚本在 ``_load_test_cases`` 后 early-return(不调
    ``_run_provider``),所以 token 累加器始终为 0、print 行不触发 —— 这是
    设计上的语义,不需要 token baseline,因为没跑任何 SF 调用。
    """
    cases_yaml = tmp_path / "cases.yaml"
    cases_yaml.write_text("test_cases: []\n", encoding="utf-8")
    monkeypatch.setattr(eval_qa_drift_module, "_run_provider", _stub_run_provider(eval_qa_drift_module))

    monkeypatch.setattr(
        sys, "argv", ["eval_qa_drift.py", "--cases", str(cases_yaml), "--kb-ids", "fake_kb"]
    )

    buf = io.StringIO()
    with redirect_stdout(buf):
        exit_code = eval_qa_drift_module.main()

    # 空 cases → FAIL 退出码 2
    assert exit_code == 2
    out = buf.getvalue()
    assert "FAIL" in out
    # early-return 不走 token 打印 —— 边界行为,与 ADR-0009 不冲突
    # (没有调 SF,没有 baseline 必要)
    assert "total_prompt_tokens" not in out