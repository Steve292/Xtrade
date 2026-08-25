from pathlib import Path
from typing import Iterable, Dict

def ocr_images(image_paths: Iterable[Path]) -> Dict[str, str]:
    """Run OCR on a list of image paths and return a mapping path->text.

    Falls back gracefully if pytesseract or PIL are unavailable.
    """
    texts = {}
    try:
        import pytesseract
        from PIL import Image
    except Exception:
        for p in image_paths:
            texts[str(p)] = "(ocr not available)"
        return texts

    for p in image_paths:
        try:
            img = Image.open(p)
            txt = pytesseract.image_to_string(img)
            texts[str(p)] = txt
        except Exception:
            texts[str(p)] = "(ocr failed)"

    return texts
