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
_DROP_CONST_RE = re.compile(r"\b(?:RUNUSER|SETPRIV|GOSU|SUDO)\b")  # the same tools held in an upper-case constant
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
    """Variables a test file binds to an agent-writable path, e.g. MIGRATE = Path("/app/migrate.py"),
    and (two passes) variables built from one, e.g. sources = sorted((APP / "src").glob("*.cc"))."""
    alt = "|".join(re.escape(d) for d in dirs)
    pattern = re.compile(rf"^\s*([A-Za-z_]\w*)\s*(?::[^=\n]+)?=[^\n]*[\"'](?:{alt})(?:/|[\"'])", re.M)
    names = set(pattern.findall(text))
    assign = re.compile(r"^\s*([A-Za-z_]\w*)\s*(?::[^=\n]+)?=([^=\n][^\n]*)$", re.M)
    for _ in range(2):
        for name, rhs in assign.findall(text):
            if name in names or re.search(r"\.read_(?:text|bytes)\(|json\.loads?\(|\bopen\(", rhs):
                continue
            if any(re.search(rf"\b{re.escape(n)}\b", rhs) for n in names):
                names.add(name)
    return names


def _constants(text: str) -> dict[str, str]:
    """NAME = "literal" (or Path("literal")) assignments, so a call through SAGE or RUNUSER can be read."""
    found = {}
    for name, value in re.findall(r"^\s*([A-Za-z_]\w*)\s*(?::[^=\n]+)?=\s*(?:Path\()?[rbfu]*[\"']([^\"'\n]+)[\"']\)?\s*$", text, re.M):
        found.setdefault(name, value)
    return found


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


def _import_hits(t: _Task) -> list[str]:
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
    return hits


def check_agent_code_in_process(t: _Task, report) -> None:
    hits = _import_hits(t)
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
_INTERPRETERS = {
    "python", "python3", "sys.executable", "bash", "sh", "zsh", "node", "ruby", "perl", "source", ".",
    # Launchers whose first path argument is the program to run (java -jar x.jar, dotnet x.dll).
    "java", "dotnet", "php", "Rscript", "julia", "deno", "bun", "lua",
}
# Building or installing what is in a directory: the directory's own files decide what runs.
# Compilers and provers: they read the sources they are given, and compiling Lean or Coq runs the author's
# tactics and macros, so handing them an agent file runs agent code. Running the result is covered by `-o`.
_COMPILERS = {
    "gcc", "g++", "cc", "c++", "clang", "clang++", "rustc", "javac", "ghc", "coqc", "lean", "ocamlfind",
    "ocamlopt", "ocamlc", "tsc", "swiftc", "nvcc", "kotlinc", "scalac", "zig", "sage", "gfortran", "nim", "dmd",
}
# Lean and Coq elaborate whatever file they are given, and the file a test hands them is usually the
# submission (copied into a scratch project, so the path cannot be followed); a call in a file that
# already names an agent path is counted.
_PROVER_RE = re.compile(r"^(?:lake\s+(?:env\s+lean|build)|lean|coqc)\b")
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
    return base in _INTERPRETERS or base in _BUILD_TOOLS or base in _COMPILERS or base in ("pip", "pip3") or any(
        word.startswith(d + "/") for d in dirs
    )


