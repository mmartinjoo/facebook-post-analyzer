import copy
import json
from pathlib import Path

import pytest

from app.ingest.comments import build_posts_by_fb_id, build_posts_by_url, map_comment_item, upsert_comments
from app.pages import load_active_pages

FIXTURES = Path(__file__).parent / "fixtures"
ITEMS = json.loads((FIXTURES / "apify_comments_sample.json").read_text(encoding="utf-8"))
POST_URL = json.loads((FIXTURES / "apify_posts_sample.json").read_text(encoding="utf-8"))[0]["url"]


@pytest.fixture
def setup(db):
    pages = load_active_pages(db)
    kontigo = next(p for p in pages if p.slug == "kontigo")
    post_id = db.execute(
        "INSERT INTO posts (page_id, fb_post_id, url, posted_at) VALUES (%s, 'p1', %s, now()) RETURNING id",
        (kontigo.id, POST_URL),
    ).fetchone()["id"]
    db.commit()
    return build_posts_by_url([{"id": post_id, "url": POST_URL, "page_id": kontigo.id}]), {p.id: p for p in pages}, kontigo


def test_map_fixture_and_author_is_page(db, setup):
    posts_by_url, pages_by_id, kontigo = setup
    records = [map_comment_item(i, posts_by_url, pages_by_id) for i in ITEMS]
    assert len(records) == len(ITEMS)
    assert all(r.posted_at.tzinfo is not None and isinstance(r.likes_count, int) for r in records)
    assert records[0].likes_count == 3 and not records[0].is_reply and not records[0].author_is_page
    assert records[1].is_reply
    modified = copy.deepcopy(ITEMS[0])
    modified["profileName"] = f"  {kontigo.name.upper()} "
    modified["profileUrl"] = "https://www.facebook.com/someone-else"
    assert map_comment_item(modified, posts_by_url, pages_by_id).author_is_page is True
    by_url = copy.deepcopy(ITEMS[0])
    by_url["profileUrl"] = kontigo.facebook_url + "/?ref=x"
    assert map_comment_item(by_url, posts_by_url, pages_by_id).author_is_page is True


def test_unknown_post_raises(db, setup):
    _, pages_by_id, _ = setup
    with pytest.raises(ValueError):
        map_comment_item(ITEMS[0], {}, pages_by_id)


def test_upsert_idempotent(db, setup):
    posts_by_url, pages_by_id, _ = setup
    records = [map_comment_item(i, posts_by_url, pages_by_id) for i in ITEMS]
    assert upsert_comments(db, records) == len(records)
    assert upsert_comments(db, records) == len(records)
    assert db.execute("SELECT count(*) AS n FROM comments").fetchone()["n"] == len(records)


def test_upsert_in_chunks(db, setup, monkeypatch):
    import app.ingest.comments as comments_module

    monkeypatch.setattr(comments_module, "CHUNK_SIZE", 2)
    posts_by_url, pages_by_id, _ = setup
    records = [map_comment_item(i, posts_by_url, pages_by_id) for i in ITEMS]
    assert len(records) > 2
    assert upsert_comments(db, records) == len(records)
    assert db.execute("SELECT count(*) AS n FROM comments").fetchone()["n"] == len(records)


def test_fallback_to_facebook_id_when_url_does_not_match(db, setup):
    _, pages_by_id, _ = setup
    post_id = db.execute("SELECT id, page_id FROM posts WHERE fb_post_id = 'p1'").fetchone()
    by_fb_id = build_posts_by_fb_id([{"id": post_id["id"], "page_id": post_id["page_id"], "fb_post_id": "p1"}])
    item = {**ITEMS[0], "facebookUrl": "https://www.facebook.com/somewhere/else", "facebookId": "p1"}
    with pytest.raises(ValueError):
        map_comment_item(item, {}, pages_by_id)
    assert map_comment_item(item, {}, pages_by_id, by_fb_id).post_id == post_id["id"]
