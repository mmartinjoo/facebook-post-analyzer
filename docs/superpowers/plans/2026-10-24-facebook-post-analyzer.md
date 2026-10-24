# Facebook versenytárs-poszt elemző – implementációs terv

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Naponta futó, felügyelet nélküli pipeline, ami öt versenytárs Facebook-oldal posztjait Apify-jal letölti, Claude-dal strukturált mezőkre bontja, és havi összegzésekkel együtt Neon Postgresbe írja.

**Architecture:** Egyetlen FastAPI alkalmazás Vercel Python függvényként (`api/index.py`), amit Vercel cron, Apify webhook és kézi `curl` hív. Semmi nem vár az Apify-ra: a futást elindítjuk, a webhook dolgozza fel. Minden hosszabb munka időkeretes, `job_locks`-szal zárt, önmagát HTTP-n újrahívó job. Az adat egyetlen Postgres sémában él, ORM nélkül, `psycopg` 3-mal.

**Tech Stack:** Python 3.12, FastAPI, psycopg 3, pydantic 2, `anthropic` SDK (`claude-haiku-5-5`, `claude-sonnet-5-5`, structured output `messages.parse`), `apify-client`, httpx, pytest, Docker (teszt-Postgres), Vercel (Hobby, Fluid compute), Neon.

**Spec:** `docs/superpowers/specs/2026-10-24-facebook-post-analyzer-design.md` – a terv a spec-ből érvel, az implementáló mindkettőt olvassa.

## Global Constraints

- Python 3.12 (`.python-version`), függőségek `requirements.txt`-ben (Vercel ezt olvassa) és `pyproject.toml`-ban (uv, dev-függőségek).
- Modellek szó szerint: poszt-kinyerés és egy mondat `claude-haiku-5-5`, komment-klaszterezés `claude-sonnet-5-5`. Thinking paraméter nincs megadva, `output_config={"effort": "low"}` a Haikunál, `"medium"` a Sonnetnél.
- Enum-mezők értékei kizárólag a spec 3. fejezetének listáiból; a sémát a `PostAnalysis` pydantic modell `Literal` típusai rögzítik, a LLM `messages.parse(output_format=PostAnalysis)`-szal hívva.
- Oszlopnevek angolul, LLM-mezők a spec magyar nevein (`celkozonseg`, `ceges_szegmens`, …, `konfidencia`).
- Minden időbélyeg `timestamptz`; hónap-bucket `Europe/Budapest` szerint, `month` oszlop a hónap első napja (`date`).
- Engagement = `reactions_count + comments_count + shares_count` (generált oszlop).
- Idempotencia: `posts.fb_post_id`, `comments.fb_comment_id`, `scrape_runs.apify_run_id` unique; `post_analyses (post_id, prompt_version)` unique; `monthly_summaries (page_id, month) NULLS NOT DISTINCT` unique.
- Titkok csak env-ből: `APIFY_API_TOKEN`, `ANTHROPIC_API_KEY`, `DATABASE_URL`, `CRON_SECRET`, `APIFY_WEBHOOK_SECRET`, `ADMIN_SECRET`, `APP_BASE_URL`, `JOB_TIME_BUDGET_SECONDS` (alap 200), `PROMPT_VERSION` (alap `v1`). A repóba `.env.example` kerül, `.env` nem.
- Vercel Hobby: legfeljebb 2 cron (napi + havi), `maxDuration` 300 s az `api/index.py`-ra, webhook-útvonal Deployment Protection alól kivéve.
- Minden job és webhook egy JSON-soros `logging.info`-t ír a végén (`{"job": ..., "run_id": ..., "processed": ..., "elapsed_s": ...}`), hogy a Vercel logból követhető legyen.
- Commit üzenetek vége: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

## Review Focus

1. **Apify item hiányos mezőkkel** (`text` null, `media` üres, `time` hiányzik): a leképezés ne dobjon, a poszt `pending` ha van szöveg vagy kép, `skipped` ha egyik sincs, `posted_at` null esetén a run `failed` hibába kerül az item azonosítójával. – Teszt: Task 6.
2. **Webhook duplán vagy ismeretlen run-ra érkezik** (az Apify újrapróbál): már `processed` run-ra 200 és semmi nem történik; ismeretlen `apify_run_id`-re 404, adatbázis érintetlen. – Teszt: Task 7.
3. **Ár csak a képen, vagy inkonzisztens LLM-válasz** (`arak` nem üres, de `ar_ertek` null, vagy `ar_megjelenik` false): a normalizálás az `arak` listából számolja `ar_ertek`/`ar_egyseg`-et (legalacsonyabb) és `ar_megjelenik`-et. – Teszt: Task 8.
4. **Hónaphatár Budapest időben**: a 2026-09-30 22:30 UTC-s poszt (00:30 október 1., CEST) októberhez tartozik; a napi cron 1-jén Budapest szerint az előző hónapot számolja. – Teszt: Task 3.
5. **Üres vagy egyposztos hónap az összegzésben**: nulla posztnál `post_count=0`, best/worst null, mondat LLM nélkül; egy posztnál best=worst ugyanaz; `pending`/`skipped` posztok a szegmens-átlagokból kimaradnak, de a `post_count`-ba beszámítanak. – Teszt: Task 12.

---

## Fájlstruktúra

```
facebook_post_analyzer/
  api/index.py                 FastAPI app; útvonalak, auth-függőségek, dispatch a job-okra
  app/
    config.py                  Settings (pydantic-settings) az env-ből
    db.py                      connect(), query helperek (dict sorok)
    auth.py                    require_cron / require_admin / require_webhook függőségek
    timeutil.py                Budapest hónap-bucket, előző hónap, hónap-sorozat
    pages.py                   aktív oldalak betöltése, URL → page feloldás
    apify_runs.py              Apify futás indítása webhookkal, scrape_runs írás/olvasás
    ingest/posts.py            Apify poszt-item → PostRecord, upsert
    ingest/comments.py         Apify komment-item → CommentRecord, upsert
    analysis/schema.py         PostAnalysis pydantic modell (enumok), normalizálás
    analysis/prompt.py         prompts/post_extraction_<ver>.md betöltése, üzenetépítés
    analysis/extractor.py      analyze_post(): Haiku hívás, kép-fallback
    analysis/job.py            run_analyze_job(): pending posztok feldolgozása időkeretben
    summary/stats.py           SQL-aggregátumok egy (page_id|None, month) párra
    summary/comments_llm.py    Sonnet: top kérdések/kifogások
    summary/sentence.py        Haiku: egy mondatos összefoglaló
    summary/job.py             run_summarize_job(): upsert monthly_summaries, stale/computed_at logika
    jobs/runner.py             TimeBudget, job_locks, continue_job() önfolytatás
    jobs/webhook.py            webhook-feldolgozás: dataset letöltés → ingest → következő job
    jobs/backfill.py           backfill / backfill-comments / reanalyze logika
  db/migrations/001_init.sql   teljes séma + seed pages
  db/migrate.py                migrációk futtatása psycopg-vel (nincs psql)
  prompts/post_extraction_v1.md, comment_clustering_v1.md, monthly_sentence_v1.md
  scripts/probe_apify_posts.py, probe_apify_comments.py   próbafutás, fixture mentés, költségbecslés
  tests/                       pytest; conftest.py a teszt-DB-vel; fixtures/
  vercel.json, requirements.txt, pyproject.toml, .python-version, .env.example,
  docker-compose.test.yml, README.md, .gitignore
```