def _command_runs_agent(words: list[str], cwd: str | None, dirs: tuple[str, ...]) -> bool:
    """Whether one command (already split into words, flags kept) executes agent-written code."""
    if not words:
        return False
    if words[0].rsplit("/", 1)[-1] in _WRAPPERS or ("=" in words[0] and not words[0].startswith("-")):
        return any(
            _command_runs_agent(words[i:], cwd, dirs) for i in range(1, len(words)) if _starts_command(words[i], dirs)
        )
    head = words[0].rsplit("/", 1)[-1].rstrip("0123456789.")
    rest = [w for w in words[1:] if not w.startswith("-")]
    if any(words[0].startswith(d + "/") for d in dirs):
        return True
    if head == "lake" and "env" in words[1:]:
        return _command_runs_agent(words[words.index("env") + 1:], cwd, dirs)
    if head in _COMPILERS:
        # what it reads, not where it writes: the word after -o is the output
        out_word = words[words.index("-o") + 1] if "-o" in words[1:-1] else None
        inputs = [w for w in rest if w != out_word]
        return any(_is_agent_path(w, cwd, dirs) for w in inputs) or (bool(cwd) and cwd.rstrip("/") in dirs and bool(inputs))
    if head in _INTERPRETERS or words[0] in _INTERPRETERS:
        if "-c" in words[1:]:
            return False  # inline code; what it runs cannot be told from here
        if head.startswith("python") and "-m" in words[1:] and any(re.fullmatch(r"-[A-Za-z]*[IP][A-Za-z]*", w) for w in words[1:]):
            return False  # python -I / -P -m <module>: no import from the current directory, so the module is not agent code
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


def _list_assignments(text: str) -> dict[str, str]:
    """NAME = [ ... ] (a command written as a list and passed by name), as a call-like "([ ... ])" text."""
    found = {}
    for m in re.finditer(r"^\s*([A-Za-z_]\w*)\s*(?::[^=\n]+)?=\s*\[", text, re.M):
        depth, out = 1, ""
        for ch in text[m.end():m.end() + 3000]:
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
                if depth == 0:
                    break
            out += ch
        found.setdefault(m.group(1), "([" + out + "])")
    return found


def _launcher_name(token: str) -> str:
    """SAGE -> sage, RUNUSER -> runuser: an upper-case constant named after the tool it holds."""
    low = token.lower()
    return low if re.fullmatch(r"[A-Z][A-Z0-9_]*", token) and (low in _INTERPRETERS or low in _COMPILERS or low in _WRAPPERS) else token


def _balanced_arg(call: str, key: str) -> str | None:
    """The text of keyword argument `key=` in a call, up to the comma or bracket that ends it."""
    m = re.search(rf"\b{key}\s*=\s*", call)
    if not m:
        return None
    depth, out = 0, ""
    for ch in call[m.end():]:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif ch == "," and depth == 0:
            break
        out += ch
    return out.strip()


def _unwrap(value: str) -> str:
    """str(X), os.fspath(X), Path(X) -> X."""
    while True:
        inner = re.fullmatch(r"(?:str|os\.fspath|Path|PurePath)\((.*)\)", value.strip())
        if not inner:
            return value.strip()
        value = inner.group(1)


def _names_in(snippet: str, names: set[str]) -> bool:
    return any(re.search(rf"\b{re.escape(n)}\b", snippet) for n in names)


def _py_call_runs_agent(
    call: str, names: set[str], dirs: tuple[str, ...], consts: dict[str, str] | None = None,
    lists: dict[str, str] | None = None,
) -> bool:
    consts = consts or {}
    first = re.match(r"[^(]*\(\s*([A-Za-z_]\w*)\s*[,)]", call)
    if first and lists and first.group(1) in lists and "([" not in call.split(",")[0]:
        call = call.replace(first.group(1), lists[first.group(1)], 1)
    cwd = None
    raw_cwd = _balanced_arg(call, "cwd")
    if raw_cwd is not None:
        value = _unwrap(raw_cwd)
        value = consts.get(value, value)
        cwd = dirs[0] if _names_in(value, names) or _is_agent_path(value.strip("\"'"), None, dirs) else value.strip("\"'")
    tokens = _list_tokens(call)
    if tokens is None:
        command = re.search(r"\(\s*[rbfu]*[\"']([^\"']+)[\"']", call)
        return bool(command) and _sh_runs_agent(command.group(1), cwd, dirs)
    words = []
    for tok in tokens:
        tok = _unwrap(tok.lstrip("*"))
        tok = _launcher_name(consts.get(tok, tok))
        words.append("/app/__agent__" if _names_in(tok, names) else tok)
    if words and words[0] in ("sys.executable",):
        words[0] = "python3"
    runs = _command_runs_agent(words, cwd, dirs)
    if runs and "-o" in words[:-1]:
        # a compiler wrote an artifact from agent sources: running that artifact later runs agent code
        target = words[words.index("-o") + 1]
        if re.fullmatch(r"[A-Za-z_]\w*", target):
            names.add(target)
    return runs


