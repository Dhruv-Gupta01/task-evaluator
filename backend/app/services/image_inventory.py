"""What the agent's image really contains (4.1 F.1 and H.2).

The judges used to treat everything under environment/ as agent-visible, but a
multi-stage Dockerfile can copy a file into a build stage only, and a file the
Dockerfile never copies is not in the container at all (4.1-34: a README rule
and a generator script the agent never had). After Build, one `docker run` of
the built image lists the files under its WORKDIR and the other non-system
folders, and records the installed package versions (pip freeze, dpkg/apk,
global npm) as reproducibility evidence: pinning a top-level package does not
pin what it pulls in (4.1-28).

Best effort: an image without `sh` (distroless) or a Docker error gives an
`error` and no listing, and nothing that reads the inventory depends on it."""

import json
import subprocess
from pathlib import Path

from app.config import get_settings, host_env_for_subprocess
from app.services.docker_orchestrator import CONTAINER_LABEL_KEY
from app.services.hygiene_checks import _own_files

MAX_FILES = 5000
_TIMEOUT_SEC = 300
_SECTIONS = ("FILES", "PIP", "DPKG", "APK", "NPM")
# Not task files: the base image's own folders, caches and installed packages.
_PRUNE = (
    "node_modules .git __pycache__ site-packages dist-packages .cache .venv venv .npm .cargo .rustup "
    ".local .conda conda miniconda3 .m2 .gradle"
).split()
_SCRIPT = (
    'echo "WORKDIR=$(pwd)"\n'
    'echo "---FILES"\n'
    'for d in "$(pwd)" /app /workspace /data /home /root /srv /opt /mnt /task; do [ -d "$d" ] && echo "$d"; done '
    '| sort -u | while read -r d; do find "$d" -xdev \\( '
    + " -o ".join(f"-name {n}" for n in _PRUNE)
    + " \\) -prune -o -type f -print0 2>/dev/null | xargs -0 stat -c '%s %n' 2>/dev/null; done "
    f"| sort -u -k2 | head -n {MAX_FILES + 1}\n"
    'echo "---PIP"\n'
    "(python3 -m pip freeze 2>/dev/null || pip freeze 2>/dev/null) | head -n 2000\n"
    'echo "---DPKG"\n'
    "dpkg-query -W -f='${Package}=${Version}\\n' 2>/dev/null | head -n 3000\n"
    'echo "---APK"\n'
    "apk info -v 2>/dev/null | head -n 3000\n"
    'echo "---NPM"\n'
    "npm ls -g --depth=0 2>/dev/null | head -n 500\n"
)


def _parse(output: str) -> dict:
    data: dict = {"workdir": None, "files": [], "truncated": False, **{s.lower(): [] for s in _SECTIONS[1:]}}
    section = None
    for line in output.splitlines():
        if line.startswith("WORKDIR="):
            data["workdir"] = line[len("WORKDIR="):] or None
        elif line.startswith("---") and line[3:] in _SECTIONS:
            section = line[3:]
        elif section == "FILES" and line.strip():
            size, _, path = line.partition(" ")
            if path and size.isdigit():
                data["files"].append({"path": path, "size": int(size)})
        elif section and line.strip():
            data[section.lower()].append(line.strip())
    if len(data["files"]) > MAX_FILES:
        data["files"] = data["files"][:MAX_FILES]
        data["truncated"] = True
    return data


def collect(image_tag: str, submission_id: str) -> dict:
    """Blocking (docker CLI); run it in a thread."""
    cmd = [
        "docker", "run", "--rm", "--network", "none",
        "--platform", get_settings().docker_platform,
        "--label", f"{CONTAINER_LABEL_KEY}={submission_id}",
        "--entrypoint", "sh", image_tag, "-c", _SCRIPT,
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_TIMEOUT_SEC, env=host_env_for_subprocess(),
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"error": f"could not list the image: {type(exc).__name__}: {exc}"}
    if "---FILES" not in proc.stdout:
        return {"error": f"could not list the image (exit {proc.returncode}): {(proc.stderr or proc.stdout)[-1500:]}"}
    return _parse(proc.stdout)


def parse_logs(logs: str | None) -> dict | None:
    """The stored inventory, or None when there is none or it failed."""
    try:
        data = json.loads(logs) if logs else None
    except ValueError:
        return None
    return data if isinstance(data, dict) and data.get("files") is not None and not data.get("error") else None


_BUILD_FILES = {"Dockerfile", ".dockerignore", "docker-compose.yaml", "docker-compose.yml", "compose.yaml", "compose.yml"}


def visibility(task_root: Path, inventory: dict) -> dict:
    """Which environment/ files reach the agent's container. Matched by file
    name and size, since COPY can put a file anywhere: same name and size =
    in the image; same name, other size = changed during the build; no
    such name = not in the image (build-stage only, or never copied)."""
    by_name: dict[str, set[int]] = {}
    for f in inventory["files"]:
        by_name.setdefault(f["path"].rsplit("/", 1)[-1], set()).add(f["size"])
    env = task_root / "environment"
    missing, changed = [], []
    env_names: set[str] = set()
    for p in _own_files(env) if env.is_dir() else []:
        if p.name in _BUILD_FILES or p.name.startswith("Dockerfile"):
            continue
        env_names.add(p.name)
        rel = p.relative_to(task_root).as_posix()
        sizes = by_name.get(p.name)
        if not sizes:
            missing.append(rel)
        elif p.stat().st_size not in sizes:
            changed.append(rel)
    workdir = inventory.get("workdir") or "/app"
    built = [
        f for f in inventory["files"]
        if f["path"].startswith(workdir.rstrip("/") + "/") and f["path"].rsplit("/", 1)[-1] not in env_names
    ]
    return {"missing": missing, "changed": changed, "built": built, "workdir": workdir}


def judge_section(task_root: Path, inventory: dict | None) -> str:
    """Plain text for the judges; empty when no inventory was recorded."""
    if not inventory:
        return ""
    v = visibility(task_root, inventory)
    lines = [
        f"=== AGENT IMAGE: what the built image really contains ({len(inventory['files'])} files under "
        f"{v['workdir']} and other non-system folders{', list truncated' if inventory.get('truncated') else ''}) ===",
    ]
    if v["missing"]:
        lines.append(
            "environment/ files that are NOT in the agent's container (copied only into a build stage, or never "
            "copied). The agent cannot read these; treat them as hidden:"
        )
        lines += [f"- {m}" for m in v["missing"][:200]]
    else:
        lines.append("Every environment/ file (by name) is in the agent's container.")
    if v["changed"]:
        lines.append("environment/ files present in the image with a different size (changed during the build):")
        lines += [f"- {c}" for c in v["changed"][:100]]
    if v["built"]:
        lines.append(f"Files under {v['workdir']} that do not come from environment/ (created while the image was built):")
        lines += [f"- {f['path']} ({f['size']} bytes)" for f in v["built"][:200]]
    return "\n".join(lines) + "\n"