---

### Task 0: Projekt-váz, teszt-DB, FastAPI health, Vercel konfig

**Files:**
- Create: `pyproject.toml`, `requirements.txt`, `.python-version`, `.gitignore`, `.env.example`, `docker-compose.test.yml`, `vercel.json`, `api/index.py`, `app/__init__.py`, `tests/__init__.py`, `tests/test_health.py`

**Interfaces:**
- Produces: `api/index.py` exportál `app: fastapi.FastAPI`; `GET /api/health` → `{"ok": true}`.

- [ ] **Step 1: git init és uv projekt**

```bash
cd /Users/joomartin/code/dokkinger/facebook_post_analyzer
git init -b main
echo "3.12" > .python-version
uv init --no-readme --python 3.12 --name facebook_post_analyzer   # ha már van pyproject, csak uv venv
```

`pyproject.toml` `[project] dependencies`: `fastapi`, `anthropic`, `apify-client`, `psycopg[binary]`, `pydantic>=2`, `pydantic-settings`, `httpx`. `[dependency-groups] dev`: `pytest`, `pytest-asyncio`, `uvicorn`. `[tool.pytest.ini_options] testpaths=["tests"]`, `pythonpath=["."]`.

`requirements.txt` = a runtime függőségek (Vercel ezt telepíti). `uv export --no-dev --no-hashes -o requirements.txt` generálja; a lépés a Task végén újrafut.

- [ ] **Step 2: .gitignore és .env.example**

`.gitignore`: `.env`, `.venv/`, `__pycache__/`, `.vercel/`, `.pytest_cache/`.
`.env.example`: a Global Constraints összes változója üres értékkel, plusz `TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:55432/fpa_test`.

- [ ] **Step 3: docker-compose.test.yml**

Egy `postgres:16` szolgáltatás, `POSTGRES_PASSWORD=postgres`, `POSTGRES_DB=fpa_test`, port `55432:5432`. Indítás: `docker compose -f docker-compose.test.yml up -d`.

- [ ] **Step 4: Írd meg a failing tesztet**

```python
# tests/test_health.py
from fastapi.testclient import TestClient
from api.index import app

def test_health():
    r = TestClient(app).get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True}
```

- [ ] **Step 5: Futtasd, bukjon** – `uv run pytest tests/test_health.py -v` → ImportError.

- [ ] **Step 6: `api/index.py`**: `app = FastAPI()`, `@app.get("/api/health")`. Más útvonal még nincs.

- [ ] **Step 7: vercel.json**

```json
{
  "rewrites": [{ "source": "/api/(.*)", "destination": "/api/index" }],
  "functions": { "api/index.py": { "maxDuration": 300 } },
  "crons": [
    { "path": "/api/cron/daily",   "schedule": "0 4 * * *" },
    { "path": "/api/cron/monthly", "schedule": "30 4 1 * *" }
  ]
}
```

(04:00 UTC = 06:00 CEST / 05:00 CET; Hobbyn a cron ±1 órát csúszhat, ez elfogadott.)

- [ ] **Step 8: Futtasd, menjen át** – `uv run pytest -v` → PASS.

- [ ] **Step 9: Commit** – `git add -A && git commit -m "chore: project scaffold with FastAPI health endpoint and Vercel config"`

---

### Task 1: Konfiguráció, DB-kapcsolat, migrációk, séma

**Files:**
- Create: `app/config.py`, `app/db.py`, `db/migrate.py`, `db/migrations/001_init.sql`, `tests/conftest.py`, `tests/test_migrate.py`

**Interfaces:**
- Produces:
  - `app.config.Settings` (pydantic-settings, `.env` olvasás): mezők a Global Constraints szerint, `job_time_budget_seconds: int = 200`, `prompt_version: str = "v1"`, `test_database_url: str | None = None`; `get_settings() -> Settings` (lru_cache).
  - `app.db.connect(dsn: str | None = None) -> psycopg.Connection` – `row_factory=dict_row`, autocommit False; `dsn` alap `Settings.database_url`.
  - `db.migrate.migrate(dsn: str) -> list[str]` – a `schema_migrations(name text primary key, applied_at)` táblában nem szereplő `db/migrations/*.sql` fájlokat sorrendben futtatja, visszaadja az alkalmazottakat.
  - `tests/conftest.py`: `db` fixture – `TEST_DATABASE_URL`-re `DROP SCHEMA public CASCADE; CREATE SCHEMA public;` majd `migrate()`, a kapcsolatot adja; `settings` fixture – `Settings` tesztértékekkel (secretek: `"cron-s"`, `"admin-s"`, `"hook-s"`, `app_base_url="http://testserver"`).

- [ ] **Step 1: Failing teszt**

```python
# tests/test_migrate.py
def test_migrate_creates_schema_and_seeds_pages(db):
    tables = {r["table_name"] for r in db.execute(
        "select table_name from information_schema.tables where table_schema='public'")}
    assert {"pages","job_locks","scrape_runs","posts","post_analyses","comments",
            "monthly_summaries","schema_migrations"} <= tables
    slugs = [r["slug"] for r in db.execute("select slug from pages order by slug")]
    assert slugs == ["erdraktar","kontenertar","kontigo","raktar24","vnv"]
    views = {r["table_name"] for r in db.execute(
        "select table_name from information_schema.views where table_schema='public'")}
    assert "posts_current" in views

def test_migrate_is_idempotent(db, settings):
    from db.migrate import migrate
    assert migrate(settings.test_database_url) == []
```

- [ ] **Step 2: Futtasd, bukjon.**

- [ ] **Step 3: `001_init.sql`** – a spec 6. fejezete szerint, pontosan ezekkel a kiegészítésekkel:
  - `posts.analysis_attempts integer not null default 0`.
  - `post_analyses` enum-oszlopok `text` + `CHECK (col IN (...))` a spec listáival; lista-oszlopok `text[]`; `arak jsonb not null default '[]'`.
  - `monthly_summaries`: a spec oszlopai + `stats_computed_at timestamptz`, `comments_computed_at timestamptz`, `stale boolean not null default false`; `UNIQUE NULLS NOT DISTINCT (page_id, month)`.
  - `posts_current` nézet: `posts` LEFT JOIN a poszt legnagyobb `created_at`-ú `post_analyses` sorával (DISTINCT ON).
  - Indexek: `posts(page_id, posted_at)`, `posts(analysis_status)`, `comments(post_id)`, `scrape_runs(status)`.
  - Seed: a spec 4. fejezetének öt sora `INSERT ... ON CONFLICT (slug) DO NOTHING`.

- [ ] **Step 4: `app/config.py`, `app/db.py`, `db/migrate.py`, `tests/conftest.py`** az Interfaces szerint. `migrate` egy tranzakcióban fájlonként.

- [ ] **Step 5: Teszt-DB indítása és tesztek** – `docker compose -f docker-compose.test.yml up -d && uv run pytest -v` → PASS.

- [ ] **Step 6: Commit** – `feat: settings, db connection, migrations and initial schema`

---

### Task 2: Végpont-védelem

