"""流式索引大 PDF — 通过 LlamaIndex 管道自动分块 + embedding。"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("AUDIT_DATA_DIR", "data")

KB_ID = "01KRNV471QA6EBKZEGFWSHRDZC"
DOC_ID = "01KRNV498PAVDCFT6AKEM23YCY"
FILE_PATH = f"data/kbs/{KB_ID}/docs/{DOC_ID}.pdf"

from services.vector_search import index_document
index_document(KB_ID, DOC_ID, FILE_PATH, source_name="制度库")
print("✓ 索引完成")
