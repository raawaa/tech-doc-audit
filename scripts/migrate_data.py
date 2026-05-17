"""将旧路径数据迁移到新目录结构。

旧：data/kb_docs/{kb_id}/    → 新：data/kbs/{kb_id}/docs/
旧：data/kb_meta/{kb_id}/    → 新：data/kbs/{kb_id}/meta/
旧：data/audit_docs/{doc_id}/ → 新：data/audits/{doc_id}/doc/
旧：data/audit_docs/meta/      → 新：data/audits/{doc_id}/meta.json
旧：data/audit_tasks/{task_id}.json → 新：data/audits/{doc_id}/tasks/{task_id}.json
"""

import json
import os
import shutil
from pathlib import Path

DATA_DIR = Path(os.environ.get("AUDIT_DATA_DIR", "./data")).resolve()


def migrate_kbs():
    """迁移知识库数据。"""
    old_docs = DATA_DIR / "kb_docs"
    old_meta = DATA_DIR / "kb_meta"
    if not old_meta.exists():
        return

    for kb_dir in old_meta.iterdir():
        if not kb_dir.is_dir():
            continue
        kb_id = kb_dir.name
        kb_file = kb_dir / "kb.json"
        if not kb_file.exists():
            continue

        new_kb_dir = DATA_DIR / "kbs" / kb_id
        new_docs_dir = new_kb_dir / "docs"
        new_meta_dir = new_kb_dir / "meta"

        # 复制 kb.json
        new_kb_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(kb_file, new_kb_dir / "kb.json")

        # 复制文档文件
        old_docs_dir = old_docs / kb_id
        if old_docs_dir.exists():
            new_docs_dir.mkdir(parents=True, exist_ok=True)
            for f in old_docs_dir.iterdir():
                if f.is_file():
                    shutil.copy2(f, new_docs_dir / f.name)

        # 复制文档元数据，更新 file_path
        if kb_dir.exists():
            new_meta_dir.mkdir(parents=True, exist_ok=True)
            for f in kb_dir.iterdir():
                if f.suffix == ".json" and f.name != "kb.json":
                    data = json.loads(f.read_text(encoding="utf-8"))
                    # 更新 file_path
                    old_fp = data.get("file_path", "")
                    if old_fp and "kb_docs" in old_fp:
                        parts = old_fp.rsplit("/", 1)
                        filename = parts[-1] if len(parts) > 1 else ""
                        data["file_path"] = str(new_docs_dir / filename)
                        (new_meta_dir / f.name).write_text(
                            json.dumps(data, ensure_ascii=False, indent=2),
                            encoding="utf-8")
                    else:
                        shutil.copy2(f, new_meta_dir / f.name)
        print(f"  KB {kb_id} → {new_kb_dir}")


def migrate_audit_docs():
    """迁移待审核文档。"""
    old_audit = DATA_DIR / "audit_docs"
    if not old_audit.exists():
        return

    # 迁移文档目录
    for d in old_audit.iterdir():
        if not d.is_dir():
            continue
        doc_id = d.name
        new_doc_dir = DATA_DIR / "audits" / doc_id / "doc"
        if d.exists():
            new_doc_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(d, new_doc_dir, dirs_exist_ok=True)
            print(f"  audit doc {doc_id} → {new_doc_dir.parent}")

    # 迁移 meta JSON 文件到 audit/{doc_id}/meta.json
    old_meta = old_audit / "meta"
    if old_meta.exists():
        for f in old_meta.iterdir():
            if f.suffix == ".json":
                doc_id = f.stem
                meta_dir = DATA_DIR / "audits" / doc_id
                meta_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, meta_dir / "meta.json")
                print(f"  audit meta {doc_id} → {meta_dir / 'meta.json'}")


def migrate_audit_tasks():
    """迁移审核任务。"""
    old_tasks = DATA_DIR / "audit_tasks"
    if not old_tasks.exists():
        return

    for f in sorted(old_tasks.iterdir()):
        if f.suffix != ".json":
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            doc_id = data.get("document_id", "")
            if not doc_id:
                continue
            tasks_dir = DATA_DIR / "audits" / doc_id / "tasks"
            tasks_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, tasks_dir / f.name)
        except Exception:
            pass
    print(f"  audit tasks → data/audits/*/tasks/")


def main():
    print("迁移知识库数据...")
    migrate_kbs()
    print("\n迁移待审核文档...")
    migrate_audit_docs()
    print("\n迁移审核任务...")
    migrate_audit_tasks()
    print("\n✓ 迁移完成")


if __name__ == "__main__":
    main()
