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
from app.summary.comments_llm import MAX_CLUSTER_COMMENTS, CommentInsights, cluster_comments
from app.summary.sentence import summary_sentence
from app.summary.stats import SummaryStats, compute_stats
from app.timeutil import month_bounds_utc

logger = logging.getLogger(__name__)

# a target can make up to 2 LLM calls (Sonnet clustering + Haiku sentence, 60 s client timeout each):
# take no new target when less than this is left of the budget
SUMMARIZE_RESERVE_SECONDS = 130

STAT_COLUMNS = list(SummaryStats.__dataclass_fields__)
COLUMNS = [
    "page_id", "month", *STAT_COLUMNS, "top_questions", "top_objections", "summary_sentence",
    "summary_model", "comments_model", "prompt_version", "comments_computed_at",
]
KEEP_IF_NULL = {"comments_model", "comments_computed_at"}
# comments_computed_at is bound as a boolean "clustered" flag -> DB now() or NULL (keeps the old value)
UPSERT = (
    f"INSERT INTO monthly_summaries ({', '.join(COLUMNS)}, stats_computed_at, computed_at, stale) "
    f"VALUES ({', '.join(['%s'] * (len(COLUMNS) - 1))}, CASE WHEN %s THEN now() END, now(), now(), %s) "
    "ON CONFLICT (page_id, month) DO UPDATE SET "
    + ", ".join(
        f"{c} = COALESCE(EXCLUDED.{c}, monthly_summaries.{c})" if c in KEEP_IF_NULL else f"{c} = EXCLUDED.{c}"
        for c in COLUMNS[2:]
    )
    + ", stats_computed_at = now(), computed_at = now(), stale = EXCLUDED.stale"
)
MARK_STALE = """
    INSERT INTO monthly_summaries (page_id, month, stale)
    SELECT t.page_id, %(month)s, true FROM (
        SELECT id AS page_id FROM pages WHERE active AND (%(page_id)s::int IS NULL OR id = %(page_id)s::int)
        UNION ALL SELECT NULL
    ) t
    ON CONFLICT (page_id, month) DO UPDATE SET stale = true
"""
STALE = "SELECT page_id, month FROM monthly_summaries WHERE stale ORDER BY month, page_id NULLS LAST"
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
    # the most recent MAX_CLUSTER_COMMENTS, returned in chronological order
    return conn.execute(
        f"""SELECT * FROM (
                SELECT c.id, c.text, c.author_is_page, c.posted_at FROM comments c JOIN posts p ON p.id = c.post_id
                WHERE p.posted_at >= %s AND p.posted_at < %s AND {where}
                ORDER BY c.posted_at DESC NULLS LAST, c.id DESC LIMIT %s
            ) recent ORDER BY posted_at NULLS FIRST, id""",
        [start, end, *extra, MAX_CLUSTER_COMMENTS],
    ).fetchall()


def _stored_insights(existing: dict | None) -> CommentInsights | None:
    if existing is None or existing["top_questions"] is None:
        return None
    return CommentInsights.model_validate(
        {"top_questions": existing["top_questions"], "top_objections": existing["top_objections"] or []}
    )


def _add_usage(tokens: dict, model: str, usage: dict) -> None:
    tokens[model]["input"] += usage["input_tokens"]
    tokens[model]["output"] += usage["output_tokens"]


def _has_pending(conn, page_id: int | None, month: date) -> bool:
    start, end = month_bounds_utc(month)
    sql = "SELECT count(*) AS n FROM posts WHERE analysis_status = 'pending' AND posted_at >= %s AND posted_at < %s"
    params: list = [start, end]
    if page_id is not None:
        sql += " AND page_id = %s"
        params.append(page_id)
    return conn.execute(sql, params).fetchone()["n"] > 0


def mark_stale(conn, month: date, page_id: int | None = None) -> int:
    """Queue (page, month) targets for the summarize drain: every active page (or just `page_id`) plus the
    all-pages row. Missing rows are created as placeholders (all stats NULL) with stale = true."""
    cur = conn.execute(MARK_STALE, {"month": month, "page_id": page_id})
    n = cur.rowcount
    conn.commit()
    return n


def _summarize_target(
    conn, settings, client, page: Page | None, month: date, stats_only: bool, tokens: dict
) -> bool:
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
            insights, usage = cluster_comments(
                client, comments=comments, page_name=page.name if page else None, total_count=stats.comment_count
            )
            _add_usage(tokens, "sonnet", usage)
            clustered = True
    sentence, usage = summary_sentence(
        client, page_name=page.name if page else None, month=month, stats=stats, insights=insights
    )
    _add_usage(tokens, "haiku", usage)
    stat_values = [Jsonb(v) if isinstance(v, (dict, list)) else v for v in asdict(stats).values()]
    questions = [i.model_dump() for i in insights.top_questions] if insights else []
    objections = [i.model_dump() for i in insights.top_objections] if insights else []
    # pending analyses would change the stats -> keep the row queued for a later drain
    still_stale = _has_pending(conn, page_id, month)
    conn.execute(
        UPSERT,
        [
            page_id, month, *stat_values, Jsonb(questions), Jsonb(objections), sentence, HAIKU,
            SONNET if clustered else None, settings.prompt_version, clustered, still_stale,
        ],
    )
    conn.commit()
    return clustered


