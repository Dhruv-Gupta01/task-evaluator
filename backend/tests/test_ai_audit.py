"""Human audit for AI detection: a named person approves, disputes or is unsure about the AI-use of
instruction.md and solution/solve.sh (and an overall call). GPTZero (Layer 3) and the rubric's
instruction_concision (Layer 2) are machine opinions; this is the human layer of Terminal-Bench's policy.
Kept against a fingerprint of those files, so a rewritten instruction needs a new look. Never a gate."""
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import Submission
from app.services import ai_audit


def make(tmp_path):
    (tmp_path / "solution").mkdir(parents=True)
    (tmp_path / "instruction.md").write_text("Write /app/out.json.\n")
    (tmp_path / "solution" / "solve.sh").write_text("echo hi\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test.sh").write_text("pytest\n")
    return tmp_path


def test_targets_and_rulings():
    assert ai_audit.TARGETS == ("instruction.md", "solution/solve.sh", "overall")
    assert ai_audit.RULINGS == ("approved", "disputed", "unsure")


@pytest.mark.parametrize("args,ok", [
    (("instruction.md", "approved", "Dhruv", "written by the author, checked against the PR"), True),
    (("overall", "disputed", "Dhruv", "reads machine-written"), True),
    (("solution/solve.sh", "unsure", "Dhruv", ""), True),
    (("elsewhere.md", "approved", "D", "x"), False),
    (("instruction.md", "maybe", "D", "x"), False),
    (("instruction.md", "approved", " ", "x"), False),
    (("instruction.md", "approved", "D", ""), False),
    (("instruction.md", "disputed", "D", "  "), False),
    (("instruction.md", "approved", "D", "x" * 2001), False),
])
def test_a_ruling_needs_a_target_a_ruling_a_name_and_a_reason_unless_unsure(args, ok):
    assert (ai_audit.decision_error(*args) is None) is ok


def test_the_fingerprint_covers_the_instruction_and_the_solution_only(tmp_path):
    t = make(tmp_path)
    a = ai_audit.fingerprint(t)
    (t / "tests" / "test.sh").write_text("changed\n")
    assert ai_audit.fingerprint(t) == a
    (t / "instruction.md").write_text("Rewritten.\n")
    assert ai_audit.fingerprint(t) != a
    b = ai_audit.fingerprint(t)
    (t / "solution" / "solve.sh").write_text("echo changed\n")
    assert ai_audit.fingerprint(t) != b


def test_a_ruling_for_older_files_is_reported_stale(tmp_path):
    t = make(tmp_path)
    entry = ai_audit.entry("approved", "Dhruv", "mine", ai_audit.fingerprint(t))
    stored = {"current": {"instruction.md": entry}, "history": [entry | {"target": "instruction.md"}]}
    view = ai_audit.view(stored, ai_audit.fingerprint(t))
    assert view["decisions"]["instruction.md"]["stale"] is False and view["stale"] == []
    (t / "instruction.md").write_text("Rewritten.\n")
    view = ai_audit.view(stored, ai_audit.fingerprint(t))
    assert view["decisions"]["instruction.md"]["stale"] is True and view["stale"] == ["instruction.md"]


def test_the_summary_counts_current_rulings_and_says_what_is_missing(tmp_path):
    t = make(tmp_path)
    fp = ai_audit.fingerprint(t)
    stored = {"current": {
        "instruction.md": ai_audit.entry("approved", "A", "ok", fp),
        "solution/solve.sh": ai_audit.entry("disputed", "A", "generated", fp),
    }, "history": []}
    s = ai_audit.view(stored, fp)["summary"]
    assert "1 approved" in s and "1 disputed" in s and "overall" in s  # overall is still open
    assert ai_audit.view(None, fp)["summary"] == "No human audit yet."


# ---- through the API ------------------------------------------------------------------------

@pytest.fixture
def api(tmp_path):
    task = make(tmp_path / "t")
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        db.add(Submission(id="s1", task_name="t", uploaded_at=datetime.now(UTC), zip_path="x", extracted_path=str(task)))
        db.commit()
    app.dependency_overrides[get_db] = lambda: Session()
    yield TestClient(app), task
    app.dependency_overrides.clear()


BODY = {"target": "instruction.md", "ruling": "approved", "adjudicator": "Dhruv", "reason": "author wrote it"}


def test_the_submission_starts_with_no_audit(api):
    client, _ = api
    assert client.get("/submissions/s1").json()["ai_audit"]["summary"] == "No human audit yet."


def test_approve_then_dispute_keeps_history_and_the_latest_wins(api):
    client, _ = api
    assert client.put("/submissions/s1/ai-detection-decision", json=BODY).status_code == 200
    out = client.put("/submissions/s1/ai-detection-decision", json={**BODY, "ruling": "disputed", "reason": "phrasing is generated"}).json()
    assert out["decisions"]["instruction.md"]["ruling"] == "disputed"
    assert [h["ruling"] for h in out["history"]] == ["approved", "disputed"]
    assert client.get("/submissions/s1").json()["ai_audit"]["decisions"]["instruction.md"]["adjudicator"] == "Dhruv"


def test_bad_requests_are_refused(api):
    client, _ = api
    for bad in ({**BODY, "ruling": "fine"}, {**BODY, "target": "x"}, {**BODY, "adjudicator": ""}, {**BODY, "reason": ""}):
        assert client.put("/submissions/s1/ai-detection-decision", json=bad).status_code == 400
    assert client.put("/submissions/nope/ai-detection-decision", json=BODY).status_code == 404


def test_editing_the_instruction_makes_the_ruling_stale(api):
    client, task = api
    client.put("/submissions/s1/ai-detection-decision", json=BODY)
    (task / "instruction.md").write_text("A new instruction.\n")
    out = client.get("/submissions/s1").json()["ai_audit"]
    assert out["stale"] == ["instruction.md"]


def test_an_unvalidated_submission_cannot_be_audited(api):
    client, _ = api
    from app.models import Submission as S
    sess = next(app.dependency_overrides[get_db]() for _ in [0])
    sess.get(S, "s1").extracted_path = None
    sess.commit()
    assert client.put("/submissions/s1/ai-detection-decision", json=BODY).status_code == 400


def test_the_evidence_summary_line_shows_the_audit():
    from types import SimpleNamespace
    from app.services import submission_service as ss
    assert "not audited" in ss._ai_audit_line(SimpleNamespace(ai_audit=SimpleNamespace(summary="No human audit yet.", stale=[])))
    line = ss._ai_audit_line(SimpleNamespace(ai_audit=SimpleNamespace(summary="1 approved, 1 disputed; still open: overall", stale=["instruction.md"])))
    assert "1 approved" in line and "changed since" in line


def test_the_evidence_bundle_carries_the_audit(api):
    import json
    client, _ = api
    client.put("/submissions/s1/ai-detection-decision", json=BODY)
    bundle = json.loads(client.get("/submissions/s1/evidence").content)
    audit = bundle["ai_detection"]["human_audit"]
    assert audit["decisions"]["instruction.md"]["ruling"] == "approved" and "approved" in audit["summary"]
