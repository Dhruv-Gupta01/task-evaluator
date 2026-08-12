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
