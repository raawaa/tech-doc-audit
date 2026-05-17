"""增量索引脚本 — 对大 PDF 逐页处理，避免内存溢出。"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("AUDIT_DATA_DIR", str(Path(__file__).resolve().parent.parent / "data"))

import pdfplumber
from services.vector_search import _get_model, _chunk_text

KB_ID = "01KRNV471QA6EBKZEGFWSHRDZC"
DOC_ID = "01KRNV498PAVDCFT6AKEM23YCY"
FILE_PATH = f"data/kbs/{KB_ID}/docs/{DOC_ID}.pdf"
VEC_DIR = Path(f"data/kbs/{KB_ID}/vectors")
VEC_DIR.mkdir(parents=True, exist_ok=True)


def extract_text_streaming(path: str):
    """逐页 yield 文本块，每次 yield 后释放 page 对象。"""
    with pdfplumber.open(path) as pdf:
        total = len(pdf.pages)
        for i, page in enumerate(pdf.pages, 1):
            t = page.extract_text() or ""
            page.flush_cache()
            yield t, i, total


# ── 阶段 1：逐页提取 + 增量分块 ──
print("阶段 1: 提取文本并分块...")
chunk_buf = []
chunks = []
for text, i, total in extract_text_streaming(FILE_PATH):
    chunk_buf.append(text)
    joined = "\n\n".join(chunk_buf)
    if len(joined) >= 512:
        # 用 _chunk_text 对这个累积块做分块
        sub_chunks = _chunk_text(joined)
        chunks.extend(sub_chunks)
        # 保留最后一个 chunk 的尾部作为 overlap
        if sub_chunks:
            last = sub_chunks[-1]
            # 只保留最后 overlap 字符继续累积
            chunk_buf = [last[-128:]] if len(last) > 128 else []
        else:
            chunk_buf = []
    if i % 500 == 0:
        print(f"  page {i}/{total}, {len(chunks)} chunks so far")

# 最后剩下的文本
if chunk_buf:
    remaining = "\n\n".join(chunk_buf)
    if remaining.strip():
        chunks.extend(_chunk_text(remaining))

print(f"共 {len(chunks)} 个 chunk, 保存中...")
chunk_file = VEC_DIR / f"{DOC_ID}_chunks.json"
chunk_file.write_text(
    json.dumps({"doc_id": DOC_ID, "file_path": FILE_PATH, "chunks": chunks},
               ensure_ascii=False),
    encoding="utf-8")
print(f"Chunks saved: {chunk_file}")

# ── 阶段 2：分批 embedding ──
import numpy as np

print("阶段 2: 计算 embedding...")
model = _get_model()
ALL_EMBS = []
BATCH = 8
total = len(chunks)
for i in range(0, total, BATCH):
    batch = chunks[i:i + BATCH]
    embs = model.encode(batch, normalize_embeddings=True, show_progress_bar=False)
    ALL_EMBS.append(embs)
    # 释放中间结果
    del batch
    if (i + BATCH) % 40 == 0 or i + BATCH >= total:
        print(f"  {min(i+BATCH, total)}/{total}")

embeddings = np.concatenate(ALL_EMBS, axis=0)
emb_file = VEC_DIR / f"{DOC_ID}_emb.npy"
np.save(str(emb_file), embeddings)

# ── 阶段 3：更新索引 ──
idx_file = VEC_DIR / "indexes.json"
idx = json.loads(idx_file.read_text(encoding="utf-8")) if idx_file.exists() else {"docs": []}
if DOC_ID not in idx["docs"]:
    idx["docs"].append(DOC_ID)
idx_file.write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")
print(f"Embedding shape: {embeddings.shape}")
print("✓ 完成")
