"""批量导入 PDF 到知识库。

用法：
  uv run python scripts/import_pdfs.py --kb-id <kb_id> --dir /path/to/pdfs
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("AUDIT_DATA_DIR", "data")

import storage.kb_repo as kb_repo
from services.doc_service import import_document


def import_pdfs(kb_id: str, pdf_dir: str):
    pdf_dir = Path(pdf_dir)
    if not pdf_dir.exists():
        print(f"目录不存在: {pdf_dir}")
        sys.exit(1)

    pdfs = sorted(pdf_dir.glob("*.pdf"))
    if not pdfs:
        print(f"未找到 PDF 文件: {pdf_dir}")
        return

    # 验证 KB 存在
    kb = kb_repo.get(kb_id)
    if not kb:
        print(f"知识库不存在: {kb_id}")
        sys.exit(1)

    print(f"目标知识库: {kb.name} ({kb.id})")
    print(f"PDF 文件数: {len(pdfs)}")
    print(f"目标目录: {pdf_dir.resolve()}")
    print()

    imported = 0
    errors = 0
    total = len(pdfs)

    for i, fpath in enumerate(pdfs, 1):
        try:
            pdf_bytes = fpath.read_bytes()
            import_document(kb_id, fpath.name, pdf_bytes)
            imported += 1
        except Exception as e:
            print(f"  [ERR] {fpath.name}: {e}")
            errors += 1

        if imported % 20 == 0 or i == total:
            print(f"  [{i}/{total}] {imported} 成功, {errors} 失败")

    print(f"\n导入完成: {imported} 成功, {errors} 失败")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="批量导入 PDF 到知识库")
    parser.add_argument("--kb-id", required=True, help="目标知识库 ID")
    parser.add_argument("--dir", required=True, help="PDF 目录路径")
    args = parser.parse_args()
    import_pdfs(args.kb_id, args.dir)
