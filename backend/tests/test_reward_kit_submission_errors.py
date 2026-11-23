"""Item 1b: a Reward Kit grader that lists the errors which score 0 must list the ones its parser raises.

GitHub Breakwater found three real tasks (glycan-ms2-elucidation, fin-saccr-rwa, gsea-proteomics) whose
`SUBMISSION_ERRORS` allowlist missed an error their parser can raise (BadZipFile / InvalidFileException /
ParseError for openpyxl, csv.Error for csv). A broken file then aborts the whole reward instead of scoring 0.
Which exceptions are not ValueErrors was checked against the interpreter (csv.Error, BadZipFile and
ElementTree.ParseError are not; ParseError is a SyntaxError)."""
import pytest

from app.services import reward_kit_checks
from tests.test_reward_kit_false_warnings import Report

def put(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


HEAD = "import rewardkit\nfrom rewardkit import criterion\n"


def task(tmp_path, body, *, reward_kit=True):
    """A task folder whose tests/checks.py is `body`."""
    put(tmp_path / "tests" / "checks.py", (HEAD if reward_kit else "") + body)
    return tmp_path


def run(tmp_path):
    report = Report()
    reward_kit_checks.check_submission_errors(tmp_path, report)
    return report


def messages(report):
    return [m for n, _, m in report.items if n == "reward_kit_submission_errors"]


OPENPYXL_INCOMPLETE = """
import openpyxl
SUBMISSION_ERRORS = (FileNotFoundError, ValueError)

@criterion
def sheet_has_header(workspace):
    try:
        wb = openpyxl.load_workbook(workspace / "out.xlsx")
    except SUBMISSION_ERRORS:
        return False
    return wb.active["A1"].value == "id"
"""


def test_openpyxl_with_an_incomplete_allowlist_is_warned(tmp_path):
    r = run(task(tmp_path, OPENPYXL_INCOMPLETE))
    assert r.sev("reward_kit_submission_errors") == ["warn"]
    text = messages(r)[0]
    for missing in ("BadZipFile", "InvalidFileException", "ParseError"):
        assert missing in text, missing
    assert "checks.py" in text and "SUBMISSION_ERRORS" in text
    assert "WorkerSetupError" in text  # the suggested pattern


def test_openpyxl_with_all_three_errors_listed_is_quiet(tmp_path):
    body = OPENPYXL_INCOMPLETE.replace(
        "(FileNotFoundError, ValueError)",
        "(FileNotFoundError, ValueError, zipfile.BadZipFile, InvalidFileException, ET.ParseError)",
    )
    assert run(task(tmp_path, body)).items == []


def test_a_listed_syntax_error_covers_parse_error(tmp_path):
    """ParseError is a SyntaxError, so catching SyntaxError handles it."""
    body = OPENPYXL_INCOMPLETE.replace(
        "(FileNotFoundError, ValueError)",
        "(FileNotFoundError, ValueError, BadZipFile, InvalidFileException, SyntaxError)",
    )
    assert run(task(tmp_path, body)).items == []


CSV_INCOMPLETE = """
import csv
ERRORS = (ValueError, KeyError)

@criterion
def rows_parse(workspace):
    try:
        rows = list(csv.DictReader(open(workspace / "out.csv", newline="")))
    except ERRORS:
        return False
    return len(rows) > 0
"""


def test_csv_without_csv_error_is_warned(tmp_path):
    r = run(task(tmp_path, CSV_INCOMPLETE))
    assert r.sev("reward_kit_submission_errors") == ["warn"]
    assert "csv.Error" in messages(r)[0]


def test_csv_with_csv_error_is_quiet(tmp_path):
    body = CSV_INCOMPLETE.replace("(ValueError, KeyError)", "(ValueError, KeyError, csv.Error)")
    assert run(task(tmp_path, body)).items == []


def test_an_except_clause_listing_the_errors_inline_counts(tmp_path):
    body = """
import csv

@criterion
def rows_parse(workspace):
    try:
        return len(list(csv.reader(open(workspace / "o.csv")))) > 1
    except (OSError, ValueError, csv.Error):
        return False
"""
    assert run(task(tmp_path, body)).items == []


@pytest.mark.parametrize("handler", ["except Exception:", "except BaseException:", "except:"])
def test_a_broad_handler_covers_everything(tmp_path, handler):
    body = f"""
import openpyxl

@criterion
def sheet(workspace):
    try:
        openpyxl.load_workbook(workspace / "out.xlsx")
    {handler}
        return False
    return True
"""
    assert run(task(tmp_path, body)).items == []


def test_a_parser_with_no_handling_at_all_is_not_guessed_at(tmp_path):
    """No error list anywhere: we cannot tell what the author meant, so the check says nothing."""
    body = """
import csv

@criterion
def rows(workspace):
    return len(list(csv.reader(open(workspace / "o.csv")))) > 1
"""
    assert run(task(tmp_path, body)).items == []


def test_json_only_is_quiet(tmp_path):
    body = """
import json

@criterion
def parses(workspace):
    try:
        json.load(open(workspace / "o.json"))
    except (OSError, ValueError):
        return False
    return True
"""
    assert run(task(tmp_path, body)).items == []


def test_a_grader_without_reward_kit_is_not_checked(tmp_path):
    assert run(task(tmp_path, OPENPYXL_INCOMPLETE, reward_kit=False)).items == []


def test_vendored_folders_are_skipped(tmp_path):
    put(tmp_path / "tests" / "vendor" / "lib.py", HEAD + OPENPYXL_INCOMPLETE)
    put(tmp_path / "tests" / "checks.py", HEAD + "@criterion\ndef ok(workspace):\n    return True\n")
    assert run(tmp_path).items == []


def test_the_check_runs_as_part_of_the_reward_kit_checklist(tmp_path):
    root = task(tmp_path, OPENPYXL_INCOMPLETE)
    put(root / "task.toml", '[verifier]\nenvironment_mode = "separate"\n')
    report = Report()
    reward_kit_checks.check_reward_kit(root, report)
    assert report.sev("reward_kit_submission_errors") == ["warn"]
