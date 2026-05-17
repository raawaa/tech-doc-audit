"""从 PDF 目录页解析制度列表，与 KB 文档逐条对比，输出差异报告。"""
import re, os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("AUDIT_DATA_DIR", "data")

from pypdf import PdfReader
import storage.doc_repo as doc_repo, storage.kb_repo as kb_repo
from services.vector_search import index_document, remove_document_index


def parse_toc(pdf_path: str) -> list[dict]:
    """从 PDF 第 3-9 页的目录中解析出所有条目。"""
    reader = PdfReader(pdf_path)
    toc_text = ""
    for pn in range(2, 10):
        toc_text += (reader.pages[pn].extract_text() or "") + "\n"

    entries = []
    for line in toc_text.split("\n"):
        line = line.strip()
        m = re.match(r"(\d+)[、）]\s*(.+)", line)
        if m:
            entries.append({"num": m.group(1), "text": m.group(2).strip()})
    return entries


def classify(title: str, page_text: str = "") -> str:
    """判断一个条目是 'regulation'（制度）、'sub'（子章节）、还是 'dept'（部门职责）。"""
    t = title.strip()
    # 子章节：含"动火作业"、"消火栓"、"细则"、"流程" 等非独立制度特征
    if any(kw in t for kw in ["动火", "消火栓", "归档范围", "期限表"]):
        return "sub"
    # 部门职责："部"结尾、不含制度关键词、较短
    if any(kw in t for kw in ["（2015", "（2010", "（2020", "（2022", "运行指挥中心"]):
        return "dept"
    # 正文第一页如果无 ZD 编号且标题不含制度关键词 → 非制度
    has_zd = bool(re.search(r"ZD/[A-Z]+-\d+-\d{4}-B\d+", page_text))
    has_kw = any(kw in t for kw in ["办法", "规定", "规则", "通知", "指引", "细则", "方案", "制度", "议事规则", "预案", "手册", "目录"])
    if has_zd or has_kw:
        return "regulation"
    return "dept"


def audit(kb_id: str, pdf_path: str):
    pdf_path = Path(pdf_path)
    kb = kb_repo.get(kb_id)
    if not kb:
        print(f"知识库不存在: {kb_id}")
        sys.exit(1)

    # 1. 解析 TOC
    print("解析目录...")
    toc_entries = parse_toc(str(pdf_path))
    print(f"  目录条目: {len(toc_entries)}")

    # 2. 获取 KB 文档列表
    kb_docs = {}
    for doc_id in kb.document_ids:
        doc = doc_repo.get_doc(kb.id, doc_id)
        if doc:
            kb_docs[doc_id] = doc

    print(f"  KB 文档: {len(kb_docs)}")

    # 3. 逐个 KB 文档读取第一页，判断类型
    regulations = []
    dept_entries = []
    sub_entries = []
    unmatched = []

    for doc_id, doc in kb_docs.items():
        if not doc.file_path or not os.path.exists(doc.file_path):
            continue
        try:
            reader = PdfReader(doc.file_path)
            page_text = reader.pages[0].extract_text() or ""
        except:
            page_text = ""

        title = doc.name.replace(".pdf", "").strip()
        cat = classify(title, page_text)

        entry = {"doc_id": doc_id, "title": title, "category": cat, "file_path": doc.file_path}

        if cat == "regulation":
            # 提取 ZD 编号
            zd = re.search(r"ZD/[A-Z]+-\d+-\d{4}-B\d+", page_text + title)
            if zd:
                entry["zd"] = zd.group(0)
                entry["display_name"] = f"{title}({zd.group(0)})"
            else:
                entry["zd"] = ""
                entry["display_name"] = title
            regulations.append(entry)
        elif cat == "sub":
            sub_entries.append(entry)
        else:
            dept_entries.append(entry)

    # 4. 输出报告
    print(f"\n{'='*60}")
    print(f"  核验报告")
    print(f"{'='*60}")
    print(f"\n【制度】{len(regulations)} 个")
    for r in sorted(regulations, key=lambda x: x.get('zd', x['title'])):
        zd = r.get("zd", "")
        print(f"  [{zd}] {r['title'][:55]}")

    print(f"\n【子章节（建议合并到父制度）】{len(sub_entries)} 个")
    for s in sub_entries:
        print(f"  {s['title'][:55]}")

    print(f"\n【非制度（部门职责/封面/目录等）】{len(dept_entries)} 个")
    for d in dept_entries:
        print(f"  {d['title'][:55]}")

    # 5. TOC 对比
    print(f"\n{'='*60}")
    print(f"  TOC 对比")
    print(f"{'='*60}")

    reg_titles = {re.sub(r"\s+", "", r["title"]) for r in regulations}
    for te in toc_entries:
        t_clean = re.sub(r"\s+", "", te["text"])
        matched = any(ct[:8] in t_clean or t_clean[:8] in ct for ct in reg_titles)
        if not matched:
            print(f"  TOC 有、KB 无: [{te['num']}] {te['text'][:55]}")

    print(f"\n建议:")
    print(f"  1. 以上 {len(regulations)} 个制度保留")
    print(f"  2. 删除 {len(dept_entries)} 个非制度条目")
    print(f"  3. {len(sub_entries)} 个子章节可合并或删除")

    # 6. 输出 delete 命令
    if dept_entries:
        print(f"\n删除非制度条目的脚本:")
        for d in dept_entries:
            print(f"    remove_document_index('{kb_id}', '{d['doc_id']}')")

    return regulations, dept_entries, sub_entries


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--kb-id", default="01KRNV471QA6EBKZEGFWSHRDZC")
    parser.add_argument("--pdf", default="data/kbs/01KRNV471QA6EBKZEGFWSHRDZC/docs/01KRNV498PAVDCFT6AKEM23YCY.pdf")
    args = parser.parse_args()
    audit(args.kb_id, args.pdf)
