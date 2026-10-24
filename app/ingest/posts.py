"""Map Apify `facebook-posts-scraper` items to `posts` rows and upsert them.

Field mapping (verified against a real probe run, see tests/fixtures/apify_posts_sample.json):

    postId                  -> fb_post_id (required)
    url (fallback topLevelUrl) -> url
    text                    -> text (may be missing/None)
    time (ISO 8601, 'Z')    -> posted_at, aware UTC; fallback `timestamp` (unix seconds)
    media[]                 -> media (stored as-is; photos carry __typename "Photo",
                               photo_image.uri, thumbnail, url)
    likes                   -> reactions_count
    comments                -> comments_count (key is often absent -> 0)
    shares                  -> shares_count
    facebookUrl / inputUrl / pageName -> page, via resolve_page
    whole item              -> raw

Missing or None counts become 0.
"""
from dataclasses import dataclass
from datetime import datetime, timezone

from psycopg.types.json import Jsonb

from app.pages import Page, resolve_page

MAX_IMAGES = 4
CHUNK_SIZE = 200  # records per transaction in upsert_posts


@dataclass
class PostRecord:
    page_id: int
    fb_post_id: str
    url: str
    text: str | None
    posted_at: datetime
    media: list[dict]
    reactions_count: int
    comments_count: int
    shares_count: int
    raw: dict


def _count(value) -> int:
    return int(value) if value is not None else 0


def _posted_at(item: dict, post_id: str) -> datetime:
    if item.get("time"):
        dt = datetime.fromisoformat(str(item["time"]).replace("Z", "+00:00"))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    if item.get("timestamp") is not None:
        return datetime.fromtimestamp(int(item["timestamp"]), tz=timezone.utc)
    raise ValueError(f"post {post_id}: missing time/timestamp")


def map_post_item(item: dict, pages: list[Page]) -> PostRecord:
    post_id = item.get("postId")
    if not post_id:
        raise ValueError("item has no postId")
    post_id = str(post_id)

    page = None
    tried = []
    for url in (item.get("facebookUrl"), item.get("inputUrl")):
        if url:
            tried.append(url)
            page = page or resolve_page(pages, url=url)
    if page is None and item.get("pageName"):
        tried.append(item["pageName"])
        page = resolve_page(pages, name=item["pageName"])
    if page is None:
        raise ValueError(f"post {post_id}: no matching page for {tried or 'no page identifiers'}")

    return PostRecord(
        page_id=page.id,
        fb_post_id=post_id,
        url=item.get("url") or item.get("topLevelUrl"),
        text=item.get("text"),
        posted_at=_posted_at(item, post_id),
        media=item.get("media") or [],
        reactions_count=_count(item.get("likes")),
        comments_count=_count(item.get("comments")),
        shares_count=_count(item.get("shares")),
        raw=item,
    )


def image_urls(media: list[dict]) -> list[str]:
    urls: list[str] = []
    for element in media or []:
        if element.get("__typename") not in (None, "Photo"):
            continue
        url = (
            (element.get("photo_image") or {}).get("uri")
            or element.get("thumbnail")
            or element.get("url")
        )
        if url and url not in urls:
            urls.append(url)
        if len(urls) == MAX_IMAGES:
            break
    return urls


_UPSERT = """
INSERT INTO posts (page_id, fb_post_id, url, text, posted_at, media, reactions_count,
                   comments_count, shares_count, raw, analysis_status, last_run_id,
                   first_seen_at, last_seen_at)
VALUES (%(page_id)s, %(fb_post_id)s, %(url)s, %(text)s, %(posted_at)s, %(media)s,
        %(reactions)s, %(comments)s, %(shares)s, %(raw)s, %(status)s, %(run_id)s, now(), now())
ON CONFLICT (fb_post_id) DO UPDATE SET
    reactions_count = EXCLUDED.reactions_count,
    comments_count = EXCLUDED.comments_count,
    shares_count = EXCLUDED.shares_count,
    media = EXCLUDED.media,
    raw = EXCLUDED.raw,
    last_seen_at = now(),
    last_run_id = EXCLUDED.last_run_id
RETURNING (xmax = 0) AS inserted
"""


def upsert_posts(conn, records: list[PostRecord], run_id: int) -> tuple[int, int]:
    """Upsert in chunks of CHUNK_SIZE with a commit per chunk, so a huge backfill dataset never sits in one
    transaction; a crash leaves the committed chunks, the re-delivered webhook upserts them again."""
    inserted = updated = 0
    for i, r in enumerate(records, 1):
        has_content = bool((r.text or "").strip()) or bool(image_urls(r.media))
        row = conn.execute(
            _UPSERT,
            {
                "page_id": r.page_id,
                "fb_post_id": r.fb_post_id,
                "url": r.url,
                "text": r.text,
                "posted_at": r.posted_at,
                "media": Jsonb(r.media),
                "reactions": r.reactions_count,
                "comments": r.comments_count,
                "shares": r.shares_count,
                "raw": Jsonb(r.raw),
                "status": "pending" if has_content else "skipped",
                "run_id": run_id,
            },
        ).fetchone()
        if row["inserted"]:
            inserted += 1
        else:
            updated += 1
        if i % CHUNK_SIZE == 0:
            conn.commit()
    conn.commit()
    return inserted, updated
