"""Drive a separate-verifier task with a scripted agent phase.

This replicates Harbor's separate-verifier contract closely enough to exercise a
task's verifier:

    build environment image
      -> run the probe script in it, as the task's declared agent user
      -> classify and copy out exactly the artifacts task.toml declares
      -> build the verifier image from tests/ (optionally mutated)
      -> restore the artifacts at their declared absolute paths
      -> run tests/test.sh
      -> read /logs/verifier/reward.txt

IT IS A REPLICA, NOT HARBOR. Two consequences, both deliberate:

  * Probes whose answer depends on Harbor's own behaviour rather than the task's
    (artifact sanitisation, reward-file deletion, `auto_mounting`) are marked
    needs_harbor and must be settled with a real `harbor run`. This module records
    what it can and says so.
  * `sanitize=False` skips the emulated artifact sanitisation, which tests whether the
    task defends itself *without* Harbor's help. That is the stronger property and the
    one that survives a harness upgrade.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import re
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.services import harborignore

from . import mutants as mutants_mod
from .taskspec import TaskSpec

# When set (the platform stage sets it to the submission id), every container this module
# starts carries the platform's orphan label, so docker_orchestrator.reap_orphans() removes
# probe containers left behind by a killed backend. Unset (the CLI): no label.
SUBMISSION_LABEL: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "tier0_submission_label", default=None
)
_LABEL_KEY = "taskeval.submission_id"

SPECIAL_KINDS = {"fifo", "socket", "block", "char"}


class DockerUnavailable(RuntimeError):
    pass


@dataclass
class Run:
    reward: float | None = None
    reward_raw: str | None = None
    verifier_exit: int | None = None
    verifier_stdout: str = ""
    timed_out: bool = False
    artifacts: dict = field(default_factory=dict)
    agent_stdout: str = ""
    diagnostics: dict = field(default_factory=dict)
    ctrf: dict = field(default_factory=dict)   # unique test key -> status, from ctrf.json
    ctrf_records: list = field(default_factory=list)  # full per-test detail, in order
    # One entry per environment setting an env_sweep probe graded the output under: {label, reward, ...}.
    env_runs: list = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _run(cmd: list[str], timeout: float | None = None, check: bool = True,
         stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=check, input=stdin,
        encoding="utf-8", errors="replace",  # a task image can print non-UTF-8 bytes
    )


def require_docker() -> str:
    try:
        out = _run(["docker", "version", "--format", "{{.Server.Version}}"], timeout=20,
                   check=False)
    except FileNotFoundError:
        raise DockerUnavailable("docker CLI not found on PATH")
    except subprocess.TimeoutExpired:
        raise DockerUnavailable("docker did not respond within 20s")
    ver = out.stdout.strip()
    if out.returncode != 0 or not ver:
        raise DockerUnavailable(
            "docker daemon unreachable. Start Docker Desktop (or colima) and retry.\n"
            + (out.stderr.strip()[:400])
        )
    return ver


def _add_host_pairs() -> list[tuple[str, str]]:
    """The platform's DOCKER_ADD_HOSTS pins, so builds behave like Build and Harbor's do on
    networks that filter one address of a multi-address host. Empty outside the platform."""
    try:
        from app.config import docker_add_host_pairs
        return docker_add_host_pairs()
    except Exception:
        return []


def _build(dockerfile: Path, context: Path, tag: str, platform: str | None,
           timeout: float) -> str:
    cmd = ["docker", "build", "-t", tag, "-f", str(dockerfile)]
    if platform:
        cmd += ["--platform", platform]
    for host, ip in _add_host_pairs():
        cmd += ["--add-host", f"{host}:{ip}"]
    cmd += [str(context)]
    out = _run(cmd, timeout=timeout, check=False)
    if out.returncode != 0:
        raise RuntimeError(f"build failed for {tag}:\n{out.stdout[-3000:]}\n{out.stderr[-3000:]}")
    return tag


def build_env_image(spec: TaskSpec, platform: str | None = None,
                    timeout: float = 1800) -> str:
    tag = f"probe-env-{spec.slug}:latest"
    return _build(spec.env_dockerfile, spec.env_dockerfile.parent, tag, platform, timeout)


def _as_list(mutant) -> list[str]:
    if mutant is None:
        return []
    return [mutant] if isinstance(mutant, str) else list(mutant)


def build_verifier_image(spec: TaskSpec, platform: str | None = None,
                         mutant=None, timeout: float = 1800) -> tuple[str, Path]:
    """Build the verifier image, optionally with one or more defences removed.

    `mutant` is a mutant id or a sequence of them. Stacking matters: both tasks gate the
    reward on every test passing, so a probe aimed at one defence usually also needs the
    gate that keeps the reward at 0 for unrelated reasons lifted, or its positive control
    can never reach 1.

    Returns (tag, staging_dir). The staging dir is a copy of tests/ with the mutants
    applied; the caller may inspect it and must remove it.
    """
    staging = Path(tempfile.mkdtemp(prefix=f"probe-tests-{spec.slug}-"))
    tests_copy = staging / "tests"
    # What Harbor uploads into the verifier: tests/ minus the files a .harborignore rule excludes.
    harborignore.copy_tests(spec.root, tests_copy)
    ids = _as_list(mutant)
    for mid in ids:
        m = mutants_mod.get(mid)
        if m.task not in (spec.slug, "*"):
            raise ValueError(f"mutant {mid} targets {m.task}, not {spec.slug}")
        m.apply(tests_copy)                      # raises if the anchor did not match
    suffix = "clean" if not ids else "-".join(
        i.split(":", 1)[-1] for i in ids
    )
    # Docker repository names must be lowercase (mutant ids can carry capitals, e.g. a constant
    # called EXPECTED), and truncating or lowercasing could make two mutants share a tag, so a
    # short hash of the full ids keeps each tag unique.
    if ids:
        digest = hashlib.sha1("|".join(ids).encode()).hexdigest()[:8]
        suffix = re.sub(r"[^a-z0-9_.-]", "-", suffix.lower())[:48] + "-" + digest
    tag = f"probe-verifier-{spec.slug}-{suffix}:latest"
    _build(tests_copy / "Dockerfile", tests_copy, tag, platform, timeout)
    return tag, staging


def _start(tag: str, platform: str | None, name: str, extra: list[str] | None = None) -> str:
    cmd = ["docker", "run", "-d", "--name", name]
    label = SUBMISSION_LABEL.get()
    if label:
        cmd += ["--label", f"{_LABEL_KEY}={label}"]
    if platform:
        cmd += ["--platform", platform]
    cmd += (extra or [])
    cmd += ["--entrypoint", "sleep", tag, "infinity"]
    out = _run(cmd, timeout=180, check=False)
    if out.returncode != 0:
        raise RuntimeError(f"could not start {tag}: {out.stderr.strip()[:600]}")
    return out.stdout.strip()


def _rm(cid: str) -> None:
    _run(["docker", "rm", "-f", cid], timeout=120, check=False)


def _classify(cid: str, paths: list[str]) -> dict:
    """Ask the container what each artifact actually is.

    Done with stat inside the container rather than after `docker cp`, because cp's
    symlink semantics differ by direction and version and would blur exactly the
    distinction these probes are testing.
    """
    script = r"""
