import io
import os
import shutil
import subprocess
import tarfile
import tempfile
import uuid
from pathlib import Path

import docker
import requests
from docker import DockerClient
from docker.errors import APIError

_client: DockerClient | None = None

MAX_LOG_CHARS = 200_000
CONTAINER_LABEL_KEY = "taskeval.submission_id"

# backend/ directory root (this file is backend/app/services/docker_orchestrator.py)
_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
_AGENT_WRAPPER_DIR = _BACKEND_ROOT / "docker" / "agent_wrapper"


class DockerBuildError(Exception):
    def __init__(self, log_text: str):
        super().__init__("docker image build failed")
        self.log_text = log_text


def get_client() -> DockerClient:
    global _client
    if _client is None:
        _client = docker.from_env()
    return _client


def _truncate(log_text: str) -> str:
    if len(log_text) > MAX_LOG_CHARS:
        return log_text[-MAX_LOG_CHARS:]
    return log_text


DEFAULT_BUILD_TIMEOUT_SEC = 600


def _run_docker_build_cli(
    context_dir,
    tag: str,
    buildargs: dict[str, str] | None,
    labels: dict[str, str],
    timeout_sec: float = DEFAULT_BUILD_TIMEOUT_SEC,
) -> tuple[str, str]:
    """Build via the `docker` CLI (BuildKit-enabled by default on modern
    Docker) rather than docker-py's classic client.api.build(), which never
    supports BuildKit and fails on Dockerfiles using BuildKit-only syntax
    (e.g. `COPY --chmod=`, `--from=`, heredocs).

    Always targets linux/amd64 (via QEMU emulation on Apple Silicon) to
    match the real Terminal-Bench/Snorkel grading environment — tasks that
    pin architecture-specific binaries (e.g. a checksummed amd64-only .so)
    must build and run under the same architecture locally as they will at
    real submission time, or failures only surface after submitting.

    Always has a timeout — a hung build (stalled network fetch, an
    interactive prompt some Dockerfile step didn't expect, emulation
    slowness) must eventually fail cleanly rather than block that
    submission's build forever."""
    cmd = ["docker", "build", "--platform", "linux/amd64", "--tag", tag]
    for k, v in (buildargs or {}).items():
        cmd += ["--build-arg", f"{k}={v}"]
    for k, v in labels.items():
        cmd += ["--label", f"{k}={v}"]
    cmd.append(str(context_dir))

    env = {**os.environ, "DOCKER_BUILDKIT": "1"}
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout_sec)
    except subprocess.TimeoutExpired as e:
        partial = (e.stdout or "") + (e.stderr or "")
        raise DockerBuildError(
            _truncate(partial) + f"\n\n[build timed out after {timeout_sec}s]"
        )
    log_text = _truncate(proc.stdout + proc.stderr)
    if proc.returncode != 0:
        raise DockerBuildError(log_text)
    return tag, log_text


def build_image(
    build_context_dir, submission_id: str, timeout_sec: float = DEFAULT_BUILD_TIMEOUT_SEC
) -> tuple[str, str]:
    """Build the task's environment/ directory into an image.
    Returns (image_tag, log_text). Raises DockerBuildError on failure."""
    image_tag = f"taskeval/{submission_id}:build"
    return _run_docker_build_cli(
        build_context_dir,
        image_tag,
        buildargs=None,
        labels={CONTAINER_LABEL_KEY: submission_id},
        timeout_sec=timeout_sec,
    )


def build_agent_wrapper_image(base_image_tag: str, submission_id: str) -> tuple[str, str]:
    """Build a wrapper image layered on the task's own built image, adding the
    agent runtime (LLM clients + control loop). Built once per submission,
    reused across all N trials. The build context never includes tests/ or
    solution/ — only app/services/llm and app/services/agent modules — so
    isolation holds structurally, not just by convention."""
    image_tag = f"taskeval/{submission_id}:agent"

    with tempfile.TemporaryDirectory(prefix="agent-wrapper-build-") as tmp:
        context_dir = Path(tmp)
        shutil.copy(_AGENT_WRAPPER_DIR / "Dockerfile", context_dir / "Dockerfile")

        runtime_dir = context_dir / "agent_runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        # entrypoint.py must live inside agent_runtime/ since the Dockerfile
        # only does `COPY agent_runtime/ /opt/agent_runtime/`
        shutil.copy(_AGENT_WRAPPER_DIR / "entrypoint.py", runtime_dir / "entrypoint.py")

        app_dir = runtime_dir / "app"
        (app_dir).mkdir(parents=True, exist_ok=True)
        (app_dir / "__init__.py").write_text("")

        services_dir = app_dir / "services"
        services_dir.mkdir(parents=True, exist_ok=True)
        (services_dir / "__init__.py").write_text("")

        shutil.copytree(_BACKEND_ROOT / "app" / "services" / "llm", services_dir / "llm")
        shutil.copytree(_BACKEND_ROOT / "app" / "services" / "agent", services_dir / "agent")

        return _run_docker_build_cli(
            context_dir,
            image_tag,
            buildargs={"BASE_IMAGE": base_image_tag},
            labels={CONTAINER_LABEL_KEY: submission_id},
        )


