"""Final candidate-facing diagnostic report: synthesizes the platform report
(Build/Oracle/Nop/Sufficiency/Agent Trials, already computed) with the
mechanical static checks and infra-marker scan (also already computed) into
one report — genuine failure vs. platform/infra noise, and what a candidate
should actually change. Mirrors the manual review workflow used all session:
verify the mechanical facts first, then reason only over what's left.

Advisory only, same as Sufficiency and Leakage Scan — a draft for human
review before anything goes to a candidate, not an auto-issued verdict."""
import json
from pathlib import Path

from app.services.infra_marker_scan import scan_trial_logs
from app.services.llm.base import Message
from app.services.llm.factory import get_llm_client
from app.services.static_checks import run_static_checks
from app.services.submission_service import to_schema

MAX_TRIAL_LOG_CHARS = 4_000
MAX_PASSING_TRIAL_SUMMARY_CHARS = 300
MAX_INSTRUCTION_CHARS = 6_000

REQUIRED_TERMINAL_GATES = ("build", "oracle", "nop", "sufficiency", "agent_trials")
_TERMINAL_STATUSES = {"passed", "failed"}

SYSTEM_PROMPT = """You are a senior reviewer producing a diagnostic report on a submitted \
benchmark task, for two audiences: the internal reviewer deciding whether the task is sound, \
and (in a final section) the candidate who submitted it.

You will be given ALREADY-VERIFIED facts: gate-by-gate platform results (Build/Oracle/Nop/\
Sufficiency/Agent Trials), mechanical static-check results (word counts, formatting, \
Dockerfile/test.sh hygiene), and an infra-noise marker scan per agent trial. Do NOT recompute \
or second-guess these — treat them as ground truth. Your job is the reasoning that requires \
actual judgment:

1. For each failing gate, classify it as one of: GENUINE (a real task/candidate-zip issue), \
PLATFORM NOISE (the infra-marker scan or logs show connection/timeout/rate-limit/Docker \
orchestration errors unrelated to task logic), or UNCLEAR (say so plainly, don't guess).
2. For agent-trial failures specifically: read the actual failure content (assertion diffs, \
error text) and distinguish real reasoning/logic gaps from incidental tooling/syntax friction \
(e.g. a missing language keyword, an unsupported tool call) — these look different and matter \
differently for judging task difficulty.
3. Write a short "what to fix" section addressed directly to the candidate: concrete, grounded \
in the actual failures you read — not generic advice.

If a gate hasn't run yet, say so and skip judging it — never fabricate a result for it.

Structure your response as markdown with these sections, in order:
## Verdict
One or two sentences: is this task sound, does it need revision, or is something broken.
## Gate-by-gate
A short table or list: gate name, result, genuine/platform-noise/unclear classification with a \
one-line reason.
## Static checks
Summarize anything from the mechanical checks worth a human's attention — skip anything that's \
just "info"/passing.
## Agent trial diagnosis
Only if agent trials ran: per failing trial (or grouped if the same root cause repeats), what \
actually went wrong, and whether it's genuine difficulty or noise.
## What to fix (candidate-facing)
Direct, second-person, concrete. What's already good, then what to change and why."""


def _trial_section(trials: list, kind_label: str) -> str:
    parts = []
    for t in trials:
        markers = scan_trial_logs(t.logs)
        marker_note = f" [infra markers: {', '.join(markers)}]" if markers else " [no infra markers detected]"
        if t.reward == 1:
            excerpt = (t.logs or "")[-MAX_PASSING_TRIAL_SUMMARY_CHARS:]
            parts.append(f"### Trial {t.index} — PASSED{marker_note}\n{excerpt}")
        else:
            excerpt = (t.logs or "")[-MAX_TRIAL_LOG_CHARS:]
            parts.append(f"### Trial {t.index} — FAILED{marker_note}\n{excerpt}")
    return "\n\n".join(parts) if parts else f"No {kind_label} trials recorded."


