# Facebook versenytárs-poszt elemző – tervezési specifikáció

Dátum: 2026-10-24
Státusz: jóváhagyva (2026-10-24)
Következő lépés: implementációs terv a `superpowers:writing-plans` skill-lel.

---

## 1. Szándék és miért

A megrendelő self-storage konténerparkot indít (előjegyzés már fut, nyitás decemberig), és látni akarja, hogyan kommunikál öt már működő magyar versenytárs a Facebookon: kit céloznak, milyen fájdalompontra, milyen árral, ajánlattal és bizalmi elemekkel, és ez hogyan változik havonta, szezonálisan. Nincs látható telephelye, ezért a bizalmi/biztonsági üzenetek és az ősz-téli horgok különösen fontosak neki.

A rendszer egy naponta futó, felügyelet nélküli pipeline: Apify-jal letölti a posztokat, Claude-dal strukturált mezőket nyer ki belőlük, és mindent egy Neon Postgres adatbázisba ír. Az eredményeket a megrendelő SQL-lel nézi (Vercel → Neon → SQL). Dashboard nincs.

## 2. Célok, nem-célok, anti-célok

**Célok**
- Mind az öt versenytárs oldal összes posztja 2022-01-01-től, naponta frissítve, egy helyen.
- Minden poszthoz a 3. fejezetben felsorolt strukturált mezők, LLM-mel kinyerve, újrafuttatható promptverzióval.
- Havi, oldalankénti és összesített összegzés táblában, egy mondatos LLM-összefoglalóval.
- Nulla kézi beavatkozás a napi működésben.

**Nem-célok**
- Nincs UI, dashboard, e-mail riport.
- Nem elemezzük a saját oldalt, Instagramot, hirdetéskönyvtárat, Google-értékeléseket.
- Nem valós idejű: napi egy futás elég.
- Nem cél az Apify output 100%-os mezőszintű megőrzése táblaoszlopként; a nyers JSON-t eltesszük, a többi később bővíthető.

**Anti-célok (ami bukás lenne, még ha "működik")**
- Egy poszt több sorban szerepel, vagy a napi futás duplikál.
- A LLM mezőkben az enumokon kívüli értékek jelennek meg (SQL-ből használhatatlan lesz).
- A Vercel függvény timeoutol, és csendben elveszik egy nap adata.
- Az ár képen van, és a rendszer `ar_megjelenik=false`-t ír, mert csak a szöveget nézte.

## 3. Mit nyerünk ki egy posztból

A LLM minden posztból az alábbi mezőket adja vissza. Az enum-mezők kizárólag a felsorolt értékeket vehetik fel (ezt a Claude structured output kényszeríti ki, nem utólagos ellenőrzés).

