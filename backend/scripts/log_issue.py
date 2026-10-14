"""Append one analysed milestone item to the TB Milestone Issue Log Google Sheet.

    uv run --with gspread python backend/scripts/log_issue.py row.json
    uv run --with gspread python backend/scripts/log_issue.py row.json --tab scratch

row.json is an object keyed by the sheet's column headers (see HEADERS). Missing
columns are left blank. The script adds the header row if the tab is empty and
refuses to add an "Item #" that is already there, so a row is never duplicated.

Auth: a Google service account that the sheet has been shared with (Editor).
The key path comes from GOOGLE_SHEETS_KEY, else the first platinum-voice-*.json /
*service-account*.json in the repo root (all gitignored). The key is a credential:
never commit or paste it.
"""

import argparse
import json
import os
import sys
from pathlib import Path

import gspread

SHEET_ID = os.environ.get("ISSUE_LOG_SHEET_ID", "18eY6r680GbxKk26x3xBMYv7IayBOagNkpj4pE_OzXng")
REPO_ROOT = Path(__file__).resolve().parents[2]

HEADERS = [
    "Item #",
    "Source (tab / row)",
    "Type",
    "Title",
    "Status upstream",
    "Link",
    "What the PR / issue is about",
    "The problem found",
    "Already on our platform?",
    "Checks that already handle it",
    "Gaps (what is NOT handled)",
    "Mitigations - what we will do",
    "Priority",
    "Effort",
    "Our status",
]


def _key_path() -> Path:
    env = os.environ.get("GOOGLE_SHEETS_KEY")
    if env:
        return Path(env).expanduser()
    for pattern in ("platinum-voice-*.json", "*service-account*.json"):
        found = sorted(REPO_ROOT.glob(pattern))
        if found:
            return found[0]
    sys.exit("No service-account key found. Set GOOGLE_SHEETS_KEY to its path.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("row_file", help="JSON object keyed by column header")
    parser.add_argument("--tab", default=None, help="worksheet name (default: first tab)")
    args = parser.parse_args()

    row = json.loads(Path(args.row_file).read_text())
    unknown = sorted(set(row) - set(HEADERS))
    if unknown:
        sys.exit(f"Unknown columns: {unknown}. Valid: {HEADERS}")
    if not str(row.get("Item #", "")).strip():
        sys.exit("row needs an 'Item #'")

    client = gspread.service_account(filename=str(_key_path()))
    book = client.open_by_key(SHEET_ID)
    sheet = book.worksheet(args.tab) if args.tab else book.sheet1

    existing = sheet.get_all_values()
    if not existing or not any(cell.strip() for cell in existing[0]):
        existing = []  # an empty tab comes back as [[]] or []
        sheet.append_row(HEADERS, value_input_option="RAW")
        existing = [HEADERS]
    header = existing[0]
    if header != HEADERS:
        sys.exit(f"Sheet header differs from the script's HEADERS; refusing to write.\n{header}")

    item_col = header.index("Item #")
    if any(r[item_col].strip() == str(row["Item #"]).strip() for r in existing[1:] if len(r) > item_col):
        sys.exit(f"Item # {row['Item #']} is already in the sheet; not adding a duplicate.")

    sheet.append_row([str(row.get(h, "")) for h in HEADERS], value_input_option="RAW")
    print(f"Added item {row['Item #']} to '{sheet.title}' ({len(existing)} rows before).")


if __name__ == "__main__":
    main()
