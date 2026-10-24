# Facebook poszt-elemző

## Mi ez

Napi rendszerességgel futó, felügyelet nélküli pipeline egy magyar self-storage konténerpark-startup számára: öt működő versenytárs Facebook-oldalának posztjait és kommentjeit Apify-jal tölti le, Claude-dal strukturált mezőkre bontja (célközönség, fájdalompont, ár, ajánlat, bizalmi elemek stb.), és mindent egy Neon Postgres adatbázisba ír. Az eredményt SQL-ből lehet nézni, dashboard nincs.

- Spec: [`docs/superpowers/specs/2026-10-24-facebook-post-analyzer-design.md`](docs/superpowers/specs/2026-10-24-facebook-post-analyzer-design.md)
- Terv: [`docs/superpowers/plans/2026-10-24-facebook-post-analyzer.md`](docs/superpowers/plans/2026-10-24-facebook-post-analyzer.md)

## Architektúra röviden

```
heti:   cron/daily ──► Apify posts run (14 nap) ──► webhooks/apify ──► posts upsert ──► jobs/analyze ──► post_analyses
havi:   cron/monthly (1-jén) ──► Apify comments run (előző hónap) ──► webhooks/apify ──► comments upsert ──► mark_stale ──► jobs/summarize ──► monthly_summaries
```

- Semmi nem vár az Apify-ra: a cron elindítja a futást és webhookot regisztrál, az Apify a végén hívja a `POST /api/webhooks/apify` végpontot.
- Minden hosszabb munka (`analyze`, `summarize`) időkeretes (`JOB_TIME_BUDGET_SECONDS`), egy `job_locks` zárral egyszerre csak egy példány fut belőle, és ha maradt munka, a job egy aszinkron HTTP-hívással önmagát folytatja. Minden lépés idempotens (upsert).
- A `summarize` egy **sor-alapú várólista**: aki egy hónap összegzését frissíteni akarja (heti cron, komment-webhook, `summarize-range`, havi cron poszt nélküli hónapra), az a `monthly_summaries` érintett sorait `stale = true`-ra állítja (a hiányzó sorokat üres helyőrzőként létrehozza), majd **egyetlen** paraméter nélküli `POST /api/jobs/summarize` hívást küld. Ez a `stale` sorokat hónap szerint sorban dolgozza fel, időkeret végén önmagát folytatja. Egy sor `stale` marad, ha a célpont hibázott, vagy ha a hónapban még van `pending` poszt; ezeket a következő heti drain veszi újra.
- Három LLM-használat:
  1. poszt-kinyerés (szöveg + képek, `messages.parse`): `claude-haiku-5-5`, `effort=low`
  2. komment-klaszterezés (top kérdések/kifogások, havonta oldalanként): `claude-sonnet-5-5`, `effort=medium`
  3. egy mondatos havi összefoglaló: `claude-haiku-5-5`, `effort=low`
- Minden job és webhook egy JSON-soros logot ír a Vercel logba (`job`, `run_id`, feldolgozott darabszám, `elapsed_s`).

## Helyi fejlesztés

```bash
uv sync
cp .env.example .env            # majd töltsd ki az értékeket
docker compose -f docker-compose.test.yml up -d   # teszt Postgres (localhost:55432)
uv run pytest -v
uv run uvicorn api.index:app --reload
```

A `.env` valódi titkokat tartalmaz, soha nem kerül commitba.

## Környezeti változók

