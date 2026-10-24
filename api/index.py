from datetime import date

from fastapi import Body, Depends, FastAPI, HTTPException

from app.apify_runs import get_apify_client
from app.analysis.extractor import get_anthropic_client
from app.analysis.job import run_analyze_job
from app.auth import require_admin, require_cron, require_webhook
from app.config import Settings, get_settings
from app.db import get_db
from app.jobs.backfill import backfill_posts, reanalyze, run_daily, run_monthly
from app.jobs.runner import TimeBudget, continue_job
from app.jobs.webhook import process_webhook

app = FastAPI()


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
