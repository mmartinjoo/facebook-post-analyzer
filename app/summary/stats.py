from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date

from app.timeutil import month_bounds_utc

_ANALYSIS_COLS = (
    "celkozonseg, ceges_szegmens, lakossagi_szegmens, szezonalis_horog, "
    "use_case, arak, ajanlat_mechanika, bizalmi_elemek, surgosseg"
)


@dataclass
class SummaryStats:
    post_count: int = 0
    comment_count: int = 0
    celkozonseg_breakdown: dict[str, int] = field(default_factory=dict)
    ceges_szegmens_breakdown: dict[str, int] = field(default_factory=dict)
    lakossagi_szegmens_breakdown: dict[str, int] = field(default_factory=dict)
    szezonalis_horog_breakdown: dict[str, int] = field(default_factory=dict)
    top_use_cases: list[dict] = field(default_factory=list)
    top_hook: str | None = None
    avg_engagement_by_celkozonseg: dict[str, float] = field(default_factory=dict)
    avg_engagement_by_lakossagi: dict[str, float] = field(default_factory=dict)
    avg_engagement_by_ceges: dict[str, float] = field(default_factory=dict)
    best_post_id: int | None = None
    best_post_url: str | None = None
    best_post_engagement: int | None = None
    worst_post_id: int | None = None
    worst_post_url: str | None = None
    worst_post_engagement: int | None = None
    price_stats: dict = field(default_factory=lambda: {"posts_with_price": 0, "by_unit": {}})
    ajanlat_mechanika_counts: dict[str, int] = field(default_factory=dict)
    bizalmi_elemek_counts: dict[str, int] = field(default_factory=dict)
    surgosseg_counts: dict[str, int] = field(default_factory=dict)


def _avg_by(done: list[dict], key: str) -> dict[str, float]:
    groups: dict[str, list[int]] = defaultdict(list)
    for r in done:
        groups[r[key]].append(r["engagement"])
    return {k: round(sum(v) / len(v), 2) for k, v in groups.items()}


def _top_use_cases(done: list[dict]) -> list[dict]:
    counts: Counter[str] = Counter()
    first_seen: dict[str, str] = {}
    for r in done:
        original = (r["use_case"] or "").strip()
        if not original:
            continue
        norm = original.lower()
        counts[norm] += 1
        first_seen.setdefault(norm, original)
    ranked = sorted(counts, key=lambda n: (-counts[n], first_seen[n]))[:3]
    return [{"use_case": first_seen[n], "count": counts[n]} for n in ranked]


def _price_stats(done: list[dict]) -> dict:
    by_unit: dict[str, list[float]] = defaultdict(list)
    posts_with_price = 0
    for r in done:
        valid = False
        for entry in r["arak"] or []:
            try:
                value = float(entry.get("ertek"))
            except (TypeError, ValueError, AttributeError):
                continue
            unit = entry.get("egyseg")
            if value <= 0 or not unit:
                continue
            by_unit[unit].append(value)
            valid = True
        posts_with_price += valid
    return {
        "posts_with_price": posts_with_price,
        "by_unit": {
            u: {
                "min": min(v),
                "max": max(v),
                "avg": round(sum(v) / len(v), 2),
                "n": len(v),
            }
            for u, v in by_unit.items()
        },
    }


def compute_stats(conn, page_id: int | None, month: date) -> SummaryStats:
    start, end = month_bounds_utc(month)
    if page_id is None:
        page_filter = "page_id IN (SELECT id FROM pages WHERE active)"
        params: list = [start, end]
    else:
        page_filter = "page_id = %s"
        params = [start, end, page_id]

    rows = conn.execute(
        f"""SELECT id, url, engagement, analysis_status, analysis_id, {_ANALYSIS_COLS}
            FROM posts_current
            WHERE posted_at >= %s AND posted_at < %s AND {page_filter}
            ORDER BY engagement DESC, id ASC""",
        params,
    ).fetchall()
    comment_count = conn.execute(
        f"""SELECT count(*) AS n FROM comments c JOIN posts p ON p.id = c.post_id
            WHERE p.posted_at >= %s AND p.posted_at < %s AND p.{page_filter}""",
        params,
    ).fetchone()["n"]

    stats = SummaryStats(post_count=len(rows), comment_count=comment_count)
    if rows:
        best, worst = rows[0], rows[-1]
        stats.best_post_id, stats.best_post_url = best["id"], best["url"]
        stats.best_post_engagement = best["engagement"]
        stats.worst_post_id, stats.worst_post_url = worst["id"], worst["url"]
        stats.worst_post_engagement = worst["engagement"]

    done = [r for r in rows if r["analysis_status"] == "done" and r["analysis_id"] is not None]
    stats.celkozonseg_breakdown = dict(Counter(r["celkozonseg"] for r in done))
    stats.ceges_szegmens_breakdown = dict(Counter(r["ceges_szegmens"] for r in done))
    stats.lakossagi_szegmens_breakdown = dict(Counter(r["lakossagi_szegmens"] for r in done))
    stats.szezonalis_horog_breakdown = dict(Counter(r["szezonalis_horog"] for r in done))
    stats.top_use_cases = _top_use_cases(done)
    hooks = Counter(r["szezonalis_horog"] for r in done if r["szezonalis_horog"] != "nincs")
    stats.top_hook = hooks.most_common(1)[0][0] if hooks else None
    stats.avg_engagement_by_celkozonseg = _avg_by(done, "celkozonseg")
    stats.avg_engagement_by_lakossagi = _avg_by(done, "lakossagi_szegmens")
    stats.avg_engagement_by_ceges = _avg_by(done, "ceges_szegmens")
    stats.price_stats = _price_stats(done)
    stats.ajanlat_mechanika_counts = dict(Counter(x for r in done for x in r["ajanlat_mechanika"] or []))
    stats.bizalmi_elemek_counts = dict(Counter(x for r in done for x in r["bizalmi_elemek"] or []))
    stats.surgosseg_counts = dict(Counter(r["surgosseg"] for r in done))
    return stats