| Mező | Típus | Értékek / megjegyzés |
|---|---|---|
| `celkozonseg` | enum | `maganszemely` / `vallalkozas` / `mindketto` / `nem_megallapithato` |
| `ceges_szegmens` | enum | `vendeglatas` / `logisztika` / `epitoipar` / `kereskedelem` / `szolgaltato` / `webshop` / `egyeb` / `nincs` |
| `lakossagi_szegmens` | enum | `csaladi_haz` / `panel` / `koltozo` / `felujito` / `motoros` / `sportolo` / `hobbi_gyujto` / `egyeb` / `nincs` |
| `use_case` | szöveg, 1 mondat | konkrét, pl. "téli gumi tárolása szezonon kívül" |
| `fajdalompont` | szöveg, 1 mondat | pl. "a panel tárolója tele" |
| `szezonalis_horog` | enum | `gumicsere` / `unnep` / `koltozesi_szezon` / `nyar_vege` / `felujitas` / `tanev` / `nincs` |
| `kontener_meretek` | lista[szöveg] | ahogy a posztban szerepel, pl. `["9", "20", "40"]` lábban vagy `["6 m2"]`; üres lista, ha nincs |
| `ar_megjelenik` | bool | |
| `arak` | lista[objektum] | minden a posztban szereplő ár külön: `{meret: "20 láb" vagy null, ertek: 45000, egyseg: "ft_ho"}`; üres lista, ha nincs ár |
| `ar_ertek` | szám vagy null | a legalacsonyabb ár az `arak` listából ("ártól"), SQL-kényelmi mező |
| `ar_egyseg` | enum vagy null | az `ar_ertek`-hez tartozó egység: `ft_ho` / `ft_m2_ho` / `ft_nap` / `egyeb` |
| `arhorgony` | enum | `garazs` / `raktar` / `masik_szolgaltato` / `nincs` |
| `ajanlat_mechanika` | lista[enum] | `kedvezmeny` / `elso_honap_ingyen` / `nincs_kaucio` / `rovid_minimum_ido` / `azonnal` / `nincs` |
| `surgosseg` | enum | `utolso_darabok` / `foglaltsag_szazalek` / `bovites` / `hataridos_akcio` / `nincs` |
| `bizalmi_elemek` | lista[enum] | `kamera` / `kerites` / `24h_belepes` / `sajat_lakat` / `orzott` / `szaraz` / `nincs` |
| `cta_tipus` | enum | `hivj` / `irj` / `kattints` / `foglalj` / `nincs` |
| `cta_szoveg` | szöveg | szó szerint a posztból, üres ha nincs |
| `kulcsszavak` | lista[szöveg] | 3–5 db |
| `konfidencia` | int 1–5 | mennyire biztos a célközönség-besorolás |

Az `ar_egyseg` listáját `ft_nap` és `egyeb` értékkel bővítettem a kértekhez képest (építő döntése), mert napi árazás és "ártól" típusú megfogalmazás előfordul; SQL-ben szűrhető. Az `arak` lista szintén az én kiegészítésem: egy poszt tipikusan méretenként több árat sorol, és ha csak egy számot tárolnánk, a `price_stats` később nem lenne bővíthető reanalyze nélkül. Az `ar_ertek`/`ar_egyseg` a kért alakban megmarad, a legalacsonyabb árral.

**Bemenet a LLM-nek:** a poszt szövege, a poszt dátuma (szezonalitáshoz), az oldal neve, és a poszt képei (legfeljebb 4, URL-ként átadva). A képek kötelezőek, mert konténerparkos posztoknál az ár és az akció gyakran csak a grafikán van. Ha egy kép URL nem tölthető be, a poszt kép nélkül megy újra, és ezt a `post_analyses.notes` jelzi.

**Kihagyás:** ha a posztnak se szövege, se képe (pl. csak megosztott link, vagy videó előnézet nélkül), `analysis_status='skipped'`, LLM-hívás nincs.

## 4. Oldalak

Egy `pages` táblába kerülnek, induló tartalom:

| slug | név | város | URL |
|---|---|---|---|
| `raktar24` | Raktar24 | Győr | https://www.facebook.com/raktar24.hu |
| `kontigo` | Kontigo | Szombathely | https://www.facebook.com/kontigokontenerpark |
| `kontenertar` | KonténerTár | Gödöllő | https://www.facebook.com/kontenertar |
| `vnv` | VNV konténer | Győr | https://www.facebook.com/vnvkontenerraktar |
| `erdraktar` | ÉrdRaktár | Érd | https://www.facebook.com/erdraktar |

Új oldal felvétele = új sor a táblában (`active=true`); a kód a táblából olvassa a listát.

## 5. Architektúra és futási modell

**Stack:** Python 3.12, Vercel Python serverless functions (`api/` mappa), Vercel Cron, Neon Postgres, Apify (`apify/facebook-posts-scraper`, `apify/facebook-comments-scraper`), Anthropic API (`claude-haiku-5-5`, `claude-sonnet-5-5`).

