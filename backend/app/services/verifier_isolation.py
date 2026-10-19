"""Does the verifier keep code the agent wrote away from the grade (4.1
categories B and C)?

Text checks over tests/, test.sh, the Dockerfiles and task.toml. No LLM, all
advisory: a finding means "a human should read this part of the verifier",
never a failure. The safe pattern they compare against (upstream's isolation
fixes): the grader's own process never imports code the agent wrote; agent
code runs in a child process as an unprivileged user, after /tests is made
unreadable to that user; the verifier owns the result files and writes the
reward last; nothing the agent wrote is evaluated as code."""

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from app.services.hygiene_checks import _listed, _own_files, _text_files

_AGENT_DIRS = ("/app", "/workspace", "/home", "/tmp", "/root", "/srv", "/data", "/opt/app")
_DROP_RE = re.compile(r"\b(?:setpriv|runuser|gosu|s6-setuidgid|setuidgid|chpst)\b|\bsu\s+-?\w|\bsudo\s+-u\b|--userspec")
_PY_DROP_RE = re.compile(r"\b(?:user|group|preexec_fn)\s*=|setpriv|runuser|gosu|\"su\"|'su'|sudo")


@dataclass
class _Task:
    root: Path
    separate: bool
    agent_user: str
    verifier_user: str
    workdir: str | None
    artifacts: list[str]
    test_sh: str
    tests_py: list[tuple[str, str]] = field(default_factory=list)

    @property
    def agent_dirs(self) -> tuple[str, ...]:
        return _AGENT_DIRS + ((self.workdir,) if self.workdir and self.workdir != "/" else ())


def _is_root(user: str) -> bool:
    return user in ("root", "0", "")


def _last_instruction(dockerfile: Path, keyword: str) -> str | None:
    if not dockerfile.is_file():
        return None
    found = re.findall(rf"^\s*{keyword}\s+(\S+)", dockerfile.read_text(errors="replace"), re.I | re.M)
    return found[-1].strip("\"'") if found else None


