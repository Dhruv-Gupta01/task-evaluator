"""The Tier 1 loop. Pure orchestration: the caller supplies the four things that touch Docker or
an LLM (run an attempt, classify it, propose a fix, check a patched copy), so the rules here can be
tested with fakes.

Rules (decided with the user):
  * every attempt is a hit or a miss (memory.is_hit); 3 misses in a row end the stage "robust";
  * a hit (exploited, or a reward 1 the judge could not clear) resets the counter, asks the fixer
    for a patch to a hardened copy, and the loop continues against the hardened copy, so the loop
    only goes on while hacks are being found and patched;
  * a patch is only accepted when the Oracle still scores 1 on it (`gate`); a rejected patch gets
    one more try, then the loop stops "unfixed" for a human;
  * there is no attempt cap: the number of accepted patches (`max_fixes`) and the spend cap bound
    the loop."""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from . import memory

FIX_TRIES = 2


@dataclass
class Limits:
    max_fixes: int = 5
    max_cost_usd: float = 0.0  # 0 = no cap beyond the per-attempt ones


@dataclass
class Hooks:
    run_attempt: Callable[[Path, str, int], Awaitable[dict]]
    classify: Callable[[dict, str], Awaitable[dict | None]]
    propose_fix: Callable[[Path, dict, str], Awaitable[dict | None]]
    apply_fix: Callable[[Path, dict], str | None]  # edit a directory in place; None or why not
    gate: Callable[[Path], Awaitable[tuple[bool, str]]]
    progress: Callable[[dict], None] = lambda state: None
    # After the Oracle gate: re-grade the attempt's own output on the patched copy. (False, why)
    # rejects a patch that still lets the earlier exploit score 1. None = not available.
    replay: Callable[[Path, dict], Awaitable[tuple[bool, str]]] | None = None


async def run(hardened: Path, scratch: Path, hooks: Hooks, limits: Limits) -> dict:
    """`hardened` starts as a copy of the upload and is edited in place by accepted fixes."""
    attempts: list[dict] = []
    fixes: list[dict] = []
    status, reason = "robust", ""
    cost = 0.0
    by = {"hacker": 0.0, "judge": 0.0, "fixer": 0.0}

    def snapshot(in_progress: bool = True) -> dict:
        return {
            "in_progress": in_progress, "status": None if in_progress else status, "reason": None if in_progress else reason,
            "attempts": attempts, "fixes": fixes, "consecutive_misses": memory.consecutive_misses(attempts),
            "cost_usd": round(cost, 4), "cost_breakdown_usd": {k: round(v, 4) for k, v in by.items()},
        }

    while True:
        if limits.max_cost_usd and cost >= limits.max_cost_usd:
            status, reason = "budget", f"spend reached the cap of ${limits.max_cost_usd:.2f}"
            break

        index = len(attempts) + 1
        mem = memory.render_memory(attempts, fixes)
        outcome = await hooks.run_attempt(hardened, mem, index)
        by["hacker"] += float(outcome.get("cost_usd") or 0.0)
        judged = await hooks.classify(outcome, mem)
        by["judge"] += float((judged or {}).get("cost_usd") or 0.0)
        cost = sum(by.values())
        verdict = memory.derive_verdict(outcome.get("reward"), outcome.get("tool_calls"), bool(outcome.get("refused")), judged)
        attempt = {
            "index": index, "verdict": verdict, "reward": outcome.get("reward"), "tool_calls": outcome.get("tool_calls"),
            "cost_usd": outcome.get("cost_usd"), "judge_cost_usd": (judged or {}).get("cost_usd"), "summary": (judged or {}).get("summary") or outcome.get("note") or "",
            "judge_error": (judged or {}).get("error"),
            "techniques": (judged or {}).get("techniques") or [], "defences": (judged or {}).get("defences") or [],
            "judge_label": (judged or {}).get("verdict"), "exploit_kind": (judged or {}).get("exploit_kind"), "judge_reasoning": (judged or {}).get("reasoning"), "judge_confidence": (judged or {}).get("confidence"),
            "trial_dir": outcome.get("trial_dir"), "refusal": outcome.get("refusal"), "note": outcome.get("note"),
            "memory_given": mem,  # the notes this attempt's agent was shown (empty for the first)
            "against_fix": len(fixes),  # how many patches the task had when this attempt ran
        }
        attempts.append(attempt)
        hooks.progress(snapshot())

        if not memory.is_hit(verdict):
            if memory.consecutive_misses(attempts) >= memory.MISS_LIMIT:
                status, reason = "robust", f"{memory.MISS_LIMIT} attempts in a row did not get reward 1 through a shortcut"
                break
        else:
            if len(fixes) >= limits.max_fixes:
                status, reason = "unfixed", f"still being exploited after {limits.max_fixes} patches"
                break
            fix = await _make_fix(hardened, scratch, attempt, mem, hooks, len(fixes) + 1)
            by["fixer"] += fix.get("cost_usd", 0.0)
            cost = sum(by.values())
            if not fix.get("accepted"):
                attempt["fix_failed"] = fix.get("note")
                if fix.get("unpatchable"):
                    status, reason = "unpatchable", f"the fixer says the exploit in attempt {index} cannot be patched: {fix['unpatchable']}"
                else:
                    status, reason = "unfixed", f"no accepted patch for the exploit in attempt {index}: {fix.get('note')}"
                fixes.append({**fix, "after_attempt": index, "accepted": False})
                break
            fixes.append({**fix, "after_attempt": index})
        hooks.progress(snapshot())

    return snapshot(in_progress=False)


