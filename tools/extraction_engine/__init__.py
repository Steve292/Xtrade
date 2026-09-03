"""Extraction engine package.

Expose `PDFExtractor` and a simple `main()` CLI.
"""
from .extractor import PDFExtractor
from .cli import main

__all__ = ["PDFExtractor", "main"]
