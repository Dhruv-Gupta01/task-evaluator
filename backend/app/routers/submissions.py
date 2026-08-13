import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import Run, Submission
from app.schemas import SubmissionListItem, SubmissionSchema, UploadResponse
from app.services import submission_service, task_runner
from app.workers import task_queue

router = APIRouter()
settings = get_settings()


@router.get("/submissions", response_model=list[SubmissionListItem])
def list_submissions(db: Session = Depends(get_db)) -> list[SubmissionListItem]:
    rows = db.execute(
        select(Submission).order_by(Submission.uploaded_at.desc())
    ).scalars().all()
    return [submission_service.to_list_item(s) for s in rows]


@router.get("/submissions/{submission_id}", response_model=SubmissionSchema)
def get_submission(submission_id: str, db: Session = Depends(get_db)) -> SubmissionSchema:
    submission = db.get(Submission, submission_id)
    if submission is None:
        raise HTTPException(status_code=404, detail="submission not found")
    return submission_service.to_schema(submission)


@router.post("/submissions", response_model=UploadResponse, status_code=201)
async def upload_submission(file: UploadFile, db: Session = Depends(get_db)) -> UploadResponse:
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="file must be a .zip")

    submission_id = str(uuid.uuid4())
    submission_dir = settings.storage_dir / "submissions" / submission_id
    submission_dir.mkdir(parents=True, exist_ok=True)
    zip_path = submission_dir / "raw.zip"

    contents = await file.read()
    zip_path.write_bytes(contents)

    task_name = file.filename.rsplit(".", 1)[0]
    submission = Submission(
        id=submission_id,
        task_name=task_name,
        uploaded_at=datetime.now(UTC),
        zip_path=str(zip_path),
        build_status="not-run",
    )
    db.add(submission)
    db.commit()

    return UploadResponse(id=submission_id)


@router.post("/submissions/{submission_id}/validate", status_code=202)
async def trigger_validate(submission_id: str, db: Session = Depends(get_db)) -> dict[str, str]:
    submission = db.get(Submission, submission_id)
    if submission is None:
        raise HTTPException(status_code=404, detail="submission not found")

    submission.build_status = "pending"
    db.commit()

    await task_queue.submit(f"{submission_id}:validate", task_runner.run_validate(submission_id))
    return {"status": "started"}


def _require_built_submission(submission_id: str, db: Session) -> Submission:
    submission = db.get(Submission, submission_id)
    if submission is None:
        raise HTTPException(status_code=404, detail="submission not found")
    if submission.image_tag is None:
        raise HTTPException(status_code=400, detail="build the image first (run Validate & Build)")
    return submission


@router.post("/submissions/{submission_id}/oracle", status_code=202)
async def trigger_oracle(submission_id: str, db: Session = Depends(get_db)) -> dict[str, str]:
    _require_built_submission(submission_id, db)
    await task_queue.submit(f"{submission_id}:oracle", task_runner.run_oracle(submission_id))
    return {"status": "started"}


@router.post("/submissions/{submission_id}/nop", status_code=202)
async def trigger_nop(submission_id: str, db: Session = Depends(get_db)) -> dict[str, str]:
    _require_built_submission(submission_id, db)
    await task_queue.submit(f"{submission_id}:nop", task_runner.run_nop(submission_id))
    return {"status": "started"}


@router.post("/submissions/{submission_id}/agent-trials", status_code=202)
async def trigger_agent_trials(
    submission_id: str, n: int = 5, db: Session = Depends(get_db)
) -> dict[str, str]:
    if n < 1 or n > settings.max_agent_trials:
        raise HTTPException(
            status_code=400, detail=f"n must be between 1 and {settings.max_agent_trials}"
        )
    _require_built_submission(submission_id, db)

    # re-run overwrites prior agent trial results
    db.query(Run).filter_by(submission_id=submission_id, kind="agent").delete()
    for run_index in range(n):
        db.add(Run(submission_id=submission_id, kind="agent", run_index=run_index, status="pending"))
    db.commit()

    await task_queue.submit(
        f"{submission_id}:agent", task_runner.run_agent_trials(submission_id, n)
    )
    return {"status": "started"}


