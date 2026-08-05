from datetime import datetime
from typing import Literal

from pydantic import BaseModel

StageStatus = Literal["pending", "running", "passed", "failed", "not-run"]


class StageResult(BaseModel):
    status: StageStatus
    reward: int | None = None  # 0 | 1 | null
    logs: str | None = None


class BuildResult(BaseModel):
    status: StageStatus
    logs: str | None = None
    image_tag: str | None = None


class TrialResult(BaseModel):
    index: int
    reward: int | None = None
    logs: str | None = None


class AgentTrialsResult(BaseModel):
    status: StageStatus
    n: int
    trials: list[TrialResult]
    pass_rate: float | None = None


class SufficiencyResult(BaseModel):
    status: StageStatus
    passed: bool | None = None
    logs: str | None = None


class SubmissionSchema(BaseModel):
    id: str
    task_name: str
    uploaded_at: datetime
    build: BuildResult
    oracle: StageResult
    nop: StageResult
    agent_trials: AgentTrialsResult
    sufficiency: SufficiencyResult


class SubmissionListItem(BaseModel):
    id: str
    task_name: str
    uploaded_at: datetime
    build_status: StageStatus
    oracle_status: StageStatus
    nop_status: StageStatus
    agent_status: StageStatus
    sufficiency_status: StageStatus


class UploadResponse(BaseModel):
    id: str