**Files:**
- Create: `app/auth.py`, `tests/test_auth.py`
- Modify: `api/index.py`

**Interfaces:**
- Produces: FastAPI dependency-k: `require_cron` (header `Authorization: Bearer <CRON_SECRET>`), `require_admin` (header `X-Admin-Secret`), `require_webhook` (query `token=<APIFY_WEBHOOK_SECRET>`). Hibánál `HTTPException(401)`. Összehasonlítás `hmac.compare_digest`. Az `api/index.py` `app.dependency_overrides`-szal fogadja a teszt-`Settings`-et: a `get_settings` függőségként megy be.

- [ ] **Step 1: Failing teszt** – `tests/test_auth.py`: három ideiglenes útvonal (`/t/cron`, `/t/admin`, `/t/hook`) a tesztben regisztrálva a megfelelő dependency-vel; jó secret → 200, rossz/hiányzó → 401, mindhárom esetre.

- [ ] **Step 2: Bukjon.** **Step 3: Implementáld.** **Step 4: PASS.**

- [ ] **Step 5: Commit** – `feat: cron, admin and webhook secret guards`

---

### Task 3: Idő-segédek

**Files:**
- Create: `app/timeutil.py`, `tests/test_timeutil.py`

**Interfaces:**
- Produces:
  - `BUDAPEST = ZoneInfo("Europe/Budapest")`
  - `month_of(ts: datetime) -> date` – a `ts` (tz-aware) Budapest-hónapjának első napja.
  - `previous_month(today: date | None = None) -> date` – Budapest „ma” szerint az előző hónap első napja.
  - `months_between(start: date, end: date) -> list[date]` – hónap-elsők, mindkét vég inkluzív.
  - `month_bounds_utc(month: date) -> tuple[datetime, datetime]` – a hónap [kezdet, vég) UTC-ben, Budapest-határokkal.

- [ ] **Step 1: Failing teszt**

```python
def test_month_of_budapest_boundary():
    ts = datetime(2026, 9, 30, 22, 30, tzinfo=timezone.utc)   # 00:30 okt. 1. CEST
    assert month_of(ts) == date(2026, 10, 1)

def test_previous_month_on_first_of_january():
    assert previous_month(date(2026, 1, 1)) == date(2025, 12, 1)

def test_months_between_inclusive():
    assert months_between(date(2025, 11, 1), date(2026, 2, 1)) == [
        date(2025,11,1), date(2025,12,1), date(2026,1,1), date(2026,2,1)]

def test_month_bounds_utc_cet_winter():
    start, end = month_bounds_utc(date(2026, 1, 1))
    assert start == datetime(2025, 12, 31, 23, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 1, 31, 23, 0, tzinfo=timezone.utc)
```

- [ ] **Step 2: Bukjon.** **Step 3: Implementáld.** **Step 4: PASS.**
- [ ] **Step 5: Commit** – `feat: Budapest month bucketing helpers`

---

### Task 4: Job-futtató: időkeret, zár, önfolytatás

**Files:**
- Create: `app/jobs/__init__.py`, `app/jobs/runner.py`, `tests/test_runner.py`

**Interfaces:**
- Produces:
  - `class TimeBudget(seconds: float)`: `.expired() -> bool`, `.remaining() -> float`.
  - `acquire_lock(conn, job_name: str, ttl_seconds: int = 600, owner: str) -> bool` – `job_locks` upsert: siker, ha nincs sor vagy `locked_until < now()`; commit.
  - `release_lock(conn, job_name: str, owner: str) -> None`.
  - `continue_job(settings, path: str, params: dict | None = None) -> None` – `httpx.post(settings.app_base_url + path, params=params, headers={"X-Admin-Secret": settings.admin_secret}, timeout=3.0)`; `httpx.TimeoutException`-t elnyeli (a hívott függvény addigra elindult), más hibát logol, nem dob.
  - `JOB_ANALYZE = "analyze"`, `JOB_SUMMARIZE = "summarize"` konstansok.

- [ ] **Step 1: Failing teszt** – `tests/test_runner.py`:
  - `test_lock_is_exclusive_until_released(db)`: első `acquire_lock` True, második (más owner) False, `release_lock` után True.
  - `test_expired_lock_can_be_taken(db)`: `ttl_seconds=-1`-gyel vett zár után másik owner True-t kap.
  - `test_continue_job_swallows_timeout(settings, monkeypatch)`: `httpx.post` monkeypatch-elve `httpx.ReadTimeout`-ot dob → nem terjed; a hívás URL-je `http://testserver/api/jobs/analyze`, header tartalmazza az admin secretet.
  - `test_time_budget_expires()`: `TimeBudget(0).expired()` True.

- [ ] **Step 2: Bukjon.** **Step 3: Implementáld.** **Step 4: PASS.**
- [ ] **Step 5: Commit** – `feat: job runner with time budget, db locks and self-continuation`

---

### Task 5: Apify futás indítása és `scrape_runs`

**Files:**
- Create: `app/pages.py`, `app/apify_runs.py`, `tests/test_apify_runs.py`

**Interfaces:**
- Produces:
  - `app.pages.load_active_pages(conn) -> list[Page]` (`Page`: dataclass `id, slug, name, city, facebook_url`); `resolve_page(pages, *, url: str | None, name: str | None) -> Page | None` – URL-t normalizálva (`https?://(www\.)?facebook\.com/` levágva, záró `/` nélkül, kisbetű) egyezteti, különben névre.
  - `app.apify_runs.POSTS_ACTOR = "apify/facebook-posts-scraper"`, `COMMENTS_ACTOR = "apify/facebook-comments-scraper"`.
  - `start_posts_run(conn, settings, client: ApifyClient, pages: list[Page], newer_than: date, kind: str, results_limit: int) -> int` – `client.actor(POSTS_ACTOR).start(run_input={"startUrls":[{"url": p.facebook_url} ...], "onlyPostsNewerThan": newer_than.isoformat(), "resultsLimit": results_limit}, webhooks=[...])`; `scrape_runs` sor (`actor='posts'`, `kind`, `params={"since": ..., "page_urls": [...]}`, `status='started'`, `apify_run_id`, `started_at`); visszaadja a `scrape_runs.id`-t.
  - `start_comments_run(conn, settings, client, post_urls: list[str], kind: str, params: dict, results_limit: int = 100) -> int` – `run_input={"startUrls":[{"url": u}...], "resultsLimit": results_limit, "includeNestedComments": True}`; `actor='comments'`, `params` (pl. `{"month": "2026-09"}` vagy `{"from": ..., "to": ...}`).
  - Webhook definíció mindkettőnél: `{"event_types": ["ACTOR.RUN.SUCCEEDED","ACTOR.RUN.FAILED","ACTOR.RUN.ABORTED","ACTOR.RUN.TIMED_OUT"], "request_url": f"{app_base_url}/api/webhooks/apify?token={apify_webhook_secret}", "payload_template": '{"runId": {{resource.id}}, "status": {{resource.status}}, "datasetId": {{resource.defaultDatasetId}}, "eventType": {{eventType}}}'}`.
  - `get_run_by_apify_id(conn, apify_run_id) -> dict | None`; `mark_run(conn, run_id, *, status, items_count=None, error=None)`.

