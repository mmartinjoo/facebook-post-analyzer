import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.auth import require_admin, require_cron, require_webhook
from app.config import get_settings


@pytest.fixture
def client(settings):
    app = FastAPI()
    app.dependency_overrides[get_settings] = lambda: settings

    @app.get("/t/cron", dependencies=[Depends(require_cron)])
    def cron() -> dict:
        return {"ok": True}

    @app.get("/t/admin", dependencies=[Depends(require_admin)])
    def admin() -> dict:
        return {"ok": True}

    @app.get("/t/hook", dependencies=[Depends(require_webhook)])
    def hook() -> dict:
        return {"ok": True}

    return TestClient(app)


def test_cron_guard(client):
    assert client.get("/t/cron", headers={"Authorization": "Bearer cron-s"}).status_code == 200
    assert client.get("/t/cron", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/t/cron", headers={"Authorization": "cron-s"}).status_code == 401
    assert client.get("/t/cron").status_code == 401


def test_admin_guard(client):
    assert client.get("/t/admin", headers={"X-Admin-Secret": "admin-s"}).status_code == 200
    assert client.get("/t/admin", headers={"X-Admin-Secret": "nope"}).status_code == 401
    assert client.get("/t/admin").status_code == 401


def test_webhook_guard(client):
    assert client.get("/t/hook", params={"token": "hook-s"}).status_code == 200
    assert client.get("/t/hook", params={"token": "nope"}).status_code == 401
    assert client.get("/t/hook").status_code == 401