for p in "$@"; do
  if [ -L "$p" ]; then
    printf '%s\tsymlink\t%s\n' "$p" "$(readlink -- "$p")"
  elif [ -p "$p" ]; then printf '%s\tfifo\t\n' "$p"
  elif [ -S "$p" ]; then printf '%s\tsocket\t\n' "$p"
  elif [ -b "$p" ]; then printf '%s\tblock\t\n' "$p"
  elif [ -c "$p" ]; then printf '%s\tchar\t\n' "$p"
  elif [ -d "$p" ]; then printf '%s\tdir\t\n' "$p"
  elif [ -f "$p" ]; then
    printf '%s\tfile\t%s\n' "$p" "$(stat -c '%s:%a' -- "$p" 2>/dev/null || echo '?:?')"
  else printf '%s\tmissing\t\n' "$p"
  fi
done
"""
    out = _run(["docker", "exec", cid, "sh", "-c", script, "sh", *paths],
               timeout=120, check=False)
    found = {}
    for line in out.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            found[parts[0]] = {"kind": parts[1], "detail": parts[2] if len(parts) > 2 else ""}
    return found


def _sanitise(kind_info: dict, artifact: str, staged: Path, notes: list[str]) -> bool:
    """Emulate Harbor's artifact sanitisation. Returns True if the artifact survives."""
    kind = kind_info.get("kind", "missing")
    if kind == "missing":
        return False
    if kind in SPECIAL_KINDS:
        notes.append(f"sanitised: {artifact} was a {kind}, dropped (Harbor skips these)")
        return False
    if kind == "symlink":
        target = kind_info.get("detail", "")
        root = str(Path(artifact).parent)
        resolved = target if target.startswith("/") else str((Path(root) / target))
        if not resolved.startswith(root.rstrip("/") + "/") and resolved != root:
            notes.append(
                f"sanitised: {artifact} was a symlink to {target!r}, outside its artifact "
                f"root {root}; dropped (Harbor drops these)"
            )
            return False
    if staged.is_file():
        mode = staged.stat().st_mode
        if mode & (stat.S_ISUID | stat.S_ISGID):
            staged.chmod(mode & ~(stat.S_ISUID | stat.S_ISGID))
            notes.append(f"sanitised: cleared setuid/setgid on {artifact}")
    return True


