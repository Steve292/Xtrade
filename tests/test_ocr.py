from pathlib import Path
from PIL import Image

def test_ocr_images_monkeypatched(monkeypatch, tmp_path):
    # Create a small dummy image
    img_path = tmp_path / "img1.png"
    img = Image.new('RGB', (10, 10), color='white')
    img.save(img_path)

    # Monkeypatch pytesseract.image_to_string
    class FakePT:
        @staticmethod
        def image_to_string(img):
            return "hello-ocr"

    monkeypatch.setitem(__import__('sys').modules, 'pytesseract', FakePT)

    from tools.extraction_engine.ocr import ocr_images

    res = ocr_images([img_path])
    assert isinstance(res, dict)
    assert str(img_path) in res
    assert res[str(img_path)] == 'hello-ocr'
