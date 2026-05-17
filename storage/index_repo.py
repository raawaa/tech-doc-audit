import json
from pathlib import Path

from models.document import KBDocument

DATA_DIR = Path(__file__).parent.parent / "data"


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _kb_index_dir(kb_id: str) -> Path:
    return DATA_DIR / "kbs" / kb_id / "index"


def _index_file(kb_id: str, doc_id: str) -> Path:
    return _kb_index_dir(kb_id) / f"{doc_id}_tree.json"


def save_index(kb_id: str, doc_id: str, tree: dict) -> str:
    _ensure_dir(_kb_index_dir(kb_id))
    path = _index_file(kb_id, doc_id)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(tree, f, ensure_ascii=False, indent=2)
    return str(path)


def load_index(kb_id: str, doc_id: str) -> dict | None:
    path = _index_file(kb_id, doc_id)
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def delete_index(kb_id: str, doc_id: str) -> bool:
    path = _index_file(kb_id, doc_id)
    if path.exists():
        path.unlink()
    return True