_PARAM_LINE = re.compile(r"^(\w+) = (.+)$")


def _param_from_trace(trace: str) -> str | None:
    """Best-effort recovery of a parametrized case's argument from the failure trace.

    pytest's assertion rewriting prints the test function's arguments above the failing
    line, so a failed case usually names itself there. There is no equivalent for a
    passing case, which is a limitation of the CTRF output rather than of this parsing:
    see the note in _ingest_ctrf.
    """
    for line in (trace or "").splitlines()[:6]:
        m = _PARAM_LINE.match(line.strip())
        if m and not m.group(2).startswith(("<", "def ")):
            # pytest puts every argument on one line when they fit, so drop the
            # fixtures that follow and keep only the first, which is the parameter.
            value = re.split(r",\s+\w+\s*=\s*", m.group(2))[0]
            return f"{m.group(1)}={value}"
    return None


def _ingest_ctrf(run: Run, tests: list) -> None:
    """Key per-test outcomes uniquely, and record what the case was where we can.

    pytest-json-ctrf 0.5.2 writes the SAME `name` for every case of a parametrized
    test, and its `tags` carry the whole parameter list rather than the case's own
    value. So a harrow run produces 55 entries all called
    `test_outputs.py::test_unseen_book`, and keying a dict by name silently collapses
    them to one, last-write-wins. Occurrence index is therefore the only identity CTRF
    offers, and it is stable here because both tasks derive their parameters from a
    sorted glob.

    Names that occur once keep their bare name, so a task with no parametrized tests
    (pgwal) produces exactly the keys it did before this was fixed, and its baselines
    stay valid.
    """
    counts: dict[str, int] = {}
    for t in tests:
        name = t.get("name", "?")
        counts[name] = counts.get(name, 0) + 1
    seen: dict[str, int] = {}
    collapsed = 0
    for t in tests:
        name = t.get("name", "?")
        idx = seen.get(name, 0)
        seen[name] = idx + 1
        if counts[name] > 1:
            key = f"{name}#{idx}"
            collapsed += 1
        else:
            key = name
        status = t.get("status", "?")
        run.ctrf[key] = status
        rec = {"key": key, "name": name, "status": status,
               "duration": t.get("duration"), "index": idx}
        # Only for collapsed (parametrized) names. On a test that takes no parameter the
        # first `x = y` line of the trace is an ordinary local, and labelling a diff with
        # it would read as a case identity that does not exist.
        if counts[name] > 1:
            param = _param_from_trace(t.get("trace", ""))
            if param:
                rec["param"] = param
        if t.get("message"):
            rec["message"] = t["message"][:300]
        run.ctrf_records.append(rec)
    if collapsed:
        run.notes.append(
            f"{collapsed} ctrf entries share a name with another (parametrized tests); "
            f"keyed by occurrence index, since pytest-json-ctrf does not record which "
            f"case an entry was"
        )


