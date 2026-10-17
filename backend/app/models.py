from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Submission(Base):
    __tablename__ = "submissions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    task_name: Mapped[str] = mapped_column(String)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    zip_path: Mapped[str] = mapped_column(String)
    extracted_path: Mapped[str | None] = mapped_column(String, nullable=True)

    build_status: Mapped[str] = mapped_column(String, default="not-run")
    build_logs: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_tag: Mapped[str | None] = mapped_column(String, nullable=True)
    # Unused since agent trials moved to Harbor; kept because there are no
    # migrations to drop the column from existing databases.
    agent_wrapper_image_tag: Mapped[str | None] = mapped_column(String, nullable=True)

    # cached parsed task.toml, re-derived on validate; stored as JSON text
    task_config_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The canonical task_checksum (Harbor's dirhash of the task dir) for this
    # submission: set from the first Harbor-backed run that reports one, then
    # left alone. Every later run's own checksum is compared against this to
    # catch a frozen-run-set violation (e.g. oracle run against one task copy,
    # rollouts against another) instead of assuming it silently held.
    task_checksum: Mapped[str | None] = mapped_column(String, nullable=True)

    runs: Mapped[list["Run"]] = relationship(
        back_populates="submission", cascade="all, delete-orphan"
    )


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (UniqueConstraint("submission_id", "kind", "run_index"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    submission_id: Mapped[str] = mapped_column(ForeignKey("submissions.id"))
    kind: Mapped[str] = mapped_column(String)  # "oracle" | "nop" | "agent" | "cheat_trial" | "rubric_check" | "static_checks" | "tb_static_checks" | "ai_detection"
    run_index: Mapped[int] = mapped_column(Integer, default=0)

    status: Mapped[str] = mapped_column(String, default="pending")
    reward: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Harbor's exact reward (reward above is the 0/1 the gates use: anything
    # below 1.0 counts as 0), and what the verifier's own report said about
    # it (services/verifier_report.py, JSON). Both null for rows recorded
    # before these existed and for stages that are not Harbor trials.
    raw_reward: Mapped[float | None] = mapped_column(Float, nullable=True)
    verifier_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    logs: Mapped[str | None] = mapped_column(Text, nullable=True)
    container_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Harbor's own dirhash of the task dir this run actually executed
    # against (result.json's task_checksum), null for runs Harbor doesn't
    # produce a result.json for (e.g. rubric_check).
    task_checksum: Mapped[str | None] = mapped_column(String, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    submission: Mapped["Submission"] = relationship(back_populates="runs")


class LlmSpend(Base):
    """One row per finished paid LLM run (currently each agent trial). Kept
    separate from Run because re-running agent trials deletes the old Run
    rows, and their cost must still count toward the monthly budget."""

    __tablename__ = "llm_spend"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    submission_id: Mapped[str] = mapped_column(String)
    stage: Mapped[str] = mapped_column(String)
    model: Mapped[str] = mapped_column(String)
    cost_usd: Mapped[float] = mapped_column(Float)
    n_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    n_cache_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    n_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Added for the evidence cost report. All nullable: rows recorded before
    # these existed show them as "not recorded" instead of being wiped.
    agent: Mapped[str | None] = mapped_column(String, nullable=True)  # harness, e.g. codex
    reasoning_effort: Mapped[str | None] = mapped_column(String, nullable=True)
    trial_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detail: Mapped[str | None] = mapped_column(String, nullable=True)  # what this call was for
    n_cache_write_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Per-unit billing (GPTZero is per word, not per token).
    unit: Mapped[str | None] = mapped_column(String, nullable=True)
    unit_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
