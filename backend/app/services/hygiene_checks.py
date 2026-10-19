"""More mechanical task-file checks (4.0 category F), added to static_checks'
report. Plain text/regex/YAML/hash checks, no LLM, and all advisory: they warn
or inform and never fail a submission. `report` is a static_checks
StaticCheckReport (only `.add(name, severity, message)` is used)."""

import hashlib
import re
from pathlib import Path

import yaml

_MAX_FILE_BYTES = 1_000_000
_MAX_LISTED = 8
_TEXT_SUFFIXES = {
    ".sh", ".py", ".md", ".txt", ".toml", ".yaml", ".yml", ".json", ".cfg", ".ini", ".conf",
    ".js", ".ts", ".rs", ".go", ".c", ".h", ".cpp", ".hpp", ".java", ".rb", ".sql", ".env", "",
}


# Third-party code a task bundles (vendored crates, node_modules) is not the
# author's own files, so the checks below skip it.
_SKIP_DIRS = {"vendor", "node_modules", ".git", "target", "__pycache__", ".venv"}


def _own_files(root: Path):
    if root.is_dir():
        for path in sorted(root.rglob("*")):
            if path.is_file() and not _SKIP_DIRS.intersection(path.relative_to(root).parts[:-1]):
                yield path


def _text_files(root: Path):
    for path in _own_files(root):
        if path.suffix.lower() in _TEXT_SUFFIXES and path.stat().st_size <= _MAX_FILE_BYTES:
            try:
                yield path, path.read_text()
            except (OSError, UnicodeDecodeError):
                continue


def _listed(items: list[str]) -> str:
    shown = ", ".join(items[:_MAX_LISTED])
    return shown + (f" (+{len(items) - _MAX_LISTED} more)" if len(items) > _MAX_LISTED else "")


# ---- named volumes ---------------------------------------------------------

def _named_volumes(compose: dict) -> list[str]:
    names: list[str] = list((compose.get("volumes") or {}))
    for service in (compose.get("services") or {}).values():
        for vol in (service or {}).get("volumes") or []:
            if isinstance(vol, str):
                src = vol.split(":", 1)[0]
                if ":" in vol and src and src[0] not in "./~$":
                    names.append(src)
            elif isinstance(vol, dict) and vol.get("type") == "volume" and vol.get("source"):
                names.append(str(vol["source"]))
    return sorted(set(names))


def check_named_volumes(task_root: Path, report) -> None:
    env = task_root / "environment"
    found: dict[str, list[str]] = {}
    for path in sorted(env.glob("docker-compose*.y*ml")) if env.is_dir() else []:
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(data, dict) and (names := _named_volumes(data)):
            found[path.name] = names
    if found:
        listing = "; ".join(f"{f}: {_listed(n)}" for f, n in found.items())
        report.add(
            "compose_named_volumes",
            "warn",
            f"Named volume(s) in a compose file ({listing}). Some sandbox providers do not support "
            "them. Upstream Terminal-Bench now rejects all compose volumes (check-compose-volumes.sh, "
            "newer than the vendored copy), so share state over the network or bake files into the image.",
        )
    else:
        report.add("compose_named_volumes", "info", "No named volumes in environment compose files.")


# ---- Dockerfile syntax line, pinning, keys, x86 ---------------------------

_HEREDOC = re.compile(r"<<-?\s*['\"]?\w+")
_SYNTAX_LINE = re.compile(r"^\s*#\s*syntax\s*=\s*docker/dockerfile", re.I)
_PIPE_TO_SHELL = re.compile(r"\b(?:curl|wget)\b[^\n|]*\|\s*(?:sudo\s+)?(?:ba|z)?sh\b")
_SPLIT = re.compile(r"&&|\|\||;")


def _run_lines(text: str) -> list[str]:
    """RUN instructions with backslash continuations joined."""
    lines, current = [], ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if current:
            current += " " + line.lstrip()
        elif re.match(r"\s*RUN\b", line, re.I):
            current = re.sub(r"^\s*RUN\s+", "", line, flags=re.I)
        else:
            continue
        if current.endswith("\\"):
            current = current[:-1]
        else:
            lines.append(current)
            current = ""
    if current:
        lines.append(current)
    return lines


# Packages the Terminal-Bench harness requires in every image.
_HARNESS_PACKAGES = {"tmux", "asciinema"}


def _unpinned(run: str) -> list[str]:
    out: list[str] = []
    for part in _SPLIT.split(run):
        tokens = part.split()
        for i, tok in enumerate(tokens):
            if tok in ("npm", "yarn", "pnpm") and i + 1 < len(tokens) and tokens[i + 1] in ("install", "i", "add"):
                pkgs = [t for t in tokens[i + 2 :] if not t.startswith("-") and t not in ("&&", "|")]
                bad = [p for p in pkgs if "@" not in p.lstrip("@") and not p.startswith((".", "/"))]
                out += [f"{tok} install {p}" for p in bad]
            elif tok == "npx":
                pkgs = [t for t in tokens[i + 1 :] if not t.startswith("-")][:1]
                out += [f"npx {p}" for p in pkgs if "@" not in p.lstrip("@")]
            elif tok in ("apt-get", "apt") and "install" in tokens[i + 1 : i + 4]:
                after = tokens[tokens.index("install", i) + 1 :]
                out += [
                    f"apt-get install {p}" for p in after
                    if not p.startswith("-") and "=" not in p and p not in _HARNESS_PACKAGES
                ]
    return out


