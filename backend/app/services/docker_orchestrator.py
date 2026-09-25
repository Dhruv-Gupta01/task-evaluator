import os
import subprocess

import docker
from docker import DockerClient

from app.config import docker_add_host_pairs, get_settings

_client: DockerClient | None = None

MAX_LOG_CHARS = 200_000
CONTAINER_LABEL_KEY = "taskeval.submission_id"


class DockerBuildError(Exception):
    def __init__(self, log_text: str):
        super().__init__("docker image build failed")
        self.log_text = log_text


def get_client() -> DockerClient:
    global _client
    if _client is None:
        # docker-py's default HTTP client timeout is 60s, applied to every
        # Docker Engine API call (including plain container creation) —
        # entirely separate from our own timeout_sec params, which only
        # bound how long we wait for a container to finish *running*.
        # Creating a container from an unusually large image (e.g. a full
        # compiled C++ toolchain with multiple binaries) can itself exceed
        # 60s, raising a raw requests.exceptions.ReadTimeout with no
        # relation to the task's actual execution timeout.
        _client = docker.from_env(timeout=300)
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

    Targets the configured DOCKER_PLATFORM (linux/arm64 by default) so
    builds, Harbor runs and agent trials all use one architecture —
    architecture-specific binaries (e.g. a checksummed amd64-only .so)
    otherwise pass in one stage and fail in another.

    Always has a timeout — a hung build (stalled network fetch, an
    interactive prompt some Dockerfile step didn't expect, emulation
    slowness) must eventually fail cleanly rather than block that
    submission's build forever."""
    cmd = ["docker", "build", "--platform", get_settings().docker_platform, "--tag", tag]
    for host, ip in docker_add_host_pairs():
        cmd += ["--add-host", f"{host}:{ip}"]
    for k, v in (buildargs or {}).items():
        cmd += ["--build-arg", f"{k}={v}"]
    for k, v in labels.items():
        cmd += ["--label", f"{k}={v}"]
    cmd.append(str(context_dir))

    env = {**os.environ, "DOCKER_BUILDKIT": "1"}
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout_sec)
    except subprocess.TimeoutExpired as e:
        # On timeout, CPython's subprocess module returns the partial
        # stdout/stderr it had buffered as raw bytes even though text=True
        # was requested — decoding only happens on the normal return path.
        def _decode(chunk: bytes | str | None) -> str:
            if chunk is None:
                return ""
            return chunk.decode(errors="replace") if isinstance(chunk, bytes) else chunk

        partial = _decode(e.stdout) + _decode(e.stderr)
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