- [ ] **Step 1: Failing teszt** – `tests/test_apify_runs.py` a `client` helyén `MagicMock` (`client.actor().start.return_value = {"id": "run1", "defaultDatasetId": "ds1"}`):
  - `test_start_posts_run_records_run_and_webhook(db, settings)`: a `start` kwargs-ban 5 `startUrls`, `onlyPostsNewerThan == "2022-01-01"`, webhook `request_url` tartalmazza `token=hook-s`, az esemény-lista a 4 típus; `scrape_runs` sor `actor='posts'`, `kind='backfill'`, `status='started'`, `apify_run_id='run1'`.
  - `test_resolve_page_by_url_variants()`: `"https://facebook.com/kontigokontenerpark/"`, `"https://www.facebook.com/KontigoKontenerPark"` → `kontigo`; ismeretlen → None.

- [ ] **Step 2: Bukjon.** **Step 3: Implementáld.** **Step 4: PASS.**
- [ ] **Step 5: Commit** – `feat: start Apify runs with webhooks and record scrape_runs`

---

### Task 6: Posztok leképezése és upsert (+ Apify próbafutás)

**Files:**
- Create: `app/ingest/__init__.py`, `app/ingest/posts.py`, `scripts/probe_apify_posts.py`, `tests/fixtures/apify_posts_sample.json`, `tests/test_ingest_posts.py`

**Interfaces:**
- Produces:
  - `@dataclass PostRecord: page_id: int, fb_post_id: str, url: str, text: str | None, posted_at: datetime, media: list[dict], reactions_count: int, comments_count: int, shares_count: int, raw: dict`
  - `map_post_item(item: dict, pages: list[Page]) -> PostRecord` – `ValueError` ha nincs oldal-egyezés vagy nincs azonosító/dátum. Mező-forrás (a próbafutás alapján véglegesítendő; induló feltételezés): `postId`→`fb_post_id`, `url`, `text`, `time`→`posted_at` (ISO, UTC-re alakítva), `media` (lista; `thumbnail` vagy `url` kulccsal képek), `likes`→reactions, `comments`, `shares`, oldal: `facebookUrl` / `inputUrl` / `pageName` a `resolve_page`-nek.
  - `image_urls(media: list[dict]) -> list[str]` – legfeljebb 4 kép-URL.
  - `upsert_posts(conn, records: list[PostRecord], run_id: int) -> tuple[int, int]` – (új, frissített). Új sor: `analysis_status = 'pending'` ha `text` nem üres vagy `image_urls` nem üres, különben `'skipped'`; `first_seen_at = last_seen_at = now()`. Meglévő: csak `reactions_count, comments_count, shares_count, media, raw, last_seen_at, last_run_id` frissül, `analysis_status` érintetlen.

- [ ] **Step 1: Próbafutás és fixture** – `scripts/probe_apify_posts.py`: `.env`-ből token, `apify/facebook-posts-scraper` **szinkron** futtatása (`client.actor(...).call(run_input=...)`) a Kontigo oldalra `resultsLimit=5`, `onlyPostsNewerThan` = 90 nap, az összes itemet `tests/fixtures/apify_posts_sample.json`-be menti, és kiírja a top-level kulcsokat. Futtatás: `uv run python scripts/probe_apify_posts.py`. Valódi Apify-díj (filléres). A kiírt kulcsok alapján igazítsd a `map_post_item` mező-forrásait, ha eltérnek a fentitől, és írd be a végleges leképezést a `posts.py` modul-docstringjébe.

- [ ] **Step 2: Failing teszt** – `tests/test_ingest_posts.py`:
  - `test_map_fixture_items(db)`: a fixture minden itemje leképződik, `page_id` a kontigo oldalé, `posted_at` tz-aware, `fb_post_id` nem üres.
  - `test_map_missing_text_and_media_is_still_mapped()`: fixture első iteme `text=None, media=[]` módosítással → `PostRecord`, `text is None`.
  - `test_map_unknown_page_raises()`: `inputUrl`/`facebookUrl` lecserélve ismeretlenre → `ValueError`.
  - `test_map_missing_time_raises()`: a dátum-mező törölve → `ValueError`, az üzenetben a `postId`.
  - `test_upsert_new_then_update_keeps_status(db)`: első upsert → (1,0), státusz `pending`; ugyanaz `likes+10`-zel → (0,1), `reactions_count` nőtt, státusz még `pending`, `first_seen_at` változatlan.
  - `test_upsert_no_text_no_media_is_skipped(db)`: státusz `skipped`.

- [ ] **Step 3: Bukjon.** **Step 4: Implementáld.** **Step 5: PASS.**
- [ ] **Step 6: Commit** – `feat: map and upsert Facebook posts from Apify items`

---

### Task 7: Apify webhook végpont

**Files:**
- Create: `app/jobs/webhook.py`, `tests/test_webhook.py`
- Modify: `api/index.py`

**Interfaces:**
- Consumes: Task 5 `get_run_by_apify_id`, `mark_run`; Task 6 `map_post_item`, `upsert_posts`; Task 4 `continue_job`; Task 11 `map_comment_item`, `upsert_comments` (a komment-ág ebben a taskban `NotImplementedError`-t dob, Task 11 tölti ki).
- Produces:
  - `process_webhook(conn, settings, client: ApifyClient, payload: dict) -> dict` – `payload["runId"]` alapján run; nincs → `LookupError`; `status == 'processed'` → `{"skipped": true}`; `eventType != "ACTOR.RUN.SUCCEEDED"` → `mark_run(status='failed', error=eventType)`; siker: `client.dataset(payload["datasetId"]).iterate_items()` → `actor=='posts'`: map+upsert, a `ValueError`-os itemek számát és első 5 hibáját a run `error` mezőjébe írja (a run státusza `processed` marad, az `error` nem null jelzi a részleges hibát), `items_count`; majd `continue_job("/api/jobs/analyze")`. `actor=='comments'`: Task 11, majd `continue_job("/api/jobs/summarize", {"month": params["month"]})` vagy tartomány esetén hónaponként.
  - `POST /api/webhooks/apify` (`require_webhook`): JSON body → `process_webhook`; `LookupError` → 404.

- [ ] **Step 1: Failing teszt** – `tests/test_webhook.py` (TestClient + dependency override a `settings`-re és az Apify kliensre; `continue_job` monkeypatch-elve listába gyűjt):
  - `test_success_posts_run_ingests_and_continues(db)`: `scrape_runs` sor `apify_run_id='run1'`, mock dataset 3 fixture-item → 200, 3 poszt, run `processed`, `items_count==3`, `continue_job` hívva `/api/jobs/analyze`-zel.
  - `test_failed_event_marks_run_failed(db)`: `eventType="ACTOR.RUN.FAILED"` → run `failed`, nincs poszt.
  - `test_duplicate_delivery_is_noop(db)`: run már `processed` → 200 `{"skipped": true}`, posztszám változatlan.
  - `test_unknown_run_404(db)`: → 404.
  - `test_bad_token_401`.

- [ ] **Step 2: Bukjon.** **Step 3: Implementáld.** **Step 4: PASS.**
- [ ] **Step 5: Commit** – `feat: Apify webhook ingests finished runs and chains jobs`

---

### Task 8: Elemzési séma és prompt

