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


def acquire_lock(conn, job_name: str, ttl_seconds: int = 600, *, owner: str) -> bool:
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
    try:
        httpx.post(
            settings.app_base_url + path,
            params=params,
            headers={"X-Admin-Secret": settings.admin_secret},
            timeout=3.0,
        )
    except httpx.TimeoutException:
        logger.debug("continue_job %s timed out; assuming the call started", path)
    except Exception as exc:
        logger.warning("continue_job %s failed: %s", path, exc)
