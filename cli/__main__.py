from pathlib import Path
from dotenv import load_dotenv

# 加载 .env 配置
env_path = Path(__file__).resolve().parent.parent / ".env"
if env_path.exists():
    load_dotenv(env_path)

from cli.main import app
app()