def _runner_helpers(text: str) -> set[str]:
    """Functions in a test file that pass their first parameter on to subprocess (def _run(cmd, **kw)),
    so a call like _run(["bun", "run", "x"], cwd=project) is read like a subprocess call."""
    found = set()
    for m in re.finditer(r"^def\s+(\w+)\(\s*(\w+)", text, re.M):
        body = text[m.end(): m.end() + 1500].split("\ndef ", 1)[0]
        if re.search(rf"{_SUBPROCESS_RE.pattern}\s*{re.escape(m.group(2))}\b", body):
            found.add(m.group(1))
    return found


# ---- agent files copied into a scratch folder and run from there --------------------------------
# Found on collab-editor-desync: `shutil.copytree(APP, root / "app")`, a fixture that hands `root` to
# the tests, and `python3 app/server/server.py` started with `cwd=work` through a helper. Nothing in it
# names /app next to the command, so the paths above never matched.

_COPY_OPENER = re.compile(r"\bshutil\.(?:copytree|copy2|copy|copyfile|move)\(")


def _top_level_args(call: str) -> list[str]:
    inner = call[call.index("(") + 1: call.rfind(")")] if "(" in call else ""
    args, depth, current = [], 0, ""
    for ch in inner:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            args.append(current.strip())
            current = ""
            continue
        current += ch
    args.append(current.strip())
    return args


def _function_bodies(text: str) -> dict[str, str]:
    bodies = {}
    for m in re.finditer(r"^def\s+(\w+)\(([^)]*)\)[^\n]*:\n(.*?)(?=^(?:def |class |@)|\Z)", text, re.M | re.S):
        bodies[m.group(1)] = m.group(3)
    return bodies


def _scratch_names(text: str, names: set[str], dirs: tuple[str, ...]) -> set[str]:
    """Variables holding a scratch copy of agent files, and fixtures that return one."""
    scratch: set[str] = set()
    for _, call in _calls(text, _COPY_OPENER):
        args = _top_level_args(call)
        if len(args) >= 2 and _mentions_agent(args[0], dirs, names):
            first = re.search(r"[A-Za-z_]\w*", args[1])
            if first:
                scratch.add(first.group(0))
    if not scratch:
        return scratch
    for fn, body in _function_bodies(text).items():
        if any(re.search(rf"\b(?:yield|return)\s+{re.escape(v)}\b", body) for v in scratch):
            scratch.add(fn)
    return scratch


def _runner_helpers_any_param(text: str) -> set[str]:
    """Functions that pass one of their own parameters on as a subprocess command, whichever position
    the command is in (def run(work, argv, data): Popen(argv, ...))."""
    found = set()
    for m in re.finditer(r"^def\s+(\w+)\(([^)]*)\)", text, re.M):
        params = [p.split(":")[0].split("=")[0].strip().lstrip("*") for p in m.group(2).split(",") if p.strip()]
        body = text[m.end(): m.end() + 1500].split("\ndef ", 1)[0]
        if any(re.search(rf"{_SUBPROCESS_RE.pattern}\s*{re.escape(p)}\b", body) for p in params):
            found.add(m.group(1))
    return found


def _drops_privileges(call: str, text: str) -> bool:
    """The call itself, or a function it uses (up to three levels, run -> as_nobody), drops privileges."""
    if _PY_DROP_RE.search(call) or _DROP_CONST_RE.search(call):
        return True
    bodies = _function_bodies(text)
    seen, frontier = set(), {call}
    for _ in range(3):
        nxt = set()
        for snippet in frontier:
            for fn in re.findall(r"\b([A-Za-z_]\w*)\s*\(", snippet):
                if fn in bodies and fn not in seen:
                    seen.add(fn)
                    if _PY_DROP_RE.search(bodies[fn]) or _DROP_CONST_RE.search(bodies[fn]):
                        return True
                    nxt.add(bodies[fn])
        frontier = nxt
    return False


