"""Apify webhook processing: ingest a finished run's dataset and chain the next job."""
import json
import logging
import time
from datetime import datetime

from app.apify_runs import get_run_by_apify_id, mark_run
from app.ingest.comments import build_posts_by_url, map_comment_item, upsert_comments
from app.ingest.posts import map_post_item, upsert_posts
from app.jobs.runner import continue_job
from app.pages import load_active_pages
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
    continue_job(settings, "/api/jobs/analyze")
    return {"run_id": run["id"], "inserted": inserted, "updated": updated, "skipped_items": len(errors)}


def _process_comments_run(conn, settings, client, run: dict, payload: dict) -> dict:
    pages_by_id = {p.id: p for p in load_active_pages(conn)}
    posts_by_url = build_posts_by_url(
        conn.execute("SELECT id, url, page_id FROM posts WHERE url IS NOT NULL").fetchall()
    )
    records = []
    errors: list[str] = []
    total = 0
    for item in client.dataset(payload["datasetId"]).iterate_items():
        total += 1
        try:
            records.append(map_comment_item(item, posts_by_url, pages_by_id))
        except ValueError as e:
            errors.append(str(e))
    count = upsert_comments(conn, records)
    error = None
    if errors:
        error = f"{len(errors)} items skipped: " + "; ".join(errors[:5])
    mark_run(conn, run["id"], status="processed", items_count=total, error=error)

    params = run.get("params") or {}
    if params.get("month"):
        months = [params["month"]]
    elif params.get("from") and params.get("to"):
        parse = lambda s: datetime.strptime(s, "%Y-%m").date().replace(day=1)  # noqa: E731
        months = [m.strftime("%Y-%m") for m in months_between(parse(params["from"]), parse(params["to"]))]
    else:
        months = []
    for month in months:
        continue_job(settings, "/api/jobs/summarize", {"month": month})
    return {"run_id": run["id"], "comments": count, "skipped_items": len(errors)}


def process_webhook(conn, settings, client, payload: dict) -> dict:
    started = time.monotonic()
    run = get_run_by_apify_id(conn, payload["runId"])
    if run is None:
        raise LookupError(f"unknown Apify run {payload['runId']}")
    if run["status"] == "processed":
        return {"skipped": True}

    event = payload.get("eventType")
    if event != SUCCEEDED:
        mark_run(conn, run["id"], status="failed", error=event)
        result = {"run_id": run["id"], "status": "failed"}
        processed = 0
    else:
        try:
            if run["actor"] == "posts":
                result = _process_posts_run(conn, settings, client, run, payload)
            else:
                result = _process_comments_run(conn, settings, client, run, payload)
        except Exception as e:
            conn.rollback()
            mark_run(conn, run["id"], status="failed", error=str(e)[:500])
            raise
        processed = result["inserted"] + result["updated"] if "inserted" in result else result["comments"]

    logger.info(json.dumps({
        "job": "webhook", "run_id": run["id"], "processed": processed,
        "elapsed_s": round(time.monotonic() - started, 2),
    }))
    return result
