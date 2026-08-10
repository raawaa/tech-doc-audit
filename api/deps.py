from typing import Annotated

from fastapi import Depends
from pathlib import Path
import os


def get_data_dir() -> Path:
    """解析数据根目录；每次调用读取 env（issue #137 per-test 隔离）。"""
    return Path(os.environ.get("AUDIT_DATA_DIR", "./data"))


DataDirDep = Annotated[Path, Depends(get_data_dir)]
