"""Lock and licence hygiene beyond pip (5.0 category F). Plain text checks over
the task files, no LLM, all advisory (warn or info, never fail).

check-pip-pinning (vendored from Terminal-Bench) covers pip, and
hygiene_checks covers npm/apt/`curl | sh`. This adds the other ecosystems the
5.0 milestone found unlocked (opam, elan/lake, sbt, Perl, conda), tool
bootstraps and Hugging Face downloads without a revision, bundled data or
models without a licence, and a size report for reviewers."""

import re
from pathlib import Path

from app.services.hygiene_checks import _SPLIT, _listed, _own_files, _run_lines, _text_files

_BIG_TASK_FILES = 80  # upstream's hard-to-review tasks had 84 and 117 files
_DATA_SUFFIXES = {
    ".csv", ".tsv", ".parquet", ".arrow", ".npz", ".npy", ".pt", ".pth", ".onnx", ".bin", ".safetensors",
    ".h5", ".hdf5", ".pkl", ".pickle", ".gz", ".zip", ".tar", ".xz", ".bz2", ".7z", ".jsonl", ".sqlite", ".db",
}
_DATA_MIN_BYTES = 100_000
_LICENCE_NAMES = ("license", "licence", "notice", "third_party", "third-party", "copying")


def _names(task_root: Path) -> set[str]:
    return {p.name for p in _own_files(task_root)}


def _build_text(task_root: Path) -> list[tuple[str, str]]:
    """(relative path, text) for the Dockerfile RUN lines and the shell scripts the task runs."""
    out: list[tuple[str, str]] = []
    dockerfile = task_root / "environment" / "Dockerfile"
    if dockerfile.is_file():
        out.append(("environment/Dockerfile", "\n".join(_run_lines(dockerfile.read_text(errors="replace")))))
    for sub in ("environment", "solution"):
        for path, text in _text_files(task_root / sub):
            if path.suffix == ".sh":
                out.append((path.relative_to(task_root).as_posix(), text))
    return out


def check_toolchain_pinning(task_root: Path, report) -> None:
    names = _names(task_root)
    problems: list[str] = []
    for rel, text in _build_text(task_root):
        commands = [c.strip() for c in _SPLIT.split(text.replace("\\\n", " "))]
        for cmd in commands:
            low = cmd.lower()
            if re.match(r"(?:sudo\s+)?opam\s+(?:install|init)\b", low) and not any(
                n.endswith((".opam.locked", ".opam.export")) or n in ("opam.export",) for n in names
            ) and "opam switch import" not in text:
                problems.append(f"{rel}: opam without a lock file or `opam switch import`")
            if re.search(r"\b(?:elan|lake)\b", low) and ("lean-toolchain" not in names or (
                "lake-manifest.json" not in names and re.search(r"\blake\s+(?:update|build|exe)\b", low)
            )):
                problems.append(f"{rel}: elan/lake without lean-toolchain and lake-manifest.json")
            if re.match(r"(?:sudo\s+)?sbt\b", low) and not any(
                n.endswith(".lock") or n in ("dependencies.lock", "lock.sbt") for n in names
            ):
                problems.append(f"{rel}: sbt without a dependency lock")
            if re.match(r"(?:sudo\s+)?cpanm?\b", low) and "cpanfile.snapshot" not in names:
                problems.append(f"{rel}: Perl cpan/cpanm without cpanfile.snapshot")
            m = re.match(r"(?:sudo\s+)?(?:conda|mamba|micromamba)\s+(?:install|create)\b(.*)", low)
            if m:
                pkgs = [t for t in m.group(1).split() if not t.startswith("-") and t not in ("python",)]
                loose = [t for t in pkgs if "=" not in t and not t.startswith((".", "/")) and "." not in t.split("/")[-1]]
                if loose:
                    problems.append(f"{rel}: conda packages without a version ({_listed(loose)})")
        # tool bootstraps downloaded and run without a version
        for m in re.finditer(r"(sh\.rustup\.rs|astral\.sh/uv/install\.sh|get\.pnpm\.io/install\.sh|nvm-sh/nvm/[^/\s]+/install\.sh)", text):
            tool = m.group(1)
            pinned = (
                "--default-toolchain" in text if "rustup" in tool
                else bool(re.search(r"/uv/\d", text)) if "uv" in tool
                else bool(re.search(r"nvm-sh/nvm/v\d", text)) if "nvm" in tool
                else bool(re.search(r"pnpm@\d|PNPM_VERSION", text))
            )
            if not pinned:
                problems.append(f"{rel}: {tool.split('/')[0]} bootstrap without a pinned version")
        if re.search(r"\bnvm\s+install\s+(?:node|--lts|lts/\*|stable)\b", text):
            problems.append(f"{rel}: nvm install of a moving target (node/--lts/stable)")
    if problems:
        report.add(
            "toolchain_pinning",
            "warn",
            "Toolchains or bootstraps that are not locked (" + "; ".join(sorted(set(problems))[:8]) + "). A new "
            "release can change what the build installs and break the Oracle later. Add the lock file the "
            "ecosystem has (opam export, lean-toolchain + lake-manifest.json, an sbt lock, cpanfile.snapshot, "
            "conda `pkg=version`) or pin the version.",
        )
    else:
        report.add("toolchain_pinning", "info", "No unlocked opam, elan/lake, sbt, Perl, conda or tool-bootstrap installs found.")


