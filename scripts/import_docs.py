"""批量导入文档到知识库。

支持 PDF / MD 格式（通过 --ext 参数指定，默认同时支持两种）。
不传 --dir 时默认扫描 data/kb_sources/ 目录。

用法：
  uv run python scripts/import_docs.py --kb-id <kb_id>
  uv run python scripts/import_docs.py --kb-id <kb_id> --dir /path/to/docs
  uv run python scripts/import_docs.py --kb-id <kb_id> --dir /path/to/docs --ext .pdf
  uv run python scripts/import_docs.py --kb-id <kb_id> --dir /path/to/docs --ext .md
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("AUDIT_DATA_DIR", "data")

_DEFAULT_SOURCE_DIR = os.environ.get("AUDIT_DATA_DIR", "data") + "/kb_sources"

import storage.kb_repo as kb_repo
from services.doc_service import import_document


def import_docs(kb_id: str, docs_dir: str, extensions: list[str] | None = None):
    docs_dir = Path(docs_dir)
    if not docs_dir.exists():
        print(f"目录不存在: {docs_dir}")
        sys.exit(1)

    if extensions is None:
        extensions = [".pdf", ".md"]

    files = []
    for ext in extensions:
        files.extend(sorted(docs_dir.glob(f"*{ext}")))
    files.sort(key=lambda p: p.name)

    if not files:
        exts = ", ".join(extensions)
        print(f"未找到 {exts} 文件: {docs_dir}")
        return

    # 验证 KB 存在
    kb = kb_repo.get(kb_id)
    if not kb:
        print(f"知识库不存在: {kb_id}")
        sys.exit(1)

    print(f"目标知识库: {kb.name} ({kb.id})")
    print(f"文档文件数: {len(files)}")
    print(f"目标目录: {docs_dir.resolve()}")
    print(f"格式: {', '.join(extensions)}")
    print()

    imported = 0
    errors = 0
    total = len(files)

    for i, fpath in enumerate(files, 1):
        try:
            content = fpath.read_bytes()
            import_document(kb_id, fpath.name, content)
            imported += 1
        except Exception as e:
            print(f"  [ERR] {fpath.name}: {e}")
            errors += 1

        if imported % 20 == 0 or i == total:
            print(f"  [{i}/{total}] {imported} 成功, {errors} 失败")

    print(f"\n导入完成: {imported} 成功, {errors} 失败")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="批量导入文档到知识库")
    parser.add_argument("--kb-id", required=True, help="目标知识库 ID")
    parser.add_argument("--dir", default=_DEFAULT_SOURCE_DIR,
                        help=f"文档目录路径（默认: {_DEFAULT_SOURCE_DIR}）")
    parser.add_argument("--ext", nargs="+", default=None,
                        help="文件后缀（默认同时导入 .pdf 和 .md）")
    args = parser.parse_args()
    import_docs(args.kb_id, args.dir, args.ext)
