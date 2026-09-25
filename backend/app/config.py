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
    # Judge calls on the real OpenAI API only (ignored for other providers).
    # max output tokens includes reasoning tokens; 0 = no cap.
    llm_reasoning_effort: str = "low"  # low | medium | high | ...; empty = model default
    llm_max_output_tokens: int = 16000

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

    # One architecture for every build and container run (Build, Harbor
    # Oracle/Nop, agent trials). arm64 runs natively on Apple Silicon;
    # linux/amd64 works too but is emulated there and much slower.
    docker_platform: str = "linux/arm64"

    # Oracle/Nop and agent trials run through the Harbor CLI
    # (`uv tool install harbor`).
    harbor_bin: str = "harbor"
    # Network policy Harbor runs every task with, applied to a temporary copy
    # of the task (the extracted files are never edited). Harbor >= 0.23 only
    # enforces "no-network" when Docker's kernel supports its egress-control
    # sidecar (not the case on Docker Desktop for Mac) and otherwise rejects
    # the task, so old `allow_internet = false` tasks would fail every stage.
    # Set empty to respect each task's own setting instead.
    harbor_network_mode: str = "public"

    # Agent trials: Harbor agent plus a LiteLLM model string. When
    # agent_model is empty it's derived from llm_provider/llm_model, so the
    # agent and the LLM judges use the same model unless overridden.
    harbor_agent: str = "terminus-2"
    agent_model: str = ""
    agent_reasoning_effort: str = ""  # e.g. low | medium | high; empty = model default
    agent_trials_concurrency: int = 1
    # Codex-only (HARBOR_AGENT=codex). Codex installs itself inside the task
    # container on every trial and runs there, so it needs network access
    # (HARBOR_NETWORK_MODE=public) and gets the OpenAI key inside the
    # container. It has no turn cap; only the task's agent timeout bounds it.
    # Absolute agent timeout (seconds) applied to every task, overriding each
    # task.toml's [agent] timeout_sec. Terminal-Bench 4.0 uses a flat 8 hours
    # (28800). 0 = respect each task's own value.
    agent_timeout_sec: int = 0
    # Stops an agent-trials run once its own cost (finished plus in-flight
    # trials) reaches this many USD; 0 disables it. The run is also capped by
    # whatever is left of LLM_BUDGET_USD this month.
    agent_run_budget_usd: float = 5.0
    # Seconds Harbor allows an agent to install itself before failing the trial
    # (its default is 360). 0 = default, except Codex, which needs 1200.
    agent_setup_timeout_sec: int = 0
    # Install Node and Codex while the task image builds (from nodejs.org and
    # the npm registry) so trials don't download them from GitHub, apt and npm
    # at run time; Harbor skips its own install once `codex` exists.
    codex_bake_into_image: bool = True
    codex_version: str = ""  # pin the Codex CLI; empty installs the latest each trial
    codex_web_search: str = ""  # disabled | cached | live; empty = Codex default
    # Spending cap for all priced LLM use (agent trials and OpenAI judge
    # calls) per calendar month (UTC); 0 disables it.
    llm_budget_usd: float = 50.0


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    (settings.storage_dir / "submissions").mkdir(parents=True, exist_ok=True)
    (settings.storage_dir / "quota").mkdir(parents=True, exist_ok=True)
    return settings
