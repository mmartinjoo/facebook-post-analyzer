def test_migrate_creates_schema_and_seeds_pages(db):
    tables = {r["table_name"] for r in db.execute(
        "select table_name from information_schema.tables where table_schema='public'")}
    assert {"pages", "job_locks", "scrape_runs", "posts", "post_analyses", "comments",
            "monthly_summaries", "schema_migrations"} <= tables
    slugs = [r["slug"] for r in db.execute("select slug from pages order by slug")]
    assert slugs == ["erdraktar", "kontenertar", "kontigo", "raktar24", "vnv"]
    views = {r["table_name"] for r in db.execute(
        "select table_name from information_schema.views where table_schema='public'")}
    assert "posts_current" in views


def test_migrate_is_idempotent(db, settings):
    from db.migrate import migrate
    assert migrate(settings.test_database_url) == []
