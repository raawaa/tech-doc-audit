"""导入审核通过的制度文件到知识库。

读取 data/pending_review/inventory.json，导入 category != "skip" 的条目。
用户可在 inventory.json 中修改 category 来控制哪些要导入。

用法：
  uv run python scripts/import_approved.py --kb-id <kb_id> [--dir data/pending_review]
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("AUDIT_DATA_DIR", "data")

# 加载 .env 配置
from dotenv import load_dotenv as _load_dotenv
_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    _load_dotenv(_env_path)

import storage.kb_repo as kb_repo
from services.doc_service import import_document


def import_approved(kb_id: str, review_dir: str):
    review_dir = Path(review_dir)
    inv_file = review_dir / "inventory.json"
    if not inv_file.exists():
        print(f"清单不存在: {inv_file}")
        sys.exit(1)

    inv = json.loads(inv_file.read_text(encoding="utf-8"))
    items = inv.get("items", [])
    total = len(items)

    # 筛选审核通过的
    approved = [it for it in items if it.get("category") != "skip"]
    skipped = [it for it in items if it.get("category") == "skip"]

    print(f"清单共 {total} 条")
    print(f"  审核通过: {len(approved)}")
    print(f"  跳过: {len(skipped)}")

    if not approved:
        print("没有需要导入的条目")
        return

    # 确认
    print(f"\n即将导入 {len(approved)} 个文件到知识库 {kb_id}:")
    for it in approved[:10]:
        print(f"  {it['seq']} [{it.get('category','?')}] {it['title'][:55]}")
    if len(approved) > 10:
        print(f"  ... 还有 {len(approved)-10} 个")
    print()

    # 开始导入
    imported = 0
    errors = 0
    for it in approved:
        fpath = review_dir / it["filename"]
        if not fpath.exists():
            print(f"  [ERR] 文件不存在: {fpath.name}")
            errors += 1
            continue

        try:
            pdf_bytes = fpath.read_bytes()
            import_document(kb_id, it["filename"], pdf_bytes)
            imported += 1
        except Exception as e:
            print(f"  [ERR] {it['title'][:50]}: {e}")
            errors += 1

        if imported % 30 == 0 or imported == len(approved):
            print(f"  [{imported}/{len(approved)}] {it['title'][:50]}")

    print(f"\n导入完成: {imported} 成功, {errors} 失败")
    print("运行 index_document 自动完成向量索引...")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--kb-id", required=True)
    parser.add_argument("--dir", default="data/pending_review")
    args = parser.parse_args()
    import_approved(args.kb-id, args.dir)
