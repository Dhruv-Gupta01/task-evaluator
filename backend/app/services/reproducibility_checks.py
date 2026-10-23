"""Will the task behave the same on another machine, on another day (4.1
categories G, H and I)? Text checks over the task files, no LLM, all advisory.

Thread-pool sizing (OMP/MKL/OpenBLAS against [environment].cpus) is not
repeated here: the vendored upstream check-cpu-threads covers it."""

import re
import tomllib
from pathlib import Path

import yaml

from app.services import tb_static_checks
from app.services.hygiene_checks import _listed, _named_paths, _own_files, _resolve, _run_lines, _text_files
from app.services.verifier_isolation import _agent_names

_AGENT_DIRS = ("/app", "/workspace", "/home", "/tmp", "/root", "/data")


def _toml(task_root: Path) -> dict:
    try:
        return tomllib.loads((task_root / "task.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _tests_code(task_root: Path) -> list[tuple[str, str]]:
    return [
        (p.relative_to(task_root).as_posix(), text)
        for p, text in _text_files(task_root / "tests")
        if p.suffix in (".py", ".js", ".ts", ".mjs", ".sh")
    ]


# ---- G.2: test code that is fragile under load or on another host ----------

_TIMING_WORDS = r"(?:elapsed|duration|runtime|latency|seconds|secs|millis|_ms|ms_|wall|took|time_taken|time_used)"
_PY_TIMING_ASSERT = re.compile(rf"^\s*assert\b[^\n]*{_TIMING_WORDS}\w*[^\n]*<|^\s*assert\b[^\n]*(?:perf_counter|monotonic|time\.time)\(\)\s*-", re.I | re.M)
_JS_TIMING_ASSERT = re.compile(rf"expect\([^)]*{_TIMING_WORDS}[^)]*\)\.toBeLessThan", re.I)


def check_timing_assertions(task_root: Path, report) -> None:
    hits: list[str] = []
    for rel, text in _tests_code(task_root):
        for pattern in (_PY_TIMING_ASSERT, _JS_TIMING_ASSERT):
            for m in pattern.finditer(text):
                hits.append(f"{rel}:{text.count(chr(10), 0, m.start()) + 1}")
    if hits:
        report.add(
            "test_timing_assertions",
            "warn",
            f"Wall-clock assertion(s) in the tests ({_listed(sorted(set(hits)))}). They can fail under load or on a "
            "slower host (4.1-81: 1 run in 9). Prefer a deterministic check, or leave a measured, generous margin "
            "and say how it was measured.",
        )


_SCAN_RE = re.compile(r"os\.walk\(|\.rglob\(|\.glob\(|os\.scandir\(|\.iterdir\(|os\.listdir\(")
_READ_RE = re.compile(r"\.read_bytes\(|\.read_text\(|\bopen\(")
_HANDLED_RE = re.compile(r"except\s*\(?[^)\n:]*\b(?:OSError|Exception|BaseException)\b|except\s*:")


def check_live_scans(task_root: Path, report) -> None:
    """A test that lists a directory the solution is still writing to, then
    reads every entry, fails when a temporary file disappears in between."""
    alt = "|".join(re.escape(d) for d in _AGENT_DIRS)
    hits: list[str] = []
    for rel, text in _tests_code(task_root):
        if not rel.endswith(".py") or "FileNotFoundError" in text:
            continue

        def live_re(names: set[str]) -> re.Pattern:
            return re.compile(rf"[\"'](?:{alt})(?:/|[\"'])" + "".join(rf"|\b{re.escape(n)}\b" for n in names))

        names = _agent_names(text, _AGENT_DIRS)
        # A loop over a list that holds an agent directory: `for root in [APP, Path("/tmp")]: root.rglob(...)`.
        names |= {
            m.group(1)
            for m in re.finditer(r"^\s*for\s+(\w+)\s+in\s+(\[[^\]\n]*\]|\([^)\n]*\))", text, re.M)
            if live_re(names).search(m.group(2))
        }
        live = live_re(names)
        lines = text.splitlines()
        for i, line in enumerate(lines, 1):
            if not (_SCAN_RE.search(line) and live.search(line)):
                continue
            # A broad handler protects the scan only inside its own loop: an `except OSError` elsewhere
            # in the file says nothing about this one (4.1-35's file had one and still raced).
            if _READ_RE.search(text) and not _HANDLED_RE.search("\n".join(lines[i : i + 25])):
                hits.append(f"{rel}:{i}")
    if hits:
        report.add(
            "test_live_directory_scan",
            "warn",
            f"A test lists a directory the solution writes to and reads each file without handling "
            f"FileNotFoundError ({_listed(hits)}). A temporary or journal file removed mid-scan fails the run at "
            "random (4.1-35).",
        )


_SH_DROP_RE = re.compile(r"\b(?:setpriv|runuser|gosu)\b|\bsu\s+-?\w|\bsudo\s+-u\b")


def check_privilege_drop_cwd(task_root: Path, report) -> None:
    """A process started as another user keeps the parent's working directory;
    if that user cannot read it, some setups fail at start or at teardown."""
    hits: list[str] = []
    test_sh = task_root / "tests" / "test.sh"
    if test_sh.is_file():
        seen_cd = False
        for i, line in enumerate(test_sh.read_text(errors="replace").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if re.search(r"(?:^|&&|;)\s*cd\s", line):
                seen_cd = True
            if _SH_DROP_RE.search(line) and not seen_cd:
                hits.append(f"tests/test.sh:{i}")
    for rel, text in _tests_code(task_root):
        if not rel.endswith(".py"):
            continue
        for m in re.finditer(r"subprocess\.(?:run|Popen|call|check_call|check_output)\(", text):
            call = text[m.start() : m.start() + 1500]
            depth, end = 0, len(call)
            for j, ch in enumerate(call):
                depth += ch == "("
                depth -= ch == ")"
                if depth == 0 and ch == ")":
                    end = j
                    break
            call = call[:end]
            if re.search(r"\buser\s*=", call) and not re.search(r"\bcwd\s*=", call):
                hits.append(f"{rel}:{text.count(chr(10), 0, m.start()) + 1}")
    if hits:
        report.add(
            "privilege_drop_cwd",
            "warn",
            f"A command runs as another user without first changing to a directory that user can read "
            f"({_listed(hits)}). It inherits the verifier's working directory; on some setups that fails at start "
            "or at teardown and the trial gets reward 0 with every test passed (4.1-76). `cd /tmp` (or pass cwd=) first.",
        )


# ---- G.6: a non-root USER in the image ---------------------------------------

def check_image_user(task_root: Path, report) -> None:
    dockerfile = task_root / "environment" / "Dockerfile"
    if not dockerfile.is_file():
        return
    users = re.findall(r"^\s*USER\s+(\S+)", dockerfile.read_text(errors="replace"), re.I | re.M)
    agent_user = (_toml(task_root).get("agent") or {}).get("user")
    if users and users[-1] not in ("root", "0") and not agent_user:
        report.add(
            "dockerfile_user",
            "info",
            f"environment/Dockerfile ends with USER {users[-1]}. Some remote sandboxes hang during setup when the "
            "image's own user is not root (4.1-31); [agent].user in task.toml sets the agent's user instead.",
        )


# ---- H.2: dependencies of pinned packages -----------------------------------

_PIP_INSTALL_RE = re.compile(r"\b(?:pip3?|uv\s+pip|python3?\s+-m\s+pip)\s+install\b([^;&|]*)")


def _floating_pins(command: str) -> list[str]:
    """Pinned top-level packages whose own dependencies are left to float."""
    out: list[str] = []
    for m in _PIP_INSTALL_RE.finditer(command):
        args = m.group(1)
        if re.search(r"--no-deps|(?:^|\s)-c\s|--constraint|--require-hashes|(?:^|\s)-r\s|--requirement", args):
            continue
        out += [a for a in args.split() if "==" in a and not a.startswith("-")]
    return out


def check_transitive_pins(task_root: Path, report) -> None:
    solve = task_root / "solution" / "solve.sh"
    in_solve = _floating_pins(solve.read_text(errors="replace")) if solve.is_file() else []
    dockerfile = task_root / "environment" / "Dockerfile"
    in_image = [p for run in (_run_lines(dockerfile.read_text(errors="replace")) if dockerfile.is_file() else []) for p in _floating_pins(run)]
    if in_solve:
        report.add(
            "transitive_pins",
            "warn",
            f"solution/solve.sh pins {_listed(in_solve)} but not their dependencies, and installs them on every run. "
            "A new release of a dependency can break the Oracle later (4.1-28: build123d pinned, its dependency "
            "not). Install from a fully pinned requirements file with --require-hashes, or bake it into the image.",
        )
    if in_image:
        report.add(
            "transitive_pins",
            "info",
            f"environment/Dockerfile pins {_listed(in_image)} but not their dependencies: the built image is fixed, "
            "but a rebuild later can pick up different versions.",
        )


# ---- H.3: every image digest-pinned ------------------------------------------

def _dockerfiles(task_root: Path) -> list[Path]:
    return [
        p for p in _own_files(task_root)
        if p.name == "Dockerfile" or p.name.startswith("Dockerfile.") or p.name.endswith(".Dockerfile")
    ]


def check_all_images_pinned(task_root: Path, report) -> None:
    unpinned: list[str] = []
    for path in _dockerfiles(task_root):
        stages: set[str] = set()
        rel = path.relative_to(task_root).as_posix()
        # `from x import y` in a heredoc is Python, not a build stage.
        for m in re.finditer(r"^\s*FROM\s+(?!\S+\s+import\b)(?:--\S+\s+)*(\S+)(?:\s+AS\s+(\S+))?", path.read_text(errors="replace"), re.I | re.M):
            image, alias = m.group(1), m.group(2)
            if image.lower() not in stages and image != "scratch" and "@sha256:" not in image:
                unpinned.append(f"{image} ({rel})")
            if alias:
                stages.add(alias.lower())
    env = task_root / "environment"
    for path in sorted(env.glob("docker-compose*.y*ml")) + sorted(env.glob("compose*.y*ml")) if env.is_dir() else []:
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except (OSError, yaml.YAMLError):
            continue
        for name, service in ((data.get("services") or {}) if isinstance(data, dict) else {}).items():
            image = (service or {}).get("image") if isinstance(service, dict) else None
            if image and "@sha256:" not in str(image):
                unpinned.append(f"{image} (service {name} in {path.name})")
    if unpinned:
        report.add(
            "all_images_digest_pinned",
            "warn",
            f"Images without a digest: {_listed(unpinned)}. Every FROM (build stages, tests/Dockerfile, helper "
            "services) and every compose image: should be pinned with @sha256:. Pin the multi-platform index "
            "digest, not one platform's, or the other DOCKER_PLATFORM will fail to pull it.",
        )
    else:
        report.add("all_images_digest_pinned", "info", "Every FROM and compose image: is digest-pinned.")


# ---- H.4: downloads at run time ----------------------------------------------

_DOWNLOAD_RE = re.compile(
    r"\b(?:curl|wget)\s+\S|\b(?:pip3?|uv\s+pip|python3?\s+-m\s+pip)\s+install\b|\bnpm\s+(?:install|i|ci)\b|\bnpx\s+\S|"
    r"\bgit\s+clone\b|\bapt(?:-get)?\s+install\b|\bcargo\s+install\b|\bgo\s+(?:install|get)\b|\bhuggingface-cli\s+download\b"
)


def check_runtime_downloads(task_root: Path, report) -> None:
    targets: list[Path] = []
    if (task_root / "solution" / "solve.sh").is_file():
        targets.append(task_root / "solution" / "solve.sh")
    instruction = task_root / "instruction.md"
    if instruction.is_file():
        for ref in _named_paths(instruction.read_text(errors="replace")):
            hit = _resolve(task_root, ref)
            if hit and hit.suffix in (".sh", ".py") and hit.relative_to(task_root).parts[0] == "environment":
                targets.append(hit)
    hits: list[str] = []
    for path in targets:
        for i, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            if not line.lstrip().startswith("#") and _DOWNLOAD_RE.search(line):
                hits.append(f"{path.relative_to(task_root).as_posix()}:{i}")
    if hits:
        report.add(
            "runtime_downloads",
            "warn",
            f"Downloads or installs at run time ({_listed(hits)}). The result then depends on the network and on "
            "whatever is current that day; bake packages and data into the image instead (4.1-47).",
        )


# ---- H.5: what the separate verifier image needs -----------------------------

_COPY_DEST_RE = re.compile(r"^\s*(?:COPY|ADD)\s+(?:--\S+\s+)*(?:\S+\s+)+(\S+)\s*$", re.I | re.M)


def _copy_dests(dockerfile: Path) -> list[str]:
    if not dockerfile.is_file():
        return []
    return [d.rstrip("/") or "/" for d in _COPY_DEST_RE.findall(dockerfile.read_text(errors="replace"))]


def _installs_named(text: str, artifact: str) -> bool:
    """A tests/ code file that runs `install ... -r` and names the collected file and its folder, with the
    path held in a variable or split across an argv list, e.g. `requirements = Path("/app/requirements.txt")`
    then `[..., "install", "-r", str(requirements)]`. tests/Dockerfile is not read here: its own
    requirements.txt is a different file."""
    path = Path(artifact)
    return bool(
        re.search(r"\binstall\b[\s\S]{0,400}?[\"'\s](?:-r|--requirement)[\"'\s]", text)
        and path.name in text
        and re.search(rf"[\"']{re.escape(path.parent.as_posix())}[/\"']", text)
    )


def check_verifier_inputs(task_root: Path, report) -> None:
    toml = _toml(task_root)
    verifier = toml.get("verifier") or {}
    if verifier.get("environment_mode") != "separate" and "environment" not in verifier:
        return
    tests_df = task_root / "tests" / "Dockerfile"
    if not tests_df.is_file():
        return
    artifacts = [a if isinstance(a, str) else str((a or {}).get("source", "")) for a in toml.get("artifacts") or []]
    tests_text = tests_df.read_text(errors="replace") + "\n".join(
        t for _, t in _tests_code(task_root)
    )
    problems: list[str] = []
    for artifact in artifacts:
        name = Path(artifact).name
        if re.fullmatch(r"requirements[\w.-]*\.txt|pyproject\.toml|package(?:-lock)?\.json|Cargo\.toml", name):
            # The verifier image usually has a requirements.txt of its own, so
            # only an install naming the collected file's own path counts.
            parent = re.escape(str(Path(artifact).parent.as_posix()))
            if not re.search(
                rf"install[^\n]*{re.escape(artifact)}|cd\s+{parent}\b[^\n]*(?:install|npm\s+ci|cargo\s)",
                tests_text,
            ) and not any(_installs_named(text, artifact) for _, text in _tests_code(task_root)):
                problems.append(f"task.toml collects {artifact} for the verifier, but nothing in tests/ installs from it")
    env_dests = _copy_dests(task_root / "environment" / "Dockerfile")
    verifier_dests = _copy_dests(tests_df) + [a.rstrip("/") for a in artifacts]
    refs = set(re.findall(r"[\"'\s](/(?:app|data|workspace)/[\w.-]+)", tests_text))
    for ref in sorted(refs):
        supplied_to_agent = any(ref == d or ref.startswith(d + "/") for d in env_dests if d not in ("/", "/app"))
        given_to_verifier = any(ref == d or ref.startswith(d + "/") or d.startswith(ref + "/") for d in verifier_dests)
        if supplied_to_agent and not given_to_verifier:
            problems.append(f"the tests use {ref}, which environment/Dockerfile supplies but tests/Dockerfile does not")
    if problems:
        report.add(
            "verifier_image_inputs",
            "warn",
            "Separate verifier image may lack what the task supplies: " + _listed(problems) + ". The Oracle may "
            "never touch it, so only an agent that uses it would fail (4.1-64).",
        )


# ---- I.3: template placeholders -----------------------------------------------

def check_placeholders(task_root: Path, report) -> None:
    left = tb_static_checks.placeholders(task_root)
    if left:
        report.add(
            "template_placeholders",
            "warn",
            f"Template placeholders were not filled in: {_listed(left)}. TB's task-fields check reports some of "
            "these only as an invalid GitHub username.",
        )


def run_reproducibility_checks(task_root: Path, report) -> None:
    for check in (
        check_timing_assertions,
        check_live_scans,
        check_privilege_drop_cwd,
        check_image_user,
        check_transitive_pins,
        check_all_images_pinned,
        check_runtime_downloads,
        check_verifier_inputs,
        check_placeholders,
    ):
        try:
            check(task_root, report)
        except Exception as exc:  # advisory: one bad file must not break the stage
            report.add(check.__name__, "info", f"check could not run: {type(exc).__name__}")
