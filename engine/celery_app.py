import os
from celery import Celery
from pathlib import Path

broker = os.environ.get("CELERY_BROKER_URL", "redis://redis:6379/0")
backend = os.environ.get("CELERY_RESULT_BACKEND", "redis://redis:6379/1")

celery_app = Celery("engine", broker=broker, backend=backend)

celery_app.conf.update(
    task_serializer='json',
    result_serializer='json',
    accept_content=['json'],
)

@celery_app.task(bind=True)
def example_extract_task(self, text: str):
    # Placeholder task: perform light-weight extraction (stub)
    # In production this would call the real extractor and persist results to storage.
    return {"text_length": len(text), "status": "done"}


@celery_app.task(bind=True)
def extraction_task(self, file_path: str, out_dir: str = "data/outputs", assets_dir: str = "data/assets", do_ocr: bool = False):
    """Run full extraction pipeline for a PDF file path.

    This task uses tools.extraction_engine.PDFExtractor to extract text and images,
    writes Markdown output, and optionally runs OCR on images.
    """
    try:
        from tools.extraction_engine.extractor import PDFExtractor
        from tools.extraction_engine.ocr import ocr_images
    except Exception as e:
        return {"error": "PDFExtractor/ocr not available", "details": str(e)}

    extractor = PDFExtractor()
    try:
        pages = extractor.extract_text(Path(file_path))
    except Exception as e:
        return {"error": "failed to extract text", "details": str(e)}

    # ensure directories
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    Path(assets_dir).mkdir(parents=True, exist_ok=True)

    file_id = Path(file_path).stem
    out_md = Path(out_dir) / f"{file_id}.md"
    extractor.write_markdown(pages, out_md, title=file_id)

    images = extractor.extract_images(Path(file_path), Path(assets_dir) / file_id)

    ocr_texts = {}
    if do_ocr and images:
        # images is a list of Path strings
        image_paths = [Path(p) for p in images]
        ocr_texts = ocr_images(image_paths)

    # persist metadata alongside the markdown
    meta = {
        "md": str(out_md),
        "images": [str(p) for p in images],
        "ocr": ocr_texts,
    }
    meta_path = Path(out_dir) / f"{file_id}.meta.json"
    try:
        import json
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
    except Exception:
        pass

    return meta