def _scratch_runs(rel: str, text: str, names: set[str], dirs: tuple[str, ...], lists: dict[str, str]) -> list[tuple[str, bool, str]]:
    scratch = _scratch_names(text, names, dirs)
    if not scratch:
        return []
    helpers = _runner_helpers_any_param(text)
    opener = re.compile(_SUBPROCESS_RE.pattern + (r"|\b(?:" + "|".join(map(re.escape, helpers)) + r")\(" if helpers else ""))
    out = []
    for line, call in _calls(text, opener):
        if not any(re.search(rf"\b{re.escape(v)}\b", call) for v in scratch):
            continue
        # expand a command constant (PY = ["python3", ...]) used as NAME + [...]
        expanded = re.sub(r"\b([A-Za-z_]\w*)\s*\+\s*\[", lambda m: (lists[m.group(1)][2:-2] + ", ") if m.group(1) in lists else m.group(0), call)
        words = [_launcher_name(w.strip("\"' ")) for w in re.findall(r"[\"'][^\"']+[\"']|\b\w+\b", expanded)]
        if any(_starts_command(w, dirs) for w in words):
            out.append((f"{rel}:{line}", _drops_privileges(call, text), call))
    return out


def _agent_runs(t: _Task) -> list[tuple[str, bool, str]]:
    """(where, privilege dropped?, call text) for each place the verifier runs agent-written code."""
    runs: list[tuple[str, bool, str]] = []
    dirs = t.agent_dirs
    for rel, text in t.tests_py:
        names = _agent_names(text, dirs)
        consts = _constants(text)
        lists = _list_assignments(text)
        helpers = _runner_helpers(text)
        openers = re.compile(_SUBPROCESS_RE.pattern + (r"|\b(?:" + "|".join(map(re.escape, helpers)) + r")\(" if helpers else ""))
        for line, call in _calls(text, openers):
            if _py_call_runs_agent(call, names, dirs, consts, lists) or (
                names and _PROVER_RE.search(" ".join((_list_tokens(call) or [])[:4]).replace(",", " "))
            ):
                runs.append((f"{rel}:{line}", bool(_PY_DROP_RE.search(call) or _DROP_CONST_RE.search(call)), call))
        seen = {w for w, _, _ in runs}
        runs.extend(r for r in _scratch_runs(rel, text, names, dirs, lists) if r[0] not in seen)
    cwd = t.workdir
    for i, line in _shell_lines(t.test_sh):
        if line.lstrip().startswith("#"):
            continue
        for m in re.finditer(r"(?:^|&&|;)\s*cd\s+([^\s;&|]+)", line):
            cwd = m.group(1).strip("\"'")
        if "pytest" not in line and _sh_runs_agent(line, cwd, dirs):
            runs.append((f"tests/test.sh:{i}", bool(_DROP_RE.search(line)), line))
    return runs


def _worker_runs(t: _Task) -> list[str]:
    """Where the grader runs submitted code through Reward Kit's `Worker` (an isolated child process
    that cannot reach the reward or /tests), as file:line. A Worker exists to run agent code, so each
    use counts as one."""
    found = []
    for rel, text in t.tests_py:
        if not re.search(r"rewardkit", text, re.I):
            continue
        for line, _ in _calls(text, re.compile(r"\bWorker\(")):
            found.append(f"{rel}:{line}")
    return found


_LOCKS_TESTS_RE = re.compile(
    r"chmod\s+(?:-R\s+)?(?:0?7[05]0|go-\w+|o-\w+|og-\w+|go=|o=)\S*\s+[^\n]*/tests\b"
    r"|os\.chmod\([^)]*/tests[^)]*0o7[05]0|\.chmod\(0o7[05]0\)"
)


