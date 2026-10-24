import copy
import json
from pathlib import Path

import pytest

from app.ingest.posts import image_urls, map_post_item, upsert_posts
from app.pages import load_active_pages

FIXTURE = Path(__file__).parent / "fixtures" / "apify_posts_sample.json"


@pytest.fixture
def items():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def pages(db):
    return load_active_pages(db)


@pytest.fixture
def run_id(db):
    return db.execute(
        "INSERT INTO scrape_runs (actor, kind, status, apify_run_id) "
        "VALUES ('posts', 'daily', 'started', 't1') RETURNING id"
    ).fetchone()["id"]


def test_map_fixture_items(db, items, pages):
    kontigo = next(p for p in pages if "kontigokontenerpark" in p.facebook_url)
    assert items
    for item in items:
        record = map_post_item(item, pages)
        assert record.page_id == kontigo.id
        assert record.posted_at.tzinfo is not None
        assert record.fb_post_id


def test_map_missing_text_and_media_is_still_mapped(db, items, pages):
    item = {**items[0], "text": None, "media": []}
    record = map_post_item(item, pages)
    assert record.text is None
    assert record.media == []


def test_map_unknown_page_raises(db, items, pages):
    item = {**items[0], "facebookUrl": "https://www.facebook.com/nobody", "inputUrl": "https://www.facebook.com/nobody", "pageName": "nobody"}
    with pytest.raises(ValueError):
        map_post_item(item, pages)


def test_map_missing_time_raises(db, items, pages):
    item = {k: v for k, v in items[0].items() if k not in ("time", "timestamp")}
    with pytest.raises(ValueError, match=item["postId"]):
        map_post_item(item, pages)


def test_image_urls_skips_videos_and_caps_at_four():
    media = [{"__typename": "Video", "thumbnail": "v"}] + [
        {"__typename": "Photo", "photo_image": {"uri": f"u{i}"}} for i in range(6)
    ]
    assert image_urls(media) == ["u0", "u1", "u2", "u3"]


def test_upsert_new_then_update_keeps_status(db, items, pages, run_id):
    record = map_post_item(items[0], pages)
    assert upsert_posts(db, [record], run_id) == (1, 0)
    first = db.execute("SELECT * FROM posts WHERE fb_post_id = %s", (record.fb_post_id,)).fetchone()
    assert first["analysis_status"] == "pending"

    record2 = copy.copy(record)
    record2.reactions_count = record.reactions_count + 10
    assert upsert_posts(db, [record2], run_id) == (0, 1)
    second = db.execute("SELECT * FROM posts WHERE fb_post_id = %s", (record.fb_post_id,)).fetchone()
    assert second["reactions_count"] == first["reactions_count"] + 10
    assert second["analysis_status"] == "pending"
    assert second["first_seen_at"] == first["first_seen_at"]
    assert db.execute("SELECT count(*) AS n FROM posts").fetchone()["n"] == 1


def test_upsert_no_text_no_media_is_skipped(db, items, pages, run_id):
    record = map_post_item({**items[0], "text": None, "media": []}, pages)
    assert upsert_posts(db, [record], run_id) == (1, 0)
    row = db.execute("SELECT analysis_status FROM posts").fetchone()
    assert row["analysis_status"] == "skipped"


def test_upsert_in_chunks_keeps_counts(db, items, pages, run_id, monkeypatch):
    import app.ingest.posts as posts_module

    monkeypatch.setattr(posts_module, "CHUNK_SIZE", 2)
    records = [map_post_item(i, pages) for i in items]
    assert upsert_posts(db, records, run_id) == (3, 0)
    assert upsert_posts(db, records, run_id) == (0, 3)
    assert db.execute("SELECT count(*) AS n FROM posts").fetchone()["n"] == 3