**Alapelv: semmi nem vár az Apify-ra.** Egy Vercel függvény legfeljebb néhány percet futhat, az Apify futás ennél hosszabb lehet. Ezért minden scrape két lépés:
1. egy függvény elindítja az Apify futást, és webhookot regisztrál rá (`ACTOR.RUN.SUCCEEDED`, `ACTOR.RUN.FAILED`, `ACTOR.RUN.ABORTED`, `ACTOR.RUN.TIMED_OUT`), majd `scrape_runs` sort ír és kilép;
2. az Apify a futás végén meghívja a webhook függvényt, ami letölti a dataset-et és feldolgozza.

**Alapelv: minden hosszabb munka időkeretes, önmagát folytató job.** A LLM-elemzés, a komment-feldolgozás és az összegzés-számítás egy közös mintát követ: a függvény addig dolgoz fel elemeket, amíg el nem ér egy időkeretet (a Vercel limit kb. 70%-a, `JOB_TIME_BUDGET_SECONDS`-szel állítható), majd ha maradt munka, egy aszinkron HTTP-hívással újraindítja önmagát. Minden lépés idempotens (upsert), így egy megszakadt futás nem okoz duplikációt, csak késést. Egy időben egy job-típusból csak egy példány fusson: a job indulásnál egy `job_locks` sorral (lejárati idővel) zárat vesz, hogy a webhook és a cron által egyszerre indított `analyze` ne dolgozza fel kétszer ugyanazt.

**Vercel csomag: Hobby, a megrendelő saját fiókjában** (Vercel, Neon, Apify egyaránt). Következmények: a napi cron pontossága ±1 óra, ami itt nem számít; az időkeret alapértéke a Hobby futásidő-limitre méretezve; a webhook-URL-t a Deployment Protection alól ki kell venni (bypass vagy kikapcsolás az adott útvonalra), különben az Apify hívása 401-et kap. A README jelzi, hogy a Hobby csomag kereskedelmi használatra nem engedélyezett, és hogy Pro-ra váltásnál csak a `JOB_TIME_BUDGET_SECONDS` értéket kell emelni.

### 5.1 Végpontok

Mindegyik `api/` alatt. A cron-végpontokat a Vercel által küldött `CRON_SECRET` bearer védi, a webhookot URL-ben átadott `APIFY_WEBHOOK_SECRET`, a kézi job-okat `ADMIN_SECRET` header.

| Végpont | Trigger | Mit csinál |
|---|---|---|
| `GET /api/cron/daily` | Vercel cron, naponta egyszer (reggel, Budapest idő szerint) | Elindít egy poszt-scrape futást mind az aktív oldalra, `onlyPostsNewerThan` = ma − 14 nap. Utána meghívja az `analyze` job-ot a korábbról maradt `pending` posztokra (biztonsági háló). |
| `POST /api/webhooks/apify` | Apify webhook | A `scrape_runs` sorból tudja, milyen típusú futás volt (posts/comments). Letölti a dataset-et, upsert-eli a posztokat vagy kommenteket, lezárja a run-t, majd elindítja a következő job-ot (`analyze`, ill. `summarize`). Hiba esetén a run `failed` státuszt és hibaüzenetet kap. |
| `POST /api/jobs/analyze` | webhook / cron / kézi | `analysis_status='pending'` posztok feldolgozása Haikuval az aktuális promptverzióval, időkeretben, önfolytató. |
| `GET /api/cron/monthly` | Vercel cron, minden hónap 1-jén | Az előző naptári hónap (Budapest idő) posztjaira elindít egy komment-scrape futást (összes oldal egy futásban), a run-ban eltárolva a célhónapot. |
| `POST /api/jobs/summarize` | komment-webhook / kézi (`?month=YYYY-MM`, opcionálisan `&page=slug`) | Kiszámolja és upsert-eli a `monthly_summaries` sorokat az adott hónapra: minden aktív oldalra egy sor + egy összesített sor. Időkeretes, önfolytató. |
| `POST /api/jobs/backfill` | kézi, egyszer | `?since=2022-01-01`: poszt-scrape futás az összes oldalra a megadott dátumtól. A webhook ugyanúgy feldolgozza, mint a napit. |
| `POST /api/jobs/backfill-comments` | kézi, egyszer | Komment-scrape futás az adatbázisban lévő összes (vagy `?from=YYYY-MM&to=YYYY-MM` közötti) posztra. A webhook ezután a `summarize` job-ot az érintett összes hónapra elindítja. |
| `POST /api/jobs/reanalyze` | kézi | `?prompt_version=v2`: minden posztot, aminek nincs elemzése ezzel a verzióval, visszaállít `pending`-re az adott verzióra, és elindítja az `analyze`-t. |

