"""Central config — loads .env, exposes typed Settings."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = _PROJECT_ROOT / ".env"
if _ENV_FILE.exists():
    load_dotenv(_ENV_FILE, override=False)


class Settings(BaseModel):
    # Provider
    provider: str = os.getenv("FARO_PROVIDER", "openai_compat")
    openai_base_url: str = os.getenv("FARO_OPENAI_BASE_URL", "https://api.deepseek.com")
    openai_api_key: str = os.getenv("FARO_OPENAI_API_KEY", "")
    openai_model: str = os.getenv("FARO_OPENAI_MODEL", "deepseek-chat")
    anthropic_api_key: str = os.getenv("FARO_ANTHROPIC_API_KEY", "")
    anthropic_model: str = os.getenv("FARO_ANTHROPIC_MODEL", "claude-opus-4-7")

    # Data
    tushare_token: str = os.getenv("TUSHARE_TOKEN", "")

    # Storage
    db_path: Path = Path(os.getenv("FARO_DB_PATH", "./data/faro.db"))
    cors_origins: list[str] = [
        s.strip()
        for s in os.getenv("FARO_CORS_ORIGINS", "http://localhost:5173").split(",")
        if s.strip()
    ]

    # Loop
    max_tool_turns: int = int(os.getenv("FARO_MAX_TOOL_TURNS", "8"))
    tool_result_max_chars: int = int(os.getenv("FARO_TOOL_RESULT_MAX_CHARS", "3500"))
    llm_timeout_sec: int = int(os.getenv("FARO_LLM_TIMEOUT_SEC", "180"))


settings = Settings()
