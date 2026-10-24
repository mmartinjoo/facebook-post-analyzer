import json
import logging
import time
from datetime import date, datetime, timedelta

from app.analysis.prompt import load_prompt
from app.apify_runs import start_comments_run, start_posts_run
from app.ingest.comments import posts_in_month
from app.jobs.runner import continue_job
from app.pages import load_active_pages
from app.timeutil import BUDAPEST, previous_month


def stale_months(conn) -> list[date]:
    rows = conn.execute("SELECT DISTINCT month FROM monthly_summaries WHERE stale ORDER BY month").fetchall()
    return [r["month"] for r in rows]


def run_daily(conn, settings, client) -> dict:
    started = time.monotonic()
    today = datetime.now(BUDAPEST).date()
    run_id = start_posts_run(
        conn,
        settings,
        client,
        load_active_pages(conn),
        newer_than=today - timedelta(days=14),
        kind="daily",
        results_limit=200,
    )
    continue_job(settings, "/api/jobs/analyze")
    months = sorted({today.replace(day=1), previous_month(today)} | set(stale_months(conn)))
    labels = [m.strftime("%Y-%m") for m in months]
    for label in labels:
        continue_job(settings, "/api/jobs/summarize", {"month": label, "stats_only": "1"})
    logging.info(
        json.dumps(
            {"job": "cron_daily", "run_id": run_id, "months": labels, "elapsed_s": round(time.monotonic() - started, 2)}
        )
    )
    return {"run_id": run_id, "summarize_months": labels}


def backfill_posts(conn, settings, client, since: date) -> int:
    return start_posts_run(
        conn, settings, client, load_active_pages(conn), newer_than=since, kind="backfill", results_limit=2000
    )


def reanalyze(conn, prompt_version: str) -> int:
    load_prompt(prompt_version)  # FileNotFoundError propagates before any DB change
    cur = conn.execute(
        "UPDATE posts SET analysis_status = 'pending', analysis_attempts = 0, analysis_error = NULL "
        "WHERE analysis_status IN ('done', 'failed') "
        "AND NOT EXISTS (SELECT 1 FROM post_analyses a WHERE a.post_id = posts.id AND a.prompt_version = %s)",
        (prompt_version,),
    )
    n = cur.rowcount
    conn.execute("UPDATE monthly_summaries SET stale = true")
    conn.commit()
    return n


def run_monthly(conn, settings, client) -> dict:
    started = time.monotonic()
    m = previous_month()
    label = m.strftime("%Y-%m")
    posts = posts_in_month(conn, m)
    if not posts:
        run_id = None
        continue_job(settings, "/api/jobs/summarize", {"month": label})
    else:
        run_id = start_comments_run(
            conn, settings, client, [p["url"] for p in posts],
            kind="monthly_comments", params={"month": label}, results_limit=100,
        )
    logging.info(
        json.dumps(
            {"job": "cron_monthly", "run_id": run_id, "month": label, "posts": len(posts),
             "elapsed_s": round(time.monotonic() - started, 2)}
        )
    )
    return {"month": label, "run_id": run_id, "posts": len(posts)}
