from datetime import date, datetime, timezone

from app.summary.stats import compute_stats
from tests.fixtures.seed_month import seed

M = date(2026, 9, 1)


def page_id(db, slug):
    return db.execute("select id from pages where slug = %s", (slug,)).fetchone()["id"]


def test_breakdowns_and_top_use_cases(db):
    seed(db, page_slug="kontigo", month=M, posts=[
        {"reactions_count": 10},
        {"reactions_count": 30},
        {"reactions_count": 20, "lakossagi_szegmens": "panel", "use_case": "költözés"},
        {"analysis_status": "pending", "reactions_count": 15},
        {"analysis_status": "skipped", "reactions_count": 50},
    ])
    s = compute_stats(db, page_id(db, "kontigo"), M)
    assert s.post_count == 5
    assert s.lakossagi_szegmens_breakdown == {"csaladi_haz": 2, "panel": 1}
    assert s.top_use_cases[0] == {"use_case": "kerti bútor tárolása", "count": 2}
    assert s.avg_engagement_by_lakossagi == {"csaladi_haz": 20.0, "panel": 20.0}
    assert s.best_post_engagement == 50
    assert s.worst_post_engagement == 10


def test_empty_month(db):
    s = compute_stats(db, page_id(db, "kontigo"), M)
    assert s.post_count == 0 and s.comment_count == 0
    assert s.best_post_id is None and s.worst_post_id is None
    assert s.celkozonseg_breakdown == {} and s.top_use_cases == [] and s.top_hook is None
    assert s.price_stats == {"posts_with_price": 0, "by_unit": {}}


def test_single_post_best_equals_worst(db):
    [pid] = seed(db, page_slug="kontigo", month=M, posts=[{"reactions_count": 7}])
    s = compute_stats(db, page_id(db, "kontigo"), M)
    assert s.best_post_id == s.worst_post_id == pid
    assert s.best_post_engagement == s.worst_post_engagement == 7


def test_all_pages_aggregates_across_pages(db):
    seed(db, page_slug="kontigo", month=M, posts=[{}])
    seed(db, page_slug="raktar24", month=M, posts=[{}])
    assert compute_stats(db, None, M).post_count == 2
    assert compute_stats(db, page_id(db, "kontigo"), M).post_count == 1


def test_price_stats_from_arak(db):
    seed(db, page_slug="kontigo", month=M, posts=[
        {"arak": [{"meret": "10", "ertek": 25000, "egyseg": "ft_ho"}]},
        {"arak": [{"meret": "20", "ertek": 45000, "egyseg": "ft_ho"},
                  {"meret": "x", "ertek": 3000, "egyseg": "ft_m2_ho"},
                  {"meret": "y", "ertek": 0, "egyseg": "ft_ho"}]},
    ])
    s = compute_stats(db, page_id(db, "kontigo"), M)
    assert s.price_stats["by_unit"]["ft_ho"] == {"min": 25000, "max": 45000, "avg": 35000.0, "n": 2}
    assert s.price_stats["posts_with_price"] == 2


def test_month_boundary_budapest(db):
    seed(db, page_slug="kontigo", month=M, posts=[
        {"posted_at": datetime(2026, 9, 30, 22, 30, tzinfo=timezone.utc)},
    ])
    pid = page_id(db, "kontigo")
    assert compute_stats(db, pid, date(2026, 10, 1)).post_count == 1
    assert compute_stats(db, pid, M).post_count == 0