def _out_of_time(budget: TimeBudget) -> bool:
    return budget.remaining() < SUMMARIZE_RESERVE_SECONDS


def _stale_keys(conn) -> list[tuple[int | None, date]]:
    return [(r["page_id"], r["month"]) for r in conn.execute(STALE).fetchall()]


def run_summarize_job(
    conn,
    settings,
    client,
    budget: TimeBudget,
    *,
    month: date | None = None,
    page_slug: str | None = None,
    stats_only: bool = False,
) -> dict:
    """`month` given: summarise that month's targets (manual use). `month` None: DRAIN mode, work through the
    stale rows of monthly_summaries (queued by `mark_stale`) until the queue is empty or the budget runs out."""
    started = time.monotonic()
    month_str = month.strftime("%Y-%m") if month else None
    pages = load_active_pages(conn)
    conn.commit()
    if month is None:
        if page_slug:
            raise ValueError("page requires month")
        stats_only = False  # the comments-changed gate is the Sonnet cost control in drain mode
        targets: list[Page | None] = []
    elif page_slug:
        targets = [p for p in pages if p.slug == page_slug]
        if not targets:
            raise LookupError(f"unknown page: {page_slug}")
    else:
        targets = [*pages, None]
    pages_by_id = {p.id: p for p in pages}
    owner = uuid4().hex
    if not acquire_lock(conn, JOB_SUMMARIZE, owner=owner):
        logger.info(json.dumps({"job": "summarize", "run_id": owner, "month": month_str, "locked": True}))
        return {"locked": True}
    released = False
    processed = clustered = failed = attempted = 0
    done: set[tuple[int | None, date]] = set()
    tokens = {"haiku": {"input": 0, "output": 0}, "sonnet": {"input": 0, "output": 0}}

    def run_target(target: Page | None, target_month: date) -> None:
        nonlocal processed, clustered, failed
        try:
            clustered += _summarize_target(conn, settings, client, target, target_month, stats_only, tokens)
            processed += 1
        except Exception as e:
            # one bad target must not block the rest; it stays stale / its comments_computed_at stays old -> retried
            conn.rollback()
            failed += 1
            logger.warning(
                json.dumps(
                    {
                        "job": "summarize",
                        "run_id": owner,
                        "target": target.slug if target else "all",
                        "month": target_month.strftime("%Y-%m"),
                        "error": str(e)[:300],
                    }
                )
            )

    try:
        if month is not None:
            for target in targets:
                if _out_of_time(budget):
                    break
                attempted += 1
                run_target(target, month)
        else:
            while not _out_of_time(budget):
                key = next((k for k in _stale_keys(conn) if k not in done), None)
                conn.commit()
                if key is None:
                    break
                done.add(key)
                page_id, target_month = key
                if page_id is not None and page_id not in pages_by_id:
                    # inactive page: nothing to summarise, take it off the queue
                    conn.execute(
                        "UPDATE monthly_summaries SET stale = false WHERE page_id = %s AND month = %s", key
                    )
                    conn.commit()
                    continue
                run_target(pages_by_id.get(page_id), target_month)
        conn.commit()
        # release before self-continuation, or the next invocation finds the lock held
        release_lock(conn, JOB_SUMMARIZE, owner)
        released = True
        if month is not None:
            remaining = len(targets) - attempted
            if remaining > 0:
                params = {"month": month_str}
                if page_slug:
                    params["page"] = page_slug
                if stats_only:
                    params["stats_only"] = "1"
                continue_job(settings, "/api/jobs/summarize", params)
        else:
            # counted after the release, so rows queued while this run held the lock are not lost; rows this run
            # already tried (failed, or kept stale by pending posts) wait for the next drain (daily cron)
            remaining = sum(1 for k in _stale_keys(conn) if k not in done)
            conn.commit()
            if remaining > 0:
                continue_job(settings, "/api/jobs/summarize")
        logger.info(
            json.dumps(
                {
                    "job": "summarize",
                    "run_id": owner,
                    "mode": "drain" if month is None else "month",
                    "month": month_str,
                    "processed": processed,
                    "remaining": remaining,
                    "clustered": clustered,
                    "failed": failed,
                    "tokens": tokens,
                    "elapsed_s": round(time.monotonic() - started, 1),
                }
            )
        )
        if month is None:
            return {"mode": "drain", "processed": processed, "failed": failed, "remaining": remaining, "tokens": tokens}
        return {
            "month": month_str,
            "processed": processed,
            "remaining": remaining,
            "clustered": clustered,
            "failed": failed,
            "tokens": tokens,
        }
    finally:
        if not released:
            conn.rollback()
            release_lock(conn, JOB_SUMMARIZE, owner)
