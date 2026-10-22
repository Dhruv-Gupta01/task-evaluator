"""Does the task still work when the agent is not root (5.0 category H)?

Upstream is moving agents to a non-root user ([agent] user in task.toml,
issue #1505). A root agent can read the verifier's files and any "hidden"
data, so a root agent is reported as a warning, not a failure. When a user is
set, the usual ways such a switch breaks a task are checked: the user is
never created, the working directory is not owned by it, it has passwordless
sudo, a toolchain was installed under /root, or solve.sh installs packages at
run time. Text checks over the task files; no LLM, no Docker, advisory."""

import re
import tomllib
from pathlib import Path

from app.services.hygiene_checks import _listed, _run_lines

_ROOT_NAMES = ("root", "0")
# Users that exist in common base images without a useradd line in the task.
_BUILT_IN_USERS = {"nobody", "ubuntu", "node", "www-data", "daemon", "postgres", "nonroot", "_apt"}
_ROOT_HOME_TOOLS = (
    "/root/.local", "/root/.cargo", "/root/.rustup", "/root/.elan", "/root/.nvm",
    "/root/.bun", "/root/.deno", "/root/.pyenv", "/root/.sdkman", "/root/.opam", "/root/.ghcup",
)
_CREATE_RE = re.compile(r"\b(?:useradd|adduser|addgroup|groupadd)\b")


def _toml(task_root: Path) -> dict:
    try:
        return tomllib.loads((task_root / "task.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _final_user(dockerfile_text: str) -> str | None:
    users = re.findall(r"^\s*USER\s+(\S+)", dockerfile_text, re.I | re.M)
    return users[-1].split(":")[0] if users else None


def check_agent_user(task_root: Path, report) -> None:
    dockerfile = task_root / "environment" / "Dockerfile"
    text = dockerfile.read_text(errors="replace") if dockerfile.is_file() else ""
    configured = (_toml(task_root).get("agent") or {}).get("user")
    image_user = _final_user(text)
    user = str(configured) if configured else image_user
    if not user or user in _ROOT_NAMES:
        report.add(
            "agent_runs_as_root",
            "warn",
            "The agent runs as root (no [agent] user in task.toml and the image does not end with a non-root "
            "USER). A root agent can read the verifier's files and any protected data. Terminal-Bench 5.0 moves "
            "agents to a non-root user (issue #1505): create a user in environment/Dockerfile, set [agent] user, "
            "and confirm the Oracle still passes. A task that must stay root should say why in README.md.",
        )
        return
    if not configured:
        report.add(
            "agent_user_only_in_dockerfile",
            "warn",
            f"The agent's user ('{user}') comes only from the Dockerfile's USER line. Some runners (Modal, "
            "upstream #2152) ignore a Dockerfile USER and start the agent as root. Set [agent] user = "
            f"\"{user}\" in task.toml (and [verifier] user if the verifier needs root).",
        )

    runs = " ".join(_run_lines(text))
    created = bool(re.search(rf"\b(?:useradd|adduser)\b[^\n;&|]*\b{re.escape(user)}\b", runs)) or user in _BUILT_IN_USERS
    if text and not created and not _CREATE_RE.search(runs):
        report.add(
            "agent_user_missing",
            "warn",
            f"[agent] user is '{user}' but environment/Dockerfile never creates it (no useradd or adduser). If the "
            "base image does not already have it, Harbor cannot start the agent.",
        )
    elif text and not re.search(r"chown\b|--chown", text):
        report.add(
            "agent_user_workdir",
            "warn",
            f"environment/Dockerfile never runs chown or COPY --chown, so files copied in as root (the working "
            f"directory and the files the instruction names) may not be writable by '{user}'.",
        )

    if re.search(r"NOPASSWD|\bsudo\b[^\n]*(?:install|apt)|usermod[^\n]*-aG\s+sudo|adduser\s+\S+\s+sudo", text):
        report.add(
            "agent_user_sudo",
            "warn",
            f"'{user}' has passwordless sudo (NOPASSWD or the sudo group). The agent is then non-root in name only "
            "and can still read everything root can.",
        )

    # Base images whose tool cache lives in root's home even though the Dockerfile never names /root.
    implicit = []
    if re.search(r"^\s*FROM\s+maven[:@\s]", text, re.I | re.M) and not re.search(r"maven\.repo\.local|/root/\.m2|MAVEN_CONFIG", text):
        implicit.append("the maven base image's repository /root/.m2")
    in_root_home = [p for p in _ROOT_HOME_TOOLS if p in text] + implicit
    if in_root_home:
        report.add(
            "agent_user_root_home",
            "warn",
            f"Toolchains are installed under root's home ({_listed(in_root_home)}). /root is mode 0700, so "
            f"'{user}' cannot run them; install to /usr/local or a world-readable prefix (upstream had to move "
            "uv, elan and rustup in many tasks).",
        )

    solve = task_root / "solution" / "solve.sh"
    if solve.is_file():
        hits = [
            f"solution/solve.sh:{i}"
            for i, line in enumerate(solve.read_text(errors="replace").splitlines(), 1)
            if re.search(r"\b(?:apt-get|apt|dnf|yum|apk)\s+(?:-\S+\s+)*install\b", line)
            and not line.lstrip().startswith("#")
        ]
        if hits:
            report.add(
                "agent_user_runtime_install",
                "warn",
                f"solution/solve.sh installs system packages at run time ({_listed(hits)}). That needs root, so it "
                f"fails for '{user}'. Install them in environment/Dockerfile instead.",
            )
