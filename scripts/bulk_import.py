"""批量导入 PDF 到知识库（跳过逐文档向量索引，导入完成后统一重建）。

用法：
  uv run python scripts/bulk_import.py --kb-id <kb_id> --dir /path/to/pdfs
"""

import hashlib
import sys
from pathlib import Path

# 确保能找到项目模块
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os
os.environ.setdefault("AUDIT_DATA_DIR", "data")

import storage.doc_repo as doc_repo
import storage.kb_repo as kb_repo
from core.logger import get_logger

_logger = get_logger(__name__)


def bulk_import(kb_id: str, pdf_dir: str):
    src = Path(pdf_dir)
    if not src.exists():
        print(f"目录不存在: {src}")
        sys.exit(1)

    pdfs = sorted(src.glob("*.pdf"))
    if not pdfs:
        print(f"未找到 PDF 文件: {src}")
        return

    kb = kb_repo.get(kb_id)
    if not kb:
        print(f"知识库不存在: {kb_id}")
        sys.exit(1)

    print(f"知识库: {kb.name} ({kb.id})")
    print(f"PDF 文件: {len(pdfs)}")
    print(f"源目录: {src.resolve()}")
    print()

    imported = 0
    errors = 0
    total = len(pdfs)

    for i, fpath in enumerate(pdfs, 1):
        try:
            content = fpath.read_bytes()
            doc = doc_repo.save_doc(kb_id, fpath.name, content, "pdf")
            doc.content_hash = hashlib.sha256(content).hexdigest()
            # 标记为未建索引状态
            doc.embedding_status = "none"
            doc_repo._save_doc_meta(doc)
            # 更新 KB document_ids
            if doc.id not in kb.document_ids:
                kb.document_ids.append(doc.id)
            imported += 1
        except Exception as e:
            print(f"  [ERR] {fpath.name}: {e}")
            errors += 1

        if imported % 30 == 0 or i == total:
            print(f"  [{i}/{total}] {imported} 成功, {errors} 失败")

    # 一次性保存 KB 元数据
    kb_repo.update(kb)
    print(f"\n导入完成: {imported} 成功, {errors} 失败")

    if imported > 0:
        print(f"\n运行以下命令重建向量索引:")
        print(f"  uv run python -m cli index rebuild --kb-id {kb_id}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="批量导入 PDF（跳过向量索引）")
    parser.add_argument("--kb-id", required=True, help="目标知识库 ID")
    parser.add_argument("--dir", required=True, help="PDF 目录路径")
    args = parser.parse_args()
    bulk_import(args.kb_id, args.dir)
