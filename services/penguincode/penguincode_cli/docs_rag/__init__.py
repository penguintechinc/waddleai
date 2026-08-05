"""Documentation RAG system for PenguinCode.

Provides automatic detection of project languages and libraries,
fetching of relevant documentation, and context injection for
improved code assistance.

Key features:
- Only indexes docs for languages/libraries actually used
- TTL-based cache expiration
- Automatic cleanup of unused library docs
- Token-aware context injection
"""

from .detector import ProjectDetector
from .fetcher import CacheEntry, DocumentationFetcher
from .indexer import DocumentationIndexer
from .injector import ContextInjector
from .models import DocChunk, DocSearchResult, Language, Library, ProjectContext
from .sources import (
    LANGUAGE_DOCS,
    LIBRARY_DOCS,
    DocSource,
    get_doc_source,
    get_language_doc_source,
    get_priority_docs_for_project,
)

__all__ = [
    # Models
    "DocChunk",
    "DocSearchResult",
    "Language",
    "Library",
    "ProjectContext",
    # Detection
    "ProjectDetector",
    # Sources
    "DocSource",
    "LANGUAGE_DOCS",
    "LIBRARY_DOCS",
    "get_doc_source",
    "get_language_doc_source",
    "get_priority_docs_for_project",
    # Fetching
    "DocumentationFetcher",
    "CacheEntry",
    # Indexing
    "DocumentationIndexer",
    # Injection
    "ContextInjector",
]
