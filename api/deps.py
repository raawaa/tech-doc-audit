from typing import Annotated

from fastapi import Depends
from pathlib import Path

from core.data_dir import get_data_dir


DataDirDep = Annotated[Path, Depends(get_data_dir)]
