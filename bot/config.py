import os
from pathlib import Path

from dotenv import load_dotenv


ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(ENV_PATH)

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
API_BASE_URL = os.getenv(
    "API_BASE_URL",
    "http://127.0.0.1:8000",
).strip().rstrip("/")

API_TIMEOUT_SECONDS = 60.0


def validate_config() -> None:
    if not DISCORD_TOKEN:
        raise RuntimeError(
            "DISCORD_TOKEN이 설정되지 않았습니다. "
            "bot/.env 파일을 확인해주세요."
        )