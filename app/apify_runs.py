"""Start Apify actor runs and record them in scrape_runs.

apify-client 3.x returns pydantic models (`Run`); `dataset.iterate_items()` yields plain dicts.
"""
import json
import logging
from datetime import date

from psycopg.types.json import Jsonb

from app.pages import Page

logger = logging.getLogger(__name__)

POSTS_ACTOR = "apify/facebook-posts-scraper"
COMMENTS_ACTOR = "apify/facebook-comments-scraper"

WEBHOOK_EVENTS = [
    "ACTOR.RUN.SUCCEEDED",
    "ACTOR.RUN.FAILED",
    "ACTOR.RUN.ABORTED",
    "ACTOR.RUN.TIMED_OUT",
]
PAYLOAD_TEMPLATE = (
    '{"runId": {{resource.id}}, "status": {{resource.status}}, '
    '"datasetId": {{resource.defaultDatasetId}}, "eventType": {{eventType}}}'
)


def _webhooks(settings) -> list[dict]:
    return [
        {
            "event_types": WEBHOOK_EVENTS,
            "request_url": f"{settings.app_base_url}/api/webhooks/apify?token={settings.apify_webhook_secret}",
            "payload_template": PAYLOAD_TEMPLATE,
        }
    ]


def _start_and_record(
    conn, settings, client, actor_id: str, run_input: dict, actor: str, kind: str, params: dict
) -> int:
    params_json = json.dumps(params, default=str)  # fail before starting the run
    run = client.actor(actor_id).start(run_input=run_input, webhooks=_webhooks(settings))
    apify_run_id = run.id
    try:
        row = conn.execute(
            """
            INSERT INTO scrape_runs (apify_run_id, actor, kind, params, status, started_at)
            VALUES (%s, %s, %s, %s, 'started', now())
            RETURNING id
            """,
            (apify_run_id, actor, kind, Jsonb(json.loads(params_json))),
        ).fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        logger.exception("scrape_runs insert failed for apify run %s; aborting it", apify_run_id)
        try:
            client.run(apify_run_id).abort()
        except Exception:
            logger.exception("could not abort apify run %s", apify_run_id)
        raise
    return row["id"]


def start_posts_run(
    conn, settings, client, pages: list[Page], newer_than: date, kind: str, results_limit: int
) -> int:
    page_urls = [p.facebook_url for p in pages]
    run_input = {
        "startUrls": [{"url": u} for u in page_urls],
        "onlyPostsNewerThan": newer_than.isoformat(),
        "resultsLimit": results_limit,
    }
    params = {"since": newer_than.isoformat(), "page_urls": page_urls}
    return _start_and_record(conn, settings, client, POSTS_ACTOR, run_input, "posts", kind, params)


def start_comments_run(
    conn, settings, client, post_urls: list[str], kind: str, params: dict, results_limit: int = 100
) -> int:
    run_input = {
        "startUrls": [{"url": u} for u in post_urls],
        "resultsLimit": results_limit,
        "includeNestedComments": True,
    }
    return _start_and_record(conn, settings, client, COMMENTS_ACTOR, run_input, "comments", kind, params)


def get_run_by_apify_id(conn, apify_run_id: str) -> dict | None:
    return conn.execute(
        "SELECT * FROM scrape_runs WHERE apify_run_id = %s", (apify_run_id,)
    ).fetchone()


def mark_run(conn, run_id: int, *, status: str, items_count: int | None = None, error: str | None = None) -> None:
    conn.execute(
        """
        UPDATE scrape_runs SET
          status = %s,
          items_count = COALESCE(%s, items_count),
          error = COALESCE(%s, error),
          finished_at = CASE WHEN %s IN ('succeeded', 'failed') THEN now() ELSE finished_at END,
          processed_at = CASE WHEN %s = 'processed' THEN now() ELSE processed_at END
        WHERE id = %s
        """,
        (status, items_count, error, status, status, run_id),
    )
    conn.commit()