### 5.2 Napi flow összefoglalva

```
cron/daily ──► Apify posts run (14 nap) ──► webhooks/apify ──► posts upsert
                                                              │  (új: pending, régi: engagement frissül)
                                                              └─► jobs/analyze ──► Haiku ──► post_analyses
```

### 5.3 Havi flow

```
cron/monthly (1-jén) ──► Apify comments run (előző hónap posztjai) ──► webhooks/apify ──► comments upsert
                                                                                        └─► jobs/summarize(hónap)
                                                                                              ├─ SQL aggregátumok
                                                                                              ├─ Sonnet: kérdések/kifogások a kommentekből
                                                                                              └─ Haiku: egy mondatos összefoglaló
```

### 5.4 Egyszeri backfill

1. `jobs/backfill?since=2022-01-01` → posztok betöltése és elemzése (az `analyze` job önfolytatva végigmegy).
2. **Komment-árbecslés, jóváhagyással.** Az építő egy kis próbafutással (pl. 20 poszt) megméri az Apify komment-scraper tényleges díját, ebből kiszámolja a teljes backfill várható költségét, és ezt a megrendelőnek bemutatja. A teljes `jobs/backfill-comments` csak a megrendelő jóváhagyása után indul. Addig a régi hónapok `top_questions`/`top_objections` mezője üres, a `comment_count` 0.
3. `jobs/backfill-comments` → az összes poszt kommentjei, szükség esetén hónap-ablakokra bontva (`?from&to`), hogy egy Apify futás ne legyen túl nagy.
4. A webhook után a `summarize` végigmegy 2022-01-től az utolsó lezárt hónapig, minden oldalra és az összesítésre. Nulla posztos (oldal, hónap) párokra is készül sor (`post_count=0`), LLM-hívás nélkül.

**Backfill mélység: best effort.** A Facebook oldalak visszagörgetése a gyakorlatban korlátos, az Apify actor előfordul, hogy pár száz posztnál megáll. A 2022-01-01 cél, nem garancia: amennyit az actor elér, azt vesszük. Az építő a próbafutás alapján kihasználja, amit az actor kínál (pl. dátumablakos paraméterek, ha vannak), de másik actort vagy kézi exportot nem vesz be. A tényleges lefedettség (oldalanként a legrégebbi elért poszt dátuma és a posztszám) a backfill után a README-be kerül, és a `scrape_runs.params`-ban is látszik. Ha a lefedettség kevés, az külön döntés később.

A backfill LLM-költsége: néhány száz poszt Haikuval és néhány száz Sonnet-összegzés, nagyságrendileg néhány dollár. Az Apify-díj a 2. pont szerint mérve.

## 6. Adatmodell (Neon Postgres)

Oszlopnevek angolul, a LLM-mezők a kért magyar nevekkel. Minden időbélyeg `timestamptz`. Migrációk számozott SQL fájlok (`db/migrations/001_….sql`), `psql`-lel futtatva; ORM nincs, `psycopg` 3.

