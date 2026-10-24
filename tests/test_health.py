from fastapi.testclient import TestClient
from api.index import app


def test_health():
    r = TestClient(app).get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_importing_app_configures_info_logging():
    import subprocess
    import sys

    code = "import logging, api.index; r = logging.getLogger(); print(r.level, len(r.handlers))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.split()
    assert out == ["20", "1"]
