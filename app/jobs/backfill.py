import json
import logging
import time
from datetime import date, datetime, timedelta

from app.analysis.prompt import load_prompt
from app.apify_runs import start_comments_run, start_posts_run
from app.ingest.comments import posts_in_month
from app.jobs.runner import continue_job
from app.pages import load_active_pages
from app.summary.job import mark_stale
from app.timeutil import BUDAPEST, months_between, previous_month

logger = logging.getLogger(__name__)


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
    # the last two months get late engagement / analyses; queue them, then ONE drain also picks up
    # every other stale month (reanalyze, failed or pending-blocked targets)
    months = [previous_month(today), today.replace(day=1)]
    for m in months:
        mark_stale(conn, m)
    labels = [m.strftime("%Y-%m") for m in months]
    continue_job(settings, "/api/jobs/analyze")
    continue_job(settings, "/api/jobs/summarize")
    logger.info(
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
        mark_stale(conn, m)
        continue_job(settings, "/api/jobs/summarize")
    else:
        run_id = start_comments_run(
            conn, settings, client, [p["url"] for p in posts],
            kind="monthly_comments", params={"month": label}, results_limit=100,
        )
    logger.info(
        json.dumps(
            {"job": "cron_monthly", "run_id": run_id, "month": label, "posts": len(posts),
             "elapsed_s": round(time.monotonic() - started, 2)}
        )
    )
    return {"month": label, "run_id": run_id, "posts": len(posts)}


def backfill_comments(conn, settings, client, *, from_month: date, to_month: date) -> list[int]:
    started = time.monotonic()
    run_ids: list[int] = []
    for m in months_between(from_month, to_month):
        posts = posts_in_month(conn, m)
        if not posts:
            continue
        run_ids.append(
            start_comments_run(
                conn, settings, client, [p["url"] for p in posts],
                kind="backfill_comments",
                params={"from": from_month.strftime("%Y-%m"), "to": to_month.strftime("%Y-%m"), "month": m.strftime("%Y-%m")},
                results_limit=100,
            )
        )
    logger.info(
        json.dumps(
            {"job": "backfill_comments", "run_ids": run_ids, "elapsed_s": round(time.monotonic() - started, 2)}
        )
    )
    return run_ids


def summarize_range(conn, settings, *, from_month: date, to_month: date) -> list[str]:
    months = months_between(from_month, to_month)
    for m in months:
        mark_stale(conn, m)
    continue_job(settings, "/api/jobs/summarize")
    return [m.strftime("%Y-%m") for m in months]