- **`pages`** – `id`, `slug` (unique), `name`, `city`, `facebook_url`, `active`, `created_at`.
- **`job_locks`** – `job_name` (PK), `locked_until`, `locked_by`.
- **`scrape_runs`** – `id`, `apify_run_id` (unique), `actor` (`posts`/`comments`), `kind` (`daily`/`backfill`/`monthly_comments`/`backfill_comments`), `params jsonb` (pl. célhónap, since), `status` (`started`/`succeeded`/`failed`/`processed`), `items_count`, `error`, `started_at`, `finished_at`, `processed_at`.
- **`posts`** – `id`, `page_id` → pages, `fb_post_id` (unique), `url`, `text`, `posted_at`, `media jsonb` (kép/videó URL-ek), `reactions_count`, `comments_count`, `shares_count`, `engagement` (generált: a három összege), `first_seen_at`, `last_seen_at`, `last_run_id` → scrape_runs, `raw jsonb` (teljes Apify item), `analysis_status` (`pending`/`done`/`skipped`/`failed`), `analysis_error`.
- **`post_analyses`** – `id`, `post_id` → posts, `prompt_version`, `model`, a 3. fejezet összes mezője (enumok `text` + CHECK constraint, listák `text[]`, `kontener_meretek text[]`, `arak jsonb`, `ar_ertek integer`), `notes` (pl. "kép nélkül elemezve"), `raw_response jsonb`, `input_tokens`, `output_tokens`, `created_at`. Unique `(post_id, prompt_version)`. Egy `posts_current` nézet a posztot a legfrissebb promptverzió elemzésével összekapcsolva, hogy a napi SQL egyszerű legyen.
- **`comments`** – `id`, `post_id` → posts, `fb_comment_id` (unique), `text`, `posted_at`, `likes_count`, `is_reply`, `author_is_page` (az oldal saját válasza-e), `raw jsonb`, `scraped_at`. Kommentelő nevét nem tároljuk külön oszlopban (építő döntése: nem kell az elemzéshez; a `raw`-ban megmarad).
- **`monthly_summaries`** – `id`, `page_id` → pages, **NULL = összes oldal**, `month date` (hónap első napja, Budapest idő szerinti bucket), `post_count`, `comment_count`, `celkozonseg_breakdown jsonb`, `ceges_szegmens_breakdown jsonb`, `lakossagi_szegmens_breakdown jsonb`, `szezonalis_horog_breakdown jsonb`, `top_use_cases jsonb` (top 3, darabszámmal), `top_hook` text, `avg_engagement_by_celkozonseg jsonb`, `avg_engagement_by_lakossagi jsonb`, `avg_engagement_by_ceges jsonb`, `best_post_id`, `best_post_url`, `best_post_engagement`, `worst_post_id`, `worst_post_url`, `worst_post_engagement`, `price_stats jsonb` (min/max/átlag egységenként, árat tartalmazó posztok száma), `ajanlat_mechanika_counts jsonb`, `bizalmi_elemek_counts jsonb`, `surgosseg_counts jsonb`, `top_questions jsonb` (3 × {kérdés, előfordulás, példa}), `top_objections jsonb` (3 × {kifogás, előfordulás, példa}), `summary_sentence text`, `summary_model`, `comments_model`, `prompt_version`, `computed_at`. Unique `(page_id, month) NULLS NOT DISTINCT`.

Engagement = `reactions_count + comments_count + shares_count` a legutóbbi scrape-kori állapot szerint. Hónaphoz a `posted_at` Budapest időzónára konvertálva tartozik.

**Az összegzések frissülése (építő döntése).** A havi sor nem fagy le az első kiszámolásnál:
- A `summarize` két részre válik: (a) SQL-aggregátumok + Haiku egy mondat (olcsó, bármikor újraszámolható), (b) Sonnet komment-klaszterezés (drágább). A (b) csak akkor fut újra, ha az adott (oldal, hónap) kommentjei változtak az utolsó számítás óta (`comments_computed_at` < max `comments.scraped_at`), különben a korábbi eredmény marad.
- A napi cron az `analyze` után az utolsó két naptári hónapra újraszámolja az (a) részt, mert a hónap utolsó napjainak posztjai még 14 napig frissülő engagementtel és esetleg `pending` elemzéssel voltak az 1-jei futásnál.
- `reanalyze` új promptverzióval minden érintett hónapot `stale`-re állít, és a `summarize` a következő napi futásban újraszámolja az (a) részt az új verzió adataival; a `monthly_summaries.prompt_version` mutatja, melyik verzióból készült.
- Ehhez a `monthly_summaries` plusz oszlopai: `stats_computed_at`, `comments_computed_at`, `stale boolean`.

