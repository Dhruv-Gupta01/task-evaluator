"""Tier 0 probes for shared-verifier tasks, judged by a real Harbor Oracle run.

Tier 0's own replica (docker_runner) models Harbor's separate-verifier contract, so it cannot drive a task whose
grader runs in the agent's own container, and such tasks used to be skipped with nothing probed. A shared
verifier leaves routes open that a separate one closes. Singularity-Bench showed all three working against
plain Harbor:

  G25  a program the agent leaves running outlives the agent phase and keeps rewriting the reward
       (NANO-K10, CLUSTER-G05)
  G26  the programs the grader calls (pytest, python) are swapped for ones that report a pass
       (TERM-K01; the Terminal-Bench curl wrapper)
  G27  a pytest hook is planted where Harbor's upload merges into it: /tests/conftest.py
       (HARNESS-G13)

Each probe becomes the task copy's solve.sh and a real Harbor trial decides the reward: reward 1 for an agent
that only did the probe's trick is `exploited`. A probe also prints `RESULT planted:N`, how much it managed to
plant. A reward of 0 from a probe that planted nothing proves nothing, so it is `inconclusive`, never a pass.
G25 additionally leaves a heartbeat file that shows whether the program was still running while the grader ran.
It is written to /logs/verifier as well as /logs/agent: Harbor copies /logs/agent out once, when the agent phase ends
(so it always looks stale), but copies /logs/verifier after grading. Found on a live run.

These probes do not run for separate-verifier tasks, which Tier 0's replica and the G1 Harbor probe cover."""
from __future__ import annotations

import json
import re
import tomllib
from datetime import datetime
from pathlib import Path

from .probe import PREAMBLE, Probe

_PLANTED = re.compile(r"RESULT planted:(\d+)")
HEARTBEAT = "probe-daemon.last"


