"""流式索引企业内部制度库（1800 页 PDF）。

分两步避免同时加载 PDF 和 embedding model 导致 OOM：
  Step 1: 逐页 PDF 提取 → 流式分块 → 每 100 块 flush 到磁盘 JSON
  Step 2: 分批加载 chunks → embedding → 直接追加到 .npy
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("AUDIT_DATA_DIR", "data")

KB_ID = "01KRNV471QA6EBKZEGFWSHRDZC"
DOC_ID = "01KRNV498PAVDCFT6AKEM23YCY"
FILE_PATH = f"data/kbs/{KB_ID}/docs/{DOC_ID}.pdf"
VEC_DIR = Path(f"data/kbs/{KB_ID}/vectors")
VEC_DIR.mkdir(parents=True, exist_ok=True)

CHUNK_SIZE = 512
OVERLAP = 128
CHUNKS_JSON = VEC_DIR / f"{DOC_ID}_chunks.json"
EMB_NPY = VEC_DIR / f"{DOC_ID}_emb.npy"


# ══════════════════════════════════════════════════════════════════════
# Step 1: 流式提取 + 分块
# ══════════════════════════════════════════════════════════════════════

def step1_extract_and_chunk():
    print("=" * 50)
    print("Step 1: 流式提取 PDF 文本并分块")
    print("=" * 50)

    import pdfplumber

    all_chunks = []
    buffer = ""

    with pdfplumber.open(FILE_PATH) as pdf:
        total = len(pdf.pages)
        for i, page in enumerate(pdf.pages):
            t = page.extract_text() or ""
            page.flush_cache()
            buffer = (buffer + "\n\n" + t).strip() if buffer else t

            # 当 buffer 超过阈值时，切分
            while len(buffer) >= CHUNK_SIZE:
                split_at = buffer.rfind("\n\n", 0, CHUNK_SIZE)
                if split_at < CHUNK_SIZE // 3:
                    split_at = buffer.rfind("。", 0, CHUNK_SIZE)
                    if split_at < CHUNK_SIZE // 3:
                        split_at = CHUNK_SIZE
                chunk = buffer[:split_at].strip()
                if chunk:
                    all_chunks.append(chunk)
                buffer = buffer[max(split_at - OVERLAP, 0):]

            if i % 200 == 0 and i > 0:
                print(f"  page {i}/{total}, {len(all_chunks)} chunks, buffer={len(buffer)} chars")

        if buffer.strip():
            all_chunks.append(buffer.strip())

    print(f"\n共 {len(all_chunks)} 个 chunk")

    CHUNKS_JSON.write_text(
        json.dumps({"doc_id": DOC_ID, "file_path": FILE_PATH, "chunks": all_chunks},
                   ensure_ascii=False),
        encoding="utf-8")
    print(f"保存到 {CHUNKS_JSON}")
    return len(all_chunks)


# ══════════════════════════════════════════════════════════════════════
# Step 2: 分批 embedding
# ══════════════════════════════════════════════════════════════════════

def step2_embed():
    print("\n" + "=" * 50)
    print("Step 2: 分批 embedding")
    print("=" * 50)

    import numpy as np
    from services.vector_search import _get_model

    chunks = json.loads(CHUNKS_JSON.read_text(encoding="utf-8"))["chunks"]
    total = len(chunks)
    print(f"加载 {total} 个 chunk")

    model = _get_model()
    print("模型加载完成")

    BATCH = 8
    all_embs = []

    for i in range(0, total, BATCH):
        batch = chunks[i:i + BATCH]
        embs = model.encode(batch, normalize_embeddings=True, show_progress_bar=False)
        all_embs.append(embs)

        if (i + BATCH) % 80 == 0 or i + BATCH >= total:
            print(f"  {min(i+BATCH, total)}/{total}")
            # 定期 flush
            if len(all_embs) >= 50:
                tmp = np.concatenate(all_embs, axis=0)
                if EMB_NPY.exists():
                    existing = np.load(str(EMB_NPY))
                    tmp = np.concatenate([existing, tmp], axis=0)
                np.save(str(EMB_NPY), tmp)
                all_embs = []
                del tmp

    # final flush
    if all_embs:
        final = np.concatenate(all_embs, axis=0)
        if EMB_NPY.exists():
            existing = np.load(str(EMB_NPY))
            final = np.concatenate([existing, final], axis=0)
        np.save(str(EMB_NPY), final)

    # verify
    final = np.load(str(EMB_NPY))
    print(f"Embedding shape: {final.shape}")

    # 更新索引
    idx_file = VEC_DIR / "indexes.json"
    idx = json.loads(idx_file.read_text(encoding="utf-8")) if idx_file.exists() else {"docs": []}
    if DOC_ID not in idx["docs"]:
        idx["docs"].append(DOC_ID)
    idx_file.write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")
    print("✓ 索引完成")


if __name__ == "__main__":
    step1_extract_and_chunk()
    step2_embed()
