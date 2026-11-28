"""app/services/harborignore.py: a port of Harbor's TaskFileFilter, checked against Harbor itself."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.config import get_settings
from app.services import harborignore as hi


def write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


FIXTURE = {
    "task.toml": "[task]\nname = 'x'\n",
    "instruction.md": "do it\n",
    ".harborignore": "everything\n",  # not a place Harbor reads rules
    "tests/test.sh": "#!/bin/bash\n",
    "tests/Dockerfile": "FROM scratch\n",
    "tests/checks.py": "x = 1\n",
    "tests/.harborignore": "generator/\n*.tmp\n!keep.tmp\n/data/raw/\nsecret.txt\nDockerfile\n*.dat\n",
    "tests/generator/gen.py": "g\n",
    "tests/generator/sub/x.py": "g\n",
    "tests/a.tmp": "t\n",
    "tests/keep.tmp": "t\n",
    "tests/data/raw/big.json": "{}\n",
    "tests/data/clean.json": "{}\n",
    "tests/data/secret.txt": "s\n",
    "tests/sub/.harborignore": "*.bak\n!important.bak\n!keep.dat\n",  # a nested file can re-include what an outer one excluded
    "tests/top.dat": "d\n",
    "tests/sub/keep.dat": "d\n",
    "tests/sub/drop.dat": "d\n",
    "tests/sub/old.bak": "b\n",
    "tests/sub/important.bak": "b\n",
    "tests/sub/code.py": "c\n",
    "solution/solve.sh": "#!/bin/bash\n",
    "solution/solve.py": "s\n",
    "solution/.harborignore": "notes/\n",
    "solution/notes/n.md": "n\n",
    "environment/Dockerfile": "FROM scratch\n",
    "environment/generator/g.py": "outside the scopes: never excluded\n",
    "steps/s1/tests/.harborignore": "x.txt\n",
    "steps/s1/tests/x.txt": "x\n",
    "steps/s1/tests/y.txt": "y\n",
}


@pytest.fixture
def task(tmp_path):
    root = write(tmp_path / "task", FIXTURE)
    (root / "tests" / "link_to_gen").symlink_to("generator/gen.py")
    return root


def test_rules_apply_only_inside_tests_and_solution(task):
    ig = hi.TaskIgnore(task)
    out = {p: ig.is_excluded(task / p) for p in ("tests/generator/gen.py", "tests/generator/sub/x.py", "tests/a.tmp",
                                                  "tests/keep.tmp", "tests/data/raw/big.json", "tests/data/clean.json",
                                                  "tests/data/secret.txt", "tests/sub/old.bak", "tests/sub/important.bak",
                                                  "tests/sub/code.py", "tests/top.dat", "tests/sub/keep.dat", "tests/sub/drop.dat", "tests/checks.py", "solution/notes/n.md",
                                                  "solution/solve.py", "environment/generator/g.py", "steps/s1/tests/x.txt",
                                                  "steps/s1/tests/y.txt", "tests/link_to_gen")}
    assert [p for p, v in out.items() if v] == [
        "tests/generator/gen.py", "tests/generator/sub/x.py", "tests/a.tmp", "tests/data/raw/big.json",
        "tests/data/secret.txt", "tests/sub/old.bak", "tests/top.dat", "tests/sub/drop.dat", "solution/notes/n.md",
        "steps/s1/tests/x.txt", "tests/link_to_gen",
    ]
    # the ignore files themselves are always left out, wherever they are
    assert ig.is_excluded(task / ".harborignore") and ig.is_excluded(task / "tests" / "sub" / ".harborignore")


def test_excluded_files_lists_each_one_once_without_the_ignore_files(task):
    assert hi.TaskIgnore(task).excluded_files() == [
        "solution/notes/n.md", "steps/s1/tests/x.txt", "tests/Dockerfile", "tests/a.tmp", "tests/data/raw/big.json",
        "tests/data/secret.txt", "tests/generator/gen.py", "tests/generator/sub/x.py", "tests/link_to_gen", "tests/sub/drop.dat",
        "tests/sub/old.bak", "tests/top.dat",
    ]
    assert hi.TaskIgnore(task).excluded_files("solution") == ["solution/notes/n.md"]


def test_a_task_without_rules_is_copied_whole(tmp_path):
    root = write(tmp_path / "t", {"tests/test.sh": "x", "tests/data/a.json": "{}"})
    assert not hi.TaskIgnore(root).has_rules()
    hi.copy_tests(root, tmp_path / "out")
    assert sorted(p.relative_to(tmp_path / "out").as_posix() for p in (tmp_path / "out").rglob("*") if p.is_file()) == [
        "data/a.json", "test.sh"]


def test_copy_tests_leaves_out_what_harbor_leaves_out_but_keeps_build_files(task, tmp_path):
    hi.copy_tests(task, tmp_path / "out")
    kept = sorted(p.relative_to(tmp_path / "out").as_posix() for p in (tmp_path / "out").rglob("*") if p.is_file() or p.is_symlink())
    # tests/Dockerfile is excluded by a rule but stays: the verifier image is built from it
    assert kept == ["Dockerfile", "checks.py", "data/clean.json", "keep.tmp", "sub/code.py", "sub/important.bak", "sub/keep.dat", "test.sh"]


def test_the_ignore_file_itself_never_reaches_a_copy(task, tmp_path):
    hi.copy_tests(task, tmp_path / "out")
    assert not list((tmp_path / "out").rglob(".harborignore"))


def test_an_unreadable_or_odd_ignore_file_means_no_rules_not_a_crash(tmp_path):
    root = write(tmp_path / "t", {"tests/test.sh": "x", "tests/a.txt": "a"})
    (root / "tests" / ".harborignore").write_bytes(b"\xff\xfe**[\n")
    assert isinstance(hi.TaskIgnore(root).excluded_files(), list)
    hi.copy_tests(root, tmp_path / "out")
    assert (tmp_path / "out" / "test.sh").exists()


def _harbor_python() -> str | None:
    """The interpreter of the Harbor install, so Harbor's own TaskFileFilter can be imported."""
    exe = shutil.which(get_settings().harbor_bin) or get_settings().harbor_bin
    try:
        first = Path(exe).resolve().read_text(errors="ignore").splitlines()[0]
    except (OSError, IndexError, UnicodeDecodeError):
        return None
    if not first.startswith("#!"):
        return None
    py = first[2:].strip().split()[0]
    probe = subprocess.run([py, "-c", "import harbor.models.task.file_filter"], capture_output=True)
    return py if probe.returncode == 0 else None


