from pathlib import Path
from dotenv import load_dotenv

# 加载 .env 配置
env_path = Path(__file__).resolve().parent.parent / ".env"
if env_path.exists():
    load_dotenv(env_path)

# PageIndex 路径
_pageindex_path = Path(__file__).resolve().parent.parent.parent / "Code" / "PageIndex"
if _pageindex_path.exists():
    import sys
    sys.path.insert(0, str(_pageindex_path))

from cli.main import app
app()