**Files:**
- Create: `app/analysis/__init__.py`, `app/analysis/schema.py`, `app/analysis/prompt.py`, `prompts/post_extraction_v1.md`, `tests/test_analysis_schema.py`

**Interfaces:**
- Produces:
  - `PostAnalysis(BaseModel)` – a spec 3. fejezetének mezői `Literal[...]` enumokkal; `arak: list[Ar]` ahol `Ar(meret: str | None, ertek: int, egyseg: Literal["ft_ho","ft_m2_ho","ft_nap","egyeb"])`; `konfidencia: int` (1–5, `Field(ge=1, le=5)`); `kulcsszavak: list[str]`.
  - `normalize(a: PostAnalysis) -> PostAnalysis` – ha `arak` nem üres: `ar_megjelenik=True`, `ar_ertek=min(ertek)`, `ar_egyseg` a minimumhoz tartozó; ha üres: `ar_megjelenik=False`, `ar_ertek=None`, `ar_egyseg=None`. Lista-enumoknál, ha `nincs` mellett más érték is van, a `nincs` kiesik.
  - `load_prompt_file(name: str) -> str` – `prompts/{name}.md` tartalma; `FileNotFoundError` ha nincs.
  - `load_prompt(version: str) -> str` – `load_prompt_file(f"post_extraction_{version}")`.
  - `build_messages(*, page_name: str, posted_at: datetime, text: str | None, image_urls: list[str]) -> list[dict]` – egy user üzenet: szöveges blokk (oldal, dátum Budapest szerint, poszt szövege vagy „(nincs szöveg)”), majd minden képhez `{"type":"image","source":{"type":"url","url":...}}`.
  - `prompts/post_extraction_v1.md`: magyar rendszerprompt a spec 7. fejezete szerint – piaci kontextus, minden mező definíciója 1–2 példával (konkrét use-case ≠ kategória; „nincs hol tartani a téli gumit” jó, „kerti bútor” rossz), képek szövegének olvasása (ár, méret, akció), `kontener_meretek` a posztban szereplő formában, `cta_szoveg` szó szerint, `konfidencia` skála jelentése, hiányzó információnál `nincs`/`nem_megallapithato`/üres lista.

- [ ] **Step 1: Failing teszt**
  - `test_schema_rejects_unknown_enum()`: `celkozonseg="ceg"` → `ValidationError`.
  - `test_normalize_derives_price_from_list()`: `arak=[{"meret":"20 láb","ertek":45000,"egyseg":"ft_ho"},{"meret":"9 láb","ertek":25000,"egyseg":"ft_ho"}]`, `ar_ertek=None`, `ar_megjelenik=False` → `ar_megjelenik True`, `ar_ertek 25000`, `ar_egyseg "ft_ho"`.
  - `test_normalize_drops_nincs_when_other_values()`: `bizalmi_elemek=["nincs","kamera"]` → `["kamera"]`.
  - `test_build_messages_has_text_then_images()`: 2 URL → 1 user üzenet, 3 blokk, az első `text`, a következők `image` `url` forrással; a szöveg tartalmazza az oldal nevét és a `2026-09-15` dátumot.
  - `test_prompt_v1_loads_and_mentions_fields()`: `load_prompt("v1")` tartalmazza `celkozonseg`, `szezonalis_horog`, `konfidencia`.

- [ ] **Step 2: Bukjon.** **Step 3: Implementáld (séma, normalizálás, prompt fájl).** **Step 4: PASS.**
- [ ] **Step 5: Commit** – `feat: post analysis schema, normalization and v1 extraction prompt`

---

### Task 9: Kinyerő és `analyze` job

**Files:**
- Create: `app/analysis/extractor.py`, `app/analysis/job.py`, `tests/test_extractor.py`, `tests/test_analyze_job.py`
- Modify: `api/index.py`

**Interfaces:**
- Consumes: Task 8; Task 4 `TimeBudget`, `acquire_lock`, `continue_job`; Task 6 `image_urls`.
- Produces:
  - `HAIKU = "claude-haiku-5-5"`, `SONNET = "claude-sonnet-5-5"` (ide, `app/analysis/extractor.py`-ba; a summary modulok innen importálják).
  - `@dataclass ExtractionResult: analysis: PostAnalysis, model: str, input_tokens: int, output_tokens: int, notes: str | None, raw: dict`
  - `analyze_post(client: anthropic.Anthropic, *, system_prompt: str, page_name, posted_at, text, image_urls) -> ExtractionResult` – `client.messages.parse(model=HAIKU, max_tokens=4096, system=[{"type":"text","text":system_prompt,"cache_control":{"type":"ephemeral"}}], messages=build_messages(...), output_format=PostAnalysis, output_config={"effort":"low"})`; `normalize()` a `parsed_output`-ra. Ha a hívás `anthropic.BadRequestError`-t dob és az üzenet képre utal (`"image"` vagy `"url"` szerepel benne), újrahív `image_urls=[]`-lel, `notes="kép nélkül elemezve: <hiba>"`.
  - `run_analyze_job(conn, settings, client, budget: TimeBudget, *, prompt_version: str, batch_size: int = 10) -> dict` – zár `JOB_ANALYZE`; ciklus amíg nincs expired: `SELECT ... FROM posts WHERE analysis_status='pending' ORDER BY posted_at DESC LIMIT batch_size FOR UPDATE SKIP LOCKED`; posztonként `analyze_post`, `INSERT post_analyses ... ON CONFLICT (post_id, prompt_version) DO UPDATE`, `analysis_status='done'`; kivételnél `analysis_attempts += 1`, `analysis_error=str(e)[:500]`, és ha `attempts >= 3` → `failed`, különben marad `pending` (de a ciklus ebben a futásban nem veszi fel újra: a lekérdezés `AND id <> ALL(%s)` a már próbáltakra). Posztonként commit. Visszatérés `{"processed": n, "failed": m, "remaining": k}`; ha `remaining > 0` → `continue_job("/api/jobs/analyze")`. Zár feloldása `finally`.
  - `POST /api/jobs/analyze` (`require_admin`) → `run_analyze_job` a `settings.prompt_version`-nel; 200 a dict-tel; ha a zár foglalt → `{"locked": true}`.

- [ ] **Step 1: Failing teszt**
  - `test_extractor.py::test_analyze_post_parses_and_normalizes`: `client.messages.parse` mock, `parsed_output` egy `PostAnalysis` `arak`-kal és `ar_ertek=None`-nal, `usage.input_tokens=100` → `ExtractionResult.analysis.ar_ertek == 25000`, `model == "claude-haiku-5-5"`, a hívás kwargs-ában `output_format is PostAnalysis`, `output_config == {"effort": "low"}`.
  - `test_extractor.py::test_image_error_falls_back_to_text_only`: első hívás `BadRequestError`-t dob „Could not fetch image url” üzenettel (a `BadRequestError` példányosításához `httpx.Response(400)` + `body`), második siker → `notes` kezdődik `"kép nélkül elemezve"`, a második hívás `messages`-ében nincs `image` blokk.
  - `test_analyze_job.py::test_processes_pending_and_marks_done(db, settings)`: 2 pending poszt, mock kliens → 2 `post_analyses` sor `prompt_version='v1'`, státusz `done`, `remaining == 0`, `continue_job` nem hívva.
  - `test_analyze_job.py::test_failure_increments_attempts_then_fails(db)`: mock mindig dob; 3 futás után `analysis_status='failed'`, `analysis_attempts==3`.
  - `test_analyze_job.py::test_budget_expired_continues(db)`: `TimeBudget(0)` 1 pending poszttal → `processed==0`, `remaining==1`, `continue_job` hívva.
  - `test_analyze_job.py::test_lock_held_returns_locked(db)`: előre vett zár → `{"locked": true}`, semmi feldolgozás.

