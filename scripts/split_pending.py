"""按 PDF 书签拆分为独立 PDF 文件，存入 pending_review 目录供人工审核。

用法：
  uv run python scripts/split_pending.py \\
    --pdf <path_to_pdf> [--out data/pending_review]
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pypdf import PdfReader, PdfWriter


def extract_outline(pdf_path: str) -> list[dict]:
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
    n = re.sub(r'[\\/:*?"<>|\n\r]', "", name).strip()
    return n[:80]


def classify(title: str, page_text: str = "") -> str:
    """初步分类，供审核参考。"""
    t = title.strip()
    has_zd = bool(re.search(r"ZD/[A-Z]+-\d+-\d{4}-B\d+", page_text))
    has_kw = any(kw in t for kw in ["办法", "规定", "规则", "通知", "指引", "细则",
                                      "方案", "制度", "议事规则", "预案", "手册"])
    if has_zd or has_kw:
        return "regulation"
    if any(kw in t for kw in ["动火", "消火栓", "归档范围", "期限表"]):
        return "sub"
    return "dept"


def split_pending(pdf_path: str, out_dir: str):
    pdf_path = Path(pdf_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"原始 PDF: {pdf_path.name} ({pdf_path.stat().st_size/1024/1024:.1f} MB)")
    print(f"输出目录: {out_dir}")

    # 1. 解析 outline
    print("\n1. 解析书签...")
    entries = extract_outline(str(pdf_path))
    print(f"   共 {len(entries)} 个")

    valid = [e for e in entries if e["page"] is not None and e["page"] >= 0 and len(e["name"]) > 2]
    print(f"   有页码: {len(valid)}")

    # 2. 排序 + 去重
    valid.sort(key=lambda e: (e["page"], -e["depth"]))
    for i, e in enumerate(valid):
        e["end_page"] = valid[i + 1]["page"] if i + 1 < len(valid) else valid[-1]["page"] + 10

    seen = set()
    deduped = []
    for e in valid:
        rng = (e["page"], e["end_page"])
        if rng not in seen:
            seen.add(rng)
            deduped.append(e)
    valid = deduped
    print(f"   去重后: {len(valid)}")

    # 3. 拆分为独立 PDF
    print("\n2. 拆分 PDF...")
    reader = PdfReader(str(pdf_path))
    inventory = []
    failures = 0

    for idx, e in enumerate(valid, 1):
        start, end = e["page"], e["end_page"]
        seq = f"{idx:03d}"
        fname = f"{seq}_{strip_name(e['name'])}.pdf"
        fpath = out_dir / fname

        writer = PdfWriter()
        for p in range(start, end):
            if p < len(reader.pages):
                try:
                    writer.add_page(reader.pages[p])
                except Exception:
                    pass

        with open(fpath, "wb") as f:
            writer.write(f)

        # 提取元数据供审核
        meta = {"page_count": end - start, "size": fpath.stat().st_size}
        try:
            r2 = PdfReader(fpath)
            first_text = r2.pages[0].extract_text() or ""
            meta["first_page"] = first_text[:200]
        except Exception:
            first_text = ""
            meta["first_page"] = ""

        zd = re.search(r"ZD/[A-Z]+-\d+-\d{4}-B\d+", first_text + e["name"])
        meta["zd_code"] = zd.group(0) if zd else ""
        meta["category"] = classify(e["name"], first_text)

        inventory.append({
            "seq": seq,
            "filename": fname,
            "title": e["name"],
            "depth": e["depth"],
            "page_start": start,
            "page_end": end,
            "page_count": meta["page_count"],
            "size_bytes": meta["size"],
            "zd_code": meta["zd_code"],
            "category": meta["category"],
            "first_page_preview": meta["first_page"],
        })

        if idx % 30 == 0 or idx == len(valid):
            print(f"   [{idx}/{len(valid)}] {e['name'][:50]}")

    # 4. 写入清单
    inv_file = out_dir / "inventory.json"
    inv_file.write_text(
        json.dumps({"total": len(inventory), "items": inventory},
                   ensure_ascii=False, indent=2),
        encoding="utf-8")

    # 5. 生成文本清单
    txt_file = out_dir / "inventory.txt"
    lines = []
    lines.append(f"{'序号':<5} {'分类':<10} {'ZD编号':<25} {'页数':<5} {'文件名':<60}")
    lines.append("-" * 110)
    for it in inventory:
        zd = it["zd_code"][:22] if it["zd_code"] else "-"
        cat = it["category"][:8]
        lines.append(f"{it['seq']:<5} {cat:<10} {zd:<25} {it['page_count']:<5} {it['filename'][:58]}")
    txt_file.write_text("\n".join(lines), encoding="utf-8")

    print(f"\n  审核清单: {txt_file}")
    print(f"  JSON清单: {inv_file}")
    print(f"  共 {len(inventory)} 个文件, {failures} 个失败")
    print("\n=== 按类别统计 ===")
    from collections import Counter
    for cat, cnt in Counter(it["category"] for it in inventory).most_common():
        print(f"  {cat}: {cnt}")
    print("\n✓ 完成。请查看 inventory.txt 或 inventory.json，\n  将不需要的条目 category 改为 'skip' 后运行 import_approved.py")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--out", default="data/pending_review")
    args = parser.parse_args()
    split_pending(args.pdf, args.out)