| Változó | Jelentés | Alapérték |
|---|---|---|
| `APIFY_API_TOKEN` | Apify API token | (kötelező) |
| `ANTHROPIC_API_KEY` | Anthropic API kulcs | (kötelező) |
| `DATABASE_URL` | Neon Postgres kapcsolat, **pooled** connection string | (kötelező) |
| `CRON_SECRET` | A Vercel cron ezt küldi `Authorization: Bearer ...` fejlécben a `/api/cron/*` végpontokra | (kötelező) |
| `APIFY_WEBHOOK_SECRET` | A webhook URL `token` paramétere | (kötelező) |
| `ADMIN_SECRET` | A kézi `/api/jobs/*` végpontok `X-Admin-Secret` fejléce | (kötelező) |
| `APP_BASE_URL` | A telepített app alap-URL-je (pl. `https://valami.vercel.app`), az önfolytatás és a webhook-URL ebből készül | üres |
| `JOB_TIME_BUDGET_SECONDS` | Egy job-hívás időkerete másodpercben. Legyen legalább 140: az `analyze` 65 s, a `summarize` 130 s tartalék alatt nem kezd új tételbe | `200` |
| `PROMPT_VERSION` | A poszt-kinyerés aktív promptverziója (`prompts/post_extraction_<verzió>.md`) | `v1` |
| `VERCEL_PROTECTION_BYPASS` | Opcionális: a Vercel "Protection Bypass for Automation" titka. Ha meg van adva, az önfolytató hívások `x-vercel-protection-bypass` fejlécben, az Apify webhook URL-je `&x-vercel-protection-bypass=...` paraméterben küldi | üres |
| `TEST_DATABASE_URL` | Csak teszthez: a docker teszt-DB | `postgresql://postgres:postgres@localhost:55432/fpa_test` |

## Deploy Vercelre

```bash
npx vercel link
# minden változóra (a TEST_DATABASE_URL kivételével):
npx vercel env add DATABASE_URL production     # Neon pooled connection string
npx vercel env add ADMIN_SECRET production     # ... és így tovább
npx vercel --prod

# migráció (egyszer, és minden új db/migrations/*.sql után)
uv run python db/migrate.py "$DATABASE_URL"
curl "$APP_BASE_URL/api/health"
```

- **Deployment Protection:** a Standard Protection a generált `*.vercel.app` production URL-t **is** védi, így az Apify webhookja és az önfolytató `continue_job` hívások 401-et kapnának, és a lánc csendben megállna. Három lehetőség közül egyet válassz:
  1. saját production domain (custom domain) legyen az `APP_BASE_URL`, mert azt a Standard Protection nem védi; vagy
  2. kapcsold ki a Deployment Protectiont a projektre; vagy
  3. hozz létre egy "Protection Bypass for Automation" titkot (Project Settings, Deployment Protection), és állítsd be `VERCEL_PROTECTION_BYPASS`-ként: az app ezt fejlécben küldi a saját hívásaihoz, és a webhook URL-be is beleteszi.
- **Cron-ok** (`vercel.json`, UTC): `/api/cron/daily` hetente, hétfőn 04:00 (`0 4 * * 1`), `/api/cron/monthly` minden hónap 1-jén 04:30. A heti ütem az Apify-költség miatt van: minden futás actor-indítási és szűrődíjat is fizet, a 14 napos ablak pedig hetente is kétszeres átfedést ad, így poszt nem marad ki. (A végpont neve kompatibilitás miatt maradt `daily`.)
- **Hobby csomag:** a cron pontossága ±1 óra; a `maxDuration: 300` Hobby csomagon csak bekapcsolt **Fluid compute** mellett érvényes (Project Settings, Functions); a `JOB_TIME_BUDGET_SECONDS` alapértéke 200. A Hobby csomag **kereskedelmi használatra nem engedélyezett**, éles használathoz Pro kell. Pro-n a `JOB_TIME_BUDGET_SECONDS` emelhető. A zár lejárata (330 s) a `maxDuration` felett van; ha a `maxDuration`-t emeled, a `acquire_lock` `ttl_seconds` alapértékét is emeld.

## Kézi végpontok

