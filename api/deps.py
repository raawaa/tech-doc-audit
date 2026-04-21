from typing import Annotated

from fastapi import Depends
from pathlib import Path
import os

DATA_DIR = Path(os.environ.get("AUDIT_DATA_DIR", "./data"))


def get_data_dir() -> Path:
    return DATA_DIR


DataDirDep = Annotated[Path, Depends(get_data_dir)]