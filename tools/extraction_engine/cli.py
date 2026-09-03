"""Command-line interface for the extraction engine."""
import sys
from pathlib import Path

from .extractor import PDFExtractor


def main(argv=None):
    argv = argv or sys.argv[1:]
    if len(argv) < 2:
        print("Usage: extraction_engine <input.pdf> <output.md> [--assets-dir assets]")
        return 2

    pdf_path = Path(argv[0])
    out_md = Path(argv[1])
    assets_dir = Path("docs/assets")
    if "--assets-dir" in argv:
        idx = argv.index("--assets-dir")
        if idx + 1 < len(argv):
            assets_dir = Path(argv[idx + 1])

    if not pdf_path.exists():
        print(f"Input PDF not found: {pdf_path}")
        return 1

    extractor = PDFExtractor()
    pages = extractor.extract_text(pdf_path)
    extractor.write_markdown(pages, out_md, title=pdf_path.stem)
    images = extractor.extract_images(pdf_path, assets_dir)
    if images:
        print(f"Extracted {len(images)} images to {assets_dir}")

    print(f"Wrote markdown to {out_md}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