_KEY_PATTERNS = {
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "OpenAI-style key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    "Google API key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "Slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}
_X86_URL = re.compile(r"https?://\S*(?:x86[_-]64|amd64|x64)\S*", re.I)
_ARCH_BRANCH = re.compile(r"uname\s+-m|TARGETARCH|dpkg\s+--print-architecture|\barch\b|\$\(arch\)")


def check_dockerfile_extras(task_root: Path, report) -> None:
    dockerfile = task_root / "environment" / "Dockerfile"
    if not dockerfile.is_file():
        return
    text = dockerfile.read_text(errors="replace")

    if _HEREDOC.search(text):
        first = next((ln for ln in text.splitlines() if ln.strip()), "")
        if _SYNTAX_LINE.match(first):
            report.add("dockerfile_syntax_line", "info", "Heredoc used and `# syntax=docker/dockerfile:` is the first line.")
        else:
            report.add(
                "dockerfile_syntax_line",
                "warn",
                "Heredoc in the Dockerfile but no `# syntax=docker/dockerfile:1` first line: older "
                "builders fail on it (a current Docker hides this).",
            )

    runs = _run_lines(text)
    found = sorted({u for run in runs for u in _unpinned(run)})
    apt = [u for u in found if u.startswith("apt-get")]
    unpinned = [u for u in found if not u.startswith("apt-get")]
    piped = [r.strip()[:80] for r in runs if _PIPE_TO_SHELL.search(r)]
    if unpinned or piped:
        parts = []
        if unpinned:
            parts.append(f"no version on: {_listed(unpinned)}")
        if piped:
            parts.append(f"a download piped into a shell: {_listed(piped)}")
        report.add(
            "dockerfile_install_pinning",
            "warn",
            "Unpinned installs (" + "; ".join(parts) + "). The build can change over time. pip is covered "
            "by Terminal-Bench's check-pip-pinning.",
        )
    elif apt:
        # apt versions are rarely pinned (and a digest-pinned base image already
        # fixes the package index), so this is a note, not a warning.
        report.add(
            "dockerfile_install_pinning",
            "info",
            f"apt packages without a version: {_listed([a.split()[-1] for a in apt])}. Fine when the base "
            "image is digest-pinned.",
        )
    else:
        report.add("dockerfile_install_pinning", "info", "No unpinned npm/npx/apt installs or curl-to-shell found.")

    keys: list[str] = []
    for path, content in list(_text_files(task_root / "environment")):
        for label, pattern in _KEY_PATTERNS.items():
            if pattern.search(content):
                keys.append(f"{label} in {path.relative_to(task_root)}")
    if keys:
        report.add(
            "environment_key_like_strings",
            "warn",
            f"Key-like string(s) baked into the environment: {_listed(keys)}. The agent can read them.",
        )

    x86: list[str] = []
    for path, content in list(_text_files(task_root / "environment")):
        if (path.name == "Dockerfile" or path.suffix == ".sh") and _X86_URL.search(content) and not _ARCH_BRANCH.search(content):
            x86.append(str(path.relative_to(task_root)))
    if x86:
        report.add(
            "x86_only_download",
            "warn",
            f"Downloads an x86-only build with no architecture branch (uname -m / TARGETARCH) in: "
            f"{_listed(x86)}. It will fail on arm64 hosts.",
        )


# ---- references, mirrors, tool parity, instruction length ------------------

_PATH_RE = re.compile(r"(?<![\w/.:-])((?:\.{0,2}/)?(?:[\w.-]+/)*[\w.-]+\.(?:md|txt|json|ya?ml|toml|py|sh|csv|pdf|c|h|rs|go|cfg|conf|html|sql))\b")


def _named_paths(text: str) -> list[str]:
    seen: dict[str, None] = {}
    for m in _PATH_RE.finditer(text):
        path = m.group(1)
        if "://" in path or path.startswith("//"):
            continue
        seen[path] = None
    return list(seen)


def _resolve(task_root: Path, ref: str) -> Path | None:
    """The task file a named path points at (by its own path, or by file name)."""
    rel = ref.lstrip("./")
    for base in (task_root, task_root / "environment", task_root / "tests"):
        if (base / rel).is_file():
            return base / rel
    name = Path(ref).name
    for hit in task_root.rglob(name):
        if hit.is_file():
            return hit
    return None


def check_references(task_root: Path, report) -> None:
    other_text = "\n".join(
        c for p, c in _text_files(task_root)
        if p.name not in ("instruction.md", "README.md")
    )
    missing: list[str] = []
    for doc in ("instruction.md", "README.md"):
        path = task_root / doc
        if not path.is_file():
            continue
        for ref in _named_paths(path.read_text(errors="replace")):
            # A runtime path (an output the agent writes) shows up in the tests or
            # solution even though no file exists; only a name found nowhere is dangling.
            if _resolve(task_root, ref) is None and Path(ref).name not in other_text:
                missing.append(f"{ref} (in {doc})")
    if missing:
        report.add(
            "dangling_references",
            "warn",
            f"Path(s) named in the task text that exist nowhere in the task and are not used by the "
            f"tests or solution: {_listed(missing)}.",
        )
    else:
        report.add("dangling_references", "info", "Every path named in instruction.md / README.md is found in the task.")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# Files that legitimately differ between the agent's image and the verifier's.
_MIRROR_IGNORED = {"Dockerfile", "test.sh", ".dockerignore", "README.md", "LICENSE", ".gitignore", "__init__.py"}


def check_mirrored_files(task_root: Path, report) -> None:
    env, tests = task_root / "environment", task_root / "tests"
    if not (env.is_dir() and tests.is_dir()):
        return
    by_name: dict[str, Path] = {}
    for p in _own_files(env):
        if p.stat().st_size <= _MAX_FILE_BYTES and p.name not in _MIRROR_IGNORED and not p.name.startswith("requirements"):
            by_name.setdefault(p.name, p)
    differ = []
    for p in _own_files(tests):
        if p.name in by_name:
            if _digest(p) != _digest(by_name[p.name]):
                differ.append(p.name)
    if differ:
        report.add(
            "mirrored_files_differ",
            "warn",
            f"Same file name in environment/ and tests/ with different content: {_listed(sorted(set(differ)))}. "
            "Copies the agent sees and the grader uses should match.",
        )
    else:
        report.add("mirrored_files_differ", "info", "No file is duplicated with different content between environment/ and tests/.")


_PKG_INSTALL = re.compile(r"\b(?:apt-get|apt|apk)\s+(?:-\S+\s+)*(?:install|add)\s+((?:-\S+\s+|[\w.+:=-]+\s*)+)")


def _packages(dockerfile: Path) -> set[str]:
    pkgs: set[str] = set()
    if not dockerfile.is_file():
        return pkgs
    for run in _run_lines(dockerfile.read_text(errors="replace")):
        for part in _SPLIT.split(run):
            m = _PKG_INSTALL.search(part)
            if m:
                pkgs |= {t.split("=")[0] for t in m.group(1).split() if not t.startswith("-")}
    return pkgs


def check_tool_parity(task_root: Path, report) -> None:
    verifier = task_root / "tests" / "Dockerfile"
    if not verifier.is_file():
        return
    agent_pkgs, verifier_pkgs = _packages(task_root / "environment" / "Dockerfile"), _packages(verifier)
    tests_text = "\n".join(c for _, c in _text_files(task_root / "tests"))
    lacking = sorted(
        p for p in agent_pkgs - verifier_pkgs
        if len(p) > 2 and re.search(rf"(?<![\w-]){re.escape(p)}(?![\w-])", tests_text)
    )
    if lacking:
        report.add(
            "verifier_tool_parity",
            "warn",
            f"The tests mention tool(s) that environment/Dockerfile installs but tests/Dockerfile does not: "
            f"{_listed(lacking)}. The verifier runs in its own image and may not have them.",
        )
    else:
        report.add("verifier_tool_parity", "info", "No tool used by the tests is missing from tests/Dockerfile.")


def _words(text: str) -> int:
    return len(text.split())


def check_effective_instruction_length(task_root: Path, report) -> None:
    """A number for the reviewer: the instruction's own words plus the words in
    the task files it names. Not a pass/fail limit."""
    path = task_root / "instruction.md"
    if not path.is_file():
        return
    text = path.read_text(errors="replace")
    own = _words(text)
    extra: dict[Path, int] = {}
    for ref in _named_paths(text):
        hit = _resolve(task_root, ref)
        # Only documents the agent can read: not solution/ or tests/, not data files.
        if (
            hit and hit != path and hit.suffix.lower() in (".md", ".txt", ".rst")
            and hit.relative_to(task_root).parts[0] not in ("solution", "tests")
            and hit.stat().st_size <= _MAX_FILE_BYTES
        ):
            try:
                extra[hit] = _words(hit.read_text())
            except (OSError, UnicodeDecodeError):
                continue
    if not extra:
        report.add("instruction_effective_length", "info", f"instruction.md is {own} words and names no other task file.")
        return
    named = ", ".join(f"{p.relative_to(task_root)} ({n})" for p, n in list(extra.items())[:_MAX_LISTED])
    report.add(
        "instruction_effective_length",
        "info",
        f"instruction.md is {own} words; the files it names add {sum(extra.values())} more ({named}). "
        f"Effective length about {own + sum(extra.values())} words. Names found by path, so a vague "
        "reference is not counted.",
    )


def run_hygiene_checks(task_root: Path, report) -> None:
    for check in (
        check_named_volumes,
        check_dockerfile_extras,
        check_references,
        check_mirrored_files,
        check_tool_parity,
        check_effective_instruction_length,
    ):
        try:
            check(task_root, report)
        except Exception as exc:  # advisory: one bad file must not break the stage
            report.add(check.__name__, "info", f"check could not run: {type(exc).__name__}")
