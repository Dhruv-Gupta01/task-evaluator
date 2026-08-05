from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://taskeval:taskeval@localhost:5433/taskeval"
    storage_dir: Path = Path(__file__).resolve().parent.parent / "storage"
    cors_origins: list[str] = ["http://localhost:8080", "http://localhost:5173"]

    # LLM provider config (env-driven, swappable without code changes)
    llm_provider: str = "fireworks"  # anthropic | openai | gemini | groq | fireworks
    llm_model: str = "accounts/fireworks/models/gpt-oss-120b"
    llm_rpm: int = 60
    llm_tpm: int | None = None
    llm_max_iters: int = 30

    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    gemini_api_key: str | None = None
    groq_api_key: str | None = None
    fireworks_api_key: str | None = None

    # Defaults applied when a task.toml omits a section
    default_cpus: float = 1.0
    default_memory_mb: int = 1024
    default_allow_internet: bool = False
    default_agent_timeout_sec: int = 600
    default_verifier_timeout_sec: int = 120
    max_agent_trials: int = 50


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    (settings.storage_dir / "submissions").mkdir(parents=True, exist_ok=True)
    (settings.storage_dir / "quota").mkdir(parents=True, exist_ok=True)
    return settings