def _exec_env_args(env: dict | None) -> list[str]:
    """`docker exec -e K=V ...` arguments for the verifier process."""
    return [a for k, v in (env or {}).items() for a in ("-e", f"{k}={v}")]


def _verify(spec: TaskSpec, run: Run, staged_dir: Path, platform: str | None,
            mutant, keep: bool, diagnostics: dict, verifier_env: dict | None = None) -> None:
    """Build the verifier, restore the staged artifacts into it, score the run.

    Shared by run_probe and run_replay: from the verifier's point of view a scripted
    probe and a banked rollout are the same thing, a set of files at the declared
    artifact paths. Keeping one implementation means a replay exercises the identical
    contract the probes do, which is the whole point of using it as a regression gate.

    Mutates `run` in place.
    """
    stamp = str(int(time.time() * 1000))[-9:]
    ver_cid = None
    staging = None
    try:
        ver_tag, staging = build_verifier_image(spec, platform, mutant)
        ver_cid = _start(ver_tag, platform, f"probe-ver-{spec.slug}-{stamp}")
        for artifact, meta in run.artifacts.items():
            if not meta["restored"]:
                continue
            parent = str(Path(artifact).parent)
            _run(["docker", "exec", ver_cid, "mkdir", "-p", parent], timeout=60, check=False)
            _run(["docker", "cp", str(staged_dir / artifact.lstrip("/")),
                  f"{ver_cid}:{artifact}"], timeout=600, check=False)

        try:
            ver = _run(["docker", "exec", *_exec_env_args(verifier_env), ver_cid, "bash", "/tests/test.sh"],
                       timeout=spec.verifier_timeout_sec, check=False)
            run.verifier_exit = ver.returncode
            run.verifier_stdout = (ver.stdout + ver.stderr)[-20000:]
        except subprocess.TimeoutExpired:
            run.timed_out = True
            run.notes.append(
                f"verifier exceeded timeout_sec={spec.verifier_timeout_sec}. In Harbor this "
                f"is an INVALID reward (trial excluded), not a 0."
            )

        if not run.timed_out:
            got = _run(["docker", "exec", ver_cid, "cat", "/logs/verifier/reward.txt"],
                       timeout=60, check=False)
            run.reward_raw = got.stdout.strip() if got.returncode == 0 else None
            if run.reward_raw:
                try:
                    run.reward = float(run.reward_raw)
                except ValueError:
                    run.notes.append(f"reward.txt not numeric: {run.reward_raw!r}")
            else:
                rj = _run(["docker", "exec", ver_cid, "cat", "/logs/verifier/reward.json"],
                          timeout=60, check=False)
                if rj.returncode == 0 and rj.stdout.strip():
                    try:
                        run.reward = float(json.loads(rj.stdout)["reward"])
                        run.reward_raw = rj.stdout.strip()
                    except Exception:
                        run.notes.append("reward.json present but unparseable")
                else:
                    run.notes.append(
                        "no reward file produced. In Harbor this is an INVALID reward."
                    )

        # Per-test outcomes. Both reviewed tasks gate the reward on every test passing,
        # so ctrf is usually the only place a single defence's behaviour is visible.
        ctrf = _run(["docker", "exec", ver_cid, "cat", "/logs/verifier/ctrf.json"],
                    timeout=60, check=False)
        if ctrf.returncode == 0 and ctrf.stdout.strip():
            try:
                doc = json.loads(ctrf.stdout)
                _ingest_ctrf(run, doc.get("results", {}).get("tests", []))
            except Exception as exc:
                run.notes.append(f"ctrf.json unparseable: {exc}")
        else:
            run.notes.append("no ctrf.json produced")

        for label, path in (diagnostics or {}).items():
            d = _run(["docker", "exec", ver_cid, "sh", "-c",
                      f"cat {path} 2>/dev/null | tail -c 4000"], timeout=60, check=False)
            if d.stdout.strip():
                run.diagnostics[label] = d.stdout.strip()
    finally:
        if ver_cid and not keep:
            _rm(ver_cid)
        if staging:
            if keep:
                run.notes.append(f"kept: tests staging {staging}")
            else:
                shutil.rmtree(staging, ignore_errors=True)