## 7. LLM-használat

**Poszt-kinyerés: `claude-haiku-5-5`**, structured output a 3. fejezet JSON-sémájával (enumok a sémában), `effort: low` kezdetnek; ha a konfidencia-mező vagy a kézi minta gyenge, `medium`. Rendszerprompt magyarul: a cég/piac kontextusa (self-storage konténerpark, magyar piac), minden mező definíciója 1–2 példával, a "konkrét use-case, ne kategória" szabály, és az utasítás, hogy a képeken lévő szöveget (ár, akció, méret) is olvassa. A rendszerprompt stabil, cache-elhető; a poszt a user üzenetben megy. A promptot `prompts/post_extraction_v1.md`-ben tartjuk, a verziószám a fájlnévből kerül a `prompt_version` mezőbe.

**Komment-elemzés: `claude-sonnet-5-5`**, structured output. Bemenet: az adott (oldal, hónap) összes kommentje, az oldal saját válaszai megjelölve. Kimenet: 3 leggyakoribb kérdés és 3 leggyakoribb kifogás, mindegyik normalizált megfogalmazással, előfordulás-számmal és egy szó szerinti példával. 3-nál kevesebb komment esetén üres listák, LLM-hívás nélkül. Az összesített (minden oldal) sorhoz külön hívás az összes kommenttel.

**Egy mondatos összegzés: `claude-haiku-5-5`.** Bemenet a kiszámolt `monthly_summaries` sor JSON-ja, kimenet egy magyar mondat ebben a formában: "Kontigo 2026 szeptember hónapban a családi házban élő magánszemélyeket célozta kerti bútorok és egyebek tárolásával." Az összesített sornál "A piac …" alannyal. Nulla posztos hónapra: "X-nek nem volt posztja 2026 szeptemberben.", LLM nélkül.

**Hiba- és költségkezelés:** minden LLM-hívásnál a tokenszámot eltároljuk. Hiba esetén a poszt `failed` + hibaüzenet, a következő `analyze` futás újrapróbálja (max 3 próbálkozás, utána marad `failed`). A LLM válaszában hiányzó vagy séma-sértő mezőt nem javítgatunk kézzel: structured output mellett ez nem fordulhat elő, ha mégis, `failed`.

## 8. Apify-használat

- **Posztok:** `apify/facebook-posts-scraper`, bemenet: az öt oldal URL-je `startUrls`-ként, `onlyPostsNewerThan` (napi: 14 nap; backfill: 2022-01-01), `resultsLimit` bőven (backfill: oldalanként legalább 1000). Egy futás, mind az öt oldal. Az output-mezők (poszt ID, URL, szöveg, idő, reakció/komment/megosztás szám, média) leképezését az építő az actor aktuális dokumentációja és egy próbafutás alapján rögzíti; a teljes item a `raw`-ba kerül.
- **Kommentek:** `apify/facebook-comments-scraper`, bemenet: poszt-URL-ek listája, `resultsLimit` posztonként (pl. 100), beágyazott válaszok is. Egy futás az adott hónap összes posztjára.
- **Webhook:** a run indításakor regisztrálva, `payloadTemplate`-ben a run ID és a `scrape_runs.id`. A webhook-handler a `scrape_runs` tábla alapján dönt, nem a payloadban bízva.
- **Oldal-azonosítás:** az Apify item oldalra vonatkozó mezője (URL vagy név) alapján `pages`-hez rendelés; ha nem egyértelmű, a run `params`-ban eltárolt URL-lista segít. Ismeretlen oldal → a sor `failed` run-hibába kerül, nem némán eldobva.
- **Képek:** a Facebook CDN URL-ek lejárnak, ezért az elemzés a scrape utáni órákban történik (ez a flow-ból adódik). Ha a backfillnél mégis lejárnának, az építő a webhook-feldolgozásnál letöltheti és base64-ben adhatja át; ez az ő döntése a próbafutás alapján.

