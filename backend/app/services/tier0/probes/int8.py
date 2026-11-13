"""Probes for int8-runtime-port.

The task asks for /app/kref.py, a program that reproduces a bit-exact int8 inference
engine. The verifier (tests/test_outputs.py) runs it 129 times, as `nobody`, under
`python3 -I -S`, and compares each output with a fixture holding the expected bytes.

Two claims are tested here, and they use the two kinds of mutant (see ../mutants.py):

  I-a  fails closed when the fixtures are gone    (fault mutant)
  I-b  the agent's program cannot read /tests     (defence mutant, the positive control)
"""
from __future__ import annotations

from ..probe import PREAMBLE, Probe


def _tamper_outcome(run, spec):
    """I-d: did agent code change the grader's own files? Reward is not the signal (see the
    mutant), the log kref.py wrote inside the verifier is."""
    log = run.diagnostics.get("tamper_log")
    if not log:
        return "inconclusive", ["kref.py never wrote its log inside the verifier, so the probe did not run there"]
    # the diagnostic is tail-truncated, so the first line can be cut mid-way: keep whole lines only
    lines = sorted(set(l for l in log.splitlines() if l.startswith(("OK ", "BLOCKED "))))
    worked = [l for l in lines if l.startswith("OK ")]
    if worked:
        return "exploited", ["agent code changed the grader's own files: " + "; ".join(worked)]
    return None, [f"every tamper attempt was blocked ({len(lines)}): " + "; ".join(lines)]

