from fastapi.testclient import TestClient
from engine.app import app as fastapi_app
from pathlib import Path
import os


client = TestClient(fastapi_app)


def test_upload_pdf(tmp_path):
    # Create a small dummy PDF-like file (not a valid PDF, but stored as bytes)
    file_content = b"%PDF-1.4\n%Dummy PDF content"
    files = {"file": ("test.pdf", file_content, "application/pdf")}
    uploads_dir = Path("data/uploads")
    # Ensure clean state
    if uploads_dir.exists():
        for f in uploads_dir.iterdir():
            f.unlink()

    r = client.post("/upload", files=files)
    assert r.status_code == 200
    j = r.json()
    assert "id" in j and "path" in j
    saved = Path(j["path"])
    assert saved.exists()
    # Clean up
    saved.unlink()