## 9. Konfiguráció és titkok

`.env` (helyben) / Vercel environment variables (élesben):
`APIFY_API_TOKEN`, `ANTHROPIC_API_KEY` (már megvannak), `DATABASE_URL` (Neon, pooled), `CRON_SECRET`, `APIFY_WEBHOOK_SECRET`, `ADMIN_SECRET`, `APP_BASE_URL` (a webhook és az önfolytatás URL-jéhez), `JOB_TIME_BUDGET_SECONDS`, `PROMPT_VERSION`.

Titok a repóba nem kerül; `.env.example` igen.

## 10. Megfigyelhetőség

Nincs dashboard, de SQL-ből mindennek látszania kell:
- `scrape_runs` megmondja, mikor futott mi, mennyi item jött, mi hibázott.
- `posts.analysis_status` és `analysis_error` megmondja, mi akadt el.
- Minden függvény strukturált logot ír a Vercel logba (run ID, feldolgozott darabszám, eltelt idő).
- Egy `README.md` leírja a kézi végpontokat `curl` példákkal és 5–6 "mindennapi" SQL-lekérdezést (pl. havi szegmens-megoszlás oldalanként, ár-posztok listája, failed posztok).

## 11. Tesztelés és átadás

- Egységtesztek az Apify-item → `posts` leképezésre, a hónap-bucketelésre (Budapest idő), az összegző SQL-aggregátumokra (fixture adatokkal), és a job időkeret/önfolytatás logikára (LLM és Apify mockolva).
- Egy kis "golden set": 10–15 valós poszt kézzel címkézve; az `analyze` prompt változtatásakor ezen ellenőrizzük, hogy a célközönség és az ár mezők nem romlanak. Nem automatizált gate, de a repóban van.
- Végponttól végpontig: első éles futás a backfill (2022-01-01-től), utána a napi cron bekapcsolása; az átadás kritériuma, hogy a `posts_current` nézetben minden poszt `done` vagy indokolt `skipped`, és a `monthly_summaries`-ben 2022-01-től minden (oldal, hónap) párra van sor.

## 12. Az építőre hagyott döntések

- Vercel Python projektstruktúra részletei, függőségek (`anthropic`, `apify-client`, `psycopg[binary]`, `pydantic`), lokális futtatás módja.
- Az Apify output-mezők pontos leképezése és a pagináció/limit beállítások a próbafutás alapján.
- A prompt konkrét szövege (a 3. fejezet definíciói és a 7. fejezet elvei szerint), a golden set összeállítása.
- Időkeret és batch-méret konkrét értékei; retry-stratégia az Apify/Anthropic hívásokon.
- Hogy a képek URL-ként vagy base64-ben mennek a LLM-nek.
- Indexek, nézetek a gyakori SQL-lekérdezésekhez.

## 13. A beszélgetésben hozott döntések (a megrendelőé)

- Neon Postgres, SQL-ből nézve; dashboard később, ha kell.
- Öt oldal, a 4. fejezet szerint, külön táblában.
- Backfill 2022-01-01-ig mind az öt oldalon, best effort alapon (amennyit az actor elér).
- Kommentek hónap végén egyszer lekérve, nem naponta.
- Komment-backfill teljes, de csak az Apify-díj próbafutásos becslése és jóváhagyás után indul.
- Vercel Hobby csomag, a megrendelő saját fiókjaiban.
- Napi 14 napos ablak az engagement frissítéséhez; képek is mennek a LLM-nek; promptverzió tárolása; havi összegzés oldalanként + összesítve; backfill után minden múltbeli hónapra összegzés; Haiku a kinyeréshez és az egy mondathoz, Sonnet a komment-klaszterezéshez. (Ezeket én javasoltam, a megrendelő "jónak tűnik"-kel elfogadta.)
