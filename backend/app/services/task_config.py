from pydantic import BaseModel

from app.config import get_settings

settings = get_settings()


class EnvironmentConfig(BaseModel):
    cpus: float = settings.default_cpus
    memory_mb: int = settings.default_memory_mb
    # What the task file declares about the network; None = not declared. Harbor's default is open
    # (public), Terminal-Bench tells authors to leave these out, and `allow_internet` is deprecated in
    # favour of `network_mode`. Nothing here is the policy a run used; see services/network_policy.py.
    allow_internet: bool | None = None
    network_mode: str | None = None
    allowed_hosts: list[str] | None = None
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
