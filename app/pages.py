import re
from dataclasses import dataclass


@dataclass
class Page:
    id: int
    slug: str
    name: str
    city: str
    facebook_url: str


def load_active_pages(conn) -> list[Page]:
    rows = conn.execute(
        "SELECT id, slug, name, city, facebook_url FROM pages WHERE active ORDER BY id"
    ).fetchall()
    return [Page(**row) for row in rows]


def _normalize_url(url: str) -> str:
    url = url.strip().split("?", 1)[0].split("#", 1)[0]
    url = re.sub(r"^https?://(www\.)?", "", url, flags=re.IGNORECASE)
    url = re.sub(r"^facebook\.com/", "", url, flags=re.IGNORECASE)
    return url.rstrip("/").lower()


def resolve_page(pages: list[Page], *, url: str | None = None, name: str | None = None) -> Page | None:
    if url:
        wanted = _normalize_url(url)
        for page in pages:
            if _normalize_url(page.facebook_url) == wanted:
                return page
    if name:
        wanted_name = name.strip().lower()
        for page in pages:
            if page.name.lower() == wanted_name:
                return page
    return None
