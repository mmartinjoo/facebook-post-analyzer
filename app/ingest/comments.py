"""Map Apify facebook-comments-scraper items to `comments` rows and upsert them.

Field sources (probe 2026-10-24: all 3 fixture posts returned only an error item,
"No public data available without Facebook login", so the fixture is hand-built from
the actor's documented output; verify on the first real run):
- fb_comment_id: `commentId` (fallback `id`)
- post: `facebookUrl` (fallback `inputUrl`), normalised, matched against posts.url
- text: `text`
- posted_at: `date` (ISO, may end in Z) -> aware UTC
- likes_count: `likesCount` (a STRING in the actor output)
- is_reply: `threadingDepth` > 0 (no parent id is documented)
- author_is_page: `profileName` equals the page name, or `profileUrl` equals the page URL
"""
from dataclasses import dataclass
from datetime import date, datetime, timezone
from urllib.parse import urlsplit

from psycopg.types.json import Jsonb

from app.pages import Page
from app.timeutil import month_bounds_utc


@dataclass
class CommentRecord:
    post_id: int
    fb_comment_id: str
    text: str | None
    posted_at: datetime | None
    likes_count: int
    is_reply: bool
    author_is_page: bool
    raw: dict


def normalize_post_url(url: str) -> str:
    parts = urlsplit(url.strip())
    return f"{parts.scheme}://{parts.netloc}{parts.path}".rstrip("/").lower()


def _parse_date(value) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def map_comment_item(item: dict, posts_by_url: dict[str, dict], pages_by_id: dict[int, Page]) -> CommentRecord:
    comment_id = item.get("commentId") or item.get("id")
    if not comment_id:
        raise ValueError(f"comment item without id (url {item.get('facebookUrl') or item.get('inputUrl')})")
    comment_id = str(comment_id)
    url = item.get("facebookUrl") or item.get("inputUrl")
    index = {normalize_post_url(u): p for u, p in posts_by_url.items()}
    post = index.get(normalize_post_url(url)) if url else None
    if post is None:
        raise ValueError(f"comment {comment_id}: no post for url {url}")

    page = pages_by_id.get(post["page_id"])
    name = (item.get("profileName") or "").strip().lower()
    profile_url = item.get("profileUrl")
    author_is_page = bool(
        page
        and (
            (name and name == page.name.strip().lower())
            or (profile_url and normalize_post_url(profile_url) == normalize_post_url(page.facebook_url))
        )
    )
    return CommentRecord(
        post_id=post["id"],
        fb_comment_id=comment_id,
        text=item.get("text"),
        posted_at=_parse_date(item.get("date")),
        likes_count=int(item.get("likesCount") or 0),
        is_reply=int(item.get("threadingDepth") or 0) > 0,
        author_is_page=author_is_page,
        raw=item,
    )


def upsert_comments(conn, records: list[CommentRecord]) -> int:
    for r in records:
        conn.execute(
            """
            INSERT INTO comments (post_id, fb_comment_id, text, posted_at, likes_count, is_reply,
                                  author_is_page, raw, scraped_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (fb_comment_id) DO UPDATE SET
                text = EXCLUDED.text, likes_count = EXCLUDED.likes_count,
                raw = EXCLUDED.raw, scraped_at = now()
            """,
            (r.post_id, r.fb_comment_id, r.text, r.posted_at, r.likes_count, r.is_reply,
             r.author_is_page, Jsonb(r.raw)),
        )
    conn.commit()
    return len(records)


def posts_in_month(conn, month: date, page_id: int | None = None) -> list[dict]:
    start, end = month_bounds_utc(month)
    sql = "SELECT id, url, page_id FROM posts WHERE posted_at >= %s AND posted_at < %s"
    params: list = [start, end]
    if page_id is not None:
        sql += " AND page_id = %s"
        params.append(page_id)
    return conn.execute(sql + " ORDER BY posted_at", params).fetchall()