| Végpont | Auth | Mit csinál |
|---|---|---|
| `GET /api/health` | nincs | életjel |
| `GET /api/cron/daily` | `Authorization: Bearer $CRON_SECRET` | poszt-scrape az utolsó 14 napra + `analyze` + az előző és az aktuális hónap `stale`-re állítása + egy `summarize` drain |
| `GET /api/cron/monthly` | `Authorization: Bearer $CRON_SECRET` | komment-scrape az előző hónap posztjaira (poszt nélküli hónapnál: `stale` + drain) |
| `POST /api/webhooks/apify?token=...` | `token` = `APIFY_WEBHOOK_SECRET` | Apify webhook, a futás eredményét dolgozza fel |
| `POST /api/jobs/analyze` | `X-Admin-Secret` | `pending` posztok elemzése |
| `POST /api/jobs/summarize` | `X-Admin-Secret` | **drain**: az összes `stale` `monthly_summaries` sor feldolgozása, önfolytató |
| `POST /api/jobs/summarize?month=YYYY-MM[&page=slug][&stats_only=1]` | `X-Admin-Secret` | egy hónap kézi összegzése (a `month` opcionális, a `page` csak `month`-tal együtt) |
| `POST /api/jobs/summarize-range?from=YYYY-MM&to=YYYY-MM` | `X-Admin-Secret` | a tartomány összes hónapját `stale`-re állítja (hiányzó sorokat létrehoz), majd egy drain-t indít |
| `POST /api/jobs/backfill?since=YYYY-MM-DD` | `X-Admin-Secret` | poszt-backfill az összes oldalra |
| `POST /api/jobs/backfill-comments?from=YYYY-MM&to=YYYY-MM` | `X-Admin-Secret` | komment-scrape hónaponként egy futásban, azokra a hónapokra, ahol van poszt |
| `POST /api/jobs/reanalyze?prompt_version=vN` | `X-Admin-Secret` | az adott verzióval nem elemzett posztok visszaállítása `pending`-re (csak ha `vN` = a telepített `PROMPT_VERSION`) |

```bash
H="X-Admin-Secret: $ADMIN_SECRET"
curl -X POST -H "$H" "$APP_BASE_URL/api/jobs/analyze"
curl -X POST -H "$H" "$APP_BASE_URL/api/jobs/summarize"                 # drain: minden stale sor
curl -X POST -H "$H" "$APP_BASE_URL/api/jobs/summarize?month=2026-09"
curl -X POST -H "$H" "$APP_BASE_URL/api/jobs/summarize?month=2026-09&page=kontigo&stats_only=1"
curl -X POST -H "$H" "$APP_BASE_URL/api/jobs/summarize-range?from=2022-01&to=2026-09"
curl -X POST -H "$H" "$APP_BASE_URL/api/jobs/backfill?since=2022-01-01"
curl -X POST -H "$H" "$APP_BASE_URL/api/jobs/backfill-comments?from=2022-01&to=2026-09"
# előbb PROMPT_VERSION=v2 a Vercel env-ben és redeploy, különben 400:
curl -X POST -H "$H" "$APP_BASE_URL/api/jobs/reanalyze?prompt_version=v2"
# cron kézzel:
curl -H "Authorization: Bearer $CRON_SECRET" "$APP_BASE_URL/api/cron/daily"
```

## Első élesítés sorrendje

