import base64
import json
from datetime import date
from unittest.mock import MagicMock

import pytest
from apify_client._utils.encoding import encode_webhooks_to_base64

from app.apify_runs import PAYLOAD_TEMPLATE, POSTS_ACTOR, _webhooks, get_run_by_apify_id, mark_run, start_comments_run, start_posts_run
from app.pages import load_active_pages, resolve_page


def make_client():
    client = MagicMock()
    client.actor.return_value.start.return_value = MagicMock(id="run1", default_dataset_id="ds1")
    return client


def test_start_posts_run_records_run_and_webhook(db, settings):
    client = make_client()
    pages = load_active_pages(db)
    run_id = start_posts_run(db, settings, client, pages, date(2022, 1, 1), "backfill", 500)

    client.actor.assert_called_once_with(POSTS_ACTOR)
    kwargs = client.actor.return_value.start.call_args.kwargs
    assert len(kwargs["run_input"]["startUrls"]) == 5
    assert kwargs["run_input"]["onlyPostsNewerThan"] == "2022-01-01"
    assert kwargs["run_input"]["resultsLimit"] == 500
    (hook,) = kwargs["webhooks"]
    assert "token=hook-s" in hook["request_url"]
    assert hook["event_types"] == [
        "ACTOR.RUN.SUCCEEDED", "ACTOR.RUN.FAILED", "ACTOR.RUN.ABORTED", "ACTOR.RUN.TIMED_OUT",
    ]
    row = db.execute("SELECT * FROM scrape_runs WHERE id = %s", (run_id,)).fetchone()
    assert (row["actor"], row["kind"], row["status"], row["apify_run_id"]) == ("posts", "backfill", "started", "run1")
    assert row["params"]["since"] == "2022-01-01"
    assert len(row["params"]["page_urls"]) == 5
    assert row["started_at"] is not None


def test_start_comments_run_and_mark_run(db, settings):
    client = make_client()
    run_id = start_comments_run(db, settings, client, ["https://fb.com/p/1"], "monthly_comments", {"month": "2026-09"})
    kwargs = client.actor.return_value.start.call_args.kwargs
    assert kwargs["run_input"] == {
        "startUrls": [{"url": "https://fb.com/p/1"}],
        "resultsLimit": 100,
        "includeNestedComments": True,
    }
    row = get_run_by_apify_id(db, "run1")
    assert row["id"] == run_id and row["actor"] == "comments" and row["params"] == {"month": "2026-09"}

    mark_run(db, run_id, status="succeeded", items_count=7)
    row = get_run_by_apify_id(db, "run1")
    assert row["status"] == "succeeded" and row["items_count"] == 7
    assert row["finished_at"] is not None and row["processed_at"] is None
    mark_run(db, run_id, status="processed")
    row = get_run_by_apify_id(db, "run1")
    assert row["processed_at"] is not None and row["items_count"] == 7
    assert get_run_by_apify_id(db, "nope") is None


def test_resolve_page_by_url_variants(db):
    pages = load_active_pages(db)
    for url in ("https://facebook.com/kontigokontenerpark/", "https://www.facebook.com/KontigoKontenerPark"):
        assert resolve_page(pages, url=url).slug == "kontigo"
    assert resolve_page(pages, url="https://facebook.com/unknown") is None
    assert resolve_page(pages, name=pages[0].name.upper()) is pages[0]


def test_start_comments_run_serializes_date_params(db, settings):
    client = make_client()
    params = {"from": date(2026, 7, 1), "to": date(2026, 9, 1), "month": "2026-07"}
    run_id = start_comments_run(db, settings, client, ["https://fb.com/p/1"], "backfill_comments", params)
    row = db.execute("SELECT params FROM scrape_runs WHERE id = %s", (run_id,)).fetchone()
    assert row["params"] == {"from": "2026-07-01", "to": "2026-09-01", "month": "2026-07"}


def test_failed_insert_aborts_apify_run(db, settings):
    client = make_client()
    pages = load_active_pages(db)
    with pytest.raises(Exception):
        start_posts_run(db, settings, client, pages, date(2022, 1, 1), "bogus", 10)
    client.run.assert_called_once_with("run1")
    client.run.return_value.abort.assert_called_once_with()
    assert db.execute("SELECT count(*) AS n FROM scrape_runs").fetchone()["n"] == 0
    assert db.execute("select 1 AS x").fetchone()["x"] == 1


def test_webhooks_encode_with_real_library_encoder(settings):
    encoded = encode_webhooks_to_base64(_webhooks(settings))
    (hook,) = json.loads(base64.b64decode(encoded))
    assert hook["eventTypes"] == [
        "ACTOR.RUN.SUCCEEDED", "ACTOR.RUN.FAILED", "ACTOR.RUN.ABORTED", "ACTOR.RUN.TIMED_OUT",
    ]
    assert hook["requestUrl"] == "http://testserver/api/webhooks/apify?token=hook-s"
    assert hook["payloadTemplate"] == PAYLOAD_TEMPLATE


def test_processed_run_cannot_be_downgraded(db, settings):
    run_id = start_comments_run(db, settings, make_client(), ["https://fb.com/p/1"], "monthly_comments", {"month": "2026-09"})
    mark_run(db, run_id, status="processed", items_count=3)
    mark_run(db, run_id, status="failed", error="late failure")
    row = get_run_by_apify_id(db, "run1")
    assert row["status"] == "processed" and row["error"] is None
    mark_run(db, run_id, status="processed", items_count=4)
    assert get_run_by_apify_id(db, "run1")["items_count"] == 4
