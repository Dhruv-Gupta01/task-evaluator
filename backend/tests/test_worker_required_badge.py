"""The Worker-required result shown as a line in the evidence summary and a field in the Review Report."""
import json

from app.services import submission_service
from app.services.static_checks import StaticCheckReport


def logs(message, severity="warn"):
    return json.dumps({"fail_count": 0, "warn_count": 1, "results": [
        {"name": "verifier_worker_required", "severity": severity, "message": message}]})


def test_the_badge_reads_the_result_from_the_static_checks_logs():
    badge = submission_service.worker_required_badge(logs("Worker required: YES, missing. Agent code runs at tests/a.py:3 without: x."))
    assert badge["result"] == "missing" and badge["label"] == "Worker required: YES – missing"
    assert "tests/a.py:3" in badge["detail"]


def test_each_result_has_its_own_label():
    cases = {"Worker required: no: nothing the agent wrote runs during grading.": "not_required",
             "Worker required: yes, present (tests/a.py:3).": "present",
             "Worker required: yes, equivalent isolation by hand (x).": "equivalent"}
    for message, result in cases.items():
        assert submission_service.worker_required_badge(logs(message, "info"))["result"] == result


def test_no_static_checks_or_no_finding_gives_no_badge():
    assert submission_service.worker_required_badge(None) is None
    assert submission_service.worker_required_badge(json.dumps({"results": []})) is None
    assert submission_service.worker_required_badge("not json") is None


def test_a_partial_worker_is_flagged_as_such():
    badge = submission_service.worker_required_badge(logs("Worker required: yes, present at a, but agent code also runs outside it (b)."))
    assert badge["result"] == "present" and badge["partial"] is True


def test_the_report_object_gives_the_same_badge():
    r = StaticCheckReport()
    r.add("verifier_worker_required", "warn", "Worker required: YES, missing. x")
    assert submission_service.worker_required_from_report(r)["result"] == "missing"


def test_the_evidence_summary_line_says_missing_or_not_checked():
    from types import SimpleNamespace
    line = submission_service._worker_line(SimpleNamespace(static_checks=SimpleNamespace(logs=logs("Worker required: YES, missing. x"))))
    assert "YES – missing" in line and "advisory" in line
    assert "not checked" in submission_service._worker_line(SimpleNamespace(static_checks=SimpleNamespace(logs=None)))