def check_agent_code_privileges(t: _Task, report) -> None:
    runs = _agent_runs(t)
    workers = _worker_runs(t)
    if workers:
        report.add(
            "verifier_runs_agent_code_in_worker", "info",
            f"The grader runs submitted code through a Reward Kit Worker ({_listed(workers)}): an isolated child "
            "process with its own unprivileged user, kept away from the reward and /tests. This is the safe pattern "
            "(whether it covers every place agent code runs needs a human).",
        )
    if not runs:
        if not workers:
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


# ---- is a Worker needed? (one result per task) -----------------------------

_SERVICE_START_RE = re.compile(
    r"\bsupervisorctl\b[^\n]*\b(?:start|restart|update|reload)\b|\bsystemctl\s+(?:start|restart|enable|daemon-reload)\b"
    r"|\bservice\s+\S+\s+start\b|\bdocker[- ]compose\s+(?:-\S+\s+\S+\s+)*up\b|\bdocker\s+compose\s+(?:-\S+\s+\S+\s+)*up\b"
)
_VM_RE = re.compile(r"\bqemu-system-\w+|\bfirecracker\b|\bkvm\b", re.I)
_NO_NEW_PRIVS_RE = re.compile(r"no-new-privs|no_new_privs|NO_NEW_PRIVS")
_KILLS_CHILDREN_RE = re.compile(r"killpg|pkill|kill\s+-9|os\.kill\(|\.kill\(\)|setsid\s+-w|--kill-on-exit|start_new_session")
_WORKER_FIX = (
    "Run the agent's code through Reward Kit's Worker(...).run(...) (pin harbor-rewardkit==0.2.4 in tests/), "
    "add tests/reward.toml, bump the task's minor version, and keep the pass/fail conditions the same."
)


def _service_launches(t: _Task) -> list[str]:
    """Lines in test.sh or a test's shell strings that start a service from an agent-writable place."""
    hits = []
    for i, line in _shell_lines(t.test_sh):
        if not line.lstrip().startswith("#") and _SERVICE_START_RE.search(line) and _mentions_agent(line, t.agent_dirs, set()):
            hits.append(f"tests/test.sh:{i}")
    for rel, text in t.tests_py:
        names = _agent_names(text, t.agent_dirs)
        for line, call in _calls(text, re.compile(r"\b(?:subprocess\.\w+|os\.system)\(")):
            if _SERVICE_START_RE.search(call.replace('", "', " ").replace("', '", " ")) and _mentions_agent(call, t.agent_dirs, names):
                hits.append(f"{rel}:{line}")
    return hits


def analyze_worker_requirement(task_root: Path) -> dict:
    """{result: not_required | present | equivalent | missing, evidence: [file:line], absent: [...], note}.
    Static and heuristic, so a person decides; the way-2 gate reads this same result."""
    t = _load(task_root)
    runs = _agent_runs(t)
    workers = _worker_runs(t)
    imports = _import_hits(t)
    services = _service_launches(t)
    undropped = [w for w, dropped, _ in runs if not dropped]
    everything = sorted({w for w, _, _ in runs} | set(imports) | set(services))
    alltext = t.test_sh + "\n" + "\n".join(text for _, text in t.tests_py)
    exposed = sorted(set(undropped) | set(imports) | set(services))
    if workers:
        return {"result": "present", "evidence": workers, "absent": [], "also_unprotected": exposed, "note": ""}
    if not everything:
        return {"result": "not_required", "evidence": [], "absent": [], "also_unprotected": [], "note": ""}
    if _VM_RE.search(alltext):
        return {"result": "not_required", "evidence": everything, "absent": [], "also_unprotected": [],
                "note": "agent code runs inside a VM"}
    absent = []
    if undropped or imports or services:
        absent.append("a separate unprivileged user")
    locked = bool(_LOCKS_TESTS_RE.search(alltext))
    if not locked:
        absent.append("/tests locked against that user")
    if not _NO_NEW_PRIVS_RE.search(alltext):
        absent.append("no-new-privs")
    if not _KILLS_CHILDREN_RE.search(alltext):
        absent.append("leftover child processes killed")
    if not absent:
        return {"result": "equivalent", "evidence": everything, "absent": [], "also_unprotected": [], "note": ""}
    return {"result": "missing", "evidence": everything, "absent": absent, "also_unprotected": [], "note": ""}


