"""按 PDF 书签全量拆分为单个文件并导入知识库。

用法：
  uv run python scripts/split_kb_pdf.py \\
    --kb-id <kb_id> \\
    --pdf <path_to_large_pdf>
"""

import argparse
import io
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("AUDIT_DATA_DIR", "data")

from pypdf import PdfReader, PdfWriter

import storage.kb_repo as kb_repo
from services.doc_service import import_document


def extract_outline_all(pdf_path: str) -> list[dict]:
    """提取全部 outline 条目，不按 depth 过滤。"""
    reader = PdfReader(pdf_path)
    entries = []

    def walk(items, depth=0):
        for item in items:
            if isinstance(item, list):
                walk(item, depth + 1)
            else:
                title = str(item.get("/Title", "")).strip()
                if not title:
                    continue
                page_ref = item.get("/Page")
                pn = None
                if page_ref is not None:
                    try:
                        pn = reader._get_page_number_by_indirect(page_ref)
                    except Exception:
                        pass
                entries.append({"name": title, "page": pn, "depth": depth})

    walk(reader.outline)
    return entries


def strip_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "", name).strip()
    return name[:80]


def split_and_import_all(kb_id: str, pdf_path: str):
    pdf_path = Path(pdf_path)
    kb = kb_repo.get(kb_id)
    if not kb:
        print(f"知识库不存在: {kb_id}")
        sys.exit(1)

    print(f"知识库: {kb.name} ({kb.id})")
    print(f"原始 PDF: {pdf_path} ({pdf_path.stat().st_size / 1024 / 1024:.1f} MB)")

    print("\n1. 解析全部书签...")
    entries = extract_outline_all(str(pdf_path))
    print(f"   共 {len(entries)} 个书签条目")

    # 过滤：有页码 + 名称 > 2 字符
    valid = [e for e in entries if e["page"] is not None and e["page"] >= 0 and len(e["name"]) > 2]
    print(f"   有页码的: {len(valid)} 个")

    # 计算 end_page：按 page 排序，当前条目的 end = 下一条目的 page
    valid.sort(key=lambda e: (e["page"], -e["depth"]))
    for i, e in enumerate(valid):
        e["end_page"] = valid[i + 1]["page"] if i + 1 < len(valid) else entries[-1]["page"] + 10

    # 去重：如果两个条目有相同的 (page, end_page)，保留深度更大的（更具体）
    seen_ranges = set()
    deduped = []
    for e in valid:
        rng = (e["page"], e["end_page"])
        if rng not in seen_ranges:
            seen_ranges.add(rng)
            deduped.append(e)
    valid = deduped
    print(f"   去重后: {len(valid)} 个")

    # 简短预览
    for e in valid[:8]:
        print(f"     d{e['depth']} p{e['page']:>4}-{e['end_page']:<4} {e['name'][:55]}")
    print(f"     ... 还有 {len(valid) - 8} 个")

    print("\n2. 逐条拆分并导入知识库...")
    reader = PdfReader(str(pdf_path))

    imported = 0
    for i, e in enumerate(valid):
        start = e["page"]
        end = e["end_page"]

        writer = PdfWriter()
        for p in range(start, end):
            if p < len(reader.pages):
                try:
                    writer.add_page(reader.pages[p])
                except Exception:
                    pass

        buf = io.BytesIO()
        writer.write(buf)
        pdf_bytes = buf.getvalue()

        fname = f"{strip_name(e['name'])}.pdf"
        try:
            import_document(kb_id, fname, pdf_bytes)
            imported += 1
        except Exception as ex:
            print(f"   [ERR] {e['name']}: {ex}")

        if imported % 15 == 0 or imported == len(valid):
            print(f"   [{imported}/{len(valid)}] {e['name'][:50]}")

    print(f"\n导入完成: {imported}/{len(valid)} 个")
    print("✓ 完成")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="按书签全拆 PDF")
    parser.add_argument("--kb-id", required=True)
    parser.add_argument("--pdf", required=True)
    args = parser.parse_args()
    split_and_import_all(args.kb_id, args.pdf)
