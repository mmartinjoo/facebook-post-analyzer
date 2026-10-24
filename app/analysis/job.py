import json
import logging
import time
from uuid import uuid4

from psycopg.types.json import Jsonb

from app.analysis.extractor import analyze_post
from app.analysis.prompt import load_prompt
from app.ingest.posts import image_urls
from app.jobs.runner import JOB_ANALYZE, TimeBudget, acquire_lock, continue_job, release_lock

logger = logging.getLogger(__name__)

# the Anthropic client timeout is 60 s: take no new post when less than this is left, so an in-flight
# call always finishes inside the budget (and the budget inside Vercel's maxDuration)
ANALYZE_RESERVE_SECONDS = 65

FIELDS = [
    "celkozonseg", "ceges_szegmens", "lakossagi_szegmens", "use_case", "fajdalompont", "szezonalis_horog",
    "kontener_meretek", "ar_megjelenik", "arak", "ar_ertek", "ar_egyseg", "arhorgony", "ajanlat_mechanika",
    "surgosseg", "bizalmi_elemek", "cta_tipus", "cta_szoveg", "kulcsszavak", "konfidencia",
]
COLUMNS = ["post_id", "prompt_version", "model", *FIELDS, "notes", "raw_response", "input_tokens", "output_tokens"]
UPSERT = (
    f"INSERT INTO post_analyses ({', '.join(COLUMNS)}) VALUES ({', '.join(['%s'] * len(COLUMNS))}) "
    "ON CONFLICT (post_id, prompt_version) DO UPDATE SET "
    + ", ".join(f"{c} = EXCLUDED.{c}" for c in COLUMNS[2:])
    + ", created_at = now()"
)
PENDING = """
    SELECT p.id, p.text, p.posted_at, p.media, pg.name AS page_name
    FROM posts p JOIN pages pg ON pg.id = p.page_id
    WHERE p.analysis_status = 'pending' AND NOT (p.id = ANY(%s))
    ORDER BY p.posted_at DESC LIMIT %s
    FOR UPDATE OF p SKIP LOCKED
"""


def _save(conn, post_id, prompt_version, result) -> None:
    a = result.analysis
    values = {f: getattr(a, f) for f in FIELDS}
    values["arak"] = Jsonb([x.model_dump() for x in a.arak])
    conn.execute(
        UPSERT,
        [
            post_id, prompt_version, result.model, *values.values(), result.notes,
            Jsonb(result.raw), result.input_tokens, result.output_tokens,
        ],
    )
    conn.execute("UPDATE posts SET analysis_status = 'done', analysis_error = NULL WHERE id = %s", (post_id,))


def _record_failure(conn, post_id, error: Exception) -> bool:
    row = conn.execute(
        """UPDATE posts SET analysis_attempts = analysis_attempts + 1, analysis_error = %s,
           analysis_status = CASE WHEN analysis_attempts + 1 >= 3 THEN 'failed' ELSE 'pending' END
           WHERE id = %s RETURNING analysis_status""",
        (str(error)[:500], post_id),
    ).fetchone()
    return row["analysis_status"] == "failed"


def run_analyze_job(conn, settings, client, budget: TimeBudget, *, prompt_version: str, batch_size: int = 10) -> dict:
    started = time.monotonic()
    owner = uuid4().hex
    if not acquire_lock(conn, JOB_ANALYZE, owner=owner):
        logger.info(json.dumps({"job": "analyze", "run_id": owner, "locked": True}))
        return {"locked": True}
    released = False
    processed = failed = 0
    attempted: list[int] = []
    try:
        system_prompt = load_prompt(prompt_version)
        while budget.remaining() >= ANALYZE_RESERVE_SECONDS:
            rows = conn.execute(PENDING, (attempted, batch_size)).fetchall()
            if not rows:
                conn.commit()
                break
            for row in rows:
                if budget.remaining() < ANALYZE_RESERVE_SECONDS:
                    break
                attempted.append(row["id"])
                try:
                    result = analyze_post(
                        client,
                        system_prompt=system_prompt,
                        page_name=row["page_name"],
                        posted_at=row["posted_at"],
                        text=row["text"],
                        image_urls=image_urls(row["media"]),
                    )
                    _save(conn, row["id"], prompt_version, result)
                    processed += 1
                except Exception as e:
                    conn.rollback()
                    if _record_failure(conn, row["id"], e):
                        failed += 1
                conn.commit()
            conn.commit()
        remaining = conn.execute(
            "SELECT count(*) AS n FROM posts WHERE analysis_status = 'pending' AND NOT (id = ANY(%s))",
            (attempted,),
        ).fetchone()["n"]
        conn.commit()
        # release before self-continuation, or the next invocation finds the lock held
        release_lock(conn, JOB_ANALYZE, owner)
        released = True
        if remaining > 0:
            continue_job(settings, "/api/jobs/analyze")
        logger.info(
            json.dumps(
                {
                    "job": "analyze",
                    "run_id": owner,
                    "processed": processed,
                    "failed": failed,
                    "remaining": remaining,
                    "elapsed_s": round(time.monotonic() - started, 1),
                }
            )
        )
        return {"processed": processed, "failed": failed, "remaining": remaining}
    finally:
        if not released:
            conn.rollback()
            release_lock(conn, JOB_ANALYZE, owner)
