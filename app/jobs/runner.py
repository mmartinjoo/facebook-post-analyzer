import json
import logging
import time

import httpx

logger = logging.getLogger(__name__)

JOB_ANALYZE = "analyze"
JOB_SUMMARIZE = "summarize"


class TimeBudget:
    def __init__(self, seconds: float) -> None:
        self._deadline = time.monotonic() + seconds

    def remaining(self) -> float:
        return max(0.0, self._deadline - time.monotonic())

    def expired(self) -> bool:
        return self.remaining() <= 0


def acquire_lock(conn, job_name: str, ttl_seconds: int = 330, *, owner: str) -> bool:
    # TTL just above Vercel's maxDuration (300 s): a killed invocation's lock frees up soon after
    cur = conn.execute(
        """
        INSERT INTO job_locks (job_name, locked_until, locked_by)
        VALUES (%(job)s, now() + make_interval(secs => %(ttl)s), %(owner)s)
        ON CONFLICT (job_name) DO UPDATE
            SET locked_until = EXCLUDED.locked_until, locked_by = EXCLUDED.locked_by
            WHERE job_locks.locked_until < now()
        """,
        {"job": job_name, "ttl": ttl_seconds, "owner": owner},
    )
    acquired = cur.rowcount == 1
    conn.commit()
    return acquired


def release_lock(conn, job_name: str, owner: str) -> None:
    conn.execute(
        "DELETE FROM job_locks WHERE job_name = %s AND locked_by = %s",
        (job_name, owner),
    )
    conn.commit()


def continue_job(settings, path: str, params: dict | None = None) -> None:
    """Fire-and-forget POST to one of our own job endpoints. Never raises; the outcome is logged."""
    headers = {"X-Admin-Secret": settings.admin_secret}
    if settings.vercel_protection_bypass:
        headers["x-vercel-protection-bypass"] = settings.vercel_protection_bypass
    try:
        r = httpx.post(settings.app_base_url + path, params=params, headers=headers, timeout=3.0)
    except httpx.TimeoutException:
        # the normal path: the job keeps running after we stop waiting for it
        logger.info(json.dumps({"job": "continue", "path": path, "status": "timeout"}))
        return
    except Exception as exc:
        logger.warning(json.dumps({"job": "continue", "path": path, "error": str(exc)[:200]}))
        return
    line = json.dumps({"job": "continue", "path": path, "status": r.status_code, "body": r.text[:200]})
    if r.status_code >= 400 or "locked" in r.text:
        logger.warning(line)
    else:
        logger.info(line)
