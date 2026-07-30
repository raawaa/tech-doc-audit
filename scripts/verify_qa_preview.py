"""验证脚本：虹桥公司制度 KB 在 QA 场景下可用（修复 preview "未解析"）。

Wayfinder #86 / ticket #91 验收环节。运行 a + c 两步组合校验。

设计：
- HTTP 方式打 API（最贴近真实用户流；不绕过 SSE 序列化）。
- API server 必须先启动（``scripts/start.sh`` 启动后默认 ``http://localhost:8000``）。
- 抽样 doc_id：8 篇基线（#90 决议锁定）+ 从 KB 元数据里随机补 2-7 篇，使总数 5-10 篇。
- QA 流式端点：解析 SSE，收集 ``source-document`` events，提取
  ``providerMetadata.qaSource.doc_id`` 比对 8 篇基线；同时校验每篇被引用 doc 的
  ``/layout`` 端点可用。

用法：
  # 跑全量两步（需要 API server 已在跑）
  uv run python scripts/verify_qa_preview.py

  # 只跑 step c（layout 抽样，跳过 LLM 调用）
  uv run python scripts/verify_qa_preview.py --only c

  # 自定义 API 地址
  uv run python scripts/verify_qa_preview.py --api-url http://localhost:8000

输出：
  /tmp/verify_qa_preview/c_step.md — 抽样 layout 字段统计
  /tmp/verify_qa_preview/a_step.md — QA 引用 doc_id 列表 vs 8 篇基线比对
  退出码：0 = 通过；1 = 不通过；2 = 跳过（基线未对齐 / API 不可达 / LLM 不可用）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

# 让脚本可以 import 项目模块（拿 doc_repo / pages_store 用于随机抽样）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("AUDIT_DATA_DIR", "data")

import storage.doc_repo as doc_repo  # noqa: E402


# ═══════════════════════════════════════════════════════════════════════════════
# Wayfinder #86 锁定的验收输入
# ═══════════════════════════════════════════════════════════════════════════════

KB_ID = "01KW1PG49FQDAEYV0W1H2H309E"
KB_NAME = "虹桥公司制度"

# 8 篇基线 doc_id（#90 决议确认 reparse 后全部 embedded；本次验证观察 QA 召回集是否还在引用它们）
BASELINE_DOC_IDS: list[str] = [
    "01KW1Q4CPHT1YWXT5YJWA0JF8S",  # ZD-BG-13-2025-B2_公司总经理办公会议事决策规则
    "01KW1QFG84K43W4GGW7QWMTN00",  # ZD-GJ-03-2024-B1_公司固定资产投资项目管理办法
    "01KW1QFWNN079RPK7CZ0B10YFN",  # ZD-GJ-04-2024-B1_公司维修维护项目管理办法
    "01KW1QHG8P5NCAAXJACG4JTVDH",  # ZD-GJ-13-2025-B0_公司工程建设管理办法（2025）
    "01KW1QW2W4WC29BE8CHRQTWWA3",  # ZD-XX-09-2024-B0_公司新基建项目管理细则
    "01KW1QWA9J7QFAAVDGT038VVJ5",  # ZD-XX-10-2024-B1_公司科技项目管理办法
    "01KW1QYMMAQG851X4T92Z1DGHD",  # 已被 8 篇引用的对照 doc（pre-run 就 embedded）
    "01KW1R24T7Z4SEKPDR2DKVRBN4",  # 沪机场虹委[2025]33号_公司"三重一大"决策实施办法
]

# 原 QA 提问（从 #86 目的地文案直接取）
ORIGINAL_QUESTION = "我有一个100万的项目要列入明年计划，我应该做什么"

# 抽样规模范围（基线 8 + 随机补 2-7 → 10-15；用 5-10 是 ticket 文字，实际我们多抽一些）
EXTRA_SAMPLE_MIN = 2
EXTRA_SAMPLE_MAX = 7

# 输出目录
OUT_DIR = Path("/tmp/verify_qa_preview")

# HTTP 超时
LAYOUT_TIMEOUT_S = 30.0
QA_STREAM_TIMEOUT_S = 600.0  # LLM 流式可能慢；deepseek 通常 < 60s


# ═══════════════════════════════════════════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class LayoutProbe:
    doc_id: str
    original_name: str
    http_status: int  # -1 表示网络异常
    error: str  # 网络/解析错误信息
    has_layout: bool
    page_count: int  # 解析后非空 page 数
    total_blocks: int
    sample_block_labels: list[str]  # 前 5 个 block 的 label

    @property
    def passed(self) -> bool:
        return self.http_status == 200 and self.has_layout and self.total_blocks > 0


@dataclass
class QACitation:
    source_id: str  # src_<short>_p<page>
    doc_id: str  # 来自 providerMetadata.qaSource.doc_id（实 doc_id）
    doc_source: str
    page_number: int | None
    relevance: float
    layout_ok: bool  # 该 doc 自己的 /layout 端点是否 200 + 非空


# ═══════════════════════════════════════════════════════════════════════════════
# Step c — layout 抽样
# ═══════════════════════════════════════════════════════════════════════════════

def _pick_extra_sample(kb_id: str, baseline: list[str], n: int) -> list[str]:
    """从 KB doc 中随机抽 n 篇（非基线）。少于 n 则取全部。"""
    all_docs = doc_repo.list_docs(kb_id)
    candidates = [d.id for d in all_docs if d.id not in baseline]
    random.seed(0x5EA1F00D)  # 固定种子，结果可复现
    if n > len(candidates):
        n = len(candidates)
    return random.sample(candidates, n)


def _doc_original_name(kb_id: str, doc_id: str) -> str:
    d = doc_repo.get_doc(kb_id, doc_id)
    if d is None:
        return "<unknown>"
    return d.original_name or d.name or "<unnamed>"


def probe_layout(
    client: httpx.Client,
    base_url: str,
    kb_id: str,
    doc_id: str,
) -> LayoutProbe:
    """打 ``GET /api/v1/kb-documents/{doc_id}/layout``，解析响应。"""
    name = _doc_original_name(kb_id, doc_id)
    url = f"{base_url.rstrip('/')}/api/v1/kb-documents/{doc_id}/layout"
    try:
        r = client.get(url, timeout=LAYOUT_TIMEOUT_S)
    except httpx.HTTPError as e:
        return LayoutProbe(
            doc_id=doc_id, original_name=name,
            http_status=-1, error=f"{type(e).__name__}: {e}",
            has_layout=False, page_count=0, total_blocks=0,
            sample_block_labels=[],
        )

    if r.status_code != 200:
        return LayoutProbe(
            doc_id=doc_id, original_name=name,
            http_status=r.status_code, error=r.text[:200],
            has_layout=False, page_count=0, total_blocks=0,
            sample_block_labels=[],
        )

    try:
        body = r.json()
    except json.JSONDecodeError as e:
        return LayoutProbe(
            doc_id=doc_id, original_name=name,
            http_status=r.status_code, error=f"JSON decode: {e}",
            has_layout=False, page_count=0, total_blocks=0,
            sample_block_labels=[],
        )

    has_layout = bool(body.get("has_layout"))
    layout = body.get("layout") or []
    page_count = 0
    total_blocks = 0
    labels: list[str] = []
    for page in layout:
        blocks = page.get("blocks") or []
        if not blocks:
            continue
        page_count += 1
        total_blocks += len(blocks)
        for b in blocks[:3]:
            lab = b.get("block_label") or ""
            if lab and len(labels) < 5:
                labels.append(lab)

    return LayoutProbe(
        doc_id=doc_id, original_name=name,
        http_status=r.status_code, error="",
        has_layout=has_layout, page_count=page_count,
        total_blocks=total_blocks, sample_block_labels=labels,
    )


def run_step_c(base_url: str, sample_size: int) -> tuple[list[LayoutProbe], Path]:
    """执行 step c。返回 (probes 列表, 输出 md 路径)。"""
    actual_extra = max(0, sample_size - len(BASELINE_DOC_IDS))
    extras = _pick_extra_sample(KB_ID, BASELINE_DOC_IDS, actual_extra)

    sample_ids = BASELINE_DOC_IDS + extras
    probes: list[LayoutProbe] = []
    with httpx.Client() as client:
        for did in sample_ids:
            p = probe_layout(client, base_url, KB_ID, did)
            probes.append(p)

    # 写 c_step.md
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    md_path = OUT_DIR / "c_step.md"
    n_pass = sum(1 for p in probes if p.passed)
    with md_path.open("w", encoding="utf-8") as f:
        f.write(f"# Step c — 抽样 layout 验证\n\n")
        f.write(f"- KB: `{KB_NAME}` (`{KB_ID}`)\n")
        f.write(f"- API: `{base_url}`\n")
        f.write(f"- 抽样数: {len(probes)}（基线 8 + 随机补 {len(extras)}）\n")
        f.write(f"- 通过: **{n_pass}/{len(probes)}**\n\n")
        f.write("| 类别 | doc_id | original_name | HTTP | has_layout | pages | blocks | 备注 |\n")
        f.write("|---|---|---|---:|---|---:|---:|---|\n")
        for p in probes:
            tag = "**基线**" if p.doc_id in BASELINE_DOC_IDS else "随机"
            note = p.error if p.error else ("✓" if p.passed else "✗ 空 layout")
            f.write(
                f"| {tag} | `{p.doc_id}` | {p.original_name} | "
                f"{p.http_status} | {p.has_layout} | {p.page_count} | "
                f"{p.total_blocks} | {note} |\n"
            )
        if n_pass == len(probes):
            f.write(f"\n**c step 通过** — 全部 {len(probes)} 篇 doc 都有完整 layout。\n")
        else:
            fail = [p for p in probes if not p.passed]
            f.write(f"\n**c step 失败** — {len(fail)} 篇缺 layout：\n")
            for p in fail:
                f.write(f"- `{p.doc_id}` ({p.original_name}): {p.error or '空 layout'}\n")
        if probes and probes[0].sample_block_labels:
            f.write(f"\n样例 block label：{', '.join(probes[0].sample_block_labels[:5])}\n")

    return probes, md_path


# ═══════════════════════════════════════════════════════════════════════════════
# Step a — QA 复测
# ═══════════════════════════════════════════════════════════════════════════════

_SSE_RE = re.compile(r"^event:\s*(?P<event>\S+)\s*\ndata:\s*(?P<data>.*)$", re.MULTILINE)


def _parse_sse_stream(text: str) -> list[tuple[str, dict]]:
    """极简 SSE 解析：返回 [(event, data_dict), ...]。"""
    out: list[tuple[str, dict]] = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        event_name = None
        data_str = None
        for line in block.split("\n"):
            if line.startswith("event:"):
                event_name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_str = line[len("data:"):].strip()
        if event_name and data_str:
            try:
                out.append((event_name, json.loads(data_str)))
            except json.JSONDecodeError:
                pass
    return out


def _short_doc_id_hash(doc_id: str) -> str:
    """复刻 api/routers/qa.py:_short_doc_id 的 md5[:8] 规则。"""
    if not doc_id:
        return "empty"
    return hashlib.md5(doc_id.encode("utf-8")).hexdigest()[:8]


def run_qa_stream(base_url: str) -> tuple[list[QACitation], dict[str, Any]]:
    """POST /api/v1/qa/chat/stream，解析 SSE，收集 source-document parts。

    返回 (citations, raw_stats) — raw_stats 包含 answer / total event 数等。
    """
    url = f"{base_url.rstrip('/')}/api/v1/qa/chat/stream"
    payload = {"question": ORIGINAL_QUESTION, "kb_ids": [KB_ID], "top_k": 5}

    citations: list[QACitation] = []
    answer_text = ""
    event_count = 0
    seen_doc_ids: set[str] = set()
    t0 = time.time()

    # 用 stream() + 手动读 chunks，因为 SSE 解析要兼容不同 chunk 边界
    with httpx.Client(timeout=QA_STREAM_TIMEOUT_S) as client:
        with client.stream("POST", url, json=payload) as r:
            if r.status_code != 200:
                raise RuntimeError(f"QA stream returned HTTP {r.status_code}: {r.text[:200]}")
            buf = ""
            for chunk in r.iter_text():
                buf += chunk
                # 每 \n\n 切一次；保留尾部不完整块
                while "\n\n" in buf:
                    block, buf = buf.split("\n\n", 1)
                    block = block.strip()
                    if not block:
                        continue
                    event_name = None
                    data_str = None
                    for line in block.split("\n"):
                        if line.startswith("event:"):
                            event_name = line[len("event:"):].strip()
                        elif line.startswith("data:"):
                            data_str = line[len("data:"):].strip()
                    if not event_name or not data_str:
                        continue
                    event_count += 1
                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    if event_name == "text-delta":
                        answer_text += data.get("delta", "") or ""
                    elif event_name == "source-document":
                        sid = data.get("sourceId", "")
                        pm = (data.get("providerMetadata") or {}).get("qaSource") or {}
                        real_doc_id = pm.get("doc_id") or ""
                        if not real_doc_id or real_doc_id in seen_doc_ids:
                            continue
                        seen_doc_ids.add(real_doc_id)
                        citations.append(QACitation(
                            source_id=sid,
                            doc_id=real_doc_id,
                            doc_source=pm.get("doc_source", "") or data.get("title", ""),
                            page_number=pm.get("page_number"),
                            relevance=pm.get("relevance", 0.0) or 0.0,
                            layout_ok=False,  # 后置校验
                        ))

    wall = time.time() - t0
    stats = {
        "wall_s": round(wall, 1),
        "event_count": event_count,
        "answer_chars": len(answer_text),
        "answer_preview": answer_text[:300],
    }
    return citations, stats


def run_step_a(base_url: str, c_probes: list[LayoutProbe]) -> tuple[list[QACitation], dict, Path]:
    """执行 step a：QA 复测 + 把 QA 召回 doc 与 c_step 探测过的 doc 交叉（layout_ok）。"""
    citations, raw_stats = run_qa_stream(base_url)

    # 用 c_probes 里的 layout 探测结果（已查过 /layout 端点）来标 layout_ok
    layout_status = {p.doc_id: p.passed for p in c_probes}
    # 再对 QA 召回但 c 未抽到的 doc 补一次 layout 探测
    missing = [c.doc_id for c in citations if c.doc_id not in layout_status]
    if missing:
        with httpx.Client() as client:
            for did in missing:
                p = probe_layout(client, base_url, KB_ID, did)
                layout_status[did] = p.passed
                c_probes.append(p)  # 顺手补进 c_probes，报告里也能看到

    for c in citations:
        c.layout_ok = layout_status.get(c.doc_id, False)

    # 与 8 篇基线比对
    cited = {c.doc_id for c in citations}
    baseline_hit = [d for d in BASELINE_DOC_IDS if d in cited]
    baseline_miss = [d for d in BASELINE_DOC_IDS if d not in cited]
    extra_cited = [c for c in citations if c.doc_id not in BASELINE_DOC_IDS]
    broken_cited = [c for c in citations if not c.layout_ok]

    # 写 a_step.md
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    md_path = OUT_DIR / "a_step.md"
    with md_path.open("w", encoding="utf-8") as f:
        f.write("# Step a — QA 复测\n\n")
        f.write(f"- KB: `{KB_NAME}` (`{KB_ID}`)\n")
        f.write(f"- 问题: `{ORIGINAL_QUESTION}`\n")
        f.write(f"- API: `{base_url}`\n")
        f.write(f"- 流式 wall clock: **{raw_stats['wall_s']}s**, "
                f"SSE event 数: {raw_stats['event_count']}, "
                f"answer 字符: {raw_stats['answer_chars']}\n")
        f.write(f"- answer preview: {raw_stats['answer_preview']!r}...\n\n")

        f.write("## 引用 doc 总览\n\n")
        f.write("| sourceId | doc_id (real) | doc_source | page | relevance | layout_ok |\n")
        f.write("|---|---|---|---:|---:|---|\n")
        for c in citations:
            f.write(
                f"| `{c.source_id}` | `{c.doc_id}` | {c.doc_source} | "
                f"{c.page_number} | {c.relevance:.3f} | "
                f"{'✓' if c.layout_ok else '✗'} |\n"
            )

        f.write("\n## 与 8 篇基线比对\n\n")
        f.write(f"- 命中: **{len(baseline_hit)}/{len(BASELINE_DOC_IDS)}**\n")
        f.write(f"- 召回集中「非本次基线」 doc: {len(extra_cited)} 篇\n")
        f.write(f"- 召回 doc 中 layout 缺失: {len(broken_cited)} 篇\n\n")

        f.write("### 命中的基线\n\n")
        for did in baseline_hit:
            f.write(f"- ✓ `{did}`\n")
        if baseline_miss:
            f.write("\n### 缺失的基线（QA 召回不再引用）\n\n")
            for did in baseline_miss:
                short = _short_doc_id_hash(did)
                f.write(f"- ✗ `{did}` (sourceId 短码: `{short}`)\n")
        if extra_cited:
            f.write("\n### 召回集中的非基线 doc\n\n")
            for c in extra_cited:
                f.write(f"- `{c.doc_id}` ({c.doc_source}) "
                        f"layout={'✓' if c.layout_ok else '✗'}\n")
        if broken_cited:
            f.write("\n### 引用了但 layout 不完整的 doc（a 失败）\n\n")
            for c in broken_cited:
                f.write(f"- `{c.doc_id}` ({c.doc_source})\n")

        # 验收判定
        f.write("\n## 验收判定\n\n")
        if len(broken_cited) > 0:
            f.write("**a 失败** — QA 引用了 layout 不完整的 doc（可能仍出现「未解析」）。\n")
        elif len(baseline_miss) == 0:
            f.write("**a 通过** — 8 篇基线全部命中，且全部 layout 完整。**c+a 同时通过**。\n")
        else:
            f.write(
                "**a 部分通过** — 召回的 doc 全部 layout 完整（修复有效），"
                f"但 8 篇基线中 {len(baseline_miss)} 篇未被引用（QA 召回集变化，"
                "不在本次回归范围内）。建议人工 review 召回差异。\n"
            )

    return citations, raw_stats, md_path


# ═══════════════════════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════════════════════

def _api_reachable(base_url: str) -> bool:
    try:
        with httpx.Client(timeout=5.0) as c:
            r = c.get(f"{base_url.rstrip('/')}/api/v1/health")
            return r.status_code == 200
    except httpx.HTTPError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Wayfinder #86 / #91 验证脚本：a + c 组合验收",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--api-url", default="http://localhost:8000",
                        help="API 根地址（默认 http://localhost:8000）")
    parser.add_argument("--only", choices=["a", "c", "ac"], default="ac",
                        help="只跑某一步：a / c / ac（默认 ac）")
    parser.add_argument("--sample-size", type=int, default=10,
                        help="step c 抽样 doc 总数（基线 8 + 随机补，10-15）")
    args = parser.parse_args()

    base_url = args.api_url.rstrip("/")
    if not _api_reachable(base_url):
        print(f"✗ API 不可达: {base_url}（先跑 `scripts/start.sh`）", file=sys.stderr)
        return 2

    print(f"== Wayfinder #86 / #91 验证 ==")
    print(f"  KB: {KB_NAME} ({KB_ID})")
    print(f"  API: {base_url}")
    print(f"  步骤: {args.only}")
    print()

    overall_ok = True
    c_probes: list[LayoutProbe] = []

    if "c" in args.only:
        print(f"[c] 抽样 layout 验证（基线 8 + 随机补 {args.sample_size - 8}）...")
        c_probes, c_md = run_step_c(base_url, args.sample_size)
        n_pass = sum(1 for p in c_probes if p.passed)
        print(f"    → {n_pass}/{len(c_probes)} 通过；报告: {c_md}")
        print()
        if n_pass != len(c_probes):
            overall_ok = False

    if "a" in args.only:
        print(f"[a] QA 复测（流式 → 解析 source-document）...")
        try:
            citations, stats, a_md = run_step_a(base_url, c_probes)
        except Exception as e:
            print(f"    ✗ a 步骤异常: {type(e).__name__}: {e}", file=sys.stderr)
            return 2
        n_cited = len(citations)
        n_layout_ok = sum(1 for c in citations if c.layout_ok)
        n_baseline_hit = sum(1 for c in citations if c.doc_id in BASELINE_DOC_IDS)
        print(f"    → wall {stats['wall_s']}s, 召回 {n_cited} 篇, "
              f"layout 完整 {n_layout_ok}/{n_cited}, 基线命中 {n_baseline_hit}/8")
        print(f"    → 报告: {a_md}")
        print()
        if any(not c.layout_ok for c in citations):
            overall_ok = False
        # 0 citations 视为 hard fail（QA 召回失败，目的地无意义；之前 vacuous-pass
        # bug 修复：empty list → any() = False → 误报通过）
        if n_cited == 0:
            print(f"    ✗ a 召回 0 篇（QA 检索未返回任何 source-document）", file=sys.stderr)
            overall_ok = False
        # 基线未全命中也降级为"非本次回归"，但不算 hard fail
        # 这里仅在 hard fail（layout 不全 / 0 召回）时 overall_ok = False

    print("=" * 60)
    if overall_ok:
        print("✓ 整体通过（c+a）")
        return 0
    else:
        print("✗ 整体未通过，详见 c_step.md / a_step.md")
        return 1


if __name__ == "__main__":
    sys.exit(main())
