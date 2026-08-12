"""Mechanical, LLM-free checks over a task's instruction.md, test.sh,
Dockerfile, and zip layout. Every result here is a verified fact (a word
count, a regex match) — never a judgment call. review_report.py feeds these
results to the LLM as already-established ground truth rather than asking
the model to recompute them, since asking an LLM to count words or
paragraphs is a reliable way to get a confidently wrong number."""
import re
from dataclasses import dataclass, field
from pathlib import Path

Severity = str  # "fail" | "warn" | "info"


@dataclass
class CheckResult:
    name: str
    severity: Severity
    message: str


@dataclass
class StaticCheckReport:
    results: list[CheckResult] = field(default_factory=list)

    def add(self, name: str, severity: Severity, message: str) -> None:
        self.results.append(CheckResult(name, severity, message))

    @property
    def fail_count(self) -> int:
        return sum(1 for r in self.results if r.severity == "fail")

    @property
    def warn_count(self) -> int:
        return sum(1 for r in self.results if r.severity == "warn")

    def as_text(self) -> str:
        if not self.results:
            return "No static checks ran (missing files)."
        lines = []
        for r in self.results:
            lines.append(f"[{r.severity.upper()}] {r.name}: {r.message}")
        return "\n".join(lines)


_HEADING_RE = re.compile(r"^\s*#{1,6}\s", re.M)
_BOLD_RE = re.compile(r"\*\*[^*]+\*\*")
_BULLET_RE = re.compile(r"^\s*[-*•]\s", re.M)
# Deliberately a WARN, not a FAIL — the aegisrank review's own experience was
# that this flags legitimate scene-setting ("read the dossier first") as
# often as it flags real solution-leaking sequencing. review_report.py's LLM
# pass is expected to use judgment on these, not auto-reject.
_SEQUENCE_WORDS_RE = re.compile(
    r"\b(first|then|next|finally|step\s*\d|step\s+one|step\s+two)\b", re.I
)


