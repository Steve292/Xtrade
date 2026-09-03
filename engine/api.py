from fastapi import APIRouter, BackgroundTasks
from fastapi.responses import JSONResponse, FileResponse
import json
from fastapi import UploadFile, File
from pathlib import Path
from .models import ExtractRequest, ExtractResponse, RewriteRequest, JobResponse, VideoProcessRequest
from .tasks import submit_extract, submit_extract_file
from .celery_app import celery_app
import uuid
import os
import shutil
from typing import Any
from tools.ai_adapter import summarize as ai_summarize_fn, rewrite as ai_rewrite_fn
from .models import AISummarizeRequest, AIRewriteRequest

router = APIRouter()


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.post("/extract", response_model=ExtractResponse)
async def extract_sync(req: ExtractRequest):
    # If a file_path is provided in the content field and content_type == 'file',
    # run the PDFExtractor synchronously and return extracted entities.
    if req.content_type == "file":
        file_path = req.content
        # Security: only allow files under uploads dir by default
        uploads_dir = os.environ.get("UPLOADS_DIR", "data/uploads")
        if not os.path.abspath(file_path).startswith(os.path.abspath(uploads_dir)):
            return JSONResponse({"error": "file_path not allowed"}, status_code=403)

        try:
            from tools.extraction_engine.extractor import PDFExtractor
        except Exception as e:
            return JSONResponse({"error": "extractor unavailable", "details": str(e)}, status_code=500)

        extractor = PDFExtractor()
        pages = extractor.extract_text(Path(file_path))
        # Basic entity extraction: return per-page text as entities
        entities = [{"page": p, "text": t} for p, t in pages]
        # Optionally extract images
        assets_dir = os.environ.get("ASSETS_DIR", "data/assets")
        images = extractor.extract_images(Path(file_path), Path(assets_dir) / Path(file_path).stem)
        return ExtractResponse(entities=entities)

    # Fallback: original behavior for direct text extraction
    entities = [{"text": req.content, "type": "text", "length": len(req.content)}]
    return ExtractResponse(entities=entities)


@router.post("/extract/async", response_model=JobResponse)
async def extract_async(req: ExtractRequest):
    if req.content_type == "file":
        file_path = req.content
        uploads_dir = os.environ.get("UPLOADS_DIR", "data/uploads")
        if not os.path.abspath(file_path).startswith(os.path.abspath(uploads_dir)):
            return JSONResponse({"error": "file_path not allowed"}, status_code=403)
        job_id = submit_extract_file(file_path)
        return JobResponse(job_id=job_id)
    job_id = submit_extract(req.content)
    return JobResponse(job_id=job_id)


@router.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    """Accept an uploaded PDF (or other file) and store it under data/uploads.

    Returns the stored path and an id for downstream processing.
    """
    uploads_dir = os.environ.get("UPLOADS_DIR", "data/uploads")
    os.makedirs(uploads_dir, exist_ok=True)
    file_id = str(uuid.uuid4())
    filename = f"{file_id}_{file.filename}"
    dest = os.path.join(uploads_dir, filename)
    with open(dest, "wb") as out:
        shutil.copyfileobj(file.file, out)
    return {"id": file_id, "path": dest, "filename": file.filename}


@router.get("/jobs/{job_id}")
async def get_job(job_id: str):
    try:
        res = celery_app.AsyncResult(job_id)
        return JSONResponse({"id": job_id, "status": res.status, "result": res.result})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/edit/rewrite")
async def rewrite(req: RewriteRequest):
    # Stub: echo back content with a note
    return {"rewritten": req.content, "note": "(stub)"}


@router.post("/video/process")
async def video_process(req: VideoProcessRequest, background: BackgroundTasks):
    # Enqueue or start background processing (stub)
    job_id = str(uuid.uuid4())
    # In production: submit a Celery video processing workflow
    return {"job_id": job_id, "status": "queued"}


@router.get("/artifacts/{file_id}")
async def get_artifacts(file_id: str):
    out_dir = os.environ.get("OUTPUTS_DIR", "data/outputs")
    meta_path = Path(out_dir) / f"{file_id}.meta.json"
    if not meta_path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/artifacts/{file_id}/md")
async def get_artifact_md(file_id: str):
    out_dir = os.environ.get("OUTPUTS_DIR", "data/outputs")
    md_path = Path(out_dir) / f"{file_id}.md"
    if not md_path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(md_path)


@router.get("/artifacts/{file_id}/images/{image_name}")
async def get_artifact_image(file_id: str, image_name: str):
    assets_dir = os.environ.get("ASSETS_DIR", "data/assets")
    img_path = Path(assets_dir) / file_id / image_name
    if not img_path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(img_path)


@router.post("/ai/summarize")
async def ai_summarize(req: AISummarizeRequest):
    summary = ai_summarize_fn(req.content, length=req.length, model=req.model)
    return {"summary": summary}


@router.post("/ai/rewrite")
async def ai_rewrite(req: AIRewriteRequest):
    rewritten = ai_rewrite_fn(req.content, style=req.style, model=req.model)
    return {"rewritten": rewritten}
