import sys
from pathlib import Path

from tools.extraction_engine.extractor import PDFExtractor


def test_extract_text_monkeypatched(monkeypatch):
    # Create a fake PyPDF2 module with PdfReader
    class FakePage:
        def __init__(self, text):
            self._text = text

        def extract_text(self):
            return self._text

    class FakePdfReader:
        def __init__(self, path):
            self.pages = [FakePage("hello"), FakePage("world")]

    fake_pypdf2 = type("m", (), {"PdfReader": FakePdfReader})
    monkeypatch.setitem(sys.modules, "PyPDF2", fake_pypdf2)

    extractor = PDFExtractor()
    pages = extractor.extract_text(Path("dummy.pdf"))
    assert pages == [(1, "hello"), (2, "world")]


def test_extract_images_no_fitz(monkeypatch):
    # Force import of 'fitz' to raise ImportError so extractor falls back
    import builtins
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "fitz":
            raise ImportError
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    extractor = PDFExtractor()
    images = extractor.extract_images(Path("dummy.pdf"), Path("tmp_assets"))
    assert images == []
