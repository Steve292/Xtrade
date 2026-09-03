#!/usr/bin/env python3
"""Simple PDF text extractor that writes a Markdown file.

Usage:
  python tools/extraction_engine/extract_pdf.py /path/to/input.pdf /path/to/output.md

This script uses PyPDF2 to extract page text and writes it to a Markdown file
with page headings. It does not extract images — for that, use PyMuPDF or pdfminer.
"""
import sys
from pathlib import Path

def extract_text(pdf_path: Path) -> list:
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

def write_markdown(pages: list, out_path: Path, title: str = None):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        if title:
            f.write(f"# {title}\n\n")
        for page_no, text in pages:
            f.write(f"## Page {page_no}\n\n")
            # Simple cleanup: strip trailing whitespace
            if text:
                f.write(text.strip() + "\n\n")
            else:
                f.write("*(no text extracted on this page)*\n\n")

def main():
    if len(sys.argv) < 3:
        print("Usage: extract_pdf.py input.pdf output.md")
        sys.exit(2)
    pdf_path = Path(sys.argv[1])
    out_path = Path(sys.argv[2])
    if not pdf_path.exists():
        print(f"Input PDF not found: {pdf_path}")
        sys.exit(1)

    pages = extract_text(pdf_path)
    title = pdf_path.stem.replace("_", " ")
    write_markdown(pages, out_path, title=title)
    print(f"Wrote markdown to {out_path}")

if __name__ == '__main__':
    main()
