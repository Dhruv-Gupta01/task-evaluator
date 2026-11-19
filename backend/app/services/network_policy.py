"""What a task's task.toml says about the network, and what a run really used.

Harbor 0.26 reads `[environment] network_mode` (public, allowlist, no-network) and `allowed_hosts`;
the deprecated `allow_internet` means true -> public and false -> no-network, and is ignored when
`network_mode` or `allowed_hosts` is also set. Nothing declared means public. `[agent]` and
`[verifier]` can override one phase. Terminal-Bench's own checks tell authors to leave all of it out
(`check-allow-internet` fails `allow_internet = false`), so most tasks declare nothing.

The platform runs every task with `HARBOR_NETWORK_MODE` (default public) by rewriting `[environment]`
in a temporary copy (`harbor_runner._prepare_task`). That rewrite does not touch `[agent]` or
`[verifier]`, so a phase override the task declares still applies. This module reports both sides so
the evidence can say "declared X, ran Y"."""
from __future__ import annotations

import tomllib
from pathlib import Path


def declared(task_toml: dict) -> dict:
    env = task_toml.get("environment") or {}
    mode, source, hosts = "public", "default", []
    if env.get("network_mode") is not None or env.get("allowed_hosts") is not None:
        hosts = [str(h) for h in (env.get("allowed_hosts") or [])]
        mode = str(env.get("network_mode") or ("allowlist" if hosts else "public"))
        source = "network_mode"
    elif env.get("allow_internet") is not None:
        mode = "public" if env.get("allow_internet") else "no-network"
        source = "allow_internet"
    phases = {}
    for role in ("agent", "verifier"):
        section = task_toml.get(role) or {}
        if isinstance(section, dict) and section.get("network_mode") is not None:
            phases[role] = {"mode": str(section["network_mode"]), "allowed_hosts": [str(h) for h in (section.get("allowed_hosts") or [])]}
    return {"mode": mode, "source": source, "allowed_hosts": hosts, "phase_overrides": phases, "deprecated_key": "allow_internet" in env}


def effective(task_declared: dict, forced_mode: str | None) -> dict:
    forced = (forced_mode or "").strip()
    mode, hosts = (forced, []) if forced else (task_declared["mode"], task_declared["allowed_hosts"])
    return {
        "mode": mode,
        "allowed_hosts": hosts,
        "forced_by_platform": bool(forced),
        "overrides_task": bool(forced) and (forced != task_declared["mode"] or bool(task_declared["allowed_hosts"])),
        "phase_overrides_kept": task_declared["phase_overrides"],
    }


def for_task(task_root: Path | str, forced_mode: str | None) -> dict | None:
    """{declared, effective} for the task in `task_root`, or None when its task.toml cannot be read."""
    try:
        data = tomllib.loads((Path(task_root) / "task.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    d = declared(data)
    return {"declared": d, "effective": effective(d, forced_mode)}


def _describe(mode: str, hosts: list[str]) -> str:
    return f"{mode} ({', '.join(hosts[:4])}{', ...' if len(hosts) > 4 else ''})" if hosts else mode


def warnings(info: dict | None, web_tools_off: bool) -> list[str]:
    """Plain-words notes for the evidence's run limits."""
    if not info:
        return []
    d, e = info["declared"], info["effective"]
    out = []
    if e["overrides_task"]:
        how = f"`{d['source']}`" if d["source"] != "default" else "its settings"
        out.append(
            f"the task declares network {_describe(d['mode'], d['allowed_hosts'])} (via {how}) but the platform ran it with "
            f"network_mode={e['mode']} (HARBOR_NETWORK_MODE), so these results do not show how the task behaves under its own policy"
        )
    for role, p in (e.get("phase_overrides_kept") or {}).items():
        out.append(
            f"the task sets its own {role}-phase network policy ({_describe(p['mode'], p['allowed_hosts'])}); the platform does not override it"
        )
    if e["mode"] == "public":
        out.append(
            "trials ran with open internet (public network) and "
            + ("the agent's built-in web tools switched off (AGENT_DISABLE_WEB_SEARCH)" if web_tools_off else "the agent's web tools on, as in Terminal-Bench")
            + ": an agent could look something up online. Each trial's web use is counted in the evidence"
        )
    return out
