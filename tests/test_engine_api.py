import pytest
from fastapi.testclient import TestClient
from engine.app import app as fastapi_app
import engine.api as api_module


client = TestClient(fastapi_app)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json().get("status") == "ok"


def test_extract_sync():
    payload = {"content": "Hello world", "content_type": "text", "extraction_type": "entities"}
    r = client.post("/extract", json=payload)
    assert r.status_code == 200
    j = r.json()
    assert "entities" in j
    assert isinstance(j["entities"], list)


def test_extract_async_and_get_job(monkeypatch):
    # Patch submit_extract to return a predictable job id
    monkeypatch.setattr(api_module, "submit_extract", lambda text: "job-123")
    r = client.post("/extract/async", json={"content": "async test"})
    assert r.status_code == 200
    j = r.json()
    assert j.get("job_id") == "job-123"

    # Patch celery AsyncResult to a fake result for /jobs/{id}
    class FakeAsyncResult:
        def __init__(self, id):
            self.id = id
            self.status = "SUCCESS"
            self.result = {"text_length": 10}

    monkeypatch.setattr(api_module.celery_app, "AsyncResult", FakeAsyncResult)
    r2 = client.get("/jobs/job-123")
    assert r2.status_code == 200
    data = r2.json()
    assert data.get("status") == "SUCCESS"
    assert data.get("result") == {"text_length": 10}