def test_agrees_with_harbors_own_filter_on_every_path(task):
    py = _harbor_python()
    if py is None:
        pytest.skip("Harbor is not importable here")
    script = (
        "import json, sys, os\n"
        "from pathlib import Path\n"
        "from harbor.models.task.file_filter import TaskFileFilter\n"
        "root = Path(sys.argv[1]); f = TaskFileFilter(root); out = {}\n"
        "for d, ds, fs in os.walk(root):\n"
        "    for n in ds + fs:\n"
        "        p = Path(d) / n\n"
        "        try: out[p.relative_to(root).as_posix()] = f.is_excluded(p)\n"
        "        except Exception as e: out[p.relative_to(root).as_posix()] = 'error: ' + type(e).__name__\n"
        "print(json.dumps(out))\n"
    )
    theirs = json.loads(subprocess.run([py, "-c", script, str(task)], capture_output=True, text=True, check=True).stdout)
    ours_filter = hi.TaskIgnore(task)
    ours = {rel: ours_filter.is_excluded(task / rel) for rel in theirs}
    assert theirs == ours
    assert any(theirs.values()) and not all(theirs.values())  # the fixture really exercises both answers


# ---- the replicas use the same rule -------------------------------------------------------------
BIG = "EXPECTED = {" + ", ".join(f"'k{i}': {i}" for i in range(80)) + "}\n"  # a 300+ character constant