@router.post("/submissions/{submission_id}/sufficiency", status_code=202)
async def trigger_sufficiency(submission_id: str, db: Session = Depends(get_db)) -> dict[str, str]:
    submission = db.get(Submission, submission_id)
    if submission is None:
        raise HTTPException(status_code=404, detail="submission not found")
    if submission.extracted_path is None:
        raise HTTPException(
            status_code=400, detail="validate the submission first (files must be extracted)"
        )

    run = (
        db.query(Run).filter_by(submission_id=submission_id, kind="sufficiency", run_index=0).one_or_none()
    )
    if run is None:
        run = Run(submission_id=submission_id, kind="sufficiency", run_index=0)
        db.add(run)
    run.status = "pending"
    db.commit()

    await task_queue.submit(
        f"{submission_id}:sufficiency", task_runner.run_sufficiency_check(submission_id)
    )
    return {"status": "started"}


@router.post("/submissions/{submission_id}/leakage-scan", status_code=202)
async def trigger_leakage_scan(submission_id: str, db: Session = Depends(get_db)) -> dict[str, str]:
    submission = db.get(Submission, submission_id)
    if submission is None:
        raise HTTPException(status_code=404, detail="submission not found")
    if submission.extracted_path is None:
        raise HTTPException(
            status_code=400, detail="validate the submission first (files must be extracted)"
        )

    run = (
        db.query(Run).filter_by(submission_id=submission_id, kind="leakage_scan", run_index=0).one_or_none()
    )
    if run is None:
        run = Run(submission_id=submission_id, kind="leakage_scan", run_index=0)
        db.add(run)
    run.status = "pending"
    db.commit()

    await task_queue.submit(
        f"{submission_id}:leakage_scan", task_runner.run_leakage_scan(submission_id)
    )
    return {"status": "started"}


@router.post("/submissions/{submission_id}/code-smell", status_code=202)
async def trigger_code_smell(submission_id: str, db: Session = Depends(get_db)) -> dict[str, str]:
    submission = db.get(Submission, submission_id)
    if submission is None:
        raise HTTPException(status_code=404, detail="submission not found")
    if submission.extracted_path is None:
        raise HTTPException(
            status_code=400, detail="validate the submission first (files must be extracted)"
        )

    run = (
        db.query(Run).filter_by(submission_id=submission_id, kind="code_smell", run_index=0).one_or_none()
    )
    if run is None:
        run = Run(submission_id=submission_id, kind="code_smell", run_index=0)
        db.add(run)
    run.status = "pending"
    db.commit()

    await task_queue.submit(
        f"{submission_id}:code_smell", task_runner.run_code_smell_check(submission_id)
    )
    return {"status": "started"}


_REVIEW_REPORT_TERMINAL = {"passed", "failed"}


@router.post("/submissions/{submission_id}/review-report", status_code=202)
async def trigger_review_report(submission_id: str, db: Session = Depends(get_db)) -> dict[str, str]:
    submission = db.get(Submission, submission_id)
    if submission is None:
        raise HTTPException(status_code=404, detail="submission not found")
    if submission.extracted_path is None:
        raise HTTPException(
            status_code=400, detail="validate the submission first (files must be extracted)"
        )

    schema = submission_service.to_schema(submission)
    missing = []
    if schema.build.status not in _REVIEW_REPORT_TERMINAL:
        missing.append("build")
    if schema.oracle.status not in _REVIEW_REPORT_TERMINAL:
        missing.append("oracle")
    if schema.nop.status not in _REVIEW_REPORT_TERMINAL:
        missing.append("nop")
    if schema.sufficiency.status not in _REVIEW_REPORT_TERMINAL:
        missing.append("sufficiency")
    if schema.agent_trials.status not in _REVIEW_REPORT_TERMINAL:
        missing.append("agent_trials")
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"run these gates to completion first: {', '.join(missing)}",
        )

    run = (
        db.query(Run).filter_by(submission_id=submission_id, kind="review_report", run_index=0).one_or_none()
    )
    if run is None:
        run = Run(submission_id=submission_id, kind="review_report", run_index=0)
        db.add(run)
    run.status = "pending"
    db.commit()

    await task_queue.submit(
        f"{submission_id}:review_report", task_runner.run_review_report(submission_id)
    )
    return {"status": "started"}
