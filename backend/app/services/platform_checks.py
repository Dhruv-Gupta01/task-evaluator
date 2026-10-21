"""Will the task run on a different CPU architecture or a different machine
(5.0 category E)? Advisory; nothing here changes a gate.

* check_bundled_binaries: executables shipped in environment/ (ELF or Mach-O
  headers) built for only one CPU architecture. No Docker.
* check_resources: a GPU the host does not have, and an agent timeout below
  the author's own expert estimate. No Docker.
* inspect_bases: asks the registry (`docker buildx imagetools inspect`) which
  architectures each `FROM` image has and whether its digest points at a
  single platform. Needs Docker and network, so it runs after Build, not in
  Static Checks."""

import json
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

from app.config import get_settings, host_env_for_subprocess
from app.services.hygiene_checks import _listed, _own_files

# ELF e_machine and Mach-O cpu types we can name.
_ELF_MACHINES = {0x3E: "x86_64", 0xB7: "aarch64", 0x28: "arm", 0x03: "x86", 0xF3: "riscv"}
_MACHO_CPUS = {0x01000007: "x86_64", 0x0100000C: "aarch64"}
_MAX_BINARY_SCAN = 4000
_MAX_BINARY_REPORT = 8


def _arch_of(path: Path) -> str | None:
    """Architecture of an ELF or Mach-O file, else None. Reads 20 bytes."""
    try:
        with path.open("rb") as f:
            head = f.read(20)
    except OSError:
        return None
    if head[:4] == b"\x7fELF" and len(head) >= 20:
        little = head[5] != 2
        machine = int.from_bytes(head[18:20], "little" if little else "big")
        return _ELF_MACHINES.get(machine, f"machine {machine:#x}")
    if head[:4] in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe"):
        cpu = int.from_bytes(head[4:8], "little")
        return _MACHO_CPUS.get(cpu, f"cpu {cpu:#x}")
    return None


def check_bundled_binaries(task_root: Path, report) -> None:
    env = task_root / "environment"
    if not env.is_dir():
        return
    found: dict[str, list[str]] = {}
    scanned = 0
    for p in _own_files(env):
        scanned += 1
        if scanned > _MAX_BINARY_SCAN:
            break
        if p.stat().st_size < 64:
            continue
        arch = _arch_of(p)
        if arch:
            found.setdefault(arch, []).append(p.relative_to(task_root).as_posix())
    if not found:
        return
    detail = "; ".join(f"{arch}: {_listed(paths[:_MAX_BINARY_REPORT])}" for arch, paths in found.items())
    if len(found) == 1:
        arch = next(iter(found))
        report.add(
            "bundled_binaries_single_arch",
            "warn",
            f"environment/ ships compiled binaries for {arch} only ({detail}). On the other CPU architecture "
            "(this platform defaults to linux/arm64) they fail with 'exec format error'. Build them in the "
            "Dockerfile, or ship one per architecture and pick with uname -m.",
        )
    else:
        report.add("bundled_binaries_multi_arch", "info", f"environment/ ships binaries for several architectures ({detail}).")


def check_resources(task_root: Path, report) -> None:
    try:
        toml = tomllib.loads((task_root / "task.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return
    env = toml.get("environment") or {}
    gpus = env.get("gpus") or 0
    if gpus and not shutil.which("nvidia-smi"):
        types = env.get("gpu_types")
        report.add(
            "gpu_not_available",
            "warn",
            f"task.toml asks for {gpus} GPU(s){f' ({_listed([str(t) for t in types])})' if types else ''} but this "
            "host has no NVIDIA GPU. Trials run on CPU, so a CUDA build of torch or a GPU-sized timeout will not "
            "behave as the author measured; numbers that depend on the GPU (accuracy, speed) can differ.",
        )
    timeout = (toml.get("agent") or {}).get("timeout_sec")
    hours = (toml.get("metadata") or {}).get("expert_time_estimate_hours")
    if isinstance(timeout, (int, float)) and isinstance(hours, (int, float)) and hours > 0 and timeout < hours * 3600:
        applied = get_settings().agent_timeout_sec
        report.add(
            "agent_timeout_below_estimate",
            "warn",
            f"[agent] timeout_sec is {timeout:g}s but the author's own expert estimate is {hours:g} h "
            f"({hours * 3600:g}s): an expert would be cut off before finishing."
            + (f" (This platform overrides it with AGENT_TIMEOUT_SEC={applied:g}s.)" if applied else ""),
        )


# --- registry check (needs Docker and network) --------------------------------------------

_FROM_RE = re.compile(r"^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?", re.I | re.M)
_PLATFORM_RE = re.compile(r"^\s*FROM\s+--platform=(\S+)", re.I | re.M)


def _from_images(dockerfile_text: str) -> list[str]:
    stages: set[str] = set()
    images: list[str] = []
    for ref, alias in _FROM_RE.findall(dockerfile_text):
        if ref.lower() != "scratch" and ref not in stages and "$" not in ref:
            images.append(ref)
        if alias:
            stages.add(alias)
    return list(dict.fromkeys(images))


def _inspect_one(ref: str) -> dict:
    try:
        proc = subprocess.run(
            ["docker", "buildx", "imagetools", "inspect", "--raw", ref],
            capture_output=True, text=True, timeout=60, env=host_env_for_subprocess(),
            encoding="utf-8", errors="replace",
        )
        if proc.returncode != 0:
            return {"ref": ref, "error": (proc.stderr or proc.stdout).strip()[-300:] or "inspect failed"}
        manifest = json.loads(proc.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return {"ref": ref, "error": f"{type(exc).__name__}: {exc}"}
    manifests = manifest.get("manifests")
    if isinstance(manifests, list):
        platforms = sorted(
            {
                f"{(m.get('platform') or {}).get('os')}/{(m.get('platform') or {}).get('architecture')}"
                for m in manifests
                if (m.get("platform") or {}).get("architecture") not in (None, "unknown")
            }
        )
        return {"ref": ref, "index": True, "platforms": platforms}
    return {"ref": ref, "index": False, "platforms": []}


def inspect_bases(task_root: Path) -> list[dict]:
    """One entry per distinct base image in environment/Dockerfile:
    {ref, index, platforms, warnings[], error?}. Blocking; run it in a thread."""
    dockerfile = task_root / "environment" / "Dockerfile"
    if not dockerfile.is_file():
        return []
    text = dockerfile.read_text(errors="replace")
    pinned_platform = bool(_PLATFORM_RE.search(text))
    out = []
    for ref in _from_images(text)[:6]:
        info = _inspect_one(ref)
        warnings: list[str] = []
        if not info.get("error"):
            if not info["index"]:
                warnings.append(
                    "this reference points at a single-platform image (a digest of one architecture, not of the "
                    "multi-architecture index): it builds on one CPU architecture only. Pin the index digest."
                )
            else:
                missing = [a for a in ("linux/amd64", "linux/arm64") if a not in info["platforms"]]
                if missing:
                    warnings.append(f"no build for {', '.join(missing)}")
        if pinned_platform:
            warnings.append("the Dockerfile forces a platform with FROM --platform, so it ignores the one the run uses")
        info["warnings"] = warnings
        out.append(info)
    return out
