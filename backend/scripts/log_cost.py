"""Append one task's cost analysis, read from its evidence JSON, as a single row
of the "Cost Analysis" tab in the TB Milestone Issue Log Google Sheet.

    uv run --with gspread python backend/scripts/log_cost.py <id>-evidence.json
    uv run --with gspread python backend/scripts/log_cost.py <id>-evidence.json --dry-run

The evidence file is the one the UI's "Download Evidence" button saves. The tab is
created with a header row on first use; a submission id already in the tab is not
added twice. Auth is the same service account as log_issue.py.
"""

import argparse
import json
import re
import sys
from pathlib import Path

import gspread

sys.path.insert(0, str(Path(__file__).resolve().parent))
from log_issue import SHEET_ID, _key_path  # noqa: E402

TAB = "Cost Analysis"
STAGES = [
    "sufficiency", "ai_detection", "agent_trials", "rubric_check", "failure_analysis",
    "cheat_trial", "review_report", "variant_probe", "weak_probe", "edge_cases",
]
MAX_TRIALS = 6


def category(record: dict) -> str:
    stage = record["stage"]
    if stage in ("agent_trials", "cheat_trial"):
        return "A"  # agent attempts
    if stage == "ai_detection":
        return "D"  # third party
    if record.get("agent_or_harness") == "direct API call":
        return "C"  # direct judge calls
    return "B"  # Harbor evaluator runs (check / analyze)


HEADERS = (
    ["Task", "Submission id", "Evidence recorded from", "Total cost (USD)", "Cost records"]
    + [f"{label} (USD)" for label in (
        "A. Agent attempts", "B. Evaluator runs", "C. Direct judge calls", "D. Third party (GPTZero)")]
    + [f"{label} (%)" for label in ("A", "B", "C", "D")]
    + [f"Stage: {s} (USD)" for s in STAGES]
    + [f"Agent trial {i} (USD)" for i in range(1, MAX_TRIALS + 1)]
    + [
        "Agent trials: n", "Agent trials: mean (USD)", "Agent trials: input tokens",
        "Agent trials: cached input (%)", "Agent trials: output tokens", "Agent trials pass rate",
        "Cheat trial: cached input (%)", "GPTZero words", "GPTZero USD per word",
        "Model share: luna (%)", "Spend without token counts (USD)", "Spend without token counts (%)",
        "First record", "Last record", "Notes",
    ]
)


def build_row(evidence: dict, source: str) -> list:
    cost = evidence["cost"]
    records = cost["records"]
    total = cost["total_usd"] or 0.0

    def pct(x: float) -> float:
        return round(100 * x / total, 1) if total else 0.0

    by_cat = {k: sum(r["cost_usd"] or 0 for r in records if category(r) == k) for k in "ABCD"}
    by_stage = {s: sum(r["cost_usd"] or 0 for r in records if r["stage"] == s) for s in STAGES}
    trials = sorted((r for r in records if r["stage"] == "agent_trials"), key=lambda r: r.get("trial") or 0)
    trial_costs = [t["cost_usd"] for t in trials][:MAX_TRIALS]
    t_in = sum(t["input_tokens"] or 0 for t in trials)
    t_cached = sum(t["cached_tokens"] or 0 for t in trials)
    t_out = sum(t["output_tokens"] or 0 for t in trials)
    cheat = next((r for r in records if r["stage"] == "cheat_trial"), None)
    gz = next((r for r in records if r["stage"] == "ai_detection"), None)
    luna = sum(r["cost_usd"] or 0 for r in records if "luna" in (r["model"] or ""))
    no_tokens = sum(
        r["cost_usd"] or 0 for r in records if r["input_tokens"] is None and r["stage"] != "ai_detection"
    )
    stamps = sorted(r["recorded_at"] for r in records)
    m = re.search(r"Pass rate:\s*([\d.]+)%", evidence.get("summary_markdown", ""))
    notes = []
    if no_tokens:
        notes.append("Harbor evaluator runs report cost but no token counts")
    if gz:
        notes.append("GPTZero is billed per word in INR; USD uses the platform's conversion")
    if any(r["stage"] in ("sufficiency",) for r in records) and not any(
        r["stage"] == "review_report" for r in records
    ):
        notes.append("no Review Report cost recorded")

    row = [
        evidence.get("task_name"), evidence.get("submission_id"), source, round(total, 6), len(records),
        *[round(by_cat[k], 6) for k in "ABCD"], *[pct(by_cat[k]) for k in "ABCD"],
        *[round(by_stage[s], 6) for s in STAGES],
        *[round(c, 6) for c in trial_costs], *[""] * (MAX_TRIALS - len(trial_costs)),
        len(trials), round(sum(trial_costs) / len(trials), 6) if trials else "",
        t_in or "", round(100 * t_cached / t_in, 1) if t_in else "", t_out or "",
        float(m.group(1)) if m else "",
        round(100 * cheat["cached_tokens"] / cheat["input_tokens"], 1)
        if cheat and cheat["input_tokens"] else "",
        gz["unit_count"] if gz else "",
        round(gz["cost_usd"] / gz["unit_count"], 6) if gz and gz["unit_count"] else "",
        pct(luna), round(no_tokens, 6), pct(no_tokens),
        stamps[0] if stamps else "", stamps[-1] if stamps else "", "; ".join(notes),
    ]
    assert len(row) == len(HEADERS), (len(row), len(HEADERS))
    return row


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("evidence")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    evidence = json.loads(Path(args.evidence).read_text())
    row = build_row(evidence, Path(args.evidence).name)
    if args.dry_run:
        for h, v in zip(HEADERS, row):
            print(f"{h}: {v}")
        return

    book = gspread.service_account(filename=str(_key_path())).open_by_key(SHEET_ID)
    try:
        sheet = book.worksheet(TAB)
    except gspread.WorksheetNotFound:
        sheet = book.add_worksheet(TAB, rows=100, cols=len(HEADERS))
    values = sheet.get_all_values()
    if not values or not any(c.strip() for c in values[0]):
        sheet.update(range_name="A1", values=[HEADERS])
        sheet.freeze(rows=1, cols=2)
        values = [HEADERS]
    if any(len(r) > 1 and r[1] == evidence["submission_id"] for r in values[1:]):
        sys.exit(f"{evidence['submission_id']} is already in '{TAB}'; not adding a duplicate.")
    sheet.append_row(row, value_input_option="USER_ENTERED")
    print(f"Added {evidence.get('task_name')} to '{TAB}' ({len(values)} rows before).")


if __name__ == "__main__":
    main()
