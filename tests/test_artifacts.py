from fastapi.testclient import TestClient
from engine.app import app as fastapi_app
from pathlib import Path
import json

client = TestClient(fastapi_app)


def test_artifacts_endpoints(tmp_path):
    out_dir = Path("data/outputs")
    assets_dir = Path("data/assets")
    out_dir.mkdir(parents=True, exist_ok=True)
    assets_dir.mkdir(parents=True, exist_ok=True)

    file_id = "testfile"
    md_path = out_dir / f"{file_id}.md"
    md_path.write_text("# Title\nContent")

    img_dir = assets_dir / file_id
    img_dir.mkdir(parents=True, exist_ok=True)
    img_path = img_dir / "img1.png"
    img_path.write_bytes(b"PNGDATA")

    meta = {"md": str(md_path), "images": [str(img_path)], "ocr": {}}
    meta_path = out_dir / f"{file_id}.meta.json"
    meta_path.write_text(json.dumps(meta))

    # GET artifacts
    r = client.get(f"/artifacts/{file_id}")
    assert r.status_code == 200
    j = r.json()
    assert j["md"].endswith(f"{file_id}.md")

    r2 = client.get(f"/artifacts/{file_id}/md")
    assert r2.status_code == 200
    assert "Title" in r2.text

    r3 = client.get(f"/artifacts/{file_id}/images/img1.png")
    assert r3.status_code == 200
    assert r3.content == b"PNGDATA"

    # cleanup
    md_path.unlink()
    img_path.unlink()
    meta_path.unlink()
    img_dir.rmdir()