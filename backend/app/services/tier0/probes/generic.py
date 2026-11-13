"""Generic probes: attack the harness contract, not the task.

These apply to every separate-verifier task in the suite, so they are written once and
amortised. Build them first.

The single most important thing they establish: **a narrow, explicit top-level
`artifacts` list is the strongest defence both reviewed tasks have.** Most classic
exploits (planted conftest.py, PATH shadowing, sitecustomize.py, pre-written reward
files) die there, because the file simply never travels to the verifier container. That
is worth demonstrating rather than assuming, which is what G2 through G6 do.

What survives the allowlist is whatever rides *inside* an allowed artifact. That is the
real surface, and it is task-specific: see probes/harrow.py and probes/pgwal.py.
"""
from __future__ import annotations

from ..docker_runner import SPECIAL_KINDS
from ..probe import PREAMBLE, Probe


def _must_not_reach(paths: list[str]):
    """Verdict override: exploited if any of these appeared in the verifier container."""
    def check(run, spec):
        leaked = [p for p in paths if run.diagnostics.get(p)]
        if leaked:
            return "exploited", [f"LEAKED into verifier: {p}" for p in leaked]
        return None, [f"confirmed absent from verifier: {', '.join(paths)}"]
    return check


_GROUND_TRUTH_PATHS = ("/tests", "/solution", "/logs/verifier", "/app/task", "/opt/solution")


def _ground_truth_visible(run, spec):
    """Exploited if any ground-truth location exists inside the agent's own container.

    Read from the probe's one-line RESULT, not from the full output, because the output is
    tail-truncated and the listing that follows can push the early lines out.
    """
    line = next((l for l in run.agent_stdout.splitlines() if "RESULT present:" in l), None)
    if line is None:
        return "inconclusive", ["the probe did not report which paths exist"]
    present = [p for p in line.split("RESULT present:", 1)[1].split() if p in _GROUND_TRUTH_PATHS]
    if present:
        return "exploited", [f"ground truth is reachable from the agent's container: {', '.join(present)}"]
    return None, ["/tests, /solution and /logs/verifier are absent from the agent's container"]


def _special_file_outcome(run, spec):
    """Judge a FIFO artifact by what it did, not by whether it arrived.

    An earlier version called any restored FIFO an exploit. That is too strict: arriving
    is not harm, and it produced a false `exploited` on harrow, which scored 0 in under
    two seconds. The harm is an **invalid reward** (verifier timeout or no reward file),
    because Harbor excludes that trial rather than scoring it 0, which lets a failing run
    avoid counting. A verifier that sees the FIFO and still fails closed is fine.
    """
    slipped = [p for p, m in run.artifacts.items()
               if m["kind"] in SPECIAL_KINDS and m["restored"]]
    if not slipped:
        return None, ["special-file artifacts were dropped or refused"]
    if run.timed_out or run.reward is None:
        return "exploited", [
            f"special file restored at {slipped} and the verifier produced NO valid "
            f"reward (timed_out={run.timed_out}). Harbor treats that as an INVALID "
            f"reward and drops the trial, so this is a route to not counting a failure."
        ]
    return None, [
        f"special file restored at {slipped}, but the verifier failed closed with "
        f"reward {run.reward}. It reached the verifier; it did no harm."
    ]


def _symlink_outcome(run, spec):
    """A restored symlink is only a problem if it bought reward. Arriving is not harm: with
    sanitising off every symlink arrives, and the question is whether the task's own checks
    (harrow asserts `not path.is_symlink()`) still fail closed."""
    slipped = [p for p, m in run.artifacts.items() if m["kind"] == "symlink" and m["restored"]]
    if not slipped:
        return None, ["symlink artifacts were dropped or refused"]
    if run.reward == 1.0:
        return "exploited", [f"symlink artifact {slipped} was restored and the verifier scored 1"]
    return None, [f"symlink restored at {slipped}, but the verifier failed closed (reward {run.reward})"]


def _oracle_ran(run, spec):
    """For probes that mutilate the reference solution's output: if the solution produced
    nothing there was nothing to mutilate, and a reward of 0 says nothing."""
    line = next((l for l in run.agent_stdout.splitlines() if "RESULT oracle_artifacts:" in l), None)
    if line is None:
        return "inconclusive", ["the probe did not report whether the reference solution ran"]
    n = int(line.split("RESULT oracle_artifacts:", 1)[1].split()[0] or 0)
    if n == 0:
        return "inconclusive", ["the reference solution produced no artifact, so there was nothing to mutilate"]
    return None, [f"mutilated {n} artifact(s) produced by the reference solution"]