def check_worker_required(t: _Task, report) -> None:
    a = analyze_worker_requirement(t.root)
    where = _listed(a["evidence"])
    if a["result"] == "not_required":
        why = " (agent code runs inside a VM)" if a["note"] else ": nothing the agent wrote runs during grading"
        report.add("verifier_worker_required", "info", f"Worker required: no{why}.")
    elif a["result"] == "present":
        extra = a["also_unprotected"]
        if extra:
            report.add(
                "verifier_worker_required", "warn",
                f"Worker required: yes, present at {where}, but agent code also runs outside it ({_listed(extra)}). "
                f"{_WORKER_FIX}",
            )
        else:
            report.add("verifier_worker_required", "info", f"Worker required: yes, present ({where}).")
    elif a["result"] == "equivalent":
        report.add(
            "verifier_worker_required", "info",
            f"Worker required: yes, equivalent isolation by hand ({where}): separate user, no-new-privs, /tests locked and "
            "leftover processes killed. Needs a human to confirm it covers every run.",
        )
    else:
        report.add(
            "verifier_worker_required", "warn",
            f"Worker required: YES, missing. Agent code runs in the verifier at {where} without: "
            f"{', '.join(a['absent'])}. {_WORKER_FIX} (Heuristic: needs a human to confirm.)",
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
    # 5.0 B: running text built at run time (a literal name like __import__("os") is harmless).
    (re.compile(r"(?<![\w.])compile\((?!\s*[\"'])"), "compile() of a non-literal"),
    (re.compile(r"(?<![\w.])__import__\((?!\s*[\"'])"), "__import__() of a non-literal name"),
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


_SOURCE_SCAN_RE = re.compile(
    r"ast\.(?:parse|walk)\(|ast\.(?:Import|ImportFrom)\b|\b(?:forbidden|banned|disallowed|prohibited|blacklist\w*|"
    r"allowed_imports|restricted)_?\w*\s*=\s*[\[{(]"
)


def check_source_scanning(t: _Task, report) -> None:
    """A restriction enforced by reading the submission's source text is easy to get around."""
    scanning = [
        f"{rel}:{i}"
        for rel, text in t.tests_py
        for i, line in _code_lines(text)
        if _SOURCE_SCAN_RE.search(line)
    ]
    shell = [
        f"tests/test.sh:{i}"
        for i, line in _shell_lines(t.test_sh)
        if re.search(r"\bgrep\b[^|;&]*(?:import|require|include|use)\b", line) and not line.lstrip().startswith("#")
    ]
    hits = scanning + shell
    if hits:
        report.add(
            "verifier_scans_source",
            "warn",
            f"A restriction seems to be enforced by reading the submission's source text ({_listed(hits)}). "
            "That is bypassed with exec/eval, importlib, getattr, an encoded string or a pre-built binary the "
            "source only loads (5.0: AST scanners, Lean macros, a CUBIN embedded in a .cu file). Enforce it at "
            "run time instead (block the import in the sandbox, check what the process actually loaded or linked).",
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
    inherited = sorted({
        f"{rel}:{i}"
        for rel, text in t.tests_py
        for i, line in _code_lines(text)
        if re.search(r"close_fds\s*=\s*False|pass_fds\s*=|os\.set_inheritable\(", line)
    })
    if inherited:
        report.add(
            "verifier_inherited_descriptors",
            "warn",
            f"A test starts a process that inherits open file descriptors ({_listed(inherited)}). If the process "
            "runs submitted code and the descriptor reaches the verdict (a pipe, a result file), the submission "
            "can write its own result.",
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
        # The seed as digits, or as an f-string field: f"/app/test_ciphertext_seed{seed}.txt" (4.1-70).
        for _, text in t.tests_py for m in re.finditer(r"[\w./-]*seed[_-]?(?:\d+|\{\w+\})[\w.-]*\.\w+", text, re.I)
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
# Commands that change what the stated theorem means without adding an axiom, so `#print axioms` and a
# sorry/axiom ban do not catch them (4.1-43: a macro turning the statement into `True`).
_LEAN_STATEMENT_COMMANDS = ("macro", "notation", "syntax", "elab")


# Toolchains reported to have a soundness problem. Hand-kept; each entry needs a source.
_UNSOUND_LEAN = {
    "4.28": "Terminal-Bench's 5.0 milestone (item 13, 'Upgrade Takens task from Lean 4.28') suggests this version had "
            "a kernel soundness concern. Unverified here: confirm against Lean's release notes before acting on it.",
}


def _lean_toolchain_pins(t: _Task) -> list[tuple[str, str, str]]:
    pins = []
    for p in _own_files(t.root):
        text = p.read_text(errors="replace") if p.name == "lean-toolchain" else ""
        if p.name == "Dockerfile":
            text = " ".join(re.findall(r"lean4?[:@ -]v?(\d+\.\d+(?:\.\d+)?)", p.read_text(errors="replace"), re.I))
        m = re.search(r"(\d+\.\d+(?:\.\d+)?)", text)
        if m:
            pins.append((p.relative_to(t.root).as_posix(), m.group(1), ""))
    return pins


def check_lean(t: _Task, report) -> None:
    lean = [p for p in _own_files(t.root) if p.suffix == ".lean"]
    if not lean:
        return
    found = sorted({
        f"{m.group(1) or 'sorry'} in {p.relative_to(t.root).as_posix()}"
        for p in lean if p.relative_to(t.root).parts[0] in ("solution", "environment")
        for m in _LEAN_KEYWORDS.finditer(p.read_text(errors="replace"))
    })
    verifier_text = "\n".join([t.test_sh] + [x for _, x in t.tests_py])
    unchecked = [w for w in _LEAN_STATEMENT_COMMANDS if not re.search(rf"\b{w}\b", verifier_text)]
    if unchecked:
        report.add(
            "lean_submission_checks",
            "warn",
            f"Lean task, but the verifier does not seem to check submitted files for {', '.join(unchecked)}. "
            "These can change what the stated theorem means without adding an axiom, so `#print axioms` and a "
            "sorry/axiom ban do not catch them (4.1-43: a macro turning the statement into `True`); reject new "
            "macro, notation, syntax and elab commands in submitted files (needs a human).",
        )
    report.add(
        "lean_cheat_trial_hint",
        "info",
        "Lean (proof) task: run the Cheat Trial with the \"Targeted\" prompt. It tells the agent to try the "
        "proof-specific holes (new axioms, macros or notation that change the theorem, custom elaborators).",
    )
    for rel, version, note in _lean_toolchain_pins(t):
        for prefix, why in _UNSOUND_LEAN.items():
            if version.startswith(prefix):
                report.add("lean_toolchain_reported_unsound", "info", f"{rel} pins Lean {version}: {why}")
    if found:
        report.add("lean_keywords", "info", f"Lean commands to review in the task's own files: {_listed(found)}.")


def run_isolation_checks(task_root: Path, report) -> None:
    t = _load(task_root)
    for check in (
        check_reward_reset,
        check_agent_code_in_process,
        check_agent_code_privileges,
        check_worker_required,
        check_answer_evaluation,
        check_source_scanning,
        check_verdict_channels,
        check_lean,
    ):
        try:
            check(t, report)
        except Exception as exc:  # advisory: one odd file must not break the stage
            report.add(check.__name__, "info", f"check could not run: {type(exc).__name__}")
