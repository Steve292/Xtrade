import json
from fastapi.testclient import TestClient
from engine.app import app as fastapi_app
from pathlib import Path
import os

client = TestClient(fastapi_app)


def test_extract_file_sync(monkeypatch, tmp_path):
    # Create a dummy uploaded file path
    uploads_dir = Path("data/uploads")
    uploads_dir.mkdir(parents=True, exist_ok=True)
    fake_file = uploads_dir / "dummy.pdf"
    fake_file.write_bytes(b"%PDF-1.4 dummy")

    # Monkeypatch the PDFExtractor to avoid real parsing
    class FakeExtractor:
        def extract_text(self, p):
            return [(1, "hello"), (2, "world")]

        def extract_images(self, p, outdir):
            return []

    monkeypatch.setattr("tools.extraction_engine.extractor.PDFExtractor", lambda: FakeExtractor())

    payload = {"content": str(fake_file), "content_type": "file", "extraction_type": "entities"}
    r = client.post("/extract", json=payload)
    assert r.status_code == 200
    j = r.json()
    assert "entities" in j
    assert len(j["entities"]) == 2

    fake_file.unlink()


def test_extract_file_async(monkeypatch, tmp_path):
    uploads_dir = Path("data/uploads")
    uploads_dir.mkdir(parents=True, exist_ok=True)
    fake_file = uploads_dir / "dummy2.pdf"
    fake_file.write_bytes(b"%PDF-1.4 dummy")

    # Monkeypatch submit_extract_file to return a known job id
    monkeypatch.setattr("engine.tasks.submit_extract_file", lambda fp: "job-xyz")
    r = client.post("/extract/async", json={"content": str(fake_file), "content_type": "file"})
    assert r.status_code == 200
    j = r.json()
    assert j.get("job_id") == "job-xyz"

    fake_file.unlink()