async def _make_fix(hardened: Path, scratch: Path, attempt: dict, mem: str, hooks: Hooks, number: int) -> dict:
    """Ask for a patch, try it on a copy, gate it, and only then edit `hardened` for real."""
    notes: list[str] = []
    cost = 0.0
    for _ in range(FIX_TRIES):
        proposal = await hooks.propose_fix(hardened, attempt, "\n".join(notes))
        cost += float((proposal or {}).get("cost_usd") or 0.0)
        if (proposal or {}).get("unpatchable"):
            return {"accepted": False, "unpatchable": proposal["unpatchable"], "number": number, "cost_usd": cost, "edits": [], "description": "", "note": proposal["unpatchable"]}
        if not proposal or not proposal.get("edits"):
            notes.append((proposal or {}).get("error") or "the fixer proposed no edits")
            continue
        candidate = scratch / f"candidate-{number}"
        if candidate.exists():
            shutil.rmtree(candidate)
        shutil.copytree(hardened, candidate, symlinks=True)
        problem = hooks.apply_fix(candidate, proposal)
        if problem:
            notes.append(f"the edits could not be applied: {problem}")
            shutil.rmtree(candidate, ignore_errors=True)
            continue
        ok, why = await hooks.gate(candidate)
        if not ok:
            notes.append(f"the patched task failed the Oracle gate: {why}")
            shutil.rmtree(candidate, ignore_errors=True)
            continue
        if hooks.replay is not None and attempt.get("exploit_kind") == "copied_answers":
            # The earlier output carries the answers inside itself, so it keeps scoring 1 however
            # well they are now hidden; only a new attempt, which cannot read them any more, tests this.
            why = f"{why}; replay skipped: the earlier output already holds the answers, so the next attempt tests the patch"
        elif hooks.replay is not None:
            replay_ok, replay_why = await hooks.replay(candidate, attempt)
            if not replay_ok:
                notes.append(f"the patched task still lets the earlier exploit through: {replay_why}")
                shutil.rmtree(candidate, ignore_errors=True)
                continue
            why = f"{why}; replay: {replay_why}"
        problem = hooks.apply_fix(hardened, proposal)
        shutil.rmtree(candidate, ignore_errors=True)
        if problem:  # applied cleanly to the copy a moment ago, so this is a surprise worth showing
            notes.append(f"the edits applied to the copy but not to the task: {problem}")
            continue
        return {
            "accepted": True, "number": number, "description": proposal.get("description", ""),
            "edits": proposal["edits"], "hash_answers": proposal.get("hash_answers") or [], "gate": why, "cost_usd": cost,
        }
    return {"accepted": False, "number": number, "note": "; ".join(notes) or "no proposal", "cost_usd": cost, "edits": [], "description": ""}
