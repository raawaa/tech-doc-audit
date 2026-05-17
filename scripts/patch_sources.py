"""补丁：为已存在的向量 chunks 添加来源标签（从文件名提取）。"""
import json
from pathlib import Path

vec_dir = Path("data/kbs/01KRNV471QA6EBKZEGFWSHRDZC/vectors")
patched = 0

for f in vec_dir.iterdir():
    if not f.name.endswith("_chunks.json"):
        continue
    data = json.loads(f.read_text(encoding="utf-8"))
    sources = data.get("sources", [])
    if sources and any(sources):
        continue
    fp = data.get("file_path", "")
    if not fp:
        continue
    # 文件名格式： {sanitized_name}_{doc_id}.{ext}
    # doc_repo.save_doc 生成: {stem}_{doc.id}.pdf
    fname = Path(fp).stem  # e.g. "公司合同管理办法_01KRR..."
    # 去掉末尾的 _ULID
    if "_01K" in fname:
        name = fname.rsplit("_", 1)[0]
    else:
        name = fname
    chunks_text = data.get("chunks", [])
    data["sources"] = [name] * len(chunks_text)
    f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    patched += 1

print(f"Patched {patched} files")
