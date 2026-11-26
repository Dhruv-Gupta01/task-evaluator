"""The one manual ruling left on the platform: a named person approves or disapproves the AI Detection result."""
import json
from datetime import UTC, datetime

from app.models import Run
from tests.test_grader_isolation_api import env  # noqa: F401  (fixture)


def flagged(Session, status="failed"):
    with Session() as db:
        db.add(Run(submission_id="s1", kind="ai_detection", run_index=0, status=status,
                   logs=json.dumps({"passed": status == "passed"}), finished_at=datetime.now(UTC)))
        db.commit()


def rule(client, ruling="approved", name="Asha", reason="written by hand"):
    return client.put("/submissions/s1/ai-detection-decision", json={"ruling": ruling, "adjudicator": name, "reason": reason})


def ai(client):
    return client.get("/submissions/s1").json()["ai_detection"]


def test_nothing_to_rule_on_before_the_check_has_run(env):
    client, _, _ = env
    assert rule(client).status_code == 400


def test_approving_a_flagged_result_makes_it_pass_and_keeps_the_machine_result(env):
    client, _, Session = env
    flagged(Session)
    assert rule(client).status_code == 200
    body = ai(client)
    assert body["status"] == "passed" and body["detected_status"] == "failed"
    assert body["decision"]["adjudicator"] == "Asha" and body["decision"]["ruling"] == "approved"


def test_disapproving_a_passed_result_makes_it_fail(env):
    client, _, Session = env
    flagged(Session, "passed")
    assert rule(client, "disapproved", reason="reads as generated").status_code == 200
    body = ai(client)
    assert body["status"] == "failed" and body["detected_status"] == "passed"


def test_a_name_a_reason_and_a_known_ruling_are_required(env):
    client, _, Session = env
    flagged(Session)
    assert rule(client, name="  ").status_code == 400
    assert rule(client, reason="").status_code == 400
    assert rule(client, ruling="unsure").status_code == 400


def test_the_ruling_stops_applying_when_the_files_it_was_about_change(env):
    client, task, Session = env
    flagged(Session)
    rule(client)
    (task / "instruction.md").write_text("a rewritten instruction\n")
    body = ai(client)
    assert body["status"] == "failed" and body["decision_stale"] is True


def test_it_is_in_the_evidence_summary_and_history_is_kept(env):
    client, _, Session = env
    flagged(Session)
    rule(client, "disapproved", reason="first look")
    rule(client, "approved", name="Ravi", reason="author showed drafts")
    assert [h["ruling"] for h in ai(client)["history"]] == ["disapproved", "approved"]
    bundle = client.get("/submissions/s1/evidence").json()
    assert "approved by Ravi" in bundle["summary_markdown"]


def test_no_other_ruling_endpoint_is_left(env):
    client, _, _ = env
    for path in ("trial-verdict", "reviewer-decision", "grader-isolation-decision", "tier0-decision", "tier1-decision", "tier2-decision"):
        assert client.put(f"/submissions/s1/{path}", json={}).status_code == 404, path
