"""Show how the vendored Terminal-Bench checks differ from upstream's current ones.

    uv run python backend/scripts/diff_vendored_checks.py

Clones upstream (shallow) into a temp dir and compares scripts/checks/ with
backend/vendor/tb_checks/scripts/ and docs/prompts/ with backend/vendor/tb_prompts/.
It only prints: update the vendored files and PROVENANCE.md by hand after reading
the differences, so a refresh never changes results unnoticed.
"""

import difflib
import subprocess
import sys
import tempfile
from pathlib import Path

UPSTREAM = "https://github.com/harbor-framework/terminal-bench"
VENDOR = Path(__file__).resolve().parents[1] / "vendor"
PAIRS = (
    ("scripts/checks", VENDOR / "tb_checks" / "scripts"),
    ("docs/prompts", VENDOR / "tb_prompts"),
)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="tb-upstream-") as tmp:
        clone = subprocess.run(["git", "clone", "--depth", "1", "-q", UPSTREAM, tmp], capture_output=True, text=True)
        if clone.returncode:
            print(f"clone failed: {clone.stderr.strip()}")
            return 2
        head = subprocess.run(["git", "-C", tmp, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        print(f"upstream HEAD: {head}\n(see PROVENANCE.md in the vendor folders for the pinned commit)\n")
        changed = 0
        for upstream_dir, local_dir in PAIRS:
            up, local = Path(tmp) / upstream_dir, local_dir
            up_files = {p.name for p in up.iterdir() if p.is_file()} if up.is_dir() else set()
            local_files = {p.name for p in local.iterdir() if p.is_file() and p.name != "PROVENANCE.md"}
            for name in sorted(up_files - local_files):
                print(f"NEW upstream: {upstream_dir}/{name}")
                changed += 1
            for name in sorted(local_files - up_files):
                print(f"REMOVED upstream: {upstream_dir}/{name}")
                changed += 1
            for name in sorted(up_files & local_files):
                a, b = (local / name).read_text(errors="replace"), (up / name).read_text(errors="replace")
                if a != b:
                    changed += 1
                    print(f"CHANGED: {upstream_dir}/{name}")
                    diff = difflib.unified_diff(a.splitlines(), b.splitlines(), "vendored", "upstream", lineterm="", n=1)
                    print("\n".join(list(diff)[:40]), "\n")
        print(f"{changed} file(s) differ." if changed else "Vendored files match upstream.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