1. Neon adatbázis létrehozása, `uv run python db/migrate.py "$DATABASE_URL"`; Vercel env változók; `npx vercel --prod`; Deployment Protection kezelése (lásd Deploy: custom domain, kikapcsolás vagy `VERCEL_PROTECTION_BYPASS`); `curl "$APP_BASE_URL/api/health"`.
2. Poszt-backfill: `POST /api/jobs/backfill?since=2022-01-01`. A webhook feldolgozza, az `analyze` önfolytatva végigmegy.
3. Várj, amíg a `pending` 0 (SQL): `select analysis_status, count(*) from posts group by 1;` (csak `done`/`skipped`, indokolt esetben `failed` maradhat). Figyelés: `scrape_runs` (lásd `docs/sql/everyday_queries.sql`).
4. Töltsd ki a "Backfill lefedettség" táblát az ott található SQL-lel.
5. Komment-költségbecslés: `uv run python scripts/probe_apify_comments.py` (a költés `max_total_charge_usd=0.50`-nel korlátozva), a számolás a következő szakaszban. Mutasd be a megrendelőnek.
6. **Jóváhagyás után**: `POST /api/jobs/backfill-comments?from=2022-01&to=<utolsó lezárt hónap>`. Hónaponként egy Apify futás indul, a webhook a hónapot `stale`-re állítja és elindítja a `summarize` drain-t.
7. Várd meg, amíg minden komment-futás feldolgozódik: `select status, count(*) from scrape_runs where kind='backfill_comments' group by 1;` (csak `processed`, esetleg `failed` maradjon; `started`/`processing` esetén lásd "Ha egy run `started`/`failed`/`processing` marad"). Utána `POST /api/jobs/summarize-range?from=2022-01&to=<utolsó lezárt hónap>`: a nulla kommentes és nulla posztos hónapokra is létrejönnek a sorok. Ellenőrzés: `select count(*) from monthly_summaries where month >= '2022-01-01';` = hónapok × 6 (5 oldal + az összesítő sor), és `select count(*) from monthly_summaries where stale;` = 0 (a drain több hívásban, önfolytatva megy végig).
8. Cron-ok ellenőrzése a Vercel felületén (Settings, Cron Jobs): a heti és a havi cron látszik, és az első futás után a `scrape_runs` tartalmaz új sort.

## Ha egy run `started`/`failed`/`processing` marad

Ha az Apify webhook nem érkezett meg (pl. Deployment Protection 401, Vercel timeout), a `scrape_runs` sor `started`, `failed` vagy `processing` állapotban marad (lásd `docs/sql/everyday_queries.sql` 9. lekérdezés). A webhook kézzel újraküldhető:

```bash
curl -X POST "$APP_BASE_URL/api/webhooks/apify?token=$APIFY_WEBHOOK_SECRET" \
  -H 'content-type: application/json' \
  -d '{"runId":"<apify run id>","datasetId":"<dataset id>","eventType":"ACTOR.RUN.SUCCEEDED"}'
```

- Az `<apify run id>` a `scrape_runs.apify_run_id`. A `<dataset id>` az Apify konzolban található: Actors, a futás megnyitása (Runs), a Storage fülön a "Dataset" azonosítója (vagy a futás részleteinél a `defaultDatasetId`).
- Csak akkor küldd `ACTOR.RUN.SUCCEEDED`-del, ha a futás az Apify konzol szerint valóban sikeres volt.
- A `processing` állapotú run-t a webhook kihagyja (`{"skipped": true, "reason": "in_progress"}`). Ha egy `processing` run ~10 percnél régebbi (a feldolgozás biztosan meghalt), előbb állítsd vissza: `update scrape_runs set status = 'started' where id = <id> and status = 'processing';`, majd küldd újra a webhookot. Az upsert idempotens, a már beírt sorok nem duplikálódnak.

## Komment-backfill költsége

- A `apify/facebook-comments-scraper` ára kommentenként **$0,0025** ($2,50 / 1000 komment) a mért `pricing_info` szerint.
- Felső becslés: 1500 poszt × 100 komment = 150 000 komment, ez ≈ **$375** ($2,50/1000-nél). A bolt oldalán feltüntetett $1,40/1000-es árral ≈ $210. A becslés feltevése: 100 komment/poszt és 1500 poszt összesen; a valós szám a poszt-backfill után pontosabban kiszámolható (`select count(*) from posts`).
- Az Apify fiók jelenleg **FREE** csomagon van ($5/hó platform-kredit), ez a komment-backfillre messze nem elég, fizetős Apify csomag kell. A teljes poszt-backfill (5 oldal × akár 1000–2000 poszt, ~$2–7/1000 poszt) is meghaladhatja a havi ingyenes keretet.
- **Login wall:** a próbafutás mind a 3 fixture-poszton egyetlen hibaelemet adott vissza: "No public data available without Facebook login or access rights". Ez azt jelenti, hogy a kommentek bejelentkezés nélkül elérhetetlenek lehetnek. Ezért a `app/ingest/comments.py` mezőleképezése a dokumentált sémát követi, valós adaton nem ellenőrzött, a `tests/fixtures/apify_comments_sample.json` kézzel épített. A költségbecslés ("$375") ezért egy felső korlát, nem garantált eredmény: a jóváhagyás előtt a próbafutást újra kell futtatni, és csak akkor érdemes a teljes backfillt indítani, ha valódi kommentek jönnek.

