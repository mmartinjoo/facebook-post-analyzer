from datetime import date

from psycopg.types.json import Jsonb

from app.pages import Page

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


def _record_run(conn, apify_run_id: str, actor: str, kind: str, params: dict) -> int:
    row = conn.execute(
        """
        INSERT INTO scrape_runs (apify_run_id, actor, kind, params, status, started_at)
        VALUES (%s, %s, %s, %s, 'started', now())
        RETURNING id
        """,
        (apify_run_id, actor, kind, Jsonb(params)),
    ).fetchone()
    conn.commit()
    return row["id"]


def start_posts_run(
    conn, settings, client, pages: list[Page], newer_than: date, kind: str, results_limit: int
) -> int:
    page_urls = [p.facebook_url for p in pages]
    run = client.actor(POSTS_ACTOR).start(
        run_input={
            "startUrls": [{"url": u} for u in page_urls],
            "onlyPostsNewerThan": newer_than.isoformat(),
            "resultsLimit": results_limit,
        },
        webhooks=_webhooks(settings),
    )
    params = {"since": newer_than.isoformat(), "page_urls": page_urls}
    return _record_run(conn, run["id"], "posts", kind, params)


def start_comments_run(
    conn, settings, client, post_urls: list[str], kind: str, params: dict, results_limit: int = 100
) -> int:
    run = client.actor(COMMENTS_ACTOR).start(
        run_input={
            "startUrls": [{"url": u} for u in post_urls],
            "resultsLimit": results_limit,
            "includeNestedComments": True,
        },
        webhooks=_webhooks(settings),
    )
    return _record_run(conn, run["id"], "comments", kind, params)


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
