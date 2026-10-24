-- Mindennapi SQL-lekérdezések (Neon SQL editor vagy psql). Csak olvasnak.
-- A paraméterek (pl. '2026-09-01', 'kontigo') helyére a kívánt értéket írd.

-- 1. Havi szegmens-megoszlás oldalanként (célközönség, céges és lakossági szegmens)
select p.name as oldal, s.month, s.post_count,
       s.celkozonseg_breakdown, s.ceges_szegmens_breakdown, s.lakossagi_szegmens_breakdown
from monthly_summaries s
join pages p on p.id = s.page_id
where s.month >= '2025-01-01'
order by s.month desc, p.name;

-- 2. Árat tartalmazó posztok (legfrissebb elöl)
select p.name as oldal, c.posted_at, c.ar_ertek, c.ar_egyseg, c.url
from posts_current c
join pages p on p.id = c.page_id
where c.ar_megjelenik and c.ar_ertek is not null
order by c.posted_at desc;

-- 3. Hibás vagy még feldolgozásra váró posztok a hibaüzenettel
select p.name as oldal, c.id, c.posted_at, c.analysis_status, c.analysis_attempts, c.analysis_error, c.url
from posts c
join pages p on p.id = c.page_id
where c.analysis_status in ('failed', 'pending')
order by c.analysis_status, c.posted_at desc;

-- 4. Legutóbbi scrape futások (státusz, elemszám, hiba)
select id, actor, kind, status, items_count, error, params, started_at, finished_at, processed_at
from scrape_runs
order by started_at desc
limit 20;

-- 5. Havi összegző mondatok egy hónapra (minden oldal + "A piac" összesítő sor)
select coalesce(p.name, 'A piac') as oldal, s.post_count, s.comment_count, s.summary_sentence
from monthly_summaries s
left join pages p on p.id = s.page_id
where s.month = '2026-09-01'
order by (s.page_id is null), p.name;

-- 6. Top kérdések és kifogások egy (oldal, hónap) párra a kommentekből
select 'kerdes' as tipus, q.value ->> 'text' as szoveg, (q.value ->> 'count')::int as darab, q.value ->> 'example' as pelda
from monthly_summaries s
join pages p on p.id = s.page_id and p.slug = 'kontigo'
cross join lateral jsonb_array_elements(coalesce(s.top_questions, '[]'::jsonb)) as q(value)
where s.month = '2026-09-01'
union all
select 'kifogas', o.value ->> 'text', (o.value ->> 'count')::int, o.value ->> 'example'
from monthly_summaries s
join pages p on p.id = s.page_id and p.slug = 'kontigo'
cross join lateral jsonb_array_elements(coalesce(s.top_objections, '[]'::jsonb)) as o(value)
where s.month = '2026-09-01'
order by tipus, darab desc;

-- 7. Elemzési státuszok darabszáma (hol tart a feldolgozás)
select analysis_status, count(*) as darab
from posts
group by analysis_status
order by analysis_status;

-- 8. Backfill lefedettség: oldalanként a legrégebbi elért poszt és a posztszám
select p.name as oldal, min(po.posted_at) as legregebbi_poszt, count(po.id) as posztszam
from pages p
left join posts po on po.page_id = p.id
group by p.id, p.name
order by p.name;

-- 9. Elakadt láncok: élő vagy lejárt job-zárak, és 30 percnél régebben `started`/`processing` állapotú futások
select 'zar' as tipus, job_name as nev, locked_by as reszlet, locked_until as ido
from job_locks
union all
select 'run', kind || ' #' || id, status || ' ' || apify_run_id, started_at
from scrape_runs
where status in ('started', 'processing') and started_at < now() - interval '30 minutes'
order by ido;