## Backfill lefedettség

A teljes backfill után töltendő ki.

| Oldal | legrégebbi elért poszt | posztszám |
|---|---|---|
| Raktar24 | TBD | TBD |
| Kontigo | TBD | TBD |
| KonténerTár | TBD | TBD |
| VNV konténer | TBD | TBD |
| ÉrdRaktár | TBD | TBD |

```sql
select p.name as oldal, min(po.posted_at) as legregebbi_poszt, count(po.id) as posztszam
from pages p
left join posts po on po.page_id = p.id
group by p.id, p.name
order by p.name;
```

A 2022-01-01 cél, nem garancia: az Apify actor a Facebook-oldalak visszagörgetésében korlátos (best effort). A `scrape_runs.params` is rögzíti a futás paramétereit.

## Ismert korlátok

- A posts actor ezeknél az oldalaknál **nem ad `comments` kulcsot**, ezért a `posts.comments_count` 0, az `engagement` = reactions + shares, amíg nincs jobb forrás. A tényleges komment-mennyiség a havi komment-futással a `comments` táblába és a `monthly_summaries.comment_count`-ba kerül.
- A Sonnet klaszterezés (oldal, hónap) párra legfeljebb a 300 legfrissebb kommentet használja.
- Nulla posztos hónap mondata sablon: "<Név>: nem volt poszt <év> <hónap> hónapban." (a spec szövegének elfogadott egyszerűsítése).
- A heti cron fail-fast: ha az Apify indítása hibázik, azon a héten az `analyze`/`summarize` folytatások kimaradnak (HTTP 500 a Vercel logban).
- Vercel Hobby: ±1 óra cron-pontosság, `maxDuration` 300 s (Fluid compute kell), `JOB_TIME_BUDGET_SECONDS` alapérték 200, a zár lejárata (330 s) maradjon a `maxDuration` felett. A Hobby nem kereskedelmi használatra való, éles üzemhez Pro kell (ott emelhető a `JOB_TIME_BUDGET_SECONDS`).
- A Deployment Protectiont kezelni kell (custom domain, kikapcsolás vagy `VERCEL_PROTECTION_BYPASS`), különben az Apify webhook és az önfolytatás 401-et kap; a `continue_job` ezt WARNING logsorral jelzi.
- A heti drain az `analyze`-zal párhuzamosan indul: amíg egy hónapban van `pending` poszt, a sorai `stale`-ek maradnak, és a következő heti drain számolja újra őket (legfeljebb egy hét késés).
- A komment-mezőleképezés valós adaton nem igazolt (lásd: login wall).

## Mindennapi SQL-lekérdezések

A [`docs/sql/everyday_queries.sql`](docs/sql/everyday_queries.sql) fájl 9 kommentált lekérdezést tartalmaz:

1. havi szegmens-megoszlás oldalanként (`monthly_summaries`)
2. árat tartalmazó posztok (oldal, dátum, `ar_ertek`, `ar_egyseg`, url)
3. hibás (`failed`) és függő (`pending`) posztok hibaüzenettel
4. legutóbbi `scrape_runs` (státusz, `items_count`, hiba)
5. havi összegző mondatok egy hónapra (minden oldal + "A piac")
6. top kérdések és kifogások egy (oldal, hónap) párra
7. `analysis_status` darabszámok
8. oldalanként a legrégebbi poszt és a posztszám (lefedettség)
9. elakadt láncok: `job_locks` sorok és 30 percnél régebben `started`/`processing` futások
