"""Monthly spending cap for paid LLM runs (agent trials). Costs come from
Harbor's per-trial cost_usd, which is only known once a trial finishes, so a
run can overshoot the cap by the cost of the trials already in flight."""

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import LlmSpend

settings = get_settings()


def _month_start() -> datetime:
    now = datetime.now(UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def spent_this_month(db: Session) -> float:
    total = db.execute(
        select(func.coalesce(func.sum(LlmSpend.cost_usd), 0.0)).where(
            LlmSpend.created_at >= _month_start()
        )
    ).scalar_one()
    return float(total)


def exceeded_message(db: Session) -> str | None:
    """None while under budget (or budget disabled), else a user-facing reason."""
    if settings.agent_budget_usd <= 0:
        return None
    spent = spent_this_month(db)
    if spent < settings.agent_budget_usd:
        return None
    return (
        f"Monthly agent budget reached: ${spent:.2f} of ${settings.agent_budget_usd:.2f} "
        "spent this month. Raise AGENT_BUDGET_USD in backend/.env to continue."
    )


def record(
    db: Session,
    submission_id: str,
    stage: str,
    model: str,
    cost_usd: float,
    n_input_tokens: int | None,
    n_cache_tokens: int | None,
    n_output_tokens: int | None,
) -> None:
    db.add(
        LlmSpend(
            submission_id=submission_id,
            stage=stage,
            model=model,
            cost_usd=cost_usd,
            n_input_tokens=n_input_tokens,
            n_cache_tokens=n_cache_tokens,
            n_output_tokens=n_output_tokens,
            created_at=datetime.now(UTC),
        )
    )
