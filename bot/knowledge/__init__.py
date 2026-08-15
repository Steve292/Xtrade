"""Trading-knowledge ingestion and concept extraction.

Pulls transcripts from configured educator channels, extracts SMC/price-action
concepts from them, and exposes the result as a queryable corpus plus a set of
*reviewable rule candidates* -- deliberately not as live strategy parameters.
See docs/KNOWLEDGE.md for why the wiring stops short of auto-tuning.
"""

from .store import KnowledgeStore, Document, Concept  # noqa: F401

__all__ = ["KnowledgeStore", "Document", "Concept"]
