"""Harbor's own classification of why a trial failed, so the platform stops guessing.

Harbor 0.26 sorts every exception type it knows into three flags (harbor/models/trial/error_policy.py):
  should_retry      worth running the trial again
  should_grade      the verifier should still run
  has_valid_reward  the reward, if any, can be trusted as the agent's result

This is a copy of that table, taken from Harbor 0.26.0 (an unknown type gets Harbor's default of True, True, True).
`tests/test_error_policy.py` compares it with the installed Harbor and fails when they differ, so refresh it then,
the way the vendored checks are refreshed. A trial Harbor says has no valid reward (cancelled, a missing reward file,
a verifier timeout and the like) says nothing about the agent: it is left out of the pass rate. A trial it grades as
valid although the agent exited non-zero (NonZeroAgentExitCodeError) keeps its reward, but the infra retry may still
rerun it when the log shows infrastructure noise (task_runner.infra_retry_reason).
"""
from __future__ import annotations

from dataclasses import dataclass

HARBOR_VERSION = "0.26.0"


@dataclass(frozen=True)
class Policy:
    should_retry: bool
    should_grade: bool
    has_valid_reward: bool
    known: bool = True


UNKNOWN = Policy(True, True, True, known=False)

_GROUPS: dict[tuple[bool, bool, bool], tuple[str, ...]] = {
    (True, False, False): (
        "AddTestsDirError",
        "AgentSetupTimeoutError",
        "DownloadVerifierDirError",
        "EnvironmentStartTimeoutError",
        "GKEExecStreamClosedError",
        "GKEPodNotFoundError",
        "HealthcheckError",
        "MemoryLimitExceededError",
        "RuntimeRequestError",
        "SSHAccessError",
        "SandboxBuildFailedError",
        "SandboxLikelyOutOfMemoryError",
    ),
    (True, True, False): (
        "APIConnectionError",
        "APIError",
        "ApiConnectionClosedError",
        "ApiConnectionError",
        "ApiError",
        "ApiInternalServerError",
        "ApiOverloadedError",
        "ApiRateLimitError",
        "ApiResponseStalledError",
        "ApiUsageLimitError",
        "InternalServerError",
        "NetworkConnectionError",
        "RateLimitError",
        "ServiceUnavailableError",
        "Timeout",
        "UnknownApiError",
    ),
    (False, True, False): (
        "APIResponseValidationError",
        "BadRequestError",
        "BudgetExceededError",
        "RewardFileEmptyError",
        "RewardFileNotFoundError",
        "UnprocessableEntityError",
        "UnsupportedModelParametersError",
        "UnsupportedParamsError",
        "VerifierOutputParseError",
        "VerifierTimeoutError",
    ),
    (False, False, False): (
        "AgentAuthenticationError",
        "ApiProviderResourceNotFoundError",
        "AuthenticationError",
        "CancelledError",
        "DspyImportError",
        "MissingExtraError",
        "ModelNotFoundError",
        "NotFoundError",
        "PermissionDeniedError",
        "RegradeError",
        "ServiceOperationsUnsupportedError",
        "VercelScopeError",
    ),
    (False, True, True): (
        "AgentSafetyRefusalError",
        "AgentTimeoutError",
        "ContentPolicyViolationError",
        "ContextLengthExceededError",
        "ContextWindowExceededError",
        "NonZeroAgentExitCodeError",
        "OutputLengthExceededError",
        "OutputTokenExceededError",
    ),
}

POLICIES: dict[str, Policy] = {
    name: Policy(*flags) for flags, names in _GROUPS.items() for name in names
}


def lookup(exception_type: str) -> Policy:
    return POLICIES.get(exception_type, UNKNOWN)


def from_exception_info(info: dict | None) -> dict | None:
    """The flags for a trial's `exception_info` (from result.json), or None when it has no exception type."""
    exception_type = (info or {}).get("exception_type")
    if not exception_type:
        return None
    p = lookup(str(exception_type))
    return {
        "exception_type": str(exception_type), "should_retry": p.should_retry, "should_grade": p.should_grade,
        "has_valid_reward": p.has_valid_reward, "known": p.known,
    }


def no_valid_reward_reason(info: dict | None) -> str | None:
    """Why this trial's reward cannot be used, when Harbor says so. An unknown exception type is not guessed at."""
    flags = from_exception_info(info)
    if flags and flags["known"] and not flags["has_valid_reward"]:
        return f"Harbor classes {flags['exception_type']} as having no valid reward"
    return None
