"""一次回填脚本：为每个 KB 索引目录写 ``index.meta.json``。

按 issues/144 acceptance criteria 第 1 项：157 个 KB 的索引目录每个都要有
``index.meta.json``,``embedding_model_id = "BAAI/bge-m3"``,``embedding_dim = 1024``。
回填脚本一次性，**不重跑任何向量**(T4 spike gate 5 实测直接复用可行)。

## 用法

    # 写所有 KB(meta 中 embedding 体系是常量,与 provider 无关)
    uv run python scripts/backfill_kb_meta.py

    # 只写缺少 meta 的 KB(默认行为,可重复跑)
    uv run python scripts/backfill_kb_meta.py

    # 强制重写已知 KB 的 meta(默认 skip)
    uv run python scripts/backfill_kb_meta.py --force
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 让脚本能引用 core 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.index_manager import (
    _vectors_dir as _kb_vectors_dir,
    _read_index_meta,
    _write_index_meta,
)
from core.logger import get_logger

_logger = get_logger(__name__)


#: 与 ``_assert_kb_embedding_system_matches`` 中默认常量一致 —— production 路径
#: 唯一标识,不随 provider (local / SF) 变化(bge-m3 模型字面 ID 跨 provider
#: 一致,T3 §1.2 + T4 §6.1)。
DEFAULT_MODEL_ID = "BAAI/bge-m3"
DEFAULT_DIM = 1024


def list_production_kbs(data_root: Path) -> list[str]:
    """列 ``data_root/kbs`` 下所有 KB 目录,排除测试用 KB(`repro_kb`)。"""
    kbs_dir = data_root / "kbs"
    if not kbs_dir.is_dir():
        return []
    out: list[str] = []
    for p in sorted(kbs_dir.iterdir()):
        if not p.is_dir():
            continue
        kb_id = p.name
        if kb_id.startswith("repro_"):
            # 测试用 KB 不进 production 索引(基线:T4 §5.1 复盘显示
            # ``repro_kb/d1.npy`` 不是 bge-m3 产出,确认排除)
            continue
        if not (p / "vectors").is_dir():
            # 连 vectors/ 都没有的不视为 KB(可能是另一个目录或空目录)
            continue
        out.append(kb_id)
    return out


def main():
    parser = argparse.ArgumentParser(
        description="一次性回填 KB 索引 sidecar index.meta.json (issues/144 AC#1)",
    )
    parser.add_argument(
        "--data-root",
        default="./data",
        help="数据根目录(含 kbs/ 子目录),默认 ./data",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="强制重写已有 meta 的 KB(默认 skip,只补缺失)",
    )
    parser.add_argument(
        "--kb-id",
        action="append",
        default=[],
        help="只处理指定 KB(可多次),默认全部 KB",
    )
    args = parser.parse_args()

    data_root = Path(args.data_root).resolve()
    kbs = list_production_kbs(data_root)

    if args.kb_id:
        target_kbs = [kb for kb in kbs if kb in set(args.kb_id)]
        missing = set(args.kb_id) - set(target_kbs)
        if missing:
            _logger.warning("--kb-id 指定的 KB 未找到: %s", sorted(missing))
    else:
        target_kbs = kbs

    if not target_kbs:
        print(f"在 {data_root}/kbs 下没找到生产 KB(全是 repro_kb 或空)")
        return 0

    written = 0
    skipped = 0
    for kb_id in target_kbs:
        meta_path = _kb_vectors_dir(kb_id) / "index.meta.json"
        existing = _read_index_meta(kb_id)
        if existing is not None and not args.force:
            print(
                f"[skip] {kb_id}: meta 已存在 {existing.get('embedding_model_id')!r}",
                f"dim={existing.get('embedding_dim')}",
            )
            skipped += 1
            continue
        _write_index_meta(
            kb_id,
            model_id=DEFAULT_MODEL_ID,
            dim=DEFAULT_DIM,
            force=True,  # backfill 总是落盘新内容
        )
        print(f"[write] {kb_id}: {(meta_path)} -> {DEFAULT_MODEL_ID}, dim={DEFAULT_DIM}")
        written += 1

    print()
    print(f"完成: wrote {written}, skipped {skipped}, total {len(target_kbs)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