def check_instruction(instruction_path: Path, report: StaticCheckReport) -> None:
    if not instruction_path.is_file():
        report.add("instruction_present", "fail", "instruction.md is missing.")
        return

    text = instruction_path.read_text(errors="replace")
    words = text.split()
    word_count = len(words)
    paragraphs = [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    para_count = len(paragraphs)

    if 30 <= word_count <= 250:
        report.add("instruction_word_count", "info", f"{word_count} words (within 30–250).")
    else:
        report.add(
            "instruction_word_count", "warn", f"{word_count} words (outside the typical 30–250 range)."
        )

    if 2 <= para_count <= 3:
        report.add("instruction_para_count", "info", f"{para_count} paragraphs (within 2–3).")
    else:
        report.add(
            "instruction_para_count",
            "warn",
            f"{para_count} paragraphs (outside the typical 2–3; consider merging or splitting).",
        )

    if _BULLET_RE.search(text):
        report.add("instruction_no_bullets", "warn", "Contains bullet-point lines.")
    else:
        report.add("instruction_no_bullets", "info", "No bullet points.")

    if _HEADING_RE.search(text) or _BOLD_RE.search(text):
        report.add("instruction_no_headings", "warn", "Contains markdown headings or bold text.")
    else:
        report.add("instruction_no_headings", "info", "No headings/bold formatting.")

    smell_matches = _SEQUENCE_WORDS_RE.findall(text)
    if smell_matches:
        report.add(
            "instruction_step_by_step_smell",
            "warn",
            f"Found {len(smell_matches)} sequencing word(s) (e.g. \"first\"/\"then\"/\"step N\") "
            "— may be legitimate scene-setting or may be leaking step-by-step solution guidance; "
            "needs a human/LLM read, not an automatic fail.",
        )
    else:
        report.add("instruction_step_by_step_smell", "info", "No sequencing-language matches.")


_PWD_GUARD_RE = re.compile(r'if\s*\[\s*"\$PWD"\s*=\s*"/"\s*\]')
_MKDIR_LOGS_RE = re.compile(r"mkdir\s+-p\s+/logs/verifier")
_CTRF_RE = re.compile(r"--ctrf\b")
_RC_CAPTURE_RE = re.compile(r'rc=\$\?')
_TRAILING_EXIT_RE = re.compile(r"\nexit\s+\d+\s*$")


def check_test_sh(test_sh_path: Path, report: StaticCheckReport) -> None:
    if not test_sh_path.is_file():
        report.add("test_sh_present", "fail", "tests/test.sh is missing.")
        return

    text = test_sh_path.read_text(errors="replace")

    checks = [
        ("test_sh_pwd_guard", _PWD_GUARD_RE, "PWD=/ guard (WORKDIR not set) present"),
        ("test_sh_mkdir_logs", _MKDIR_LOGS_RE, "mkdir -p /logs/verifier present"),
        ("test_sh_ctrf_flag", _CTRF_RE, "--ctrf flag present"),
        ("test_sh_rc_capture", _RC_CAPTURE_RE, "rc=$? capture present"),
    ]
    for name, pattern, ok_message in checks:
        if pattern.search(text):
            report.add(name, "info", ok_message + ".")
        else:
            report.add(name, "warn", ok_message.replace("present", "missing") + ".")

    if _TRAILING_EXIT_RE.search(text.rstrip() + "\n"):
        report.add(
            "test_sh_no_trailing_exit",
            "warn",
            "Script ends with a bare `exit N` after the reward.txt write — the canonical shape "
            "always exits 0 regardless of outcome, relying on reward.txt as the real signal.",
        )
    else:
        report.add("test_sh_no_trailing_exit", "info", "No unconditional trailing exit code.")


_DIGEST_PIN_RE = re.compile(r"^FROM\s+\S+@sha256:[0-9a-f]{64}", re.M | re.I)
_BLANKET_COPY_RE = re.compile(r"^\s*COPY\s+\.\s+\.", re.M)
_HEREDOC_RE = re.compile(r"<<['\"]?\w+['\"]?")
_RECURSIVE_CHOWN_RE = re.compile(r"\b(chmod|chown)\s+-R\b")
_TMUX_ASCIINEMA_RE = re.compile(r"\btmux\b.*\basciinema\b|\basciinema\b.*\btmux\b", re.S)


def check_dockerfile(dockerfile_path: Path, report: StaticCheckReport) -> None:
    if not dockerfile_path.is_file():
        report.add("dockerfile_present", "fail", "environment/Dockerfile is missing.")
        return

    text = dockerfile_path.read_text(errors="replace")

    if _DIGEST_PIN_RE.search(text):
        report.add("dockerfile_digest_pinned", "info", "Base image is digest-pinned.")
    else:
        report.add(
            "dockerfile_digest_pinned",
            "warn",
            "Base image is not digest-pinned (FROM ...@sha256:...) — build is not fully reproducible.",
        )

    if _TMUX_ASCIINEMA_RE.search(text):
        report.add("dockerfile_harness_deps", "info", "tmux and asciinema both installed.")
    else:
        report.add(
            "dockerfile_harness_deps",
            "warn",
            "tmux and/or asciinema not both found — required by the harness convention used elsewhere.",
        )

    if _BLANKET_COPY_RE.search(text):
        report.add("dockerfile_no_blanket_copy", "warn", "Contains a blanket `COPY . .` — copies more than intended.")
    else:
        report.add("dockerfile_no_blanket_copy", "info", "No blanket `COPY . .`.")

    if _HEREDOC_RE.search(text):
        report.add("dockerfile_no_heredocs", "warn", "Contains a heredoc — some builders/BuildKit versions handle these inconsistently.")
    else:
        report.add("dockerfile_no_heredocs", "info", "No heredocs.")

    if _RECURSIVE_CHOWN_RE.search(text):
        report.add("dockerfile_no_recursive_chmod", "warn", "Contains a recursive chmod/chown -R — can be slow and overly broad.")
    else:
        report.add("dockerfile_no_recursive_chmod", "info", "No recursive chmod/chown.")


_FORBIDDEN_TOP_LEVEL = {"__MACOSX", ".git", ".DS_Store"}


def check_zip_layout(task_root: Path, report: StaticCheckReport) -> None:
    if (task_root / "task.toml").is_file():
        report.add("zip_layout_task_toml_at_root", "info", "task.toml sits at the task root.")
    else:
        report.add("zip_layout_task_toml_at_root", "fail", "task.toml not found at the task root.")

    forbidden_present = [
        p.name for p in task_root.iterdir() if p.name in _FORBIDDEN_TOP_LEVEL
    ]
    if forbidden_present:
        report.add(
            "zip_layout_no_forbidden_entries",
            "warn",
            f"Forbidden entries present: {', '.join(forbidden_present)}.",
        )
    else:
        report.add("zip_layout_no_forbidden_entries", "info", "No forbidden top-level entries.")


def check_rubric(task_root: Path, report: StaticCheckReport) -> None:
    has_rubric = (task_root / "rubric.txt").is_file() or (task_root / "rubric.md").is_file()
    if has_rubric:
        report.add("rubric_present", "info", "A rubric file was submitted alongside the task.")
    else:
        report.add("rubric_present", "info", "No separate rubric file (not required).")


def run_static_checks(task_root: Path) -> StaticCheckReport:
    report = StaticCheckReport()
    check_instruction(task_root / "instruction.md", report)
    check_test_sh(task_root / "tests" / "test.sh", report)
    check_dockerfile(task_root / "environment" / "Dockerfile", report)
    check_zip_layout(task_root, report)
    check_rubric(task_root, report)
    return report