def reap_orphans() -> None:
    """On backend startup, force-remove any containers left over from a
    previous process lifetime (identified by our label). A killed backend
    can't run its own try/finally cleanup, so these must be swept on boot."""
    client = get_client()
    containers = client.containers.list(all=True, filters={"label": CONTAINER_LABEL_KEY})
    for c in containers:
        try:
            c.remove(force=True)
        except Exception:
            pass


def seed_workdir_from_image(
    image_tag: str, workdir: Path, container_workdir_path: str, submission_id: str
) -> None:
    """Populate workdir with whatever the image's own Dockerfile baked into
    its WorkingDir (fixtures, starter code, a pre-built database, etc.)
    before any solve/verify phase runs. Without this, bind-mounting an empty
    host directory over the container's WorkingDir would silently erase
    content the task's own image legitimately shipped with — which is the
    normal case for a real task (COPY app/ /app/ + a build-time setup step),
    not just an edge case."""
    # Uses a container-side `cp -a` into a bind-mounted host directory,
    # rather than `get_archive()` + Python's `tarfile` extraction. tarfile's
    # hardlink handling requires the link's target file to also be present
    # within the same tar stream; a subtree archive (e.g. just /app) can
    # contain a hardlink whose target lives outside that subtree (observed
    # with a compiled native Node addon under node_modules, hardlinked from
    # an npm cache path), which tarfile can't resolve and raises KeyError.
    # `cp -a` operates on the container's real filesystem and has no such
    # constraint.
    seed_dest = "/__taskeval_seed_dest"
    exit_code, logs = run_phase(
        image_tag=image_tag,
        mounts={str(workdir): (seed_dest, "rw")},
        command=[
            "sh",
            "-c",
            f'if [ -d "{container_workdir_path}" ]; then '
            f'cp -a "{container_workdir_path}/." "{seed_dest}/"; '
            f"fi",
        ],
        network_disabled=True,
        timeout_sec=180,
        container_name=f"taskeval-seed-{submission_id}-{uuid.uuid4().hex[:8]}",
        submission_id=submission_id,
    )
    if exit_code != 0:
        raise DockerBuildError(f"seeding workdir from image failed (exit {exit_code}):\n{logs}")


def get_image_workdir(image_tag: str, fallback: str = "/app") -> str:
    """Introspect the image's configured WORKDIR rather than assuming a
    convention, so the host-side workdir gets bind-mounted to wherever the
    task's own Dockerfile expects to operate."""
    client = get_client()
    image = client.images.get(image_tag)
    working_dir = image.attrs.get("Config", {}).get("WorkingDir")
    return working_dir or fallback


def run_phase(
    image_tag: str,
    mounts: dict[str, tuple[str, str]],
    command: list[str] | None,
    network_disabled: bool,
    timeout_sec: int,
    container_name: str,
    submission_id: str,
    mem_limit_mb: int | None = None,
    nano_cpus: int | None = None,
    environment: dict[str, str] | None = None,
) -> tuple[int, str]:
    """Run a single container phase (oracle solve / verifier / agent loop).
    mounts: {host_path: (container_path, mode)}. Always removes the
    container afterward, even on timeout or error, and always returns
    (exit_code, logs) rather than raising for normal failure cases."""
    client = get_client()

    volumes = {
        host_path: {"bind": container_path, "mode": mode}
        for host_path, (container_path, mode) in mounts.items()
    }

    kwargs: dict = {}
    if mem_limit_mb:
        kwargs["mem_limit"] = f"{mem_limit_mb}m"
    if nano_cpus:
        kwargs["nano_cpus"] = nano_cpus
    if environment:
        kwargs["environment"] = environment

    container = None
    try:
        container = client.containers.run(
            image_tag,
            command=command,
            volumes=volumes,
            network_disabled=network_disabled,
            detach=True,
            name=container_name,
            remove=False,
            labels={CONTAINER_LABEL_KEY: submission_id},
            platform="linux/amd64",
            **kwargs,
        )
        try:
            result = container.wait(timeout=timeout_sec)
            exit_code = result.get("StatusCode", 1)
        except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError, APIError):
            try:
                container.kill()
            except Exception:
                pass
            exit_code = 124  # conventional "timed out" exit code

        try:
            logs = container.logs(stdout=True, stderr=True).decode(errors="replace")
        except Exception:
            logs = ""
        return exit_code, _truncate(logs)
    finally:
        if container is not None:
            try:
                container.remove(force=True)
            except Exception:
                pass