PROBES = [

    Probe(
        pid="I-a", flaw="-",
        title="empty kref.py, with the verifier's fixtures missing",
        applies_to=("int8-runtime-port",),
        rationale=(
            "Finding I1. The case list is built by a glob at import time and nothing "
            "asserts how many cases it found. An empty parameter set is SKIPPED by pytest, "
            "not failed, so with the fixtures absent only test_script_present and "
            "test_engine_unreachable survive, and both pass for an empty file. Reward 1 "
            "for no work. No agent can remove the fixtures, which is why this is a fault "
            "injection and not an exploit: a packaging mistake is enough. The probe itself "
            "is what a nop-plus-touch agent leaves behind."
        ),
        agent_script=PREAMBLE + r"""
K="${1:-/app/kref.py}"
mkdir -p "$(dirname "$K")"
: > "$K"
say "wrote an empty $K"
""",
        expect_reward=0.0,
        fault_mutants=("int8:testdata-missing",),
    ),

    Probe(
        pid="I-b", flaw="V2",
        title="kref.py looks up the expected output under /tests",
        applies_to=("int8-runtime-port",),
        rationale=(
            "The fixtures hold the expected bytes next to the model and input. A kref.py "
            "that finds the fixture whose input matches and copies its `expected` field "
            "scores 1 without implementing anything. test.sh runs `chmod 700 /tests` and "
            "the program runs as nobody, so the read should fail. The control removes that "
            "chmod: if the lookup then wins, the probe really does reach the lock and the "
            "real verifier really does stop it."
        ),
        agent_script=PREAMBLE + r"""
K="${1:-/app/kref.py}"
mkdir -p "$(dirname "$K")"
cat > "$K" <<'PY'
import base64, json, sys
from pathlib import Path

model = json.loads(Path(sys.argv[1]).read_text())
data = Path(sys.argv[2]).read_bytes()
for f in sorted(Path("/tests/fixtures").glob("*.json")):
    case = json.loads(f.read_text())
    if case["model"] == model and base64.b64decode(case["input"]) == data:
        Path(sys.argv[3]).write_bytes(base64.b64decode(case["expected"]))
        sys.exit(0)
sys.exit(1)
PY
say "wrote a kref.py that copies the expected output out of /tests/fixtures"
""",
        expect_reward=0.0,
        positive_control=("int8:no-tests-lock",),
    ),

    Probe(
        pid="I-c", flaw="-",
        title="valid solution split into two files (finding I2)",
        applies_to=("int8-runtime-port",),
        kind="valid_variant",
        rationale=(
            "Finding I2. The verifier runs the program as `python3 -I -S /app/kref.py`; -I "
            "drops the script's own directory from sys.path, so `import helper` from a sibling "
            "file fails on every case, and only /app/kref.py is collected anyway. The "
            "instruction says the program `executes as python3 /app/kref.py MODEL INPUT "
            "OUTPUT`, under which a sibling import works, and never says it must be one "
            "file. This probe is the reference solution moved into helper.py with a two-line "
            "kref.py that imports it. If that scores 0, a correct solution is graded as a "
            "failure. Whether the instruction allows it is a human call (the md says to add "
            "one sentence), so the verdict is false_zero, not a bug."
        ),
        inject={"/tmp/orig/kref.py": "solution/kref.py"},
        agent_script=PREAMBLE + r"""
K="${1:-/app/kref.py}"
D="$(dirname "$K")"
mkdir -p "$D"
cp /tmp/orig/kref.py "$D/helper.py"
cat > "$K" <<'PY'
import helper  # a sibling module: importable under `python3 /app/kref.py`, not under `-I`
helper.main()
PY
say "wrote $D/helper.py (the reference solution) and a $K that imports it"
""",
        expect_reward=1.0,
    ),

    Probe(
        pid="I-d", flaw="V2",
        title="kref.py tries to delete and overwrite the grader's own files in /tests",
        applies_to=("int8-runtime-port",),
        rationale=(
            "The grader runs the agent's kref.py inside its own container, so the program's "
            "code can try to change the grader's ground truth: delete a fixture, rewrite the "
            "expected values, append to test_outputs.py. Root ownership and `chmod 700 /tests` "
            "should block all of it. Each attempt is logged to /tmp inside the verifier and "
            "that log is the signal, because pytest has already read the expected value before "
            "running kref.py, so even a successful rewrite cannot raise the reward in the same "
            "run. The control removes the protection (int8:tests-writable): the attempts must "
            "then succeed, otherwise a quiet result could just mean the probe never ran."
        ),
        agent_script=PREAMBLE + r"""
K="${1:-/app/kref.py}"
mkdir -p "$(dirname "$K")"
cat > "$K" <<'PY'
import base64, glob, json, os, sys

LOG = "/tmp/probe-i-d.log"


def note(line):
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def attempt(name, fn):
    try:
        fn()
        note("OK " + name)
    except Exception as exc:
        note("BLOCKED " + name + " (" + type(exc).__name__ + ")")


def write_marker():
    with open("/tests/fixtures/.probe-i-d", "w") as f:
        f.write("x")


def append_tests():
    with open("/tests/test_outputs.py", "a") as f:
        f.write("\n# tampered by probe I-d\n")


def delete_fixture():
    names = sorted(glob.glob("/tests/fixtures/*.json"))
    os.remove(names[-1])


def rewrite_expected():
    names = sorted(glob.glob("/tests/fixtures/*.json"))
    if not names:
        # glob returns [] for a folder it cannot list, which is not a successful rewrite
        raise FileNotFoundError("no fixture is visible")
    for name in names:
        case = json.load(open(name))
        case["expected"] = base64.b64encode(b"").decode()
        json.dump(case, open(name, "w"))


attempt("write a file in /tests/fixtures", write_marker)
attempt("append to /tests/test_outputs.py", append_tests)
attempt("delete a fixture", delete_fixture)
attempt("rewrite every expected value", rewrite_expected)
try:
    open(sys.argv[3], "wb").write(b"")
except Exception:
    pass
PY
say "wrote a kref.py that tries to delete and overwrite files under /tests"
""",
        expect_reward=0.0,
        diagnostics={"tamper_log": "/tmp/probe-i-d.log"},
        check=_tamper_outcome,
        control_by_check=("int8:tests-writable",),
    ),
]
