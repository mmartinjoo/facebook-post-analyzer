import uuid
from datetime import date, datetime, timezone

from psycopg.types.json import Jsonb

ANALYSIS_DEFAULTS = {
    "celkozonseg": "maganszemely",
    "ceges_szegmens": "nincs",
    "lakossagi_szegmens": "csaladi_haz",
    "use_case": "kerti bútor tárolása",
    "fajdalompont": "",
    "szezonalis_horog": "nincs",
    "kontener_meretek": [],
    "ar_megjelenik": False,
    "arak": [],
    "ar_ertek": None,
    "ar_egyseg": None,
    "arhorgony": "nincs",
    "ajanlat_mechanika": ["nincs"],
    "surgosseg": "nincs",
    "bizalmi_elemek": ["nincs"],
    "cta_tipus": "nincs",
    "cta_szoveg": "",
    "kulcsszavak": ["a", "b", "c"],
    "konfidencia": 3,
}


def seed(conn, *, page_slug: str, month: date, posts: list[dict]) -> list[int]:
    page_id = conn.execute("select id from pages where slug = %s", (page_slug,)).fetchone()["id"]
    ids = []
    for spec in posts:
        spec = dict(spec)
        posted_at = spec.pop("posted_at", None) or datetime(
            month.year, month.month, spec.pop("day", 15), spec.pop("hour", 10), tzinfo=timezone.utc
        )
        spec.pop("day", None)
        spec.pop("hour", None)
        status = spec.pop("analysis_status", "done")
        fb_id = f"seed-{uuid.uuid4()}"
        post_id = conn.execute(
            """insert into posts (page_id, fb_post_id, url, text, posted_at, reactions_count,
                                  comments_count, shares_count, analysis_status)
               values (%s, %s, %s, %s, %s, %s, %s, %s, %s) returning id""",
            (
                page_id, fb_id, f"https://facebook.com/{fb_id}", spec.pop("text", "poszt"), posted_at,
                spec.pop("reactions_count", 0), spec.pop("comments_count", 0),
                spec.pop("shares_count", 0), status,
            ),
        ).fetchone()["id"]
        if status == "done":
            a = {**ANALYSIS_DEFAULTS, **spec}
            a["arak"] = Jsonb(a["arak"])
            conn.execute(
                """insert into post_analyses (post_id, prompt_version, model, celkozonseg, ceges_szegmens,
                       lakossagi_szegmens, use_case, fajdalompont, szezonalis_horog, kontener_meretek,
                       ar_megjelenik, arak, ar_ertek, ar_egyseg, arhorgony, ajanlat_mechanika, surgosseg,
                       bizalmi_elemek, cta_tipus, cta_szoveg, kulcsszavak, konfidencia)
                   values (%(post_id)s, 'v1', 'claude-haiku-5-5', %(celkozonseg)s, %(ceges_szegmens)s,
                       %(lakossagi_szegmens)s, %(use_case)s, %(fajdalompont)s, %(szezonalis_horog)s,
                       %(kontener_meretek)s, %(ar_megjelenik)s, %(arak)s, %(ar_ertek)s, %(ar_egyseg)s,
                       %(arhorgony)s, %(ajanlat_mechanika)s, %(surgosseg)s, %(bizalmi_elemek)s,
                       %(cta_tipus)s, %(cta_szoveg)s, %(kulcsszavak)s, %(konfidencia)s)""",
                {**a, "post_id": post_id},
            )
        ids.append(post_id)
    conn.commit()
    return ids