def replica_task(tmp_path, with_rules: bool) -> Path:
    files = {
        "task.toml": "[task]\nname = 'terminal-bench/x'\n",
        "environment/Dockerfile": "FROM scratch\n",
        "tests/test.sh": "#!/bin/bash\npython3 checks.py\n",
        "tests/Dockerfile": "FROM scratch\n",
        "tests/checks.py": "print(1)\n",
        "tests/ground_truth.json": "{}\n",
        "tests/generator/gen.py": BIG,
        "tests/generator/data.json": "{}\n",
        "tests/answers.json": "{}\n",
    }
    if with_rules:
        files["tests/.harborignore"] = "generator/\nanswers.json\n"
    return write(tmp_path / ("a" if with_rules else "b"), files)


def test_tier0_verifier_image_is_built_from_what_harbor_would_upload(tmp_path, monkeypatch):
    from types import SimpleNamespace as NS

    from app.services.tier0 import docker_runner

    built = {}
    monkeypatch.setattr(docker_runner, "_build", lambda dockerfile, ctx, tag, platform, timeout: built.update(
        files=sorted(p.relative_to(ctx).as_posix() for p in Path(ctx).rglob("*") if p.is_file())))
    for with_rules, expected in ((True, ["Dockerfile", "checks.py", "ground_truth.json", "test.sh"]),
                                 (False, ["Dockerfile", "answers.json", "checks.py", "generator/data.json", "generator/gen.py",
                                          "ground_truth.json", "test.sh"])):
        root = replica_task(tmp_path, with_rules)
        _, staging = docker_runner.build_verifier_image(NS(root=root, slug="x"), mutant=None)
        shutil.rmtree(staging, ignore_errors=True)
        assert built["files"] == expected


def test_generated_fault_mutants_skip_files_the_verifier_does_not_have(tmp_path):
    from app.services.tier0 import mutants

    with_rules, without = replica_task(tmp_path, True) / "tests", replica_task(tmp_path, False) / "tests"
    drops = lambda d: mutants.expand(d, (mutants.GENERIC_DROP_DIRS,))
    assert [m for m in drops(with_rules) if "generator" in m or "answers" in m] == []
    assert any("generator" in m for m in drops(without)) and any("answers" in m for m in drops(without))
    empties = lambda d: [m for m in mutants.expand(d, (mutants.GENERIC_EMPTY_LITERALS,)) if "gen.py" in m]
    assert empties(with_rules) == [] and len(empties(without)) == 1


def test_the_verifier_fingerprint_ignores_files_the_verifier_does_not_get(tmp_path):
    from types import SimpleNamespace as NS

    from app.services.tier0 import replay

    root = replica_task(tmp_path, True)
    before = replay.verifier_fingerprint(NS(root=root))
    (root / "tests" / "generator" / "gen.py").write_text("changed\n")  # excluded: not part of the verifier
    assert replay.verifier_fingerprint(NS(root=root)) == before
    (root / "tests" / "checks.py").write_text("print(2)\n")  # kept: a real verifier change
    assert replay.verifier_fingerprint(NS(root=root)) != before


def test_a_task_without_rules_keeps_its_old_fingerprint(tmp_path):
    """Stored baselines compare fingerprints, so a task with no .harborignore must hash exactly as before."""
    import hashlib
    from types import SimpleNamespace as NS

    from app.services.tier0 import replay

    root = replica_task(tmp_path, False)
    h = hashlib.sha256()
    tests = root / "tests"
    for path in sorted(p for p in tests.rglob("*") if p.is_file()):
        h.update(str(path.relative_to(tests)).encode())
        h.update(b"\0")
        h.update(path.read_bytes())
    assert replay.verifier_fingerprint(NS(root=root)) == h.hexdigest()[:16]


def test_the_tier1_hint_shows_what_the_verifier_gets(tmp_path):
    from app.services.tier1 import hacker

    root = replica_task(tmp_path, True)
    assert hacker.build_hinted_task(root, tmp_path / "hinted") is None
    shown = sorted(p.relative_to(tmp_path / "hinted" / "environment" / hacker._COPY_NAME).as_posix()
                   for p in (tmp_path / "hinted" / "environment" / hacker._COPY_NAME).rglob("*") if p.is_file())
    assert shown == ["Dockerfile", "checks.py", "ground_truth.json", "test.sh"]
