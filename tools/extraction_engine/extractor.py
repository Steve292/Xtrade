from pathlib import Path
from typing import List, Tuple

class PDFExtractor:
    """Simple PDF extractor supporting text (PyPDF2) and optional images (PyMuPDF).

    Methods:
    - extract_text(pdf_path) -> List[Tuple[int, str]]
    - extract_images(pdf_path, out_dir) -> List[Path]
    - write_markdown(pages, out_path, title=None)
    """

    def extract_text(self, pdf_path: Path) -> List[Tuple[int, str]]:
        try:
            from PyPDF2 import PdfReader
        except Exception as e:
            raise RuntimeError("PyPDF2 is required. Install with 'pip install PyPDF2'") from e

        reader = PdfReader(str(pdf_path))
        pages = []
        for i, page in enumerate(reader.pages, start=1):
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
            pages.append((i, text))
        return pages

    def extract_images(self, pdf_path: Path, out_dir: Path) -> List[Path]:
        """Extract images using PyMuPDF (fitz) if available. Returns list of saved image paths."""
        try:
            import fitz  # PyMuPDF
        except Exception:
            return []

        out_dir.mkdir(parents=True, exist_ok=True)
        doc = fitz.open(str(pdf_path))
        saved = []
        for page_index in range(len(doc)):
            for img_index, img in enumerate(doc.get_page_images(page_index), start=1):
                xref = img[0]
                pix = fitz.Pixmap(doc, xref)
                if pix.n < 5:
                    ext = "png"
                    img_path = out_dir / f"page{page_index+1}_img{img_index}.{ext}"
                    pix.save(str(img_path))
                    saved.append(img_path)
                else:
                    # CMYK: convert to RGB
                    pix0 = fitz.Pixmap(fitz.csRGB, pix)
                    img_path = out_dir / f"page{page_index+1}_img{img_index}.png"
                    pix0.save(str(img_path))
                    saved.append(img_path)
                    pix0 = None
                pix = None
        return saved

    def write_markdown(self, pages: List[Tuple[int, str]], out_path: Path, title: str = None):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as f:
            if title:
                f.write(f"# {title}\n\n")
            for page_no, text in pages:
                f.write(f"## Page {page_no}\n\n")
                if text:
                    f.write(text.strip() + "\n\n")
                else:
                    f.write("*(no text extracted on this page)*\n\n")