def _load(task_root: Path) -> _Task:
    try:
        toml = tomllib.loads((task_root / "task.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        toml = {}
    verifier = toml.get("verifier") or {}
    separate = verifier.get("environment_mode") == "separate" or "environment" in verifier
    env_df = task_root / "environment" / "Dockerfile"
    agent_user = str((toml.get("agent") or {}).get("user") or _last_instruction(env_df, "USER") or "root")
    verifier_df = task_root / "tests" / "Dockerfile" if separate else env_df
    verifier_user = str(verifier.get("user") or _last_instruction(verifier_df, "USER") or "root")
    test_sh_path = task_root / "tests" / "test.sh"
    return _Task(
        root=task_root,
        separate=separate,
        agent_user=agent_user,
        verifier_user=verifier_user,
        workdir=_last_instruction(env_df, "WORKDIR"),
        artifacts=[a if isinstance(a, str) else str(a.get("source", "")) for a in toml.get("artifacts") or []],
        test_sh=test_sh_path.read_text(errors="replace") if test_sh_path.is_file() else "",
        tests_py=[
            (p.relative_to(task_root).as_posix(), text)
            for p, text in _text_files(task_root / "tests")
            if p.suffix == ".py"
        ],
    )


def _code_lines(text: str):
    """(line number, line) for lines that are not comments."""
    for i, line in enumerate(text.splitlines(), 1):
        if not line.lstrip().startswith("#"):
            yield i, line


def _agent_names(text: str, dirs: tuple[str, ...]) -> set[str]:
    """Variables a test file binds to an agent-writable path, e.g. MIGRATE = Path("/app/migrate.py")."""
    alt = "|".join(re.escape(d) for d in dirs)
    pattern = re.compile(rf"^\s*([A-Za-z_]\w*)\s*(?::[^=\n]+)?=[^\n]*[\"'](?:{alt})(?:/|[\"'])", re.M)
    return set(pattern.findall(text))


def _mentions_agent(snippet: str, dirs: tuple[str, ...], names: set[str]) -> bool:
    if any(re.search(rf"[\"'\s=]{re.escape(d)}(?:/|[\"'\s]|$)", snippet) for d in dirs):
        return True
    return any(re.search(rf"\b{re.escape(n)}\b", snippet) for n in names)


def _calls(text: str, opener: re.Pattern):
    """(line number, full call text) for each call matching `opener`, up to its closing parenthesis."""
    for m in opener.finditer(text):
        depth, end = 0, m.end() - 1
        for j in range(m.end() - 1, min(len(text), m.end() + 2000)):
            if text[j] == "(":
                depth += 1
            elif text[j] == ")":
                depth -= 1
                if depth == 0:
                    end = j
                    break
        line = text.count("\n", 0, m.start()) + 1
        if not text[text.rfind("\n", 0, m.start()) + 1 : m.start()].lstrip().startswith("#"):
            yield line, text[m.start() : end + 1]


_SUBPROCESS_RE = re.compile(
    r"\b(?:subprocess\.(?:run|Popen|call|check_call|check_output)|os\.system|os\.popen|pexpect\.spawn|"
    r"asyncio\.create_subprocess_(?:exec|shell))\("
)


# ---- the reward file -------------------------------------------------------

_RESETS_JSON = re.compile(
    r"\brm\b[^\n]*(?:reward\.json|/logs/verifier/\*)|>\s*\S*reward\.json|find\s+/logs/verifier[^\n]*-delete"
)
_RESETS_TXT = re.compile(r"\brm\b[^\n]*(?:reward\.txt|/logs/verifier/\*)|find\s+/logs/verifier[^\n]*-delete")


def check_reward_reset(t: _Task, report) -> None:
    if not t.test_sh:
        return
    if t.separate:
        report.add(
            "verifier_reward_reset", "info",
            "Separate verifier: Harbor empties /logs/verifier in the verifier's own container before test.sh runs.",
        )
        return
    if not _RESETS_JSON.search(t.test_sh):
        report.add(
            "verifier_reward_reset", "warn",
            "Shared verifier: Harbor 0.23 reads /logs/verifier/reward.json before reward.txt and does not empty "
            "/logs/verifier before test.sh runs, and that folder is mounted in the agent's container. This "
            "test.sh never removes or writes reward.json, so one already there would decide the reward. Start "
            "test.sh with `rm -f /logs/verifier/reward.json /logs/verifier/reward.txt`, or use a separate verifier.",
        )
    elif not _RESETS_TXT.search(t.test_sh):
        report.add(
            "verifier_reward_reset", "info",
            "test.sh handles reward.json; also remove reward.txt at the start, in case the script is stopped "
            "before it writes its own.",
        )
    else:
        report.add("verifier_reward_reset", "info", "test.sh clears the reward files before grading.")


# ---- agent code inside the grader ------------------------------------------

_PY_IMPORT_RE = re.compile(
    r"sys\.path\.(?:insert|append)\(|spec_from_file_location\(|SourceFileLoader\(|run_path\(|"
    r"import_module\(|__import__\(|(?<![\w.])exec\(\s*(?:open|compile|\w+\.read)"
)
_PYTEST_MODULE_RE = re.compile(r"\bpython[\d.]*\s+(?:-\w+\s+)*-m\s+pytest\b")
_ISOLATED_PY_RE = re.compile(r"\bpython[\d.]*\s+(?:-\w*\s+)*-\w*[IP]\w*\s")


def _cwd_at(test_sh: str, needle: re.Pattern, default: str | None) -> str | None:
    cwd = default
    for line in test_sh.splitlines():
        if line.lstrip().startswith("#"):
            continue
        for m in re.finditer(r"(?:^|&&|;)\s*cd\s+([^\s;&|]+)", line):
            cwd = m.group(1).strip("\"'")
        if needle.search(line):
            return cwd
    return None


def check_agent_code_in_process(t: _Task, report) -> None:
    hits: list[str] = []
    for rel, text in t.tests_py:
        names = _agent_names(text, t.agent_dirs)
        for line, call in _calls(text, _PY_IMPORT_RE):
            if _mentions_agent(call, t.agent_dirs, names):
                hits.append(f"{rel}:{line}")
    for m in re.finditer(r"PYTHONPATH=([^\s;]+)", t.test_sh):
        if any(part.rstrip("/") in t.agent_dirs or part.startswith(t.agent_dirs) for part in m.group(1).split(":")):
            hits.append(f"tests/test.sh: PYTHONPATH={m.group(1)}")
    if not t.separate and t.test_sh:
        cwd = _cwd_at(t.test_sh, _PYTEST_MODULE_RE, t.workdir)
        line = next((ln for ln in t.test_sh.splitlines() if _PYTEST_MODULE_RE.search(ln)), "")
        if cwd and cwd.rstrip("/") in t.agent_dirs and not _ISOLATED_PY_RE.search(line):
            hits.append(
                f"tests/test.sh: `python -m pytest` runs from {cwd}, which `python -m` puts first on sys.path"
            )
    if hits:
        report.add(
            "verifier_imports_agent_code",
            "warn",
            "The grader's own process may load code the agent wrote: " + _listed(hits) + ". Code loaded into "
            "the pytest process can change its results. Run agent code only in a child process and keep "
            "agent-writable directories off sys.path (needs a human to confirm).",
        )
    else:
        report.add("verifier_imports_agent_code", "info", "No test loads agent-written code into the pytest process.")
    if not t.separate and _is_root(t.agent_user):
        report.add(
            "verifier_shared_container",
            "info",
            "Shared verifier and the agent ran as root: the tests run in the container the agent fully "
            "controlled, so installed packages and files outside /tests may have been changed. A separate "
            "verifier ([verifier] environment_mode = \"separate\") avoids this.",
        )


def _shell_lines(text: str):
    """(first line number, command) with backslash continuations joined."""
    start, parts = 0, []
    for i, raw in enumerate(text.splitlines(), 1):
        if not parts:
            start = i
        line = raw.rstrip()
        if line.endswith("\\"):
            parts.append(line[:-1])
            continue
        parts.append(line)
        yield start, " ".join(p.strip() for p in parts)
        parts = []
    if parts:
        yield start, " ".join(p.strip() for p in parts)


# Running a file: an interpreter followed by the file it runs.
_INTERPRETERS = {"python", "python3", "sys.executable", "bash", "sh", "zsh", "node", "ruby", "perl", "source", "."}
# Building or installing what is in a directory: the directory's own files decide what runs.
_BUILD_TOOLS = {"make", "cmake", "npm", "yarn", "pnpm", "cargo", "go", "lake", "mvn", "gradle", "uv"}


def _is_agent_path(token: str, cwd: str | None, dirs: tuple[str, ...]) -> bool:
    token = token.strip("\"'")
    if any(token == d or token.startswith(d + "/") for d in dirs):
        return True
    return bool(cwd) and cwd.rstrip("/") in dirs and not token.startswith(("/", "-", "$"))


# Commands that run another command (often to drop privileges); what they run is checked instead.
_WRAPPERS = {
    "sudo", "env", "exec", "timeout", "nice", "nohup", "stdbuf", "time", "setpriv", "runuser", "gosu", "su",
    "chroot", "s6-setuidgid", "setuidgid", "chpst",
}


def _starts_command(word: str, dirs: tuple[str, ...]) -> bool:
    base = word.rsplit("/", 1)[-1].rstrip("0123456789.")
    return base in _INTERPRETERS or base in _BUILD_TOOLS or base in ("pip", "pip3") or any(
        word.startswith(d + "/") for d in dirs
    )


def _command_runs_agent(words: list[str], cwd: str | None, dirs: tuple[str, ...]) -> bool:
    """Whether one command (already split into words, flags kept) executes agent-written code."""
    if not words:
        return False
    if words[0] in _WRAPPERS or ("=" in words[0] and not words[0].startswith("-")):
        return any(
            _command_runs_agent(words[i:], cwd, dirs) for i in range(1, len(words)) if _starts_command(words[i], dirs)
        )
    head = words[0].rsplit("/", 1)[-1].rstrip("0123456789.")
    rest = [w for w in words[1:] if not w.startswith("-")]
    if any(words[0].startswith(d + "/") for d in dirs):
        return True
    if head in _INTERPRETERS or words[0] in _INTERPRETERS:
        if "-c" in words[1:]:
            return False  # inline code; what it runs cannot be told from here
        return bool(rest) and _is_agent_path(rest[0], cwd, dirs)
    if head in ("pip", "pip3") or (head == "uv" and rest[:2] == ["pip", "install"]):
        args = words[1:]
        local = [args[i + 1] for i, a in enumerate(args[:-1]) if a in ("-r", "--requirement", "-e", "--editable")]
        local += [a for a in args if a.startswith((".", "/"))]
        return "install" in args and any(_is_agent_path(p, cwd, dirs) for p in local)
    if head in _BUILD_TOOLS:
        if "-g" in words or "--global" in words:
            return False
        target = next((words[i + 1] for i, w in enumerate(words[:-1]) if w in ("-C", "--prefix", "--manifest-path", "--dir")), None)
        return _is_agent_path(target, None, dirs) if target else bool(cwd) and cwd.rstrip("/") in dirs
    return False


def _sh_runs_agent(line: str, cwd: str | None, dirs: tuple[str, ...]) -> bool:
    for segment in re.split(r"&&|\|\||;|\|", line):
        words = segment.replace('"', " ").replace("'", " ").split()
        if words and words[0] != "cd" and _command_runs_agent(words, cwd, dirs):
            return True
    return False


def _list_tokens(call: str) -> list[str] | None:
    """The first list literal in a call (its argv), as plain tokens; None when there is none."""
    m = re.search(r"\(\s*\[", call)
    if not m:
        return None
    tokens, depth, current = [], 0, ""
    for ch in call[m.end():]:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                break
            depth -= 1
        if ch == "," and depth == 0:
            tokens.append(current.strip())
            current = ""
            continue
        current += ch
    tokens.append(current.strip())
    out = []
    for tok in tokens:
        tok = re.sub(r"^(?:str|os\.fspath)\((.*)\)$", r"\1", tok)
        tok = re.sub(r"^[rbfu]*([\"'])(.*)\1$", r"\2", tok)
        if tok:
            out.append(tok)
    return out


def _py_call_runs_agent(call: str, names: set[str], dirs: tuple[str, ...]) -> bool:
    cwd_match = re.search(r"cwd\s*=\s*([^,)]+)", call)
    cwd = None
    if cwd_match:
        value = cwd_match.group(1).strip()
        cwd = dirs[0] if value in names or _is_agent_path(value.strip("\"'"), None, dirs) else value.strip("\"'")
    tokens = _list_tokens(call)
    if tokens is None:
        command = re.search(r"\(\s*[rbfu]*[\"']([^\"']+)[\"']", call)
        return bool(command) and _sh_runs_agent(command.group(1), cwd, dirs)
    words = ["/app/__agent__" if tok in names else tok for tok in tokens]
    if words and words[0] in ("sys.executable",):
        words[0] = "python3"
    return _command_runs_agent(words, cwd, dirs)


def _agent_runs(t: _Task) -> list[tuple[str, bool, str]]:
    """(where, privilege dropped?, call text) for each place the verifier runs agent-written code."""
    runs: list[tuple[str, bool, str]] = []
    dirs = t.agent_dirs
    for rel, text in t.tests_py:
        names = _agent_names(text, dirs)
        for line, call in _calls(text, _SUBPROCESS_RE):
            if _py_call_runs_agent(call, names, dirs):
                runs.append((f"{rel}:{line}", bool(_PY_DROP_RE.search(call)), call))
    cwd = t.workdir
    for i, line in _shell_lines(t.test_sh):
        if line.lstrip().startswith("#"):
            continue
        for m in re.finditer(r"(?:^|&&|;)\s*cd\s+([^\s;&|]+)", line):
            cwd = m.group(1).strip("\"'")
        if "pytest" not in line and _sh_runs_agent(line, cwd, dirs):
            runs.append((f"tests/test.sh:{i}", bool(_DROP_RE.search(line)), line))
    return runs


_LOCKS_TESTS_RE = re.compile(
    r"chmod\s+(?:-R\s+)?(?:0?7[05]0|go-\w+|o-\w+|og-\w+|go=|o=)\S*\s+[^\n]*/tests\b"
    r"|os\.chmod\([^)]*/tests[^)]*0o7[05]0|\.chmod\(0o7[05]0\)"
)


def check_agent_code_privileges(t: _Task, report) -> None:
    runs = _agent_runs(t)
    if not runs:
        report.add("verifier_runs_agent_code", "info", "No place found where the verifier runs agent-written code.")
        return
    undropped = [w for w, dropped, _ in runs if not dropped]
    if undropped and _is_root(t.verifier_user):
        report.add(
            "verifier_runs_agent_code",
            "warn",
            f"The verifier runs agent-written code as root ({_listed(undropped)}): builds, installs and scripts "
            "it supplies run with full rights before the reward is written, and can read /tests. Run them as an "
            "unprivileged user (subprocess user=, setpriv, runuser) and keep the grading in the parent "
            "(needs a human to confirm).",
        )
    elif undropped:
        report.add(
            "verifier_runs_agent_code", "info",
            f"The verifier runs agent-written code as {t.verifier_user} ({_listed(undropped)}).",
        )
    else:
        report.add(
            "verifier_runs_agent_code", "info",
            f"Agent-written code runs as a separate user ({_listed([w for w, _, _ in runs])}).",
        )

    locked = bool(_LOCKS_TESTS_RE.search(t.test_sh) or any(_LOCKS_TESTS_RE.search(text) for _, text in t.tests_py))
    dropped = [w for w, d, _ in runs if d]
    if dropped and not locked:
        report.add(
            "verifier_tests_readable",
            "warn",
            f"Agent code runs as another user ({_listed(dropped)}) but nothing makes /tests unreadable to it first; "
            "Harbor copies /tests with default permissions, so expected answers stay readable. `chmod 700 /tests` "
            "before running it.",
        )
    evaluator_reach = [
        w for w, _, call in runs
        if re.search(r"cwd\s*=\s*[^,)]*tests|PYTHONPATH[^,)]*?/tests", call)
    ]
    if evaluator_reach:
        report.add(
            "verifier_tests_importable",
            "warn",
            f"Agent code runs with /tests as its working directory or on its path ({_listed(evaluator_reach)}): "
            "it can import the reference evaluator or read the expected outputs.",
        )


# ---- evaluating answer text, verdict channels, seeds ------------------------

_UNSAFE_LOADS = (
    (re.compile(r"(?<![\w.])eval\("), "eval()"),
    (re.compile(r"(?<![\w.])exec\((?!\s*(?:open|compile))"), "exec()"),
    (re.compile(r"\bsympify\("), "sympify()"),
    (re.compile(r"\bparse_expr\("), "parse_expr()"),
    (re.compile(r"\b(?:c?pickle|dill|cloudpickle|joblib)\.loads?\("), "unpickling"),
    (re.compile(r"\bmarshal\.loads?\("), "marshal.load"),
    (re.compile(r"\byaml\.load\((?![^)]*Safe)"), "yaml.load without SafeLoader"),
    (re.compile(r"\btorch\.load\((?![^)]*weights_only\s*=\s*True)"), "torch.load without weights_only=True"),
    (re.compile(r"allow_pickle\s*=\s*True"), "numpy allow_pickle=True"),
)


def check_answer_evaluation(t: _Task, report) -> None:
    hits: list[str] = []
    for rel, text in t.tests_py:
        for i, line in _code_lines(text):
            for pattern, label in _UNSAFE_LOADS:
                if pattern.search(line):
                    hits.append(f"{label} at {rel}:{i}")
    if hits:
        report.add(
            "verifier_evaluates_answer",
            "warn",
            f"The grader evaluates or unpickles data ({_listed(hits)}). If that data can come from the agent, "
            "code it wrote runs inside the grader; parse a data-only format (JSON, a strict parser) instead.",
        )


def check_verdict_channels(t: _Task, report) -> None:
    piped = [
        rel for rel, text in t.tests_py
        if "os.fork(" in text and re.search(r"os\.pipe\(|socketpair\(|\bPipe\(", text)
    ]
    if piped:
        report.add(
            "verifier_inherited_pipe",
            "warn",
            f"A test forks and passes results through a pipe ({_listed(piped)}). A forked child that runs "
            "submitted code inherits the pipe's descriptors, so the submission can write to the result channel. "
            "Have the child return data the parent validates, from a process that cannot reach the verdict.",
        )
    scratch = sorted({
        m.group(1)
        for text in [t.test_sh] + [x for _, x in t.tests_py]
        for m in re.finditer(r"[\"'\s](/tmp/[\w./-]*(?:score|verdict|result|status|bench|timing|metric)[\w./-]*)", text)
    })
    if scratch:
        report.add(
            "verifier_result_files",
            "info",
            f"The verifier reads or writes result files under /tmp ({_listed(scratch)}). Processes the agent left "
            "running can write there too; keep result files in a directory only the verifier can write.",
        )
    seeded = sorted({
        p.name for p in _own_files(t.root / "tests") if re.search(r"seed[_-]?\d+", p.name, re.I)
    } | {
        Path(m.group(0)).name
        for _, text in t.tests_py for m in re.finditer(r"[\w./-]*seed[_-]?\d+[\w.-]*\.\w+", text, re.I)
    })
    if seeded:
        report.add(
            "verifier_seeded_inputs",
            "info",
            f"Test input names carry their seed ({_listed(seeded)}). A program that can see the name can regenerate "
            "the expected answer instead of solving the input.",
        )


# ---- Lean --------------------------------------------------------------------

_LEAN_KEYWORDS = re.compile(r"(?m)^\s*(?:@\[[^\]]*\]\s*)?(macro_rules|macro|notation|syntax|elab|set_option|axiom)\b|\bsorry\b")
_LEAN_GUARD = re.compile(r"#print\s+axioms|\b(macro|notation|axiom|sorry)\b")


def check_lean(t: _Task, report) -> None:
    lean = [p for p in _own_files(t.root) if p.suffix == ".lean"]
    if not lean:
        return
    found = sorted({
        f"{m.group(1) or 'sorry'} in {p.relative_to(t.root).as_posix()}"
        for p in lean if p.relative_to(t.root).parts[0] in ("solution", "environment")
        for m in _LEAN_KEYWORDS.finditer(p.read_text(errors="replace"))
    })
    guarded = _LEAN_GUARD.search(t.test_sh) or any(_LEAN_GUARD.search(x) for _, x in t.tests_py)
    if not guarded:
        report.add(
            "lean_submission_checks",
            "warn",
            "Lean task, but the verifier does not seem to check submitted files for macro, notation, syntax, "
            "elab, set_option, axiom or sorry. These can change what the stated theorem means (4.1-43); check "
            "`#print axioms` on the final theorem and reject new notation or macros (needs a human).",
        )
    if found:
        report.add("lean_keywords", "info", f"Lean commands to review in the task's own files: {_listed(found)}.")


def run_isolation_checks(task_root: Path, report) -> None:
    t = _load(task_root)
    for check in (
        check_reward_reset,
        check_agent_code_in_process,
        check_agent_code_privileges,
        check_answer_evaluation,
        check_verdict_channels,
        check_lean,
    ):
        try:
            check(t, report)
        except Exception as exc:  # advisory: one odd file must not break the stage
            report.add(check.__name__, "info", f"check could not run: {type(exc).__name__}")
