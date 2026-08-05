from pydantic import BaseModel

from app.config import get_settings

settings = get_settings()


class EnvironmentConfig(BaseModel):
    cpus: float = settings.default_cpus
    memory_mb: int = settings.default_memory_mb
    allow_internet: bool = settings.default_allow_internet
    build_timeout_sec: float = 600.0


class AgentConfig(BaseModel):
    timeout_sec: int = settings.default_agent_timeout_sec


class VerifierConfig(BaseModel):
    timeout_sec: int = settings.default_verifier_timeout_sec


class TaskConfig(BaseModel):
    environment: EnvironmentConfig = EnvironmentConfig()
    agent: AgentConfig = AgentConfig()
    verifier: VerifierConfig = VerifierConfig()

    @classmethod
    def from_toml_dict(cls, data: dict) -> "TaskConfig":
        return cls(
            environment=EnvironmentConfig(**data.get("environment", {})),
            agent=AgentConfig(**data.get("agent", {})),
            verifier=VerifierConfig(**data.get("verifier", {})),
        )
