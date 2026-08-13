"""SiliconFlow 在线 embedding/rerank 契约测试(issues/144 AC#5 + #143 T5)。

背景:由 ``research/t3-siliconflow-probe-results.md`` §6 + ``t4-spike-results.md``
§6 决定,**客户端不需要** ``normalize_l2``,**不需要**主动剥 query instruction
前缀。这三条契约测试用来在未来 SF 服务端行为变化时给出明确失败信号:

1. **归一化**:SF 在线 embedding L2 范数 ≈ 1.0(± 1e-3)。阈值来自 T3 §1.3
   实测 1.0 ± 1e-8(float32 round-trip 噪声)。
2. **维度**:SF 输出维度 = 1024。固定硬阈值。
3. **跨 provider query 一致性**:raw query 送 SF 与 raw query 送本机 bge-m3
   ``cos ≥ 0.999``(T4 §4 实测全 ≥ 0.9999)。

## 何时跑

- **CI**:默认不跑 —— 拉真实 SF API。跑时设 ``SILICONFLOW_API_KEY`` +
  ``PYTEST_RUN_SILICONFLOW_CONTRACT=1``。
- **手动**:
  ``PYTEST_RUN_SILICONFLOW_CONTRACT=1 uv run --env-file .env pytest tests/test_siliconflow_contract.py``
- **不可** 在 ``fake_models`` 全局 stub 下跑 —— 必须连真实 SF。

回退:本测试不存在时,主路径 contract 退化由人工 spike 验证(见 #142/#143)。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest

# 让顶层能从 tests/ 目录 import core 包(与既有契约测试一致)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


#: 跑本测试的 opt-in marker。
REQUIRES_SF = pytest.mark.skipif(
    "PYTEST_RUN_SILICONFLOW_CONTRACT" not in os.environ,
    reason=(
        "SiliconFlow 契约测试默认不在 CI 中跑（真实在线 API）。"
        "手动跑: PYTEST_RUN_SILICONFLOW_CONTRACT=1 uv run --env-file .env "
        "pytest tests/test_siliconflow_contract.py"
    ),
)


#: SF API key 缺失时直接 skip（与 conftest 网络守卫同模式）。
REQUIRES_SF_KEY = pytest.mark.skipif(
    not os.environ.get("SILICONFLOW_API_KEY"),
    reason=(
        "SILICONFLOW_API_KEY 未设置；契约测试需要真实 API key。"
        "手动跑:在 .env 配置 SILICONFLOW_API_KEY=sk-... 后设 "
        "PYTEST_RUN_SILICONFLOW_CONTRACT=1"
    ),
)


# ── 共享 fixture：单例缓存本机 bge-m3 模型(避免每次 parametrize 重复 load)───


@pytest.fixture(scope="module")
def local_bge_m3():
    """本机 bge-m3 单例(整个 contract test 模块只 load 一次)。

    避免每次参数化都触发 ~30s 模型加载 + GPU 显存分配 —— 4 条 parametrize
    不共享 fixture 时会导致 GPU OOM(GTX 1070 Ti 8G 多次瞬时分配)。
    """
    try:
        from llama_index.embeddings.huggingface import HuggingFaceEmbedding
        modelscope_path = Path.home() / ".cache/modelscope/hub/BAAI/bge-m3"
        path = str(modelscope_path) if modelscope_path.is_dir() else "BAAI/bge-m3"
        local = HuggingFaceEmbedding(
            model_name=path,
            normalize=True,
            device=os.getenv("EMBED_DEVICE") or None,
            max_length=512,
        )
        # 触发一次 warmup 避免首次 _get_query_embedding 计入测试耗时
        local._get_query_embedding("warmup")
        return local
    except Exception as e:
        pytest.skip(
            f"本机 bge-m3 不可用({type(e).__name__}: {e});无法做跨 provider 对照"
        )


# ── 契约测试 1:归一化 ───────────────────────────────────────────────────────


@REQUIRES_SF
@REQUIRES_SF_KEY
def test_sf_embedding_normalization_within_tolerance():
    """§6.1 决策 1:客户端不需要 normalize_l2(SF 已归一化)。

    实测 L2 范数应在 1.0 ± 1e-3(0.1% 偏差上限)内。
    阈值偏宽松:不误判常规浮点噪声,但严格捕捉"服务端不再 normalize"。
    """
    from core.siliconflow_client import encode_query_for_siliconflow

    # 5 条样本,中英、长短都覆盖
    texts = [
        "hello world",
        "你好世界",
        "SiliconFlow bge-m3 contract test",
        "招标投标法实施细则 施工组织设计 质量管理",
        "钢结构施工要求 焊缝等级 防腐处理",
    ]
    for t in texts:
        v = np.asarray(encode_query_for_siliconflow(t), dtype=np.float64)
        norm = float(np.linalg.norm(v))
        assert abs(norm - 1.0) <= 1e-3, (
            f"text={t!r}: ||v||={norm:.6e} 偏离 1.0 超过 1e-3;"
            f" SF 端 normalize 行为可能已变。"
        )


# ── 契约测试 2:维度 ─────────────────────────────────────────────────────────


@REQUIRES_SF
@REQUIRES_SF_KEY
def test_sf_embedding_dim_is_1024():
    """§6.1 决策 2:SF 输出维度 = 1024(与本机 bge-m3 / 存量索引一致)。"""
    from core.siliconflow_client import encode_query_for_siliconflow, EMBEDDING_DIM

    for t in ["hello", "你好", "127 chars ...", "中英混合字符串 123"]:
        v = encode_query_for_siliconflow(t)
        assert len(v) == EMBEDDING_DIM == 1024, (
            f"text={t!r}: dim={len(v)} (期望 1024);"
            f" SF 模型可能已切换或 EMBEDDING_DIM 常量失同步。"
        )


# ── 契约测试 3:跨 provider query 一致性 ─────────────────────────────────────
# raw query 送 SF ≈ raw query 送本机 bge-m3 的 cos 相似度 ≥ 0.999。
# 这条测试同时要求 SF key 和本机 bge-m3,本机端用
# ``HuggingFaceEmbedding(normalize=True, max_length=512)``(与存量一致)。
# 跨 provider 一致性等于"query 端可放心切 SF" —— 索引体系无需重跑。


@REQUIRES_SF
@REQUIRES_SF_KEY
@pytest.mark.parametrize("query_text", [
    "突发事件 应急预案 处置",
    "投标保证金 缴纳 退还 付款",
    "增值税税率 合规 投标报价",
    "钢结构施工要求 焊缝等级",
])
def test_query_local_vs_sf_cos_above_0_999(query_text: str, local_bge_m3):
    """§6.1 决策 3:跨 provider query 一致性 cos ≥ 0.999。"""
    from core.siliconflow_client import encode_query_for_siliconflow

    sf_vec = np.asarray(encode_query_for_siliconflow(query_text), dtype=np.float64)
    local_vec = np.asarray(
        local_bge_m3._get_query_embedding(query_text), dtype=np.float64
    )
    # 两端都已 L2 归一化(本机端 normalize=True;SF 端 §1.2 实测)
    cos = float(
        np.dot(sf_vec, local_vec)
        / (np.linalg.norm(sf_vec) * np.linalg.norm(local_vec))
    )
    assert cos >= 0.999, (
        f"query={query_text!r}: SF vs 本机 bge-m3 cos={cos:.6f} < 0.999;"
        f" query 端一致性破坏,需复跑 T4 spike 排查 SF 行为变化。"
    )


# ── 不变量:服务端 normalize / 前缀决策由这三条契约统一保护 ────────────────
# 任何一条失败都意味着 SF 服务端行为与 decisions(issues/138/#143)记录偏离,
# **回退方案**:暂时客户端调用 ``normalize_l2`` / 剥前缀,等待 SF 修复。
