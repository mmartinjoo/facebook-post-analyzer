"""Apify webhook processing: ingest a finished run's dataset and chain the next job."""
import json
import logging
import time
from datetime import datetime

from app.apify_runs import get_run_by_apify_id, mark_run
from app.ingest.comments import build_posts_by_fb_id, build_posts_by_url, map_comment_item, upsert_comments
from app.ingest.posts import map_post_item, upsert_posts
from app.jobs.runner import continue_job
from app.pages import load_active_pages
from app.summary.job import mark_stale
from app.timeutil import months_between

logger = logging.getLogger(__name__)

SUCCEEDED = "ACTOR.RUN.SUCCEEDED"


def _process_posts_run(conn, settings, client, run: dict, payload: dict) -> dict:
    pages = load_active_pages(conn)
    records = []
    errors: list[str] = []
    total = 0
    for item in client.dataset(payload["datasetId"]).iterate_items():
        total += 1
        try:
            records.append(map_post_item(item, pages))
        except ValueError as e:
            errors.append(str(e))
    inserted, updated = upsert_posts(conn, records, run["id"])
    error = None
    if errors:
        error = f"{len(errors)} items skipped: " + "; ".join(errors[:5])
    mark_run(conn, run["id"], status="processed", items_count=total, error=error)
    return {"run_id": run["id"], "inserted": inserted, "updated": updated, "skipped_items": len(errors)}


def _process_comments_run(conn, settings, client, run: dict, payload: dict) -> dict:
    pages_by_id = {p.id: p for p in load_active_pages(conn)}
    post_rows = conn.execute("SELECT id, url, page_id, fb_post_id FROM posts").fetchall()
    posts_by_url = build_posts_by_url(post_rows)
    posts_by_fb_id = build_posts_by_fb_id(post_rows)
    records = []
    errors: list[str] = []
    total = 0
    for item in client.dataset(payload["datasetId"]).iterate_items():
        total += 1
        try:
            records.append(map_comment_item(item, posts_by_url, pages_by_id, posts_by_fb_id))
        except ValueError as e:
            errors.append(str(e))
    count = upsert_comments(conn, records)
    error = None
    if errors:
        error = f"{len(errors)} items skipped: " + "; ".join(errors[:5])

    params = run.get("params") or {}
    if params.get("month"):
        months = [params["month"]]
    elif params.get("from") and params.get("to"):
        parse = lambda s: datetime.strptime(s, "%Y-%m").date().replace(day=1)  # noqa: E731
        months = [m.strftime("%Y-%m") for m in months_between(parse(params["from"]), parse(params["to"]))]
    else:
        months = []
    # queue the months BEFORE marking the run processed: if this fails, the run is marked failed and can be
    # re-delivered (a processed run can no longer be downgraded)
    for month in months:
        mark_stale(conn, datetime.strptime(month, "%Y-%m").date())
    mark_run(conn, run["id"], status="processed", items_count=total, error=error)
    return {"run_id": run["id"], "comments": count, "skipped_items": len(errors), "months": months}


CLAIM = """
    UPDATE scrape_runs SET status = 'processing'
    WHERE id = %s AND status IN ('started', 'succeeded', 'failed')
    RETURNING id
"""


def process_webhook(conn, settings, client, payload: dict) -> dict:
    started = time.monotonic()
    run = get_run_by_apify_id(conn, payload["runId"])
    if run is None:
        raise LookupError(f"unknown Apify run {payload['runId']}")
    if run["status"] == "processed":
        return {"skipped": True}
    # atomic claim: a duplicate delivery arriving while this one is still ingesting finds 'processing'
    claimed = conn.execute(CLAIM, (run["id"],)).fetchone()
    conn.commit()
    if claimed is None:
        logger.info(json.dumps({"job": "webhook", "run_id": run["id"], "skipped": "in_progress"}))
        return {"skipped": True, "reason": "in_progress"}

    event = payload.get("eventType")
    next_job = None
    if event != SUCCEEDED:
        mark_run(conn, run["id"], status="failed", error=event)
        result = {"run_id": run["id"], "status": "failed"}
        processed = 0
    else:
        try:
            if run["actor"] == "posts":
                result = _process_posts_run(conn, settings, client, run, payload)
                next_job = "/api/jobs/analyze"
            else:
                result = _process_comments_run(conn, settings, client, run, payload)
                next_job = "/api/jobs/summarize" if result.pop("months") else None
        except Exception as e:
            conn.rollback()
            mark_run(conn, run["id"], status="failed", error=str(e)[:500])
            raise
        processed = result["inserted"] + result["updated"] if "inserted" in result else result["comments"]

    # outside the try: a continuation hiccup must not flip an already processed run to failed
    if next_job:
        continue_job(settings, next_job)
    logger.info(json.dumps({
        "job": "webhook", "run_id": run["id"], "processed": processed,
        "elapsed_s": round(time.monotonic() - started, 2),
    }))
    return result
