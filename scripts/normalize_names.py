"""规范化制度文件名和来源标签：从 PDF 正文提取 ZD 编号。

流程：
  1. 遍历 KB 中每个文档
  2. 从 PDF 第一页提取 ZD 编号（如 ZD/ZB-02-2024-B1）
  3. 计算展示名：公司合同管理办法(ZD/ZB-02-2024-B1)
  4. 重命名物理文件 + 更新元数据
  5. 重建向量索引（来源标签 = 展示名）
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("AUDIT_DATA_DIR", "data")

from pypdf import PdfReader

import storage.doc_repo as doc_repo
import storage.kb_repo as kb_repo
from services.vector_search import index_document, remove_document_index

ZD_PATTERN = re.compile(r"ZD/[A-Z]+-\d+-\d{4}-B\d+")


def extract_zd_code(pdf_path: str) -> str | None:
    """从 PDF 第一页提取 ZD 编号。"""
    try:
        reader = PdfReader(pdf_path)
        text = ""
        for p in reader.pages[:3]:
            t = p.extract_text() or ""
            text += t
        m = ZD_PATTERN.search(text)
        return m.group(0) if m else None
    except Exception:
        return None


def clean_stem(stem: str) -> str:
    """去掉文件名末尾的 _ULID 后缀。"""
    if "_01K" in stem:
        stem = stem.rsplit("_", 1)[0]
    return stem


def normalize(kb_id: str):
    kb = kb_repo.get(kb_id)
    if not kb:
        print(f"知识库不存在: {kb_id}")
        sys.exit(1)

    renamed = 0
    no_code = 0

    for doc_id in list(kb.document_ids):
        doc = doc_repo.get_doc(kb_id, doc_id)
        if not doc or not doc.file_path:
            continue
        fp = Path(doc.file_path)
        if not fp.exists():
            continue

        zd = extract_zd_code(str(fp))
        if not zd:
            no_code += 1
            continue

        # 计算展示名
        old_stem = clean_stem(fp.stem)
        display_name = f"{old_stem}({zd})"

        # 重命名物理文件
        new_stem = f"{old_stem}_{zd}".replace("/", "-")
        new_fp = fp.parent / f"{new_stem}_{doc.id}.pdf"
        fp.rename(new_fp)

        # 更新元数据
        doc.name = f"{display_name}.pdf"
        doc.original_name = doc.name
        doc.file_path = str(new_fp)
        doc_repo._save_doc_meta(doc)

        # 重建索引（传入展示名作为 source_name）
        remove_document_index(kb_id, doc_id)
        index_document(kb_id, doc_id, doc.file_path, source_name=display_name)

        # 更新 KB 的 document_ids 不变（id 没变）
        renamed += 1
        if renamed % 20 == 0:
            print(f"   [{renamed}] {display_name}")

    print(f"\n完成: {renamed} 个已重命名, {no_code} 个未找到编号")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="规范化制度文件名和来源标签")
    parser.add_argument("--kb-id", required=True)
    args = parser.parse_args()
    normalize(args.kb_id)
