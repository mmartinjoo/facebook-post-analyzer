from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def migrate(dsn: str) -> list[str]:
    applied: list[str] = []
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "create table if not exists schema_migrations ("
            "name text primary key, applied_at timestamptz not null default now())"
        )
        conn.commit()
        done = {row[0] for row in conn.execute("select name from schema_migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in done:
                continue
            with conn.transaction():
                conn.execute(path.read_text())
                conn.execute("insert into schema_migrations (name) values (%s)", (path.name,))
            applied.append(path.name)
    return applied


if __name__ == "__main__":
    from app.config import get_settings

    print(migrate(get_settings().database_url))