- [ ] **Step 2: Bukjon.** **Step 3: Implementáld.** **Step 4: PASS.**
- [ ] **Step 5: Commit** – `feat: Haiku extraction and time-budgeted analyze job`

---

### Task 10: Napi cron, backfill, reanalyze végpontok

**Files:**
- Create: `app/jobs/backfill.py`, `tests/test_cron_daily.py`, `tests/test_backfill.py`
- Modify: `api/index.py`

**Interfaces:**
- Consumes: Task 5 `start_posts_run`, Task 4 `continue_job`, Task 3 `previous_month`, `months_between`; Task 13 `run_summarize_job` (a napi újraszámolás hívása ebben a taskban `continue_job("/api/jobs/summarize", {"month": ..., "stats_only": "1"})`, így nem függ Task 13 kódjától).
- Produces:
  - `GET /api/cron/daily` (`require_cron`): `start_posts_run(kind='daily', newer_than=today_budapest-14 nap, results_limit=200)`; `continue_job("/api/jobs/analyze")`; az aktuális és az előző Budapest-hónapra, plusz minden `stale_months(conn)` hónapra `continue_job("/api/jobs/summarize", {"month": "YYYY-MM", "stats_only": "1"})`. Válasz `{"run_id": ..}`.
  - `stale_months(conn) -> list[date]` – `SELECT DISTINCT month FROM monthly_summaries WHERE stale`.
  - `backfill_posts(conn, settings, client, since: date) -> int` – `start_posts_run(kind='backfill', results_limit=2000)`.
  - `reanalyze(conn, prompt_version: str) -> int` – `UPDATE posts SET analysis_status='pending', analysis_attempts=0 WHERE analysis_status IN ('done','failed') AND NOT EXISTS (post_analyses ezzel a verzióval)`; `UPDATE monthly_summaries SET stale=true`; visszaadja az érintett posztok számát.
  - `POST /api/jobs/backfill?since=YYYY-MM-DD` (`require_admin`) → `backfill_posts`.
  - `POST /api/jobs/reanalyze?prompt_version=vN` (`require_admin`) → `reanalyze` + `continue_job("/api/jobs/analyze")`. A `prompt_version`-hez léteznie kell a prompt fájlnak, különben 400.

- [ ] **Step 1: Failing teszt**
  - `test_cron_daily.py::test_daily_starts_run_and_chains(db)`: mock Apify, `continue_job` gyűjtve → `scrape_runs` 1 sor `kind='daily'`, `onlyPostsNewerThan` = 14 nappal korábbi dátum; `continue_job` hívások: `/api/jobs/analyze`, és két `/api/jobs/summarize` a jelen és előző hónapra `stats_only=1`-gyel; egy `stale=true` 2025-03 sorral még egy `summarize` 2025-03-ra.
  - `test_cron_daily.py::test_daily_requires_cron_secret` → 401.
  - `test_backfill.py::test_backfill_since(db)`: `?since=2022-01-01` → run `kind='backfill'`, `params.since=="2022-01-01"`.
  - `test_backfill.py::test_reanalyze_resets_posts_without_version(db)`: 2 `done` poszt v1 elemzéssel, 1 `skipped` → `reanalyze("v2")` → 2 pending, a skipped marad; `monthly_summaries` (1 seed sor) `stale=true`. `?prompt_version=v9` (nincs fájl) → 400.

- [ ] **Step 2: Bukjon.** **Step 3: Implementáld.** **Step 4: PASS.**
- [ ] **Step 5: Commit** – `feat: daily cron, backfill and reanalyze endpoints`

---

### Task 11: Kommentek leképezése, upsert, havi cron

**Files:**
- Create: `app/ingest/comments.py`, `scripts/probe_apify_comments.py`, `tests/fixtures/apify_comments_sample.json`, `tests/test_ingest_comments.py`, `tests/test_cron_monthly.py`
- Modify: `app/jobs/webhook.py` (komment-ág), `api/index.py`

**Interfaces:**
- Consumes: Task 5 `start_comments_run`, Task 3 `previous_month`, `month_bounds_utc`, Task 7 `process_webhook`.
- Produces:
  - `@dataclass CommentRecord: post_id: int, fb_comment_id: str, text: str | None, posted_at: datetime | None, likes_count: int, is_reply: bool, author_is_page: bool, raw: dict`
  - `map_comment_item(item: dict, posts_by_url: dict[str, dict], pages_by_id: dict[int, Page]) -> CommentRecord` – a poszt az item poszt-URL mezője (`facebookUrl` / `inputUrl`, URL normalizálva, query nélkül) alapján; `author_is_page` = a kommentelő neve (kisbetű, trim) == az oldal neve, vagy a profil-URL az oldal URL-je; `is_reply` = `threadingDepth > 0` vagy szülő-azonosító jelenléte. Mezőneveket a próbafutás véglegesíti, mint Task 6-ban.
  - `upsert_comments(conn, records) -> int` – `ON CONFLICT (fb_comment_id) DO UPDATE SET text, likes_count, raw, scraped_at`.
  - `posts_in_month(conn, month: date, page_id: int | None = None) -> list[dict]` – `id, url, page_id` a `month_bounds_utc` közé eső `posted_at`-tal.
  - `GET /api/cron/monthly` (`require_cron`): `m = previous_month()`; `urls = [p.url for p in posts_in_month(conn, m)]`; ha üres → `continue_job("/api/jobs/summarize", {"month": m})` közvetlenül; különben `start_comments_run(kind='monthly_comments', params={"month": "YYYY-MM"})`.
  - A webhook komment-ága: map+upsert, run `processed`, majd `continue_job("/api/jobs/summarize", {"month": params["month"]})`; tartománynál (`params` `from`/`to`) minden hónapra külön `continue_job`.

- [ ] **Step 1: Próbafutás** – `scripts/probe_apify_comments.py`: egy, a Task 6 fixture-ében szereplő poszt-URL-re `resultsLimit=20`, eredmény `tests/fixtures/apify_comments_sample.json`-be, kulcsok kiírva. **Ugyanitt költségbecslés:** a futás `client.run(run_id).get()` adataiból (`usageTotalUsd` vagy a `stats`) kiírja az egy posztra jutó díjat és a `SELECT count(*) FROM posts` alapján a teljes komment-backfill becsült árát. Ezt a számot a README „Komment-backfill költsége” szakaszába kell írni, és a megrendelő jóváhagyása kell a Task 14 teljes backfill-comments indításához.

