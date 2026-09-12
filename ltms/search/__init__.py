from .base import SearchOutcome, SearchResult, canonical_url, categories_for, dedupe_and_cap
from .searxng import SearxngBackend

__all__ = [
    "SearchOutcome",
    "SearchResult",
    "SearxngBackend",
    "canonical_url",
    "categories_for",
    "dedupe_and_cap",
]
