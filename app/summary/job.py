import json
import logging
import time
from dataclasses import asdict
from datetime import date
from uuid import uuid4

from psycopg.types.json import Jsonb

from app.analysis.extractor import HAIKU, SONNET
from app.jobs.runner import JOB_SUMMARIZE, TimeBudget, acquire_lock, continue_job, release_lock
from app.pages import Page, load_active_pages
from app.summary.comments_llm import CommentInsights, cluster_comments
from app.summary.sentence import summary_sentence
from app.summary.stats import SummaryStats, compute_stats
from app.timeutil import month_bounds_utc

logger = logging.getLogger(__name__)

STAT_COLUMNS = list(SummaryStats.__dataclass_fields__)
COLUMNS = [
    "page_id", "month", *STAT_COLUMNS, "top_questions", "top_objections", "summary_sentence",
    "summary_model", "comments_model", "prompt_version", "comments_computed_at",
]
KEEP_IF_NULL = {"comments_model", "comments_computed_at"}
# comments_computed_at is bound as a boolean "clustered" flag -> DB now() or NULL (keeps the old value)
UPSERT = (
    f"INSERT INTO monthly_summaries ({', '.join(COLUMNS)}, stats_computed_at, computed_at, stale) "
    f"VALUES ({', '.join(['%s'] * (len(COLUMNS) - 1))}, CASE WHEN %s THEN now() END, now(), now(), false) "
    "ON CONFLICT (page_id, month) DO UPDATE SET "
    + ", ".join(
        f"{c} = COALESCE(EXCLUDED.{c}, monthly_summaries.{c})" if c in KEEP_IF_NULL else f"{c} = EXCLUDED.{c}"
        for c in COLUMNS[2:]
    )
    + ", stats_computed_at = now(), computed_at = now(), stale = false"
)
EXISTING = """
    SELECT top_questions, top_objections, comments_computed_at FROM monthly_summaries
    WHERE page_id IS NOT DISTINCT FROM %s AND month = %s
"""


def _comment_filter(page_id: int | None) -> tuple[str, list]:
    if page_id is None:
        return "p.page_id IN (SELECT id FROM pages WHERE active)", []
    return "p.page_id = %s", [page_id]


def _latest_scraped_at(conn, page_id: int | None, month: date):
    start, end = month_bounds_utc(month)
    where, extra = _comment_filter(page_id)
    return conn.execute(
        f"""SELECT max(c.scraped_at) AS latest FROM comments c JOIN posts p ON p.id = c.post_id
            WHERE p.posted_at >= %s AND p.posted_at < %s AND {where}""",
        [start, end, *extra],
    ).fetchone()["latest"]


def _load_comments(conn, page_id: int | None, month: date) -> list[dict]:
    start, end = month_bounds_utc(month)
    where, extra = _comment_filter(page_id)
    return conn.execute(
        f"""SELECT c.id, c.text, c.author_is_page FROM comments c JOIN posts p ON p.id = c.post_id
            WHERE p.posted_at >= %s AND p.posted_at < %s AND {where}
            ORDER BY c.posted_at NULLS LAST, c.id""",
        [start, end, *extra],
    ).fetchall()


def _stored_insights(existing: dict | None) -> CommentInsights | None:
    if existing is None or existing["top_questions"] is None:
        return None
    return CommentInsights.model_validate(
        {"top_questions": existing["top_questions"], "top_objections": existing["top_objections"] or []}
    )


def _summarize_target(conn, settings, client, page: Page | None, month: date, stats_only: bool) -> bool:
    page_id = page.id if page else None
    stats = compute_stats(conn, page_id, month)
    existing = conn.execute(EXISTING, (page_id, month)).fetchone()
    insights = _stored_insights(existing)
    clustered = False
    if not stats_only and stats.comment_count >= 3:
        latest = _latest_scraped_at(conn, page_id, month)
        computed_at = existing["comments_computed_at"] if existing else None
        if computed_at is None or (latest is not None and latest > computed_at):
            comments = _load_comments(conn, page_id, month)
            insights, _usage = cluster_comments(client, comments=comments, page_name=page.name if page else None)
            clustered = True
    sentence, _usage = summary_sentence(
        client, page_name=page.name if page else None, month=month, stats=stats, insights=insights
    )
    stat_values = [Jsonb(v) if isinstance(v, (dict, list)) else v for v in asdict(stats).values()]
    questions = [i.model_dump() for i in insights.top_questions] if insights else []
    objections = [i.model_dump() for i in insights.top_objections] if insights else []
    conn.execute(
        UPSERT,
        [
            page_id, month, *stat_values, Jsonb(questions), Jsonb(objections), sentence, HAIKU,
            SONNET if clustered else None, settings.prompt_version, clustered,
        ],
    )
    conn.commit()
    return clustered


def run_summarize_job(
    conn, settings, client, budget: TimeBudget, *, month: date, page_slug: str | None = None, stats_only: bool = False
) -> dict:
    started = time.monotonic()
    month_str = month.strftime("%Y-%m")
    pages = load_active_pages(conn)
    conn.commit()
    if page_slug:
        targets: list[Page | None] = [p for p in pages if p.slug == page_slug]
        if not targets:
            raise LookupError(f"unknown page: {page_slug}")
    else:
        targets = [*pages, None]
    owner = uuid4().hex
    if not acquire_lock(conn, JOB_SUMMARIZE, owner=owner):
        logger.info(json.dumps({"job": "summarize", "run_id": owner, "month": month_str, "locked": True}))
        return {"locked": True}
    released = False
    processed = clustered = 0
    try:
        for target in targets:
            if budget.expired():
                break
            clustered += _summarize_target(conn, settings, client, target, month, stats_only)
            processed += 1
        remaining = len(targets) - processed
        conn.commit()
        # release before self-continuation, or the next invocation finds the lock held
        release_lock(conn, JOB_SUMMARIZE, owner)
        released = True
        if remaining > 0:
            params = {"month": month_str}
            if page_slug:
                params["page"] = page_slug
            if stats_only:
                params["stats_only"] = "1"
            continue_job(settings, "/api/jobs/summarize", params)
        logger.info(
            json.dumps(
                {
                    "job": "summarize",
                    "run_id": owner,
                    "month": month_str,
                    "processed": processed,
                    "remaining": remaining,
                    "clustered": clustered,
                    "elapsed_s": round(time.monotonic() - started, 1),
                }
            )
        )
        return {"month": month_str, "processed": processed, "remaining": remaining, "clustered": clustered}
    finally:
        if not released:
            conn.rollback()
            release_lock(conn, JOB_SUMMARIZE, owner)