def _verify_under_settings(spec: TaskSpec, run: Run, staged_dir: Path, platform: str | None,
                           mutant, keep: bool, settings: list[tuple[str, dict]]) -> None:
    """Grade the collected output once per environment setting, each in a fresh verifier container. The first
    setting is the baseline: its result becomes the run's own, the rest are listed in `run.env_runs`."""
    for i, (label, env) in enumerate(settings):
        one = Run(artifacts=dict(run.artifacts))
        _verify(spec, one, staged_dir, platform, mutant, keep, {}, verifier_env=env)
        run.env_runs.append({"label": label, "reward": one.reward, "timed_out": one.timed_out,
                             "verifier_exit": one.verifier_exit})
        if i == 0:
            for name in ("reward", "reward_raw", "verifier_exit", "verifier_stdout", "timed_out",
                         "ctrf", "ctrf_records", "diagnostics"):
                setattr(run, name, getattr(one, name))
        run.notes += [f"[{label}] {n}" for n in one.notes if "ctrf" not in n]


def run_probe(spec: TaskSpec, probe, platform: str | None = None,
              mutant: str | None = None, sanitize: bool = True,
              keep: bool = False, extra_artifacts: list[str] | None = None,
              extra_inject: dict | None = None,
              verifier_envs: list[tuple[str, dict]] | None = None) -> Run:
    require_docker()
    run = Run()
    stamp = str(int(time.time() * 1000))[-9:]
    env_name = f"probe-env-{spec.slug}-{stamp}"
    env_cid = None
    staged_dir = Path(tempfile.mkdtemp(prefix="probe-artifacts-"))

    try:
        env_tag = build_env_image(spec, platform)
        env_cid = _start(env_tag, platform, env_name)

        # ---- injected material (positive controls that need the real answers) --
        for dest, rel in {**getattr(probe, "inject", {}), **(extra_inject or {})}.items():
            src = (spec.root / rel).resolve()
            if not src.exists():
                raise FileNotFoundError(f"probe {probe.pid}: inject source missing: {src}")
            _run(["docker", "exec", env_cid, "mkdir", "-p", str(Path(dest).parent)],
                 timeout=60, check=False)
            cp = _run(["docker", "cp", str(src), f"{env_cid}:{dest}"], timeout=600, check=False)
            if cp.returncode != 0:
                raise RuntimeError(f"inject {src} -> {dest} failed: {cp.stderr[:400]}")
            _run(["docker", "exec", env_cid, "chmod", "-R", "a+rX", dest], timeout=60, check=False)
            run.notes.append(f"injected {rel} -> {dest} (models prior answer access)")

        # ---- agent phase: the probe stands in for the agent -------------------
        user = "root" if probe.AS_ROOT else (spec.agent_user or "root")
        # Artifact paths arrive as "$@" so probe scripts stay task-agnostic.
        exec_cmd = ["docker", "exec", "-u", user, "-i", env_cid, "bash", "-s",
                    "--", *spec.artifacts]
        agent = _run(exec_cmd, timeout=getattr(probe, "AGENT_TIMEOUT_SEC", 900),
                     check=False, stdin=probe.AGENT_SCRIPT)
        run.agent_stdout = (agent.stdout + agent.stderr)[-8000:]
        if agent.returncode != 0:
            run.notes.append(f"probe script exited {agent.returncode} (not necessarily wrong)")

        # ---- artifact collection ---------------------------------------------
        # Extra paths are collected and restored like declared artifacts, but the probe
        # script never sees them in "$@". Used by observer controls: declaring a path the
        # probe planted shows whether the probe's check can see a leak at all.
        collect = list(spec.artifacts) + [a for a in (extra_artifacts or []) if a not in spec.artifacts]
        kinds = _classify(env_cid, collect)
        for artifact in collect:
            info = kinds.get(artifact, {"kind": "missing", "detail": ""})
            local = staged_dir / artifact.lstrip("/")
            local.parent.mkdir(parents=True, exist_ok=True)
            cp = _run(["docker", "cp", f"{env_cid}:{artifact}", str(local)],
                      timeout=600, check=False)
            copied = cp.returncode == 0 and (local.exists() or local.is_symlink())
            survives = copied
            if copied and sanitize:
                survives = _sanitise(info, artifact, local, run.notes)
            if copied and not survives:
                shutil.rmtree(local, ignore_errors=True) if local.is_dir() else local.unlink(missing_ok=True)
            run.artifacts[artifact] = {
                "kind": info["kind"], "detail": info["detail"],
                "copied": copied, "restored": survives,
            }
        if not sanitize:
            run.notes.append("artifact sanitisation DISABLED: testing the task's own defences")
        _rm(env_cid); env_cid = None

        # ---- verify phase ----------------------------------------------------
        if verifier_envs:
            _verify_under_settings(spec, run, staged_dir, platform, mutant, keep, verifier_envs)
        else:
            _verify(spec, run, staged_dir, platform, mutant, keep,
                    getattr(probe, "DIAGNOSTICS", {}))
        return run

    finally:
        if env_cid and not keep:
            _rm(env_cid)
        if not keep:
            shutil.rmtree(staged_dir, ignore_errors=True)
        else:
            run.notes.append(f"kept: artifacts {staged_dir}")


