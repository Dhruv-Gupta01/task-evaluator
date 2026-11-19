"""Batch 2: the Flag framework (N15) and the static Flag rules of CK-B, CK-D, CK-G, CK-J, CK-K.
Every rule has a fixture it must fire on and one it must not fire on."""

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Flag, Submission
from app.services import flag_rules as fr
from app.services import flags

PRIVATE = "0f0e0d0c-1111-2222-3333-444455556666"
TOML = """# harbor-canary GUID {canary}
artifacts = ["/app/out.json"]

[task]
name = "terminal-bench/fixture"
version = "{version}"

[metadata]
author_name = "A"
author_email = "a@example.com"
author_github = "{github}"
author_organization = ""
category = "Software"
subcategory = "x"
tags = []
expert_time_estimate_hours = 1

[verifier]
timeout_sec = {verifier_timeout}
environment_mode = "separate"
{verifier_user}
[agent]
timeout_sec = 600.0
user = "agent"

[environment]
build_timeout_sec = 600.0
cpus = 1
memory_mb = 2048
storage_mb = 10240
gpus = 0
network_mode = "public"
"""


def make_task(
    root: Path,
    test_py: str = "",
    test_sh: str = "#!/bin/bash\npython3 -m pytest /tests/test_outputs.py\n",
    instruction: str = "Write /app/out.json.\n",
    github: str = "someone",
    canary: str = fr.PUBLIC_CANARY,
    verifier_timeout: float = 600.0,
    verifier_user: str = "",
    files: dict[str, str] | None = None,
    version: str = "1.0.0",
) -> Path:
    for sub in ("environment", "tests", "solution"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    (root / "task.toml").write_text(TOML.format(
        canary=canary, github=github, verifier_timeout=verifier_timeout, version=version,
        verifier_user=f'user = "{verifier_user}"\n' if verifier_user else "",
    ))
    (root / "instruction.md").write_text(f"<!-- harbor-canary GUID {canary} -->\n{instruction}")
    (root / "environment" / "Dockerfile").write_text(f"# harbor-canary GUID {canary}\nFROM ubuntu:22.04\nWORKDIR /app\n")
    (root / "tests" / "test.sh").write_text(test_sh)
    (root / "tests" / "test_outputs.py").write_text(test_py)
    for rel, text in (files or {}).items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return root


def fired(root: Path, rule: str, **ctx) -> list[fr.Finding]:
    return fr.STATIC_RULES[rule](fr.load_context(root, **ctx)).findings


def test_catalogue_has_37_rules_in_11_checks():
    assert list(fr.RULES) == [f"15.{i}" for i in range(1, 38)]
    assert len({r.check for r in fr.RULES.values()}) == 11
    for rule_id in fr.STATIC_RULES:
        assert fr.RULES[rule_id].method == "static" and fr.RULES[rule_id].batch in (2, 3)


PSQL = 'import subprocess\ndef q(sql):\n    return subprocess.run(["psql", "-c", sql])\n'


def test_15_2_unqualified_function_in_agent_database(tmp_path):
    bad = make_task(tmp_path / "a", PSQL + 'q(f"SELECT md5(string_agg(x::text, \',\')) FROM ledger.{t} x")\n')
    good = make_task(tmp_path / "b", PSQL + 'q("SELECT pg_catalog.md5(pg_catalog.string_agg(x::text, \',\')) FROM t x")\n')
    assert "md5, string_agg" in fired(bad, "15.2")[0].evidence[0]
    assert fired(good, "15.2") == []


def test_15_4_root_unpack_of_agent_archive(tmp_path):
    code = 'import subprocess\nOUT = "/app/output"\nsubprocess.run(f"tar -xzf {OUT}/data.tgz -C /tmp/x", shell=True)\n'
    assert fired(make_task(tmp_path / "a", code), "15.4")
    dropped = code.replace("tar -xzf", "runuser -u postgres -- tar -xzf")
    assert fired(make_task(tmp_path / "b", dropped), "15.4") == []
    assert fired(make_task(tmp_path / "c", code, verifier_user="nobody"), "15.4") == []


def test_15_5_superuser_statement_in_agent_database(tmp_path):
    assert fired(make_task(tmp_path / "a", PSQL + 'q("CREATE EXTENSION pgcrypto")\n'), "15.5")
    assert fired(make_task(tmp_path / "b", PSQL + 'q("SELECT 1")\n'), "15.5") == []


RUN_PROGRAM = (
    'import subprocess\nPROG = "/app/prog.py"\n'
    'def test_x():\n    subprocess.run(["python3", "-I", "-S", PROG], user=65534, timeout=5)\n'
)


def test_15_7_isolated_interpreter_not_in_instruction(tmp_path):
    loose = "Implement `/app/prog.py`, a program using only the standard library that runs as `python3 /app/prog.py`.\n"
    told = loose + "It must be a single self-contained file.\n"
    findings = fired(make_task(tmp_path / "a", RUN_PROGRAM, instruction=loose), "15.7")
    assert findings and any(e.startswith("-I ") for e in findings[0].evidence)
    assert not any(e.startswith("-S ") for e in findings[0].evidence), "-S is disclosed by 'standard library'"
    assert fired(make_task(tmp_path / "b", RUN_PROGRAM, instruction=told), "15.7") == []


def test_15_14_public_canary_only(tmp_path):
    assert fired(make_task(tmp_path / "a"), "15.14")
    both = make_task(tmp_path / "b", files={"tests/notes.txt": f"# harbor-canary GUID {PRIVATE}\n"})
    assert fired(both, "15.14") == []


def test_15_15_author_and_template(tmp_path):
    keys = {f.key for f in fired(make_task(tmp_path / "a", github="anonymous"), "15.15")}
    assert keys == {"author"}
    root = make_task(tmp_path / "b")
    toml = (root / "task.toml").read_text().replace('network_mode = "public"\n', "")
    (root / "task.toml").write_text(toml)
    findings = fired(root, "15.15")
    assert [f.key for f in findings] == ["template"]
    assert "environment.network_mode" in findings[0].evidence[0]


def test_15_16_rubric_in_reward(tmp_path):
    sh = "#!/bin/bash\nscore=$(python3 /tests/grade.py /tests/rubric.md)\necho $score > /logs/verifier/reward.txt\n"
    assert fired(make_task(tmp_path / "a", test_sh=sh), "15.16")
    assert fired(make_task(tmp_path / "b"), "15.16") == []


def test_15_17_rubric_failure_contradicted_by_schema(tmp_path):
    root = make_task(tmp_path / "a")
    wrong = [{"checks": {"resource_configuration": {
        "outcome": "fail", "explanation": "network_mode is not a valid task.toml field; remove it."}}}]
    fair = [{"checks": {"resource_configuration": {
        "outcome": "fail", "explanation": "network_mode is public but the task needs no network."}}}]
    findings = fired(root, "15.17", rubric_results=wrong)
    assert findings and findings[0].suggested_status == "disputed"
    assert "Harbor schema has environment.network_mode" in findings[0].evidence
    assert fired(root, "15.17", rubric_results=fair) == []


def test_15_21_path_from_environment(tmp_path):
    assert fired(make_task(tmp_path / "a", 'import os\nD = os.environ.get("TEST_DIR", "/tests")\n'), "15.21")
    assert fired(make_task(tmp_path / "b", 'import os\np = os.environ.get("PATH")\n'), "15.21") == []


def test_15_22_agent_program_without_memory_bound(tmp_path):
    assert fired(make_task(tmp_path / "a", RUN_PROGRAM), "15.22")
    bounded = "import resource\nresource.setrlimit(resource.RLIMIT_AS, (1 << 30, 1 << 30))\n" + RUN_PROGRAM
    assert fired(make_task(tmp_path / "b", bounded), "15.22") == []


def test_15_23_agent_code_as_root(tmp_path):
    root_run = 'import subprocess\nsubprocess.run(["/app/run.sh"])\n'
    assert fired(make_task(tmp_path / "a", root_run), "15.23")
    dropped = 'import subprocess\nsubprocess.run(["/app/run.sh"], user="nobody")\n'
    assert fired(make_task(tmp_path / "b", dropped), "15.23") == []


BUDGET = (
    "import subprocess\nfrom pathlib import Path\nimport pytest\n"
    'CASES = sorted(Path("/tests/cases").glob("*.json"))\nLIMIT = 30\n'
    '@pytest.mark.parametrize("case", CASES)\n'
    'def test_case(case):\n    subprocess.run(["python3", "/app/p.py", str(case)], timeout=LIMIT)\n'
)


def test_15_24_case_budget(tmp_path):
    files = {f"tests/cases/c{i}.json": "{}" for i in range(10)}
    over = make_task(tmp_path / "a", BUDGET, verifier_timeout=200, files=files)
    findings = fired(over, "15.24")
    assert findings and findings[0].severity is None and "10 x 30 s = 300 s of 200 s" in findings[0].evidence[0]
    near = make_task(tmp_path / "b", BUDGET, verifier_timeout=360, files=files)
    assert fired(near, "15.24")[0].severity == fr.ADVISORY
    roomy = make_task(tmp_path / "c", BUDGET, verifier_timeout=1000, files=files)
    assert fired(roomy, "15.24") == []


def test_15_26_private_canary_reused(tmp_path):
    root = make_task(tmp_path / "a", files={"tests/notes.txt": f"# harbor-canary GUID {PRIVATE}\n"})
    assert fired(root, "15.26", other_canaries={PRIVATE: ["terminal-bench/other-task"]})
    assert fired(root, "15.26", other_canaries={}) == []


def test_15_35_unbounded_read_of_agent_artifact(tmp_path):
    code = 'import json\nfrom pathlib import Path\nOUT = Path("/app/out")\ndef test_r():\n    return json.loads((OUT / "r.json").read_text())\n'
    assert fired(make_task(tmp_path / "a", code), "15.35")
    bounded = code.replace("def test_r():\n", "def test_r():\n    assert (OUT / 'r.json').stat().st_size < 1 << 20\n")
    assert fired(make_task(tmp_path / "b", bounded), "15.35") == []
    test_data = 'from pathlib import Path\nT = Path("/tests/x.json")\ndef f():\n    return T.read_text()\n'
    assert fired(make_task(tmp_path / "c", test_data), "15.35") == []


def test_a_broken_rule_is_reported_not_hidden(tmp_path, monkeypatch):
    def boom(ctx):
        raise RuntimeError("bad input")

    monkeypatch.setitem(fr.STATIC_RULES, "15.2", boom)
    results = fr.evaluate(fr.load_context(make_task(tmp_path / "a")))
    assert results["15.2"].note == "rule error: RuntimeError: bad input"
    assert results["15.14"].findings


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _submission(db, root: Path, sid: str = "s1", name: str = "terminal-bench/fixture") -> Submission:
    sub = Submission(id=sid, task_name=name, uploaded_at=datetime.now(UTC), zip_path="x", extracted_path=str(root))
    db.add(sub)
    db.commit()
    return sub


def test_framework_keeps_rulings_across_reruns(tmp_path, db):
    root = make_task(tmp_path / "t", github="anonymous")
    sub = _submission(db, root)
    rec = flags.run_static(db, sub)
    assert sorted(f["rule"] for f in rec["flags"]) == ["15.14", "15.15", "15.19"]  # 15.19: no cheat trial yet
    canary = next(f for f in rec["flags"] if f["rule"] == "15.14")
    assert canary["severity"] == fr.BLOCKER and canary["status"] == "open"
    assert rec["counts"]["open_blockers"] == 1
    # static + result rules: every catalogued rule has an evaluator
    from app.services import flag_results

    assert sum(r["ran"] for r in rec["rules"]) == len(fr.STATIC_RULES) + len(flag_results.RESULT_RULES) == len(fr.RULES)

    flag = db.get(Flag, canary["id"])
    with pytest.raises(ValueError):
        flags.set_status(db, flag, "accepted", "Reviewer", "")
    with pytest.raises(ValueError):
        flags.set_status(db, flag, "accepted", "", "public GUID only, private one coming")
    flags.set_status(db, flag, "accepted", "Reviewer", "public GUID only, private one coming")

    rec = flags.run_static(db, sub)
    again = next(f for f in rec["flags"] if f["rule"] == "15.14")
    assert again["id"] == canary["id"] and again["status"] == "accepted" and again["present"]
    assert again["history"][-1]["by"] == "Reviewer"
    assert rec["counts"]["open_blockers"] == 0 and rec["counts"]["accepted"] == 1

    (root / "tests" / "notes.txt").write_text(f"# harbor-canary GUID {PRIVATE}\n")
    rec = flags.run_static(db, sub)
    gone = next(f for f in rec["flags"] if f["rule"] == "15.14")
    assert gone["present"] is False and gone["status"] == "accepted"
    assert rec["counts"]["no_longer_found"] == 1


def test_framework_finds_canary_reuse_across_submissions(tmp_path, db):
    shared = {"tests/notes.txt": f"# harbor-canary GUID {PRIVATE}\n"}
    _submission(db, make_task(tmp_path / "other", files=shared), "s0", "terminal-bench/other-task")
    sub = _submission(db, make_task(tmp_path / "mine", files=shared), "s1", "terminal-bench/fixture")
    rec = flags.run_static(db, sub)
    reuse = [f for f in rec["flags"] if f["rule"] == "15.26"]
    assert reuse and "terminal-bench/other-task" in reuse[0]["evidence"][0]


def _harbor_python() -> Path | None:
    try:
        tool_dir = subprocess.run(["uv", "tool", "dir"], capture_output=True, text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    for rel in ("harbor/Scripts/python.exe", "harbor/bin/python"):
        if tool_dir and (Path(tool_dir) / rel).exists():
            return Path(tool_dir) / rel
    return None


def test_vendored_harbor_fields_match_the_installed_harbor():
    python = _harbor_python()
    if python is None:
        pytest.skip("harbor is not installed as a uv tool")
    code = (
        "import json\nfrom harbor.models.task.config import TaskConfig\n"
        "def f(m, p=''):\n    out=[]\n    for n, x in m.model_fields.items():\n"
        "        out.append(p+n)\n        out += f(x.annotation, p+n+'.') if hasattr(x.annotation, 'model_fields') else []\n"
        "    return out\nprint(json.dumps(f(TaskConfig)))\n"
    )
    proc = subprocess.run([str(python), "-c", code], capture_output=True, text=True, timeout=120,
                          env={**os.environ, "PYTHONUTF8": "1"})
    assert proc.returncode == 0, proc.stderr
    assert set(json.loads(proc.stdout)) == fr.harbor_fields()


def test_15_15_misspelled_anonymous_author(tmp_path):
    assert [f.key for f in fired(make_task(tmp_path / "a", github="annonymous"), "15.15")] == ["author"]
    assert fired(make_task(tmp_path / "b", github="RajeshA76"), "15.15") == []
