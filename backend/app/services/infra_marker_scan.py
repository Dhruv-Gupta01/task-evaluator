"""Mechanical pre-pass over trial logs for known infra/network noise
markers — the same first step used manually all session before trusting an
agent-trial failure as "genuine": scan for connection/timeout/rate-limit
signatures before concluding the model actually failed to solve the task.
This is a plain substring/regex scan, no LLM — it hands review_report.py a
verified list of which trials show infra noise, rather than asking the
model to notice it buried in a wall of log text."""
import re

_INFRA_MARKERS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"ConnectTimeout", re.I), "connection timeout"),
    (re.compile(r"ReadTimeout", re.I), "read timeout"),
    (re.compile(r"RateLimitError", re.I), "rate limit error"),
    (re.compile(r"InternalServerError", re.I), "provider-side internal server error"),
    (re.compile(r"\bEAI_AGAIN\b"), "DNS resolution failure (EAI_AGAIN)"),
    (re.compile(r"ConnectionResetError|ConnectionRefusedError", re.I), "connection reset/refused"),
    (re.compile(r"503 Service Unavailable", re.I), "upstream 503"),
    # Agent setup downloads (Codex install via nvm/npm/apt) failing inside the trial
    # before the agent ran at all.
    (re.compile(r"NetworkConnectionError"), "network error while setting up the agent"),
    (
        re.compile(r"NonZeroAgentExitCodeError[\s\S]{0,300}apt-get (update|install)", re.I),
        "apt install failed while setting up the agent",
    ),
    (re.compile(r"no space left on device", re.I), "disk full on the host (no space left on device)"),
    (re.compile(r"litellm\.(Timeout|APIConnectionError)|APITimeoutError", re.I), "model request timed out or dropped"),
    (re.compile(r"Cannot connect to the Docker daemon|docker daemon is not running", re.I), "Docker daemon unreachable"),
    (re.compile(r"docker\.errors\.(ImageNotFound|APIError)", re.I), "Docker orchestration error (not a task-logic failure)"),
]


def scan_trial_logs(logs: str | None) -> list[str]:
    """Returns a list of human-readable infra-marker descriptions found in
    this trial's logs. Empty list means no known infra noise was detected —
    NOT proof the failure is genuine, just that it isn't one of these
    specific, well-known noise patterns."""
    if not logs:
        return []
    found = []
    for pattern, description in _INFRA_MARKERS:
        if pattern.search(logs):
            found.append(description)
    return found


# 5.0 E: failures caused by the machine or the image, not by the agent or the task's difficulty.
# Kept apart from _INFRA_MARKERS on purpose: those are retried once automatically, and
# re-running a trial cannot fix a wrong architecture or an old glibc.
_ENVIRONMENT_MARKERS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"no matching manifest for linux/\w+", re.I), "the base image has no build for this CPU architecture"),
    (re.compile(r"exec format error", re.I), "a binary built for another CPU architecture (exec format error)"),
    (re.compile(r"PackagesNotFoundError[\s\S]{0,400}(aarch64|arm64)|no matching distribution found[\s\S]{0,200}(aarch64|arm64)", re.I),
     "a package has no build for this CPU architecture (aarch64)"),
    (re.compile(r"GLIBC_[0-9.]+['\" ]* not found|GLIBC_ABI_\w+ not found", re.I),
     "the image's glibc is older than the agent harness needs"),
    (re.compile(r"API rate limit exceeded|rate limit exceeded for (?:url|ip)|secondary rate limit", re.I),
     "a download hit a rate limit (for example GitHub's 60 requests per hour)"),
    (re.compile(r"(?:403|429)[^\n]{0,80}(?:github|githubusercontent)|(?:github|githubusercontent)[^\n]{0,80}(?:403|429)", re.I),
     "GitHub refused a download (HTTP 403 or 429)"),
    (re.compile(r"connection refused[^\n]{0,120}(?:port|localhost|127\.0\.0\.1)|port \d+ (?:is )?not (?:ready|open)", re.I),
     "a service the task starts was not ready when it was used"),
]


def scan_environment_causes(logs: str | None) -> list[str]:
    """Environment-caused failure signatures (architecture, glibc, rate limits,
    service start-up). Same contract as scan_trial_logs: empty means none of
    these known patterns, not proof the failure is the agent's."""
    if not logs:
        return []
    return [d for pattern, d in _ENVIRONMENT_MARKERS if pattern.search(logs)]