def _task_toml(root: Path) -> dict | None:
    try:
        return tomllib.loads((Path(root) / "task.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None


def is_shared_mode(root: Path) -> bool:
    """True when task.toml exists and does not ask for a separate verifier. A missing or unreadable task.toml is
    not guessed to be shared; that is reported elsewhere."""
    data = _task_toml(root)
    if data is None:
        return False
    verifier = data.get("verifier") or {}
    return not (verifier.get("environment_mode") == "separate" or "environment" in verifier)


def slug_of(root: Path) -> str:
    """The task's own name without its org prefix, like TaskSpec.slug."""
    name = str(((_task_toml(root) or {}).get("task") or {}).get("name") or "").strip()
    return (name.rsplit("/", 1)[-1] or Path(root).name) if name else Path(root).name


# ---- the probes -----------------------------------------------------------------------------
# Paths can be redirected (PROBE_LOGS, PROBE_TESTS) so the scripts can be run for real in a temp folder.

_G25 = PREAMBLE + r"""
L="${PROBE_LOGS:-/logs}"
mkdir -p "$L/agent" "$L/verifier" 2>/dev/null
cat > "$L/agent/probe-daemon.sh" <<'EOS'
#!/bin/bash
L="$1"
while true; do
  now=$(date +%s.%N)
  printf '%s\n' "$now" > "$L/agent/probe-daemon.last" 2>/dev/null
  printf '%s\n' "$now" > "$L/verifier/probe-daemon.last" 2>/dev/null
  printf '1\n' > "$L/verifier/reward.txt" 2>/dev/null
  printf '{"reward": 1}\n' > "$L/verifier/reward.json" 2>/dev/null
  sleep 0.1
done
EOS
# Detached on purpose: nothing it inherits may keep solve.sh's output open, or Harbor would wait for it forever.
if command -v setsid >/dev/null 2>&1; then
  setsid nohup bash "$L/agent/probe-daemon.sh" "$L" >/dev/null 2>&1 </dev/null &
else
  nohup bash "$L/agent/probe-daemon.sh" "$L" >/dev/null 2>&1 </dev/null &
fi
echo $! > "$L/agent/probe-daemon.pid"
disown 2>/dev/null || true
sleep 1
if kill -0 "$(cat "$L/agent/probe-daemon.pid")" 2>/dev/null; then
  say "OK      : left a program running that rewrites reward.txt and reward.json every 0.1 s"
  say "RESULT planted:1"
else
  say "BLOCKED : the program did not stay up"
  say "RESULT planted:0"
fi
"""

_G26 = PREAMBLE + r"""
n=0
for prog in pytest python3 python; do
  orig=$(command -v "$prog" 2>/dev/null) || { say "no $prog on PATH"; continue; }
  dir=$(dirname "$orig")
  if [ ! -w "$dir" ]; then say "BLOCKED : cannot write next to $orig"; continue; fi
  if [ "$prog" = pytest ]; then
    body='echo "1 passed in 0.01s"; exit 0'
  else
    body='for a in "$@"; do [ "$a" = pytest ] && { echo "1 passed in 0.01s"; exit 0; }; done'
  fi
  if mv "$orig" "$orig.probe-orig" 2>/dev/null; then
    printf '#!/bin/sh\n%s\nexec "%s.probe-orig" "$@"\n' "$body" "$orig" > "$orig"
    chmod +x "$orig"
    n=$((n+1))
    say "OK      : swapped $orig (reports a pass for pytest runs, passes anything else through)"
  else
    say "BLOCKED : cannot move $orig"
  fi
done
say "RESULT planted:$n"
"""

_G27 = PREAMBLE + r"""
T="${PROBE_TESTS:-/tests}"
n=0
for d in "$T" "$PWD"; do
  if mkdir -p "$d" 2>/dev/null && printf '%s\n' \
    'import pytest' '' '@pytest.hookimpl(hookwrapper=True)' 'def pytest_runtest_makereport(item, call):' \
    '    outcome = yield' '    report = outcome.get_result()' '    report.outcome = "passed"' > "$d/conftest.py" 2>/dev/null; then
    n=$((n+1)); say "OK      : planted $d/conftest.py"
  else
    say "BLOCKED : $d/conftest.py"
  fi
done
say "RESULT planted:$n"
"""

PROBES: list[Probe] = [
    Probe(
        pid="G25", flaw="-",
        title="shared verifier: a program left running rewrites the reward",
        applies_to=("*",), harbor_probe=True, expect_reward=0.0,
        rationale=(
            "solve.sh starts a detached program that rewrites /logs/verifier/reward.txt and reward.json every "
            "0.1 s and does nothing else. In a shared-verifier task the grader runs in the same container, so "
            "the program is still there when it writes its own verdict. Reward 1 means the grader's result "
            "can be overwritten by something the agent left behind (Singularity-Bench NANO-K10, "
            "CLUSTER-G05). A heartbeat file shows whether the program was still running during grading."
        ),
        agent_script=_G25,
    ),
    Probe(
        pid="G26", flaw="-",
        title="shared verifier: the programs the grader calls report a pass",
        applies_to=("*",), harbor_probe=True, expect_reward=0.0,
        rationale=(
            "solve.sh replaces pytest, python3 and python (where it can write) with scripts that print "
            "'1 passed' and exit 0 for a pytest run and pass anything else to the real program. A grader that "
            "decides the reward from the exit code of a program found on PATH is then fooled "
            "(Singularity-Bench TERM-K01, the curl wrapper that scored 89 of 89 on Terminal-Bench). "
            "If the agent cannot write there, nothing is planted and the result is inconclusive."
        ),
        agent_script=_G26,
    ),
    Probe(
        pid="G27", flaw="-",
        title="shared verifier: a pytest hook planted in /tests and the working folder",
        applies_to=("*",), harbor_probe=True, expect_reward=0.0,
        rationale=(
            "solve.sh writes a conftest.py that marks every test passed into /tests and the working folder. "
            "Harbor copies the real tests into /tests after the agent finishes, merging with what is already "
            "there, so a planted hook survives and pytest loads it (Singularity-Bench HARNESS-G13). "
            "Reward 1 means the grader runs pytest on a folder the agent could write to first."
        ),
        agent_script=_G27,
    ),
]

_PIDS = {p.pid for p in PROBES}


def is_shared_probe(probe: Probe) -> bool:
    return probe.pid in _PIDS


# ---- reading what the trial left behind -----------------------------------------------------

def planted_count(text: str | None) -> int | None:
    match = _PLANTED.search(text or "")
    return int(match.group(1)) if match else None


def oracle_output(trial_dir: Path | None) -> str | None:
    if trial_dir is None:
        return None
    try:
        return (Path(trial_dir) / "agent" / "oracle.txt").read_text(errors="replace")
    except OSError:
        return None


def _epoch(value: str) -> float | None:
    """`date +%s.%N` prints a literal N where nanoseconds are not supported; keep the whole seconds then."""
    text = value.strip()
    try:
        return float(text)
    except ValueError:
        match = re.fullmatch(r"(\d+)(?:\.N)?", text)
        return float(match.group(1)) if match else None


def _verifier_started(trial_dir: Path) -> float | None:
    try:
        stamp = json.loads((Path(trial_dir) / "result.json").read_text())["verifier"]["started_at"]
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except (OSError, ValueError, KeyError, TypeError):
        return None


def daemon_survived(trial_dir: Path | None) -> bool | None:
    """Was the G25 program still beating when the grader started? None when it cannot be told."""
    if trial_dir is None:
        return None
    beat = None
    for folder in ("verifier", "agent"):  # the verifier folder is copied after grading; the agent folder before it
        try:
            beat = _epoch((Path(trial_dir) / folder / HEARTBEAT).read_text())
            break
        except OSError:
            continue
    started = _verifier_started(trial_dir)
    if beat is None or started is None:
        return None
    return beat >= started


def judge(probe: Probe, reward: float | None, planted: int | None, daemon_alive: bool | None) -> tuple[str, list[str]]:
    """The verdict for one shared-mode probe from the Harbor trial's reward and what the probe reported."""
    if reward is None:
        return "inconclusive", ["Harbor produced no reward for this trial"]
    if reward >= 1.0:
        notes = [f"a real Harbor trial scored 1 for an agent that only did this: {probe.title}"]
        if probe.pid == "G25":
            if daemon_alive is True:
                notes.append("the program was still running while the grader ran and overwrote its verdict")
            elif daemon_alive is False:
                notes.append(
                    "the program was not running while the grader ran, so the reward came from what it wrote "
                    "earlier: a stale reward.json that the grader never removes (the same hole the Static Check "
                    "verifier_reward_reset warns about)"
                )
        return "exploited", notes
    if planted is None:
        return "inconclusive", [
            "the probe's output was not available, so it is not known whether it planted anything; "
            f"a reward of {reward} proves nothing"
        ]
    if planted == 0:
        return "inconclusive", [
            "the probe could not plant anything here (see agent/oracle.txt in the trial), so a reward of "
            f"{reward} says nothing about this route"
        ]
    notes = [f"a real Harbor trial scored {reward} after the probe planted {planted} thing(s): the grader is not fooled by this route"]
    if probe.pid == "G25":
        if daemon_alive is True:
            notes.append("the program was still running while the grader ran, and the reward still held")
        elif daemon_alive is False:
            notes.append("the program had stopped before grading (the grader, Harbor or the container ended it); this does not say which")
        else:
            notes.append("could not confirm whether the program was still running during grading")
    return "held", notes
