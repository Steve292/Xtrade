from .celery_app import example_extract_task


def submit_extract(text: str):
    # Submit a Celery task and return AsyncResult id
    res = example_extract_task.delay(text)
    return res.id


def submit_extract_file(file_path: str, out_dir: str = "data/outputs", assets_dir: str = "data/assets", do_ocr: bool = False):
    from .celery_app import extraction_task
    res = extraction_task.delay(file_path, out_dir, assets_dir, do_ocr)
    return res.id