- [ ] **Step 2: Failing teszt**
  - `test_ingest_comments.py::test_map_fixture_and_author_is_page(db)`: fixture + 1 beszúrt poszt → minden item leképződik; egy, a fixture-ben az oldal nevével módosított item `author_is_page True`.
  - `test_ingest_comments.py::test_upsert_idempotent(db)`: kétszer ugyanaz → `count(*) == len(records)`.
  - `test_cron_monthly.py::test_monthly_starts_comments_run_for_previous_month(db)`: 2 poszt előző hónapban, 1 a jelenlegiben → mock `start` 2 `startUrls`, `scrape_runs.params.month == "<előző hónap>"`.
  - `test_cron_monthly.py::test_monthly_without_posts_goes_straight_to_summarize(db)`.
  - `test_webhook.py::test_comments_run_ingests_and_continues_summarize(db)` (a Task 7 fájlba): komment-run → kommentek, `continue_job("/api/jobs/summarize", {"month": ...})`.

- [ ] **Step 3: Bukjon.** **Step 4: Implementáld.** **Step 5: PASS.**
- [ ] **Step 6: Commit** – `feat: comment ingestion and monthly comments cron`

---

### Task 12: Havi statisztikák SQL-ből

**Files:**
- Create: `app/summary/__init__.py`, `app/summary/stats.py`, `tests/test_summary_stats.py`, `tests/fixtures/seed_month.py`

**Interfaces:**
- Consumes: Task 3 `month_bounds_utc`.
- Produces:
  - `@dataclass SummaryStats` – a `monthly_summaries` statisztikai oszlopai: `post_count, comment_count, celkozonseg_breakdown: dict[str,int], ceges_szegmens_breakdown, lakossagi_szegmens_breakdown, szezonalis_horog_breakdown, top_use_cases: list[dict]` (`{"use_case": str, "count": int}`, top 3, azonos `use_case` szöveg kisbetű/trim után összevonva), `top_hook: str | None` (leggyakoribb nem-`nincs` horog), `avg_engagement_by_celkozonseg: dict[str,float]`, `avg_engagement_by_lakossagi`, `avg_engagement_by_ceges`, `best_post_id, best_post_url, best_post_engagement, worst_post_id, worst_post_url, worst_post_engagement` (null, ha nincs poszt), `price_stats: dict` (`{"posts_with_price": n, "by_unit": {"ft_ho": {"min":..,"max":..,"avg":..,"n":..}}}` az `arak` jsonb-ből kibontva), `ajanlat_mechanika_counts, bizalmi_elemek_counts, surgosseg_counts: dict[str,int]`.
  - `compute_stats(conn, page_id: int | None, month: date) -> SummaryStats` – a `posts_current` nézetből; `page_id None` = minden aktív oldal. A szegmens-bontások és átlagok csak `done` posztokból; `post_count` minden posztból (`skipped`, `pending` is); `comment_count` a hónap posztjainak `comments` sorai.
  - `tests/fixtures/seed_month.py::seed(conn, *, page_slug, month, posts: list[dict])` – posztok + elemzések beszúrása tesztekhez (mezők felülírhatók, alap: `celkozonseg="maganszemely"`, `engagement`-hez `reactions_count` adott).

- [ ] **Step 1: Failing teszt**
  - `test_breakdowns_and_top_use_cases(db)`: kontigo 2026-09: 3 done poszt (2 `csaladi_haz` „kerti bútor tárolása” 10/30 reakció, 1 `panel` 20), 1 pending, 1 skipped → `post_count 5`, `lakossagi_szegmens_breakdown == {"csaladi_haz": 2, "panel": 1}`, `top_use_cases[0] == {"use_case": "kerti bútor tárolása", "count": 2}`, `avg_engagement_by_lakossagi == {"csaladi_haz": 20.0, "panel": 20.0}`, a skipped poszt 50 reakcióval → `best_post_engagement 50` (best/worst az összes hónapbeli posztból megy, elemzés nélkül is), `worst_post_engagement 10`.
  - `test_empty_month(db)`: `post_count 0`, `best_post_id None`, bontások `{}`.
  - `test_single_post_best_equals_worst(db)`.
  - `test_all_pages_aggregates_across_pages(db)`: kontigo 1 + raktar24 1 poszt → `compute_stats(None, m).post_count == 2`.
  - `test_price_stats_from_arak(db)`: két poszt `arak` 25000/45000 `ft_ho` és 3000 `ft_m2_ho` → `price_stats["by_unit"]["ft_ho"] == {"min":25000,"max":45000,"avg":35000.0,"n":2}`, `posts_with_price 2`.
  - `test_month_boundary_budapest(db)`: 2026-09-30 22:30 UTC poszt októberbe számít.

- [ ] **Step 2: Bukjon.** **Step 3: Implementáld.** **Step 4: PASS.**
- [ ] **Step 5: Commit** – `feat: monthly summary statistics from SQL`

---

### Task 13: Komment-klaszterezés, egy mondat, `summarize` job

**Files:**
- Create: `app/summary/comments_llm.py`, `app/summary/sentence.py`, `app/summary/job.py`, `prompts/comment_clustering_v1.md`, `prompts/monthly_sentence_v1.md`, `tests/test_summary_llm.py`, `tests/test_summarize_job.py`
- Modify: `api/index.py`

**Interfaces:**
- Consumes: Task 12 `compute_stats`, Task 9 `HAIKU`, `SONNET`, Task 4 runner, Task 11 `posts_in_month`.
- Produces:
  - `CommentInsights(BaseModel)`: `top_questions: list[Insight]`, `top_objections: list[Insight]`, `Insight(text: str, count: int, example: str)`; legfeljebb 3-3 (`Field(max_length=3)`).
  - `cluster_comments(client, *, comments: list[dict], page_name: str) -> tuple[CommentInsights, dict]` – `messages.parse(model=SONNET, max_tokens=4096, output_format=CommentInsights, output_config={"effort":"medium"}, system=load_prompt_file("comment_clustering_v1"), messages=[user: a kommentek sorszámozva, az oldal saját válaszai `[OLDAL]` előtaggal])`; második elem a usage dict. Ha `len(comments) < 3` → üres `CommentInsights`, hívás nélkül (a hívó dönti el, a függvény `ValueError`-t dob 3 alatt).
  - `summary_sentence(client, *, page_name: str | None, month: date, stats: SummaryStats, insights: CommentInsights | None) -> tuple[str, dict]` – `messages.create(model=HAIKU, max_tokens=300, output_config={"effort":"low"}, system=monthly_sentence_v1, messages=[user: a stats JSON + oldal/hónap])`; `page_name None` → a prompt szerint „A piac …” alany. `post_count == 0` esetén hívás nélkül, fix sablonnal: `f"{page_name}: nem volt poszt {év} {hónapnév} hónapban."` (összesítésnél `page_name` = „A piac”; `HUN_MONTHS` lista a hónapnevekhez). A spec „-nek nem volt posztja” alakja helyett ez a ragozásmentes sablon az elfogadott egyszerűsítés.
  - `run_summarize_job(conn, settings, client, budget, *, month: date, page_slug: str | None = None, stats_only: bool = False) -> dict` – zár `JOB_SUMMARIZE`; célok: adott hónapra minden aktív oldal + `None` (összesített), vagy csak `page_slug`. Egy célra: `stats = compute_stats`; `insights`: ha `stats_only` → a meglévő sor `top_questions/top_objections`-e; különben ha `comments_computed_at` null vagy `< max(comments.scraped_at)` a hónap posztjaira és `comment_count >= 3` → `cluster_comments`, `comments_computed_at=now()`; különben meglévő. `summary_sentence` mindig újra (olcsó). Upsert `monthly_summaries` `ON CONFLICT (page_id, month)`, `stats_computed_at=now()`, `stale=false`, `prompt_version=settings.prompt_version`, `summary_model=HAIKU`, `comments_model=SONNET` (ha futott). Időkeret lejártakor `continue_job("/api/jobs/summarize", {"month": ..., "stats_only": ...})`.
  - `POST /api/jobs/summarize?month=YYYY-MM[&page=slug][&stats_only=1]` (`require_admin`).

