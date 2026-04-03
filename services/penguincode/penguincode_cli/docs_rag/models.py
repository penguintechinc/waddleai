"""Data models for the documentation RAG system."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Language(Enum):
    """Supported programming languages."""

    PYTHON = "python"
    JAVASCRIPT = "javascript"
    TYPESCRIPT = "typescript"
    GO = "go"
    RUST = "rust"
    HCL = "hcl"  # OpenTofu/Terraform
    ANSIBLE = "ansible"
    RUBY = "ruby"
    PHP = "php"
    DART = "dart"


@dataclass
class Library:
    """Represents a detected library/dependency."""

    name: str
    language: Language
    version: str | None = None
    doc_url: str | None = None

    def __hash__(self) -> int:
        return hash((self.name, self.language))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Library):
            return False
        return self.name == other.name and self.language == other.language


@dataclass
class ProjectContext:
    """Detected project context including languages and libraries."""

    languages: list[Language] = field(default_factory=list)
    libraries: list[Library] = field(default_factory=list)
    dependency_files: dict[str, str] = field(default_factory=dict)  # path -> content
    detection_timestamp: str = field(
        default_factory=lambda: datetime.now().isoformat()
    )

    @property
    def language_names(self) -> list[str]:
        """Get list of language names."""
        return [lang.value for lang in self.languages]

    @property
    def library_names(self) -> list[str]:
        """Get list of library names."""
        return [lib.name for lib in self.libraries]

    def has_language(self, language: Language) -> bool:
        """Check if project uses a specific language."""
        return language in self.languages

    def get_libraries_for_language(self, language: Language) -> list[Library]:
        """Get libraries for a specific language."""
        return [lib for lib in self.libraries if lib.language == language]


@dataclass
class DocChunk:
    """A chunk of documentation for indexing."""

    content: str
    metadata: dict[str, str]  # library, version, section, url, language
    chunk_id: str

    @property
    def library(self) -> str:
        """Get library name from metadata."""
        return self.metadata.get("library", "")

    @property
    def section(self) -> str:
        """Get section from metadata."""
        return self.metadata.get("section", "")

    @property
    def url(self) -> str:
        """Get source URL from metadata."""
        return self.metadata.get("url", "")


@dataclass
class DocSearchResult:
    """Result from documentation search."""

    content: str
    library: str
    section: str
    relevance_score: float
    url: str = ""
    language: str = ""

    def __str__(self) -> str:
        """String representation for display."""
        header = f"[{self.library}]"
        if self.section:
            header += f" {self.section}"
        return f"{header}\n{self.content}"
