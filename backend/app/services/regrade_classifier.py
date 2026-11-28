"""NR-78: after a regrade, a model says whether each changed reward was expected.

Terminal-Bench's /regrade has an AI step that labels every reward change Expected, Unexpected or Unclear, so a grader
fix can be checked against saved runs without paying for new agent runs. Our regrade (task_runner.run_regrade) already
labelled each change (same, loosened, tightened, now scored, no score) but never judged whether it was expected.

Opt-in (`classify=true` on the endpoint), because it costs credits: one call per changed trial, at most MAX_CALLS,
sequential, recorded in the spend log under `regrade_classify`. Unchanged trials cost nothing. Advisory: the label is
evidence for a person, never a gate, and a call that fails or answers badly becomes `unclear`, not an error.

The model sees the trial's old and new reward, the tests that failed or went missing, and what changed in the task
between the two uploads (submission_diff.diff_tasks; "nothing changed" for a re-score of the same upload). It does not
see the verifier's code or the agent's transcript."""
from __future__ import annotations

import json
import re

from app.services.judge_guard import EVIDENCE_GUARD
from app.services.llm.base import Message
from app.services.llm.factory import get_llm_client

MAX_CALLS = 12
LABELS = ("expected", "unexpected", "unclear")

SYSTEM = (
    "You review one saved agent trial that was scored again after a benchmark task's grader (its tests) changed. "
    "You get the trial's reward before and after, the tests that failed or went missing, and what changed in the "
    "task between the two versions. Decide whether the change in reward is:\n"
    '  "expected"    it follows from the changes (a fixed or removed test, a changed threshold or instruction) '
    "and the new reward is the better result\n"
    '  "unexpected"  nothing in the changes explains it; a reward rose without a matching fix (the grader may have '
    "become easier); a test the saved run reported has disappeared; or the reward moved although nothing relevant "
    "changed\n"
    '  "unclear"     the evidence does not settle it\n'
    'Say "confidence": "high" only when the evidence settles it, otherwise "low". '
    + EVIDENCE_GUARD
    + '\nRespond with ONLY JSON: {"classification": "expected|unexpected|unclear", "confidence": "high|low", '
    '"reason": "one or two sentences naming the evidence"}'
)


def _clip(items, n: int) -> str:
    items = list(items or [])
    shown = ", ".join(str(i)[:120] for i in items[:n])
    return shown + (f" ... and {len(items) - n} more" if len(items) > n else "")


def build_evidence(item: dict, changes: dict | None) -> str:
    lines = [
        f"TRIAL: {item.get('trial')} (job {item.get('job')})",
        f"REWARD: {item.get('old_reward')} -> {item.get('new_reward')} ({item.get('change')})",
        f"TESTS REPORTED: before {item.get('old_tests')}, after {item.get('new_tests')}",
    ]
    if item.get("tests_missing"):
        lines.append("TESTS THE SAVED RUN REPORTED THAT THE REGRADED RUN DOES NOT: " + _clip(item["tests_missing"], 20))
    if item.get("failed_checks"):
        lines.append("FAILED CHECKS AFTER THE REGRADE: " + _clip(item["failed_checks"], 10))
    if item.get("new_grader_error"):
        lines.append(f"GRADER ERROR AFTER THE REGRADE: {item['new_grader_error']}"[:300])
    if item.get("flags"):
        lines.append("FLAGS: " + _clip([str(f)[:200] for f in item["flags"]], 3))
    if changes is None:
        lines.append(
            "TASK CHANGES: this is the same upload scored again, so nothing in the task changed; any difference "
            "comes from running the grader again."
        )
    else:
        words = changes.get("instruction_words") or {}
        lines += [
            "TASK CHANGES BETWEEN THE TWO UPLOADS:",
            "  files changed: " + (_clip(changes.get("changed"), 25) or "none"),
            "  files added: " + (_clip(changes.get("added"), 15) or "none"),
            "  files removed: " + (_clip(changes.get("removed"), 15) or "none"),
            "  tests removed: " + (_clip(changes.get("tests_removed"), 20) or "none"),
            "  tests added: " + (_clip(changes.get("tests_added"), 20) or "none"),
            f"  instruction words: {words.get('before')} -> {words.get('after')}",
        ]
    return "\n".join(lines)


def _json(text: str) -> dict:
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    try:
        data = json.loads(match.group(0)) if match else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _unclear(reason: str, error: str | None = None) -> dict:
    out = {"classification": "unclear", "confidence": "low", "reason": reason}
    if error:
        out["error"] = error
    return out


async def classify_item(item: dict, changes: dict | None) -> dict:
    """One trial's label. Never raises: a failed call or an unreadable answer is `unclear` with the reason."""
    try:
        client = get_llm_client()
        response = await client.complete(
            [Message(role="system", content=SYSTEM), Message(role="user", content=build_evidence(item, changes))],
            tools=[],
        )
    except Exception as exc:  # advisory: the regrade itself must never fail because of this
        return _unclear("the model could not be asked", f"{type(exc).__name__}: {exc}")
    data = _json(response.text)
    if not data:
        return _unclear("the model's answer could not be read", "the answer was not JSON")
    label = str(data.get("classification", "")).strip().lower()
    if label not in LABELS:
        return _unclear(str(data.get("reason") or "the model gave no usable label")[:300])
    confidence = "high" if data.get("confidence") == "high" else "low"
    return {"classification": label, "confidence": confidence, "reason": str(data.get("reason") or "")[:400]}


async def classify_changes(items: list[dict], changes: dict | None) -> list[dict]:
    """Add `classification` to each changed item (up to MAX_CALLS, one at a time). Items whose reward did not change
    are returned as they are; changed items beyond the limit are marked not classified."""
    out: list[dict] = []
    calls = 0
    for it in items:
        if it.get("change") == "same":
            out.append(it)
            continue
        if calls >= MAX_CALLS:
            out.append({**it, "classification": _unclear(f"not classified: only the first {MAX_CALLS} changed trials are sent to the model")})
            continue
        calls += 1
        out.append({**it, "classification": await classify_item(it, changes)})
    return out


def tally(items: list[dict]) -> dict[str, int]:
    return {label: sum(1 for i in items if (i.get("classification") or {}).get("classification") == label) for label in LABELS}
