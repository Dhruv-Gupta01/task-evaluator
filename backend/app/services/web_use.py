"""What an agent did with the web, counted from its own tool calls (`agent/trajectory.json`), not from
what it says it did. Terminal-Bench runs agents with open internet and web tools on, so an agent can
look an answer up; the evidence should show whether it did, and where.

Counted, per trial:
  web_tool_calls  calls to a built-in web tool (Claude Code's WebSearch and WebFetch, Codex's web_search)
  fetches         shell commands that download: curl, wget, git clone
  installs        shell commands that install packages: pip, uv, npm, apt, apk, go, cargo
  hosts           outside hosts named in those calls (localhost, private addresses and bare names are skipped)
Only shell-like tools are scanned for commands, so editing a file that merely contains the word `curl`
does not count. A package install is usually legitimate, so it is counted apart from a lookup."""
from __future__ import annotations

import ipaddress
import json
import re
from pathlib import Path

_WEB_TOOL = re.compile(r"web[_ -]?(search|fetch)|browse|http[_ -]?request", re.IGNORECASE)
_SHELL_TOOL = re.compile(r"bash|shell|exec|command|terminal|run", re.IGNORECASE)
_FETCH = re.compile(r"(?<![\w./-])(curl|wget|git\s+clone)\b", re.IGNORECASE)
_INSTALL = re.compile(
    r"(?<![\w./-])(pip3?\s+install|python3?\s+-m\s+pip\s+install|uv\s+(?:pip\s+install|add|sync)|npm\s+(?:install|i)\b|pnpm\s+(?:add|install)|"
    r"yarn\s+add|apt(?:-get)?\s+install|apk\s+add|go\s+(?:get|install)|cargo\s+(?:install|add))",
    re.IGNORECASE,
)
_URL = re.compile(r"https?://([A-Za-z0-9.-]+)", re.IGNORECASE)
_MAX_EXAMPLES = 5
_MAX_HOSTS = 10


def _external(host: str) -> bool:
    host = host.strip(".").lower()
    if not host or "." not in host:
        return False  # localhost or a bare compose service name
    try:
        ip = ipaddress.ip_address(host)
        return not (ip.is_private or ip.is_loopback or ip.is_link_local)
    except ValueError:
        return True


def _text(arguments) -> str:
    if isinstance(arguments, dict):
        return " ".join(_text(v) for v in arguments.values())
    if isinstance(arguments, (list, tuple)):
        return " ".join(_text(v) for v in arguments)
    return "" if arguments is None else str(arguments)


def scan(trial_dir: Path | str | None) -> dict | None:
    """The counts for one trial, or None when it has no readable trajectory."""
    if trial_dir is None:
        return None
    try:
        steps = json.loads((Path(trial_dir) / "agent" / "trajectory.json").read_text(encoding="utf-8")).get("steps") or []
    except (OSError, ValueError):
        return None
    web_calls = fetches = installs = 0
    hosts: list[str] = []
    examples: list[str] = []

    def see(host_source: str) -> None:
        for h in _URL.findall(host_source):
            if _external(h) and h.lower() not in hosts:
                hosts.append(h.lower())

    for step in steps:
        for call in step.get("tool_calls") or []:
            name = str(call.get("function_name") or "")
            args = _text(call.get("arguments"))
            if _WEB_TOOL.search(name):
                web_calls += 1
                see(args)
                if len(examples) < _MAX_EXAMPLES:
                    examples.append(f"{name}: {' '.join(args.split())[:140]}")
            elif _SHELL_TOOL.search(name):
                text = " ".join(args.split())
                got_fetch, got_install = bool(_FETCH.search(text)), bool(_INSTALL.search(text))
                urls = _URL.findall(text)
                if got_fetch and urls and not any(_external(h) for h in urls):
                    got_fetch = False  # curl to localhost or a sidecar is not a look-up on the web
                if got_fetch:
                    fetches += 1
                if got_install:
                    installs += 1
                if got_fetch or got_install:
                    see(text)
                    if len(examples) < _MAX_EXAMPLES:
                        examples.append(f"{name}: {text[:140]}")
    return {
        "used_web": bool(web_calls or fetches),
        "web_tool_calls": web_calls,
        "fetches": fetches,
        "installs": installs,
        "hosts": hosts[:_MAX_HOSTS],
        "examples": examples,
    }


def describe(web_use: dict | None) -> str:
    """One line for a transcript, a flag or the evidence."""
    if not web_use:
        return "no web use could be read"
    bits = []
    if web_use.get("web_tool_calls"):
        bits.append(f"{web_use['web_tool_calls']} web-tool call(s)")
    if web_use.get("fetches"):
        bits.append(f"{web_use['fetches']} curl/wget/git clone command(s)")
    if web_use.get("installs"):
        bits.append(f"{web_use['installs']} package install(s)")
    if not bits:
        return "none"
    hosts = web_use.get("hosts") or []
    return ", ".join(bits) + (f"; hosts: {', '.join(hosts)}" if hosts else "")


def totals(attempts: list[dict]) -> dict:
    """Across a tier's attempts or a set of trials: how many used the web, and the outside hosts seen."""
    uses = [a.get("web_use") for a in attempts if a.get("web_use")]
    hosts: list[str] = []
    for u in uses:
        for h in u.get("hosts") or []:
            if h not in hosts:
                hosts.append(h)
    return {
        "attempts": len(attempts),
        "readable": len(uses),
        "used_web": sum(1 for u in uses if u.get("used_web")),
        "web_tool_calls": sum(u.get("web_tool_calls") or 0 for u in uses),
        "fetches": sum(u.get("fetches") or 0 for u in uses),
        "installs": sum(u.get("installs") or 0 for u in uses),
        "hosts": hosts[:_MAX_HOSTS],
    }
