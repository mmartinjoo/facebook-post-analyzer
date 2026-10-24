from fastapi import Body, Depends, FastAPI, HTTPException

from app.apify_runs import get_apify_client
from app.auth import require_webhook
from app.config import Settings, get_settings
from app.db import get_db
from app.jobs.webhook import process_webhook

app = FastAPI()


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.post("/api/webhooks/apify", dependencies=[Depends(require_webhook)])
def apify_webhook(
    payload: dict = Body(...),
    settings: Settings = Depends(get_settings),
    conn=Depends(get_db),
    client=Depends(get_apify_client),
) -> dict:
    try:
        return process_webhook(conn, settings, client, payload)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