def run_replay(spec: TaskSpec, bundle, platform: str | None = None,
               mutant=None, keep: bool = False) -> Run:
    """Score a banked rollout's artifacts against the (optionally mutated) verifier.

    No environment image, no agent phase, no model spend. The bundle already holds what
    a real agent produced, collected by real Harbor, so this is the cheap half of the
    regression gate: a verifier change is a MINOR bump precisely because the agent's
    output stays valid, and this is how you cash that in.

    The bundle is agent output, which makes it untrusted input to a root verifier. Two
    guards, both in replay.Bundle rather than here: no entry may restore under /logs
    (that is where the reward lives), and no entry may escape its declared artifact
    path. A bundle that violates either is refused at load time, not here.
    """
    require_docker()
    run = Run()
    staged_dir = Path(tempfile.mkdtemp(prefix="replay-artifacts-"))
    try:
        for artifact in spec.artifacts:
            src = bundle.file_for(artifact)
            local = staged_dir / artifact.lstrip("/")
            local.parent.mkdir(parents=True, exist_ok=True)
            if src is None:
                run.artifacts[artifact] = {
                    "kind": "missing", "detail": "not in bundle",
                    "copied": False, "restored": False,
                }
                run.notes.append(
                    f"{artifact}: absent from the bundle, so the verifier sees it missing, "
                    f"exactly as Harbor would have presented it"
                )
                continue
            if src.is_dir():
                shutil.copytree(src, local, symlinks=True, dirs_exist_ok=True)
                kind = "dir"
            else:
                shutil.copy2(src, local)
                kind = "file"
            run.artifacts[artifact] = {
                "kind": kind, "detail": f"{src.stat().st_size}", "copied": True,
                "restored": True,
            }
        run.notes.append(f"replayed bundle {bundle.key} ({bundle.origin})")
        _verify(spec, run, staged_dir, platform, mutant, keep, {})
        return run
    finally:
        if not keep:
            shutil.rmtree(staged_dir, ignore_errors=True)
        else:
            run.notes.append(f"kept: artifacts {staged_dir}")


def cleanup_images(spec: TaskSpec) -> int:
    """Remove the images this module built for the task (environment and every verifier
    variant). They are big, and a stage that ran many mutants leaves one per mutant.
    Layer cache is kept by Docker, so a re-run is still fast. Returns how many were removed."""
    out = _run(
        ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"], timeout=60, check=False,
    )
    prefixes = (f"probe-env-{spec.slug}:", f"probe-verifier-{spec.slug}-")
    tags = [t for t in out.stdout.split() if t.startswith(prefixes)]
    removed = 0
    for tag in tags:
        if _run(["docker", "rmi", "-f", tag], timeout=120, check=False).returncode == 0:
            removed += 1
    return removed