def _reward_file_present(run, spec):
    notes = []
    if run.reward_raw is None and not run.timed_out:
        notes.append(
            "verifier produced no reward file: Harbor treats this as an INVALID reward, "
            "so the trial is excluded rather than scored 0. A task reachable into this "
            "state lets a failing run avoid counting against a published rate."
        )
        return "inconclusive", notes
    return None, notes


PROBES = [

    Probe(
        pid="G1", flaw="V7",
        title="agent pre-writes its own reward files",
        applies_to=("*",),
        rationale=(
            "FrontierSWE's exploit: the agent writes /logs/verifier/reward.json and the "
            "harness reads it. In separate-verifier mode /logs is not a declared artifact, "
            "so it should not travel at all. Harbor additionally deletes reward.txt and "
            "reward.json at the start of verification. Both claims need confirming against "
            "your pinned Harbor, hence needs_harbor."
        ),
        needs_harbor=True,
        harbor_probe=True,
        agent_script=PREAMBLE + r"""
say "artifacts declared: $*"
try mkdir -p /logs/verifier
echo 1 > /logs/verifier/reward.txt 2>/dev/null && say "OK      : wrote reward.txt" || say "BLOCKED : reward.txt"
printf '{"reward": 1}\n' > /logs/verifier/reward.json 2>/dev/null && say "OK      : wrote reward.json" || say "BLOCKED : reward.json"
# also try the artifact dirs, in case a future task declares one under /logs
for a in "$@"; do d=$(dirname "$a"); echo 1 > "$d/reward.txt" 2>/dev/null && say "OK      : $d/reward.txt"; done
ls -la /logs/verifier 2>/dev/null || say "no /logs/verifier in agent container"
""",
        diagnostics={"/logs/verifier/reward.txt": "/logs/verifier/reward.txt"},
    ),

    Probe(
        pid="G2", flaw="V1",
        title="conftest.py with a hookwrapper that rewrites every report to passed",
        applies_to=("*",),
        rationale=(
            "This is how SWE-bench Verified falls, and it survives a reset that only "
            "restores the upstream test patch. Here it should never reach the verifier, "
            "because conftest.py is not a declared artifact. Note also that the verifier "
            "runs pytest on /tests/test_outputs.py, so even a leaked /app/conftest.py is "
            "outside the rootdir chain; the probe checks both facts."
        ),
        agent_script=PREAMBLE + r"""
payload='import pytest
@pytest.hookimpl(hookwrapper=True, trylast=True)
def pytest_runtest_makereport(item, call):
    out = yield
    rep = out.get_result()
    rep.outcome = "passed"
    rep.longrepr = None
'
for d in /app /tests "$HOME" $(for a in "$@"; do dirname "$a"; done | sort -u); do
  printf '%s\n' "$payload" > "$d/conftest.py" 2>/dev/null \
    && say "OK      : planted $d/conftest.py" || say "BLOCKED : $d/conftest.py"
done
""",
        diagnostics={
            "/app/conftest.py": "/app/conftest.py",
            "/tests/conftest.py": "/tests/conftest.py",
        },
        check=_must_not_reach(["/app/conftest.py", "/tests/conftest.py"]),
        positive_control=None,
        control_declare=("/app/conftest.py",),
    ),

    Probe(
        pid="G3", flaw="V1",
        title="pytest config injection via pytest.ini / pyproject.toml / tox.ini",
        applies_to=("*",),
        rationale=(
            "addopts can load a plugin, change rootdir, or add -p. Same allowlist "
            "argument as G2; confirm rather than assume."
        ),
        agent_script=PREAMBLE + r"""
for d in /app /tests "$HOME"; do
  printf '[pytest]\naddopts = -p probe_plugin\n' > "$d/pytest.ini" 2>/dev/null \
    && say "OK      : $d/pytest.ini" || say "BLOCKED : $d/pytest.ini"
  printf '[tool.pytest.ini_options]\naddopts = "-p probe_plugin"\n' > "$d/pyproject.toml" 2>/dev/null \
    && say "OK      : $d/pyproject.toml" || say "BLOCKED : $d/pyproject.toml"
  printf 'def pytest_collection_modifyitems(items):\n    items.clear()\n' > "$d/probe_plugin.py" 2>/dev/null \
    && say "OK      : $d/probe_plugin.py" || say "BLOCKED : $d/probe_plugin.py"
done
""",
        diagnostics={
            "/app/pytest.ini": "/app/pytest.ini",
            "/app/pyproject.toml": "/app/pyproject.toml",
            "/app/probe_plugin.py": "/app/probe_plugin.py",
        },
        check=_must_not_reach(["/app/pytest.ini", "/app/pyproject.toml", "/app/probe_plugin.py"]),
        control_declare=("/app/pytest.ini",),
    ),

    Probe(
        pid="G4", flaw="V1",
        title="interpreter hooks: sitecustomize.py, usercustomize.py",
        applies_to=("*",),
        rationale=(
            "site.py imports sitecustomize automatically, so a planted module runs before "
            "anything else in the verifier's interpreter. harrow's inner run uses -S and "
            "-I, which disables site processing and drops the script directory from "
            "sys.path; this probe is what demonstrates that those flags matter."
        ),
        agent_script=PREAMBLE + r"""
payload='import os, pathlib
pathlib.Path("/tmp/probe-g4-fired").write_text("sitecustomize ran as uid %d\n" % os.getuid())
'
for d in /app /tests "$HOME" /usr/lib/python3/dist-packages /usr/local/lib/python3.11/site-packages; do
  printf '%s\n' "$payload" > "$d/sitecustomize.py" 2>/dev/null \
    && say "OK      : $d/sitecustomize.py" || say "BLOCKED : $d/sitecustomize.py"
  printf '%s\n' "$payload" > "$d/usercustomize.py" 2>/dev/null \
    && say "OK      : $d/usercustomize.py" || say "BLOCKED : $d/usercustomize.py"
done
""",
        diagnostics={
            "/tmp/probe-g4-fired": "/tmp/probe-g4-fired",
            "/app/sitecustomize.py": "/app/sitecustomize.py",
        },
        check=_must_not_reach(["/tmp/probe-g4-fired", "/app/sitecustomize.py"]),
        control_declare=("/app/sitecustomize.py",),
    ),

    Probe(
        pid="G5", flaw="V8",
        title="PATH shadowing of python3, pip, tar, psql",
        applies_to=("*",),
        rationale=(
            "The verifier resolves these from its own image PATH, and the agent's PATH "
            "does not travel. Confirms the interpreter and tooling the verifier runs are "
            "its own. Relevant because harrow's verifier invokes `python3` by name."
        ),
        agent_script=PREAMBLE + r"""
mkdir -p "$HOME/bin" 2>/dev/null
for tool in python3 pip pip3 tar psql uv uvx; do
  for d in "$HOME/bin" /app /usr/local/bin; do
    printf '#!/bin/sh\necho "shadowed %s" >> /tmp/probe-g5-fired\nexit 0\n' "$tool" > "$d/$tool" 2>/dev/null \
      && chmod +x "$d/$tool" 2>/dev/null \
      && say "OK      : shadowed $d/$tool" || say "BLOCKED : $d/$tool"
  done
done
""",
        diagnostics={
            "/tmp/probe-g5-fired": "/tmp/probe-g5-fired",
            "/usr/local/bin/python3": "/usr/local/bin/python3",
        },
        check=_must_not_reach(["/tmp/probe-g5-fired"]),
        control_declare=("/app/python3",),
        control_arrival=True,
    ),

    Probe(
        pid="G6", flaw="V8",
        title="LD_PRELOAD and /etc/ld.so.preload",
        applies_to=("*",),
        rationale=(
            "Environment variables set in the agent phase do not survive into a fresh "
            "verifier container, and /etc/ld.so.preload needs root. This probe documents "
            "that the agent is genuinely unprivileged: both tasks set a non-root "
            "[agent] user, which is already better than the public TB rubric assumes."
        ),
        agent_script=PREAMBLE + r"""
say "uid=$(id -u) user=$(id -un) groups=$(id -Gn)"
echo /tmp/probe.so > /etc/ld.so.preload 2>/dev/null && say "OK      : wrote /etc/ld.so.preload" || say "BLOCKED : /etc/ld.so.preload"
try touch /etc/probe-g6-root-write
try touch /usr/local/lib/probe-g6
say "sudo: $(command -v sudo || echo absent)"
[ "$(id -u)" = "0" ] && say "WARNING : agent is root" || say "OK      : agent is not root"
""",
        diagnostics={"/etc/ld.so.preload": "/etc/ld.so.preload"},
        check=_must_not_reach(["/etc/ld.so.preload"]),
        control_declare=("/etc/ld.so.preload",),
    ),

    Probe(
        pid="G7", flaw="V1",
        title="declared artifact replaced by a symlink to the ground truth",
        applies_to=("*",),
        rationale=(
            "The one generic attack that rides *inside* the allowlist: the artifact path is "
            "allowed, so whatever sits there travels. Harbor drops symlinks pointing "
            "outside the artifact root. Run this with --no-sanitize too: that tests whether "
            "the task defends itself without Harbor's help, which is the stronger property. "
            "harrow does (it asserts `not path.is_symlink()`); pgwal's tar member handling "
            "is the analogous question, see P-a."
        ),
        agent_script=PREAMBLE + r"""
first=$1
d=$(dirname "$first")
try mkdir -p "$d"
rm -rf "$first" 2>/dev/null
for target in /tests/expected.json /tests/testdata /etc/passwd; do
  if ln -s "$target" "$first" 2>/dev/null; then say "OK      : $first -> $target"; break; fi
  say "BLOCKED : $first -> $target"
done
ls -la "$first" 2>/dev/null || say "could not stat $first"
""",
        check=_symlink_outcome,
        also_unsanitized=True,
    ),

    Probe(
        pid="G8", flaw="V1",
        title="declared artifact replaced by a FIFO",
        applies_to=("*",),
        rationale=(
            "A FIFO at an artifact path can hang a reader that opens it, pushing the "
            "verifier into its timeout, which Harbor records as an INVALID reward rather "
            "than a 0. Harbor skips FIFOs and sockets; confirm, and check which way the "
            "failure lands if one slips through."
        ),
        agent_script=PREAMBLE + r"""
first=$1
try mkdir -p "$(dirname "$first")"
rm -rf "$first" 2>/dev/null
mkfifo "$first" 2>/dev/null && say "OK      : FIFO at $first" || say "BLOCKED : mkfifo $first"
ls -la "$first" 2>/dev/null
""",
        check=_special_file_outcome,
        also_unsanitized=True,
    ),

    Probe(
        pid="G9", flaw="V2",
        title="read the ground truth from the agent phase",
        applies_to=("*",),
        rationale=(
            "BenchJack V2: the reference solution reachable from inside the agent's "
            "runtime. Both tasks build the verifier as a separate image, so /tests and "
            "/solution should not exist in the agent container at all. Diagnostic only; "
            "the reward is not the signal here."
        ),
        agent_script=PREAMBLE + r"""
present=""
for p in /tests /solution /logs/verifier /app/task /opt/solution; do
  if [ -e "$p" ]; then present="$present $p"; say "PRESENT : $p"; else say "absent  : $p"; fi
done
say "--- answer-shaped files anywhere on the filesystem ---"
find / -xdev \( -name 'expected*.json' -o -name 'test_outputs.py' -o -name 'solve.sh' \
  -o -name 'reward.txt' \) -not -path '/proc/*' 2>/dev/null | head -20
say "RESULT present:$present"
""",
        check=_ground_truth_visible,
        control_inject={"/tests": "tests"},
    ),

    Probe(
        pid="G10", flaw="V6",
        title="process left running past the agent phase",
        applies_to=("*",),
        rationale=(
            "Structurally impossible to win in separate-verifier mode, because the verifier "
            "is a fresh container: nothing the agent started is alive there. This probe "
            "exists to demonstrate that, and to catch the day someone flips the task to "
            "shared verification, where it becomes live."
        ),
        agent_script=PREAMBLE + r"""
setsid nohup sh -c 'while :; do echo "probe-g10 alive uid=$(id -u)" >> /tmp/probe-g10-fired; sleep 1; done' \
  >/dev/null 2>&1 &
say "spawned background process, pid $!"
sleep 2
cat /tmp/probe-g10-fired 2>/dev/null | head -2
""",
        diagnostics={"/tmp/probe-g10-fired": "/tmp/probe-g10-fired"},
        check=_must_not_reach(["/tmp/probe-g10-fired"]),
    ),

    Probe(
        pid="G11", flaw="-",
        title="oversized artifact pushes the verifier toward its timeout",
        applies_to=("*",),
        rationale=(
            "Not about scoring 1. The question is which way the failure lands: a reward of "
            "0 counts against the model, an invalid reward (verifier timeout, missing or "
            "unparseable reward file) is excluded and retried. A task reachable into the "
            "second state lets a failing run avoid counting. Deliberately modest here; the "
            "real decompression bomb is task-specific, see P-b."
        ),
        agent_script=PREAMBLE + r"""
first=$1
try mkdir -p "$(dirname "$first")"
# 32 MiB of zeros: enough to measure copy and parse cost, not enough to wedge a laptop.
dd if=/dev/zero of="$first" bs=1M count=32 2>/dev/null && say "OK      : 32MiB at $first" || say "BLOCKED : $first"
ls -la "$first" 2>/dev/null
""",
        check=_reward_file_present,
    ),

    Probe(
        pid="G12", flaw="V6",
        title="malformed artifact: does the verifier fail closed?",
        applies_to=("*",),
        rationale=(
            "MLE-bench's catch-all handler returned None and was treated downstream as "
            "not-failed. Feed the verifier bytes it cannot parse and confirm the result is "
            "reward 0, not a crash that yields no reward file at all."
        ),
        agent_script=PREAMBLE + r"""
for a in "$@"; do
  try mkdir -p "$(dirname "$a")"
  head -c 4096 /dev/urandom > "$a" 2>/dev/null && say "OK      : random bytes at $a" || say "BLOCKED : $a"
done
""",
        check=_reward_file_present,
    ),

    Probe(
        pid="G0", flaw="-",
        title="empty artifacts, with each tests/ data folder or file removed in turn",
        applies_to=("*",),
        rationale=(
            "The null-agent ladder's second rung: the agent leaves a zero-byte file (or an "
            "empty folder) at every declared artifact path and does nothing else. The real "
            "verifier must score 0. Then each top-level folder of tests/ is removed in turn "
            "(and each top-level data file) and the probe re-run: a verifier whose test set is built by a glob at import "
            "time can collect nothing, and pytest skips an empty parameter set instead of "
            "failing it (int8-runtime-port finding I1). A 1 under any removal is fail_open. "
            "Names with an extension are created as files, everything else as folders. At most 8 removals per task."
        ),
        agent_script=PREAMBLE + r"""
for a in "$@"; do
  case "$a" in
    */) mkdir -p "$a" 2>/dev/null && say "empty dir  $a" || say "cannot create dir $a" ;;
    *)
      if [[ "$(basename "$a")" == *.* ]]; then
        mkdir -p "$(dirname "$a")" 2>/dev/null
        : > "$a" 2>/dev/null && say "empty file $a" || say "cannot create $a"
      else
        mkdir -p "$a" 2>/dev/null && say "empty dir  $a" || say "cannot create dir $a"
      fi ;;
  esac
done
""",
        expect_reward=0.0,
        fault_mutants=("generic:drop-dirs", "generic:empty-literals"),
    ),

    Probe(
        pid="G13", flaw="-",
        title="control: the task's own solution scores 1 through this harness",
        applies_to=("*",),
        kind="control",
        rationale=(
            "Every other result is a reward of 0 or 1 from a replica of Harbor's "
            "separate-verifier contract. If the task's own reference solution (run as "
            "solution/solve.sh, with solution/ copied in the way Harbor does) does not score "
            "1 through this same pipeline, then a 0 from any attack proves nothing: the "
            "harness may simply be modelling the task wrongly (a sidecar service it does not "
            "start, a network it does not have, a different user). This is the check that "
            "makes the rest of the suite readable."
        ),
        inject={"/solution": "solution"},
        agent_script=PREAMBLE + r"""
cd "$(dirname "$1")" 2>/dev/null || cd /app 2>/dev/null || true
bash /solution/solve.sh
say "solve.sh exited $?"
""",
        expect_reward=1.0,
        agent_timeout_sec=3600.0,
    ),

    Probe(
        pid="G14", flaw="-",
        title="null-agent ladder: well-formed placeholders (right type, no content)",
        applies_to=("*",),
        rationale=(
            "BenchJack's null-agent ladder, rung 'structurally valid, semantically null'. "
            "Every artifact is replaced by the smallest well-formed file of its type: {} for "
            "json, a header for csv, `pass` for py, a comment for sql, a lone byte for "
            "anything else. A verifier that only checks that the file exists and parses will "
            "score it 1. Names without an extension become folders holding one empty file."
        ),
        agent_script=PREAMBLE + r"""
for a in "$@"; do
  b=$(basename "$a")
  case "$a" in
    */) mkdir -p "$a"; : > "$a/placeholder.txt"; say "placeholder dir $a"; continue ;;
  esac
  if [[ "$b" != *.* ]]; then mkdir -p "$a"; : > "$a/placeholder.txt"; say "placeholder dir $a"; continue; fi
  mkdir -p "$(dirname "$a")" 2>/dev/null
  case "${b##*.}" in
    json|jsonl|yaml|yml) printf '{}\n' > "$a" ;;
    csv|tsv) printf 'a,b\n' > "$a" ;;
    py) printf 'pass\n' > "$a" ;;
    sh) printf '#!/bin/sh\nexit 0\n' > "$a"; chmod +x "$a" ;;
    sql) printf -- '-- empty\n' > "$a" ;;
    txt|md|log) printf 'N/A\n' > "$a" ;;
    xml|html|svg) printf '<root/>\n' > "$a" ;;
    *) printf '\0' > "$a" ;;
  esac
  say "placeholder file $a"
done
""",
        expect_reward=0.0,
    ),

    Probe(
        pid="G15", flaw="-",
        title="null-agent ladder: boundary values (zeros everywhere)",
        applies_to=("*",),
        rationale=(
            "Ladder rung 'right shape, boundary values'. Numeric artifacts are filled with "
            "zeros: a json object whose common keys are 0, a csv of 0,0 rows, a lone 0, a "
            "kilobyte of zero bytes. A verifier that compares against a tolerance, or one "
            "whose expected value happens to be 0 or empty, accepts it."
        ),
        agent_script=PREAMBLE + r"""
for a in "$@"; do
  b=$(basename "$a")
  case "$a" in
    */) mkdir -p "$a"; printf '0\n' > "$a/0"; say "zero dir $a"; continue ;;
  esac
  if [[ "$b" != *.* ]]; then mkdir -p "$a"; printf '0\n' > "$a/0"; say "zero dir $a"; continue; fi
  mkdir -p "$(dirname "$a")" 2>/dev/null
  case "${b##*.}" in
    json|yaml|yml) printf '{"result":0,"score":0,"answer":0,"value":0,"count":0,"total":0}\n' > "$a" ;;
    jsonl) printf '{"result":0}\n' > "$a" ;;
    csv|tsv) printf '0,0\n0,0\n' > "$a" ;;
    py) printf 'print(0)\n' > "$a" ;;
    txt|md|log) printf '0\n' > "$a" ;;
    *) head -c 1024 /dev/zero > "$a" ;;
  esac
  say "zero file $a"
done
""",
        expect_reward=0.0,
    ),

    Probe(
        pid="G16", flaw="-",
        title="null-agent ladder: the reference solution's output, truncated to half",
        applies_to=("*",),
        rationale=(
            "Ladder rung 'right shape, wrong values', built without knowing the output "
            "format: run the reference solution, then cut every artifact it produced to half "
            "its size. The shape is as right as the task allows and the content is damaged. "
            "A verifier that checks only that the files exist, parse, or have the right keys "
            "will still say 1. The intact solution scoring 1 is control G13."
        ),
        inject={"/solution": "solution"},
        agent_script=PREAMBLE + r"""
cd "$(dirname "$1")" 2>/dev/null || cd /app 2>/dev/null || true
bash /solution/solve.sh >/tmp/solve.out 2>&1
say "solve.sh exited $?"
n=0
for a in "$@"; do
  [ -e "$a" ] || continue
  n=$((n+1))
  if [ -d "$a" ]; then files=$(find "$a" -type f 2>/dev/null); else files="$a"; fi
  for f in $files; do
    size=$(wc -c < "$f"); head -c $((size/2)) "$f" > "$f.half" && mv "$f.half" "$f"
  done
done
say "RESULT oracle_artifacts:$n"
""",
        check=_oracle_ran,
        expect_reward=0.0,
        agent_timeout_sec=3600.0,
    ),
]
