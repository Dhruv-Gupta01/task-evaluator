"""SubmissionSchema.files_ready: the zip has been unpacked (Validate & Build has started at least once), so the
page can enable the buttons that need the files. The page used to guess from the build status, which enabled
them while a build was only queued."""

from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Submission
from app.services import submission_service


def test_files_ready_follows_the_unpacked_folder():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    db.add(Submission(id="queued", task_name="t", uploaded_at=now, zip_path="x", extracted_path=None, build_status="pending"))
    db.add(Submission(id="unpacked", task_name="t", uploaded_at=now, zip_path="x", extracted_path="/tmp/x", build_status="running"))
    db.commit()
    assert submission_service.to_schema(db.get(Submission, "queued")).files_ready is False
    assert submission_service.to_schema(db.get(Submission, "unpacked")).files_ready is True