- [ ] **Step 1: Failing teszt**
  - `test_summary_llm.py::test_cluster_comments_marks_page_replies_and_limits_three`: mock parse → a user üzenetben az `author_is_page` komment `[OLDAL]` előtaggal; 4 elemű lista a mockban → `ValidationError` (max 3).
  - `test_summary_llm.py::test_cluster_requires_three_comments` → `ValueError`.
  - `test_summary_llm.py::test_sentence_zero_posts_no_llm`: `post_count 0` → mock nem hívva, a mondat tartalmazza az oldal nevét és `2026 szeptember`-t.
  - `test_summary_llm.py::test_sentence_calls_haiku_low_effort`.
  - `test_summarize_job.py::test_creates_rows_for_each_page_and_all(db)`: 5 oldal + összesített = 6 sor, `page_id IS NULL` sor létezik, `stale false`.
  - `test_summarize_job.py::test_comments_clustering_only_when_new_comments(db)`: első futás 3 kommenttel → Sonnet mock 1× hívva; második futás új komment nélkül → 1× marad (nem hívta újra); új komment beszúrása (`scraped_at` later) → 2×.
  - `test_summarize_job.py::test_stats_only_skips_sonnet(db)`.
  - `test_summarize_job.py::test_upsert_overwrites_existing(db)`: módosított engagement után újrafutás → `best_post_engagement` frissül, sorok száma változatlan.

- [ ] **Step 2: Bukjon.** **Step 3: Implementáld (prompt fájlok is: a klaszterező prompt kéri a normalizált megfogalmazást, előfordulás-számot és szó szerinti példát, kérdés vs. kifogás definícióval; a mondat-prompt a spec példamondatát adja mintának és „A piac …” alanyt kér összesítésnél).** **Step 4: PASS.**
- [ ] **Step 5: Commit** – `feat: comment clustering, monthly sentence and summarize job`

---

### Task 14: Komment-backfill, README, deploy

**Files:**
- Create: `README.md`, `docs/sql/everyday_queries.sql`
- Modify: `app/jobs/backfill.py`, `api/index.py`, `.env.example`

**Interfaces:**
- Consumes: Task 5 `start_comments_run`, Task 11 `posts_in_month`, Task 3 `months_between`.
- Produces:
  - `backfill_comments(conn, settings, client, *, from_month: date, to_month: date) -> list[int]` – hónaponként egy `start_comments_run(kind='backfill_comments', params={"from": .., "to": .., "month": "YYYY-MM"})` azokra a hónapokra, ahol van poszt; visszaadja a run id-kat. (Hónaponkénti futás = a webhook hónaponként indítja a `summarize`-t, nincs külön tartomány-logika.)
  - `POST /api/jobs/backfill-comments?from=YYYY-MM&to=YYYY-MM` (`require_admin`).
  - `POST /api/jobs/summarize-range?from=YYYY-MM&to=YYYY-MM` (`require_admin`) – minden hónapra `continue_job("/api/jobs/summarize", {"month": m})` (a nulla kommentes múltbeli hónapok sorai így is létrejönnek).

- [ ] **Step 1: Failing teszt** – `tests/test_backfill.py`-ba: `test_backfill_comments_one_run_per_month_with_posts(db)`: posztok 2026-07 és 2026-09 → 2 run, `params.month` `"2026-07"`, `"2026-09"`; `test_summarize_range_chains_each_month`.
- [ ] **Step 2: Bukjon.** **Step 3: Implementáld.** **Step 4: PASS.**

- [ ] **Step 5: README.md** – szakaszok: Mi ez (2 mondat + spec link); Helyi fejlesztés (`uv sync`, teszt-DB docker, `uv run pytest`, `uv run uvicorn api.index:app --reload`); Env változók táblája; Deploy Vercelre (`npx vercel link`, `npx vercel env add` minden változóra, Neon `DATABASE_URL` pooled, migráció: `uv run python db/migrate.py "$DATABASE_URL"`, Deployment Protection: a Vercel projekt beállításaiban Protection Bypass for Automation vagy a védelem kikapcsolása, mert az Apify webhook másképp 401-et kap; Hobby-megjegyzés a kereskedelmi használatról és hogy Pro-n a `JOB_TIME_BUDGET_SECONDS` emelhető); Kézi végpontok `curl` példákkal (`backfill`, `backfill-comments`, `summarize`, `summarize-range`, `reanalyze`, `analyze`); Első élesítés sorrendje (backfill posztok → várj, amíg `pending` 0 → próbafutás költségbecslés → jóváhagyás → backfill-comments → summarize-range 2022-01-től → cron él); Komment-backfill költsége (a Task 11 becslése); Backfill lefedettség (oldalanként legrégebbi poszt dátuma, a backfill után kitöltve). SQL-példák: `docs/sql/everyday_queries.sql` – havi szegmens-megoszlás oldalanként, árat tartalmazó posztok, failed/pending posztok, legutóbbi scrape_runs, havi összegző mondatok, top kérdések egy hónapra.

- [ ] **Step 6: `requirements.txt` frissítése** – `uv export --no-dev --no-hashes -o requirements.txt`; `uv run pytest -v` → minden PASS.

- [ ] **Step 7: Commit** – `feat: comment backfill, summarize-range, README and everyday SQL`

- [ ] **Step 8: Deploy és első élesítés (a megrendelővel együtt, valódi fiókok)** – a README sorrendje szerint: Neon DB + migráció; Vercel env; `npx vercel --prod`; `curl /api/health`; `backfill?since=2022-01-01`; figyelés SQL-ből (`scrape_runs`, `posts.analysis_status`); lefedettség beírása a README-be; komment-költségbecslés bemutatása; jóváhagyás után `backfill-comments` és `summarize-range`; cron-ok ellenőrzése a Vercel felületen. Ez a lépés kézi ellenőrzéssel zárul, nem automatizált teszttel.

---

## Verifikáció a terv végén

1. `uv run pytest -v` – minden teszt zöld a Docker teszt-DB-vel.
2. `uv run uvicorn api.index:app` lokálisan, `.env`-vel: `curl -H "X-Admin-Secret: $ADMIN_SECRET" -X POST "localhost:8000/api/jobs/analyze"` → `{"processed":0,...}` üres DB-n.
3. Élesben a backfill után SQL: `select analysis_status, count(*) from posts group by 1` → csak `done`/`skipped` (és indokolt `failed`); `select count(*) from monthly_summaries where month >= '2022-01-01'` = hónapok × 6.
4. Egy kézzel ellenőrzött minta: 10 poszt `posts_current`-ből, a `celkozonseg` és `ar_ertek` mezők szemrevételezve a poszt képével együtt (a golden set alapja; `tests/fixtures/golden_posts.json`-ba mentve a későbbi promptverziókhoz).
