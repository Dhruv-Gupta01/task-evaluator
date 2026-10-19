import subprocess

import docker
from docker import DockerClient

from app.config import docker_add_host_pairs, get_settings, host_env_for_subprocess

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
    fresh: bool = False,
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
    if fresh:
        # H.1: no layer cache and newly pulled base tags, like a build on a clean machine today.
        cmd += ["--no-cache", "--pull"]
    for host, ip in docker_add_host_pairs():
        cmd += ["--add-host", f"{host}:{ip}"]
    for k, v in (buildargs or {}).items():
        cmd += ["--build-arg", f"{k}={v}"]
    for k, v in labels.items():
        cmd += ["--label", f"{k}={v}"]
    cmd.append(str(context_dir))

    env = host_env_for_subprocess({"DOCKER_BUILDKIT": "1"})
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
    build_context_dir, submission_id: str, timeout_sec: float = DEFAULT_BUILD_TIMEOUT_SEC, fresh: bool = False
) -> tuple[str, str]:
    """Build the task's environment/ directory into an image.
    Returns (image_tag, log_text). Raises DockerBuildError on failure.
    fresh=True builds a separate tag with no cache and freshly pulled bases."""
    image_tag = f"taskeval/{submission_id}:{'fresh' if fresh else 'build'}"
    return _run_docker_build_cli(
        build_context_dir,
        image_tag,
        buildargs=None,
        labels={CONTAINER_LABEL_KEY: submission_id},
        timeout_sec=timeout_sec,
        fresh=fresh,
    )


def remove_image(tag: str) -> None:
    subprocess.run(["docker", "image", "rm", "-f", tag], capture_output=True, timeout=120, env=host_env_for_subprocess())


# One busy loop per CPU the container sees, until it is removed.
_CPU_LOAD_SCRIPT = 'n=$(nproc 2>/dev/null || echo 2); i=0; while [ "$i" -lt "$n" ]; do (while :; do :; done) & i=$((i+1)); done; wait'


def _docker_cpus() -> int:
    try:
        out = subprocess.run(
            ["docker", "info", "--format", "{{.NCPU}}"], capture_output=True, text=True, timeout=60,
            env=host_env_for_subprocess(),
        ).stdout.strip()
        return int(out)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


def start_cpu_load(image_tag: str, submission_id: str) -> tuple[str | None, str]:
    """G.3: a sibling container that keeps the Docker VM's CPUs busy while a
    stress Oracle run goes on, so a verifier or solution that only passes on a
    quiet machine shows up. One busy loop per CPU, capped at all CPUs but one:
    on Docker Desktop the daemon runs in the same VM, and with every CPU taken
    Harbor's own start-up check decided Docker was not running (seen live).
    Uses the task's own built image, so nothing is pulled; labelled, so
    reap_orphans removes it after a crash. Returns (container id or None, a
    note for the run's logs)."""
    cpus = _docker_cpus()
    limit = ["--cpus", str(cpus - 1)] if cpus >= 2 else []
    cmd = [
        "docker", "run", "-d", "--rm", "--network", "none", *limit,
        "--platform", get_settings().docker_platform,
        "--label", f"{CONTAINER_LABEL_KEY}={submission_id}",
        "--entrypoint", "sh", image_tag, "-c", _CPU_LOAD_SCRIPT,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=host_env_for_subprocess())
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"(stress run: could not start the CPU load: {exc}; this run was NOT under load)"
    if proc.returncode != 0 or not proc.stdout.strip():
        return None, f"(stress run: could not start the CPU load: {proc.stderr.strip()[-300:]}; this run was NOT under load)"
    share = f"{cpus - 1} of {cpus} CPUs" if cpus >= 2 else "the CPUs"
    return proc.stdout.strip(), (
        f"(stress run: a sibling container ran a busy loop on every CPU, using {share} of the Docker VM, "
        "for the whole trial)"
    )


def stop_cpu_load(container_id: str | None) -> None:
    if container_id:
        subprocess.run(["docker", "rm", "-f", container_id], capture_output=True, timeout=120, env=host_env_for_subprocess())


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