_HF_CALL = re.compile(r"\b(?:from_pretrained|hf_hub_download|snapshot_download|load_dataset)\s*\(")


def check_hf_revisions(task_root: Path, report) -> None:
    hits: list[str] = []
    for sub in ("environment", "solution", "tests"):
        for path, text in _text_files(task_root / sub):
            if path.suffix not in (".py", ".sh", ".md", ".txt", ""):
                continue
            for m in _HF_CALL.finditer(text):
                window = text[m.end() : m.end() + 400]
                depth, end = 1, len(window)
                for i, ch in enumerate(window):
                    depth += ch == "("
                    depth -= ch == ")"
                    if depth == 0:
                        end = i
                        break
                call = window[:end]
                # a local path or an already-pinned download is fine
                if "revision" in call or re.match(r"\s*[\"']?(?:\.{0,2}/|~)", call):
                    continue
                hits.append(f"{path.relative_to(task_root).as_posix()}:{text.count(chr(10), 0, m.start()) + 1}")
    if hits:
        report.add(
            "huggingface_revision",
            "warn",
            f"A Hugging Face download has no `revision=` ({_listed(hits)}). The model or dataset can change on the "
            "hub, so the Oracle and the expected values can drift. Pin a commit hash.",
        )


def check_licences_and_size(task_root: Path, report) -> None:
    files = list(_own_files(task_root))
    if not files:
        return
    data = [
        p for p in files
        if p.suffix.lower() in _DATA_SUFFIXES and p.stat().st_size >= _DATA_MIN_BYTES
        and p.relative_to(task_root).parts[0] in ("environment", "tests")
    ]
    has_notice = any(any(k in p.name.lower() for k in _LICENCE_NAMES) for p in files)
    readme = task_root / "README.md"
    if not has_notice and readme.is_file() and re.search(r"licen[cs]e|third[- ]party|attribution", readme.read_text(errors="replace"), re.I):
        has_notice = True
    if data and not has_notice:
        report.add(
            "bundled_data_licence",
            "info",  # 9 of 40 stored tasks bundle generated data; only third-party data needs a licence
            f"The task bundles data or model files ({_listed([p.relative_to(task_root).as_posix() for p in data])}) and has no "
            "LICENSE, NOTICE or THIRD_PARTY_NOTICES file; README.md does not mention a licence. "
            "Third-party data must allow redistribution and be credited; generated data needs nothing.",
        )
    by_folder: dict[str, list[int]] = {}
    for p in files:
        top = p.relative_to(task_root).parts[0] if len(p.relative_to(task_root).parts) > 1 else "(root)"
        lines = 0
        if p.suffix.lower() not in _DATA_SUFFIXES and p.stat().st_size <= 1_000_000:
            try:
                lines = len(p.read_text().splitlines())
            except (OSError, UnicodeDecodeError):
                pass
        entry = by_folder.setdefault(top, [0, 0])
        entry[0] += 1
        entry[1] += lines
    summary = ", ".join(f"{k}: {v[0]} files / {v[1]} lines" for k, v in sorted(by_folder.items()))
    words = 0
    instruction = task_root / "instruction.md"
    if instruction.is_file():
        words = len(instruction.read_text(errors="replace").split())
    test_lines = by_folder.get("tests", [0, 0])[1]
    note = f" Tests are {test_lines} lines against an instruction of {words} words." if words else ""
    total = len(files)
    report.add(
        "task_size",
        "info",  # 9 of 40 stored tasks exceed 80 files, so a warning would be noise
        f"{total} own files ({summary}).{note}"
        + (" That is a lot for a reviewer to read; consider vendoring less or splitting the task." if total > _BIG_TASK_FILES else ""),
    )