def _read_instruction(task_root: Path) -> str:
    path = task_root / "instruction.md"
    if not path.is_file():
        return "(instruction.md not found)"
    text = path.read_text(errors="replace")
    if len(text) > MAX_INSTRUCTION_CHARS:
        text = text[:MAX_INSTRUCTION_CHARS] + "\n... [truncated]"
    return text


def all_gates_terminal(submission) -> tuple[bool, list[str]]:
    """Returns (ready, missing_gate_names). A gate counts as terminal once
    it has a passed/failed status; agent_trials counts once its aggregate
    status (built from all N trials) is passed/failed, not per-trial."""
    schema = to_schema(submission)
    missing = []
    if schema.build.status not in _TERMINAL_STATUSES:
        missing.append("build")
    if schema.oracle.status not in _TERMINAL_STATUSES:
        missing.append("oracle")
    if schema.nop.status not in _TERMINAL_STATUSES:
        missing.append("nop")
    if schema.sufficiency.status not in _TERMINAL_STATUSES:
        missing.append("sufficiency")
    if schema.agent_trials.status not in _TERMINAL_STATUSES:
        missing.append("agent_trials")
    return (len(missing) == 0, missing)


async def run_review_report(submission, task_root: Path) -> dict:
    schema = to_schema(submission)
    static_report = run_static_checks(task_root)

    platform_summary = json.dumps(
        {
            "build": {"status": schema.build.status},
            "oracle": {"status": schema.oracle.status, "reward": schema.oracle.reward},
            "nop": {"status": schema.nop.status, "reward": schema.nop.reward},
            "sufficiency": {"status": schema.sufficiency.status, "passed": schema.sufficiency.passed},
            "agent_trials": {
                "status": schema.agent_trials.status,
                "n": schema.agent_trials.n,
                "pass_rate": schema.agent_trials.pass_rate,
            },
        },
        indent=2,
    )

    user_content = (
        "=== PLATFORM REPORT (verified) ===\n"
        "Note on \"nop\": its displayed status is intentionally INVERTED. Nop runs the verifier "
        "against an unsolved/empty workdir, so the real tests are EXPECTED to fail there — a "
        "failing test suite in the Nop logs is the correct, healthy outcome, not a contradiction "
        "to explain away. \"nop.status: passed\" means the tests correctly failed against nothing; "
        "\"nop.status: failed\" would mean doing nothing somehow passed the real tests, which is "
        "the actual problem case. Explain it this way if you reference it — don't tell the "
        "candidate to \"ignore\" the failing output, since that failure IS the pass condition.\n\n"
        f"{platform_summary}\n\n"
        "=== STATIC CHECKS (verified, mechanical) ===\n"
        f"{static_report.as_text()}\n\n"
        "=== instruction.md ===\n"
        f"{_read_instruction(task_root)}\n\n"
        "=== ORACLE verify-phase logs (tail) ===\n"
        f"{(schema.oracle.logs or '')[-MAX_TRIAL_LOG_CHARS:]}\n\n"
        "=== NOP verify-phase logs (tail) ===\n"
        f"{(schema.nop.logs or '')[-MAX_TRIAL_LOG_CHARS:]}\n\n"
        "=== SUFFICIENCY verdict ===\n"
        f"{schema.sufficiency.logs or '(no verdict text)'}\n\n"
        "=== AGENT TRIALS (per trial, infra markers pre-tagged) ===\n"
        f"{_trial_section(schema.agent_trials.trials, 'agent')}\n"
    )

    client = get_llm_client()
    response = await client.complete(
        [
            Message(role="system", content=SYSTEM_PROMPT),
            Message(role="user", content=user_content),
        ],
        tools=[],
    )

    return {
        "report_markdown": response.text,
        "static_fail_count": static_report.fail_count,
        "static_warn_count": static_report.warn_count,
    }
