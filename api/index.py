from datetime import date, datetime

from fastapi import Body, Depends, FastAPI, HTTPException, Query

from app.apify_runs import get_apify_client
from app.analysis.extractor import get_anthropic_client
from app.analysis.job import run_analyze_job
from app.auth import require_admin, require_cron, require_webhook
from app.config import Settings, get_settings
from app.db import get_db
from app.jobs.backfill import backfill_comments, backfill_posts, reanalyze, run_daily, run_monthly, summarize_range
from app.jobs.runner import TimeBudget, continue_job
from app.jobs.webhook import process_webhook
from app.summary.job import run_summarize_job

app = FastAPI()


def _parse_month(value: str, name: str = "month") -> date:
    try:
        return datetime.strptime(value, "%Y-%m").date()
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{name} must be YYYY-MM")


def _parse_range(from_: str, to: str) -> tuple[date, date]:
    start, end = _parse_month(from_, "from"), _parse_month(to, "to")
    if start > end:
        raise HTTPException(status_code=400, detail="from must not be after to")
    return start, end


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.post("/api/webhooks/apify", dependencies=[Depends(require_webhook)])
def apify_webhook(
    payload: dict = Body(...),
    settings: Settings = Depends(get_settings),
    conn=Depends(get_db),
    client=Depends(get_apify_client),
) -> dict:
    try:
        return process_webhook(conn, settings, client, payload)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/api/jobs/analyze", dependencies=[Depends(require_admin)])
def analyze_job(
    settings: Settings = Depends(get_settings),
    conn=Depends(get_db),
    client=Depends(get_anthropic_client),
) -> dict:
    budget = TimeBudget(settings.job_time_budget_seconds)
    return run_analyze_job(conn, settings, client, budget, prompt_version=settings.prompt_version)


@app.get("/api/cron/daily", dependencies=[Depends(require_cron)])
def cron_daily(
    settings: Settings = Depends(get_settings),
    conn=Depends(get_db),
    client=Depends(get_apify_client),
) -> dict:
    return run_daily(conn, settings, client)


@app.get("/api/cron/monthly", dependencies=[Depends(require_cron)])
def cron_monthly(
    settings: Settings = Depends(get_settings),
    conn=Depends(get_db),
    client=Depends(get_apify_client),
) -> dict:
    return run_monthly(conn, settings, client)


@app.post("/api/jobs/backfill", dependencies=[Depends(require_admin)])
def backfill_job(
    since: date,
    settings: Settings = Depends(get_settings),
    conn=Depends(get_db),
    client=Depends(get_apify_client),
) -> dict:
    return {"run_id": backfill_posts(conn, settings, client, since)}


@app.post("/api/jobs/reanalyze", dependencies=[Depends(require_admin)])
def reanalyze_job(
    prompt_version: str,
    settings: Settings = Depends(get_settings),
    conn=Depends(get_db),
) -> dict:
    try:
        n = reanalyze(conn, prompt_version)
    except FileNotFoundError:
        raise HTTPException(status_code=400, detail="unknown prompt version")
    continue_job(settings, "/api/jobs/analyze")
    return {"reset": n}


@app.post("/api/jobs/summarize", dependencies=[Depends(require_admin)])
def summarize_job(
    month: str | None = None,
    page: str | None = None,
    stats_only: str | None = None,
    settings: Settings = Depends(get_settings),
    conn=Depends(get_db),
    client=Depends(get_anthropic_client),
) -> dict:
    if month is None and page:
        raise HTTPException(status_code=400, detail="page requires month")
    month_date = _parse_month(month) if month else None  # no month -> drain the stale-row queue
    budget = TimeBudget(settings.job_time_budget_seconds)
    try:
        return run_summarize_job(
            conn, settings, client, budget,
            month=month_date, page_slug=page, stats_only=stats_only in ("1", "true"),
        )
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/api/jobs/backfill-comments", dependencies=[Depends(require_admin)])
def backfill_comments_job(
    from_: str = Query(alias="from"),
    to: str = Query(),
    settings: Settings = Depends(get_settings),
    conn=Depends(get_db),
    client=Depends(get_apify_client),
) -> dict:
    start, end = _parse_range(from_, to)
    return {"run_ids": backfill_comments(conn, settings, client, from_month=start, to_month=end)}


@app.post("/api/jobs/summarize-range", dependencies=[Depends(require_admin)])
def summarize_range_job(
    from_: str = Query(alias="from"),
    to: str = Query(),
    settings: Settings = Depends(get_settings),
    conn=Depends(get_db),
) -> dict:
    start, end = _parse_range(from_, to)
    return {"months": summarize_range(conn, settings, from_month=start, to_month=end)}
