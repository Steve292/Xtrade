from fastapi.testclient import TestClient
from engine.app import app as fastapi_app

client = TestClient(fastapi_app)


def test_ai_summarize_endpoint():
    payload = {"content": "This is a long document. It has many sentences.", "length": "short"}
    r = client.post("/ai/summarize", json=payload)
    assert r.status_code == 200
    j = r.json()
    assert "summary" in j


def test_ai_rewrite_endpoint():
    payload = {"content": "Our app is super cool and stuff...", "style": {"tone": "professional"}}
    r = client.post("/ai/rewrite", json=payload)
    assert r.status_code == 200
    j = r.json()
    assert "rewritten" in j
