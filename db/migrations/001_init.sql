create table pages (
    id serial primary key,
    slug text not null unique,
    name text not null,
    city text not null,
    facebook_url text not null,
    active boolean not null default true,
    created_at timestamptz not null default now()
);

create table job_locks (
    job_name text primary key,
    locked_until timestamptz not null,
    locked_by text not null
);

create table scrape_runs (
    id serial primary key,
    apify_run_id text not null unique,
    actor text not null check (actor in ('posts', 'comments')),
    kind text not null check (kind in ('daily', 'backfill', 'monthly_comments', 'backfill_comments')),
    params jsonb not null default '{}',
    status text not null default 'started' check (status in ('started', 'succeeded', 'failed', 'processed')),
    items_count integer,
    error text,
    started_at timestamptz not null default now(),
    finished_at timestamptz,
    processed_at timestamptz
);
create index scrape_runs_status_idx on scrape_runs (status);

create table posts (
    id serial primary key,
    page_id integer not null references pages (id),
    fb_post_id text not null unique,
    url text,
    text text,
    posted_at timestamptz,
    media jsonb not null default '[]',
    reactions_count integer not null default 0,
    comments_count integer not null default 0,
    shares_count integer not null default 0,
    engagement integer generated always as (reactions_count + comments_count + shares_count) stored,
    first_seen_at timestamptz not null default now(),
    last_seen_at timestamptz not null default now(),
    last_run_id integer references scrape_runs (id),
    raw jsonb,
    analysis_status text not null default 'pending' check (analysis_status in ('pending', 'done', 'skipped', 'failed')),
    analysis_error text,
    analysis_attempts integer not null default 0
);
create index posts_page_posted_idx on posts (page_id, posted_at);
create index posts_analysis_status_idx on posts (analysis_status);

create table post_analyses (
    id serial primary key,
    post_id integer not null references posts (id),
    prompt_version text not null,
    model text not null,
    celkozonseg text not null check (celkozonseg in ('maganszemely', 'vallalkozas', 'mindketto', 'nem_megallapithato')),
    ceges_szegmens text not null check (ceges_szegmens in ('vendeglatas', 'logisztika', 'epitoipar', 'kereskedelem', 'szolgaltato', 'webshop', 'egyeb', 'nincs')),
    lakossagi_szegmens text not null check (lakossagi_szegmens in ('csaladi_haz', 'panel', 'koltozo', 'felujito', 'motoros', 'sportolo', 'hobbi_gyujto', 'egyeb', 'nincs')),
    use_case text not null,
    fajdalompont text not null,
    szezonalis_horog text not null check (szezonalis_horog in ('gumicsere', 'unnep', 'koltozesi_szezon', 'nyar_vege', 'felujitas', 'tanev', 'nincs')),
    kontener_meretek text[] not null default '{}',
    ar_megjelenik boolean not null,
    arak jsonb not null default '[]',
    ar_ertek integer,
    ar_egyseg text check (ar_egyseg in ('ft_ho', 'ft_m2_ho', 'ft_nap', 'egyeb')),
    arhorgony text not null check (arhorgony in ('garazs', 'raktar', 'masik_szolgaltato', 'nincs')),
    ajanlat_mechanika text[] not null default '{}',
    surgosseg text not null check (surgosseg in ('utolso_darabok', 'foglaltsag_szazalek', 'bovites', 'hataridos_akcio', 'nincs')),
    bizalmi_elemek text[] not null default '{}',
    cta_tipus text not null check (cta_tipus in ('hivj', 'irj', 'kattints', 'foglalj', 'nincs')),
    cta_szoveg text not null default '',
    kulcsszavak text[] not null default '{}',
    konfidencia integer not null check (konfidencia between 1 and 5),
    notes text,
    raw_response jsonb,
    input_tokens integer,
    output_tokens integer,
    created_at timestamptz not null default now(),
    unique (post_id, prompt_version)
);

create table comments (
    id serial primary key,
    post_id integer not null references posts (id),
    fb_comment_id text not null unique,
    text text,
    posted_at timestamptz,
    likes_count integer not null default 0,
    is_reply boolean not null default false,
    author_is_page boolean not null default false,
    raw jsonb,
    scraped_at timestamptz not null default now()
);
create index comments_post_idx on comments (post_id);

create table monthly_summaries (
    id serial primary key,
    page_id integer references pages (id),
    month date not null,
    post_count integer,
    comment_count integer,
    celkozonseg_breakdown jsonb,
    ceges_szegmens_breakdown jsonb,
    lakossagi_szegmens_breakdown jsonb,
    szezonalis_horog_breakdown jsonb,
    top_use_cases jsonb,
    top_hook text,
    avg_engagement_by_celkozonseg jsonb,
    avg_engagement_by_lakossagi jsonb,
    avg_engagement_by_ceges jsonb,
    best_post_id integer references posts (id),
    best_post_url text,
    best_post_engagement integer,
    worst_post_id integer references posts (id),
    worst_post_url text,
    worst_post_engagement integer,
    price_stats jsonb,
    ajanlat_mechanika_counts jsonb,
    bizalmi_elemek_counts jsonb,
    surgosseg_counts jsonb,
    top_questions jsonb,
    top_objections jsonb,
    summary_sentence text,
    summary_model text,
    comments_model text,
    prompt_version text,
    computed_at timestamptz,
    stats_computed_at timestamptz,
    comments_computed_at timestamptz,
    stale boolean not null default false,
    unique nulls not distinct (page_id, month)
);

create view posts_current as
select p.*,
       a.id as analysis_id,
       a.prompt_version, a.model,
       a.celkozonseg, a.ceges_szegmens, a.lakossagi_szegmens, a.use_case, a.fajdalompont,
       a.szezonalis_horog, a.kontener_meretek, a.ar_megjelenik, a.arak, a.ar_ertek, a.ar_egyseg,
       a.arhorgony, a.ajanlat_mechanika, a.surgosseg, a.bizalmi_elemek, a.cta_tipus,
       a.cta_szoveg, a.kulcsszavak, a.konfidencia, a.notes as analysis_notes
from posts p
left join (
    select distinct on (post_id) *
    from post_analyses
    order by post_id, created_at desc, id desc
) a on a.post_id = p.id;

insert into pages (slug, name, city, facebook_url) values
    ('raktar24', 'Raktar24', 'Győr', 'https://www.facebook.com/raktar24.hu'),
    ('kontigo', 'Kontigo', 'Szombathely', 'https://www.facebook.com/kontigokontenerpark'),
    ('kontenertar', 'KonténerTár', 'Gödöllő', 'https://www.facebook.com/kontenertar'),
    ('vnv', 'VNV konténer', 'Győr', 'https://www.facebook.com/vnvkontenerraktar'),
    ('erdraktar', 'ÉrdRaktár', 'Érd', 'https://www.facebook.com/erdraktar')
on conflict (slug) do nothing;
