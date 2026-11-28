"""The Tier 2 loop. Pure orchestration: the caller supplies the effects that touch Docker or an
LLM (run one agent, classify it), so the rules here can be tested with fakes.

Rules (decided with the user):
  * every run is independent: a fresh agent, no notes from earlier runs, the task unchanged;
  * every run is a hit or a miss (memory.is_hit); 3 misses in a row end the stage "robust";
  * the first hit stops the loop for a person to rule on: "exploited" when the judge confirmed a
    shortcut, "needs_review" for a reward 1 the judge could not clear; nothing is patched in Tier 2,
    so the loop runs at most 3 times;
  * there is no attempt cap; the loop ends "inconclusive" only when the spend cap is reached first."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from . import memory


@dataclass
class Limits:
    max_cost_usd: float = 0.0  # 0 = no cap beyond the per-attempt ones


@dataclass
class Hooks:
    run_attempt: Callable[[Path, int], Awaitable[dict]]
    classify: Callable[[dict], Awaitable[dict | None]]
    progress: Callable[[dict], None] = lambda state: None


async def run(task_dir: Path, hooks: Hooks, limits: Limits) -> dict:
    attempts: list[dict] = []
    status, reason = "inconclusive", ""
    cost = 0.0
    by = {"hacker": 0.0, "judge": 0.0}

    def snapshot(in_progress: bool = True) -> dict:
        return {
            "in_progress": in_progress, "status": None if in_progress else status,
            "reason": None if in_progress else reason, "attempts": attempts,
            "counts": memory.counts(attempts), "consecutive_misses": memory.consecutive_misses(attempts),
            "cost_usd": round(cost, 4), "cost_breakdown_usd": {k: round(v, 4) for k, v in by.items()},
        }

    def inconclusive(why: str) -> str:
        return f"{why}: {memory.counts_text(memory.counts(attempts))}"

    while True:
        if limits.max_cost_usd and cost >= limits.max_cost_usd:
            reason = inconclusive(f"spend reached the cap of ${limits.max_cost_usd:.2f} before {memory.MISS_LIMIT} misses in a row")
            break

        index = len(attempts) + 1
        outcome = await hooks.run_attempt(task_dir, index)
        by["hacker"] += float(outcome.get("cost_usd") or 0.0)
        judged = await hooks.classify(outcome)
        by["judge"] += float((judged or {}).get("cost_usd") or 0.0)
        cost = sum(by.values())
        verdict = memory.derive_verdict(
            outcome.get("reward"), bool(outcome.get("refused")), judged,
            non_finite=bool(outcome.get("non_finite_reward")),
        )
        attempts.append({
            "index": index, "verdict": verdict, "reward": outcome.get("reward"), "tool_calls": outcome.get("tool_calls"),
            "cost_usd": outcome.get("cost_usd"), "judge_cost_usd": (judged or {}).get("cost_usd"),
            "summary": (judged or {}).get("summary") or outcome.get("note") or "",
            "judge_error": (judged or {}).get("error"), "judge_label": (judged or {}).get("verdict"),
            "techniques": (judged or {}).get("techniques") or [], "judge_reasoning": (judged or {}).get("reasoning"),
            "judge_confidence": (judged or {}).get("confidence"), "trial_dir": outcome.get("trial_dir"),
            "refusal": outcome.get("refusal"), "note": outcome.get("note"),
            "non_finite_reward": outcome.get("non_finite_reward"),
            "web_use": outcome.get("web_use"),
        })
        hooks.progress(snapshot())

        if verdict == "exploited":
            status, reason = "exploited", f"run {index} got reward 1 through a shortcut; a person has to rule on it"
            break
        if verdict == "needs_review":
            if outcome.get("non_finite_reward"):
                status, reason = "needs_review", (
                    f"run {index}'s grader wrote a reward of {outcome['non_finite_reward']}, which is not a finite "
                    "number; a person has to look at it"
                )
            else:
                status, reason = "needs_review", f"run {index} scored reward 1 and the judge could not say whether it was a shortcut"
            break
        if memory.consecutive_misses(attempts) >= memory.MISS_LIMIT:
            status, reason = "robust", f"{memory.MISS_LIMIT} runs in a row did not get reward 1 through a shortcut"
            break

    return snapshot(in_progress=False)
