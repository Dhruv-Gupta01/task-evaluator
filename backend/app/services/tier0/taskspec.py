"""Read a Terminal-Bench / Harbor task package well enough to drive it.

Deliberately narrow: only the fields the probe harness needs. Anything this cannot
determine raises, rather than guessing, because a probe that runs against a
misunderstood task spec produces a confident wrong answer.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_COMPOSE_GLOB = "docker-compose*.y*ml"


@dataclass
class TaskSpec:
    root: Path
    name: str
    artifacts: list[str]
    verifier_mode: str
    agent_user: str | None
    verifier_timeout_sec: float
    agent_timeout_sec: float | None
    env_dockerfile: Path
    tests_dockerfile: Path
    test_entrypoint: Path
    solution_dir: Path | None
    raw: dict = field(repr=False, default_factory=dict)
    # Compose services other than `main` (e.g. an evaluation server). The harness never
    # starts them: probes stand in for the agent and the verifier runs in its own image,
    # so they only matter to a probe that needs the sidecar, which none here do.
    sidecars: list[str] = field(default_factory=list)

    @property
    def slug(self) -> str:
        """The task's own name, e.g. `int8-runtime-port` for `terminal-bench/int8-runtime-port`.

        Not the folder name: the platform extracts every upload into a generic
        `extracted/` folder, so the folder says nothing about which task this is, and
        mutants are matched to a task by this slug.
        """
        name = str(self.name or "").strip()
        return (name.rsplit("/", 1)[-1] or self.root.name) if name else self.root.name

    @property
    def artifact_parents(self) -> set[str]:
        return {str(Path(a).parent) for a in self.artifacts}

    def reward_paths_in_artifacts(self) -> list[str]:
        """Artifacts that would let an agent hand the verifier its own reward."""
        return [a for a in self.artifacts if a.startswith("/logs")]


def load(task_root: str | Path) -> TaskSpec:
    root = Path(task_root).resolve()
    toml_path = root / "task.toml"
    if not toml_path.is_file():
        raise FileNotFoundError(f"no task.toml under {root}")
    data = tomllib.loads(toml_path.read_text(encoding="utf-8"))

    artifacts = data.get("artifacts", [])
    if not isinstance(artifacts, list) or not artifacts:
        raise ValueError(f"{toml_path}: top-level `artifacts` missing or empty")
    # Harbor also accepts table entries {source=..., destination=...}
    flat: list[str] = []
    for entry in artifacts:
        if isinstance(entry, str):
            flat.append(entry)
        elif isinstance(entry, dict) and "source" in entry:
            flat.append(entry["source"])
        else:
            raise ValueError(f"{toml_path}: unsupported artifacts entry {entry!r}")

    verifier = data.get("verifier", {})
    mode = verifier.get("environment_mode", "shared")
    if mode != "separate":
        raise ValueError(
            f"{toml_path}: this harness only models separate-verifier tasks "
            f"(environment_mode={mode!r}). Shared-verifier tasks need a different "
            f"contract and a different probe set."
        )

    env_dockerfile = root / "environment" / "Dockerfile"
    sidecars = _compose_sidecars(root / "environment")
    if not env_dockerfile.is_file():
        raise FileNotFoundError(f"missing {env_dockerfile}")

    tests_dockerfile = root / "tests" / "Dockerfile"
    test_entrypoint = root / "tests" / "test.sh"
    for p in (tests_dockerfile, test_entrypoint):
        if not p.is_file():
            raise FileNotFoundError(f"missing {p}")

    solution = root / "solution"
    return TaskSpec(
        root=root,
        name=data.get("task", {}).get("name", root.name),
        artifacts=flat,
        verifier_mode=mode,
        agent_user=data.get("agent", {}).get("user"),
        verifier_timeout_sec=float(verifier.get("timeout_sec", 600.0)),
        agent_timeout_sec=(
            float(data["agent"]["timeout_sec"])
            if "agent" in data and "timeout_sec" in data["agent"]
            else None
        ),
        env_dockerfile=env_dockerfile,
        tests_dockerfile=tests_dockerfile,
        test_entrypoint=test_entrypoint,
        solution_dir=solution if solution.is_dir() else None,
        raw=data,
        sidecars=sidecars,
    )


def _compose_sidecars(env_dir: Path) -> list[str]:
    """Names of compose services other than `main`.

    A compose file is fine when `main` is still built from environment/Dockerfile, which
    is what the harness builds. If `main` has its own `build:` or `image:`, the agent's
    container may come from somewhere else, so a probe would run in the wrong image and
    the answer would be about a container the agent never had. That, or an unparseable
    file, is refused.
    """
    names: list[str] = []
    for path in sorted(env_dir.glob(_COMPOSE_GLOB)):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            services = data.get("services") or {}
            main = services.get("main") or {}
        except (yaml.YAMLError, AttributeError, OSError) as exc:
            raise ValueError(f"{path.name} could not be parsed ({exc}); refusing to guess which image `main` uses")
        if not isinstance(main, dict) or "build" in main or "image" in main:
            raise ValueError(
                f"{path.name} gives the `main` service its own build/image, so the agent's "
                f"container may not come from environment/Dockerfile; this harness cannot "
                f"drive it faithfully."
            )
        names += [n for n in services if n != "main" and n not in names]
    return names
