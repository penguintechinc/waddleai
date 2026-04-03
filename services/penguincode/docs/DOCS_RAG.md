# Documentation RAG System

## Table of Contents

- [Overview](#overview)
- [What is Documentation RAG](#what-is-documentation-rag)
- [Auto-Detection](#auto-detection-of-project-languages)
- [Supported Languages](#supported-languages)
- [Detection Methods](#how-detection-works)
- [Configuration](#configuration-options)
- [Indexing](#how-indexing-works)
- [RAG Retrieval](#rag-retrieval-and-enhancement)
- [Manual Configuration](#manual-configuration-for-libraries)
- [Troubleshooting](#troubleshooting-indexing-issues)

---

## Overview

PenguinCode's Documentation RAG (Retrieval-Augmented Generation) system automatically detects which programming languages and libraries your project uses, fetches their official documentation, and uses it to provide accurate, context-aware answers.

**Key Features**:
- **Automatic Detection** - Scans dependency files to find languages and libraries
- **Smart Caching** - TTL-based cache with configurable expiration (default 7 days)
- **Vector Search** - Uses ChromaDB for semantic document retrieval
- **Seamless Integration** - Auto-indexes docs on startup or on-demand
- **Multi-Language Support** - Python, JavaScript, TypeScript, Go, Rust, HCL, Ansible

---

## What is Documentation RAG

RAG (Retrieval-Augmented Generation) combines retrieval and generation for better answers:

1. **Retrieval**: Search indexed documentation for relevant content
2. **Augmentation**: Pass retrieved docs to the LLM as context
3. **Generation**: LLM generates answers grounded in official sources

Example: When you ask "How do I validate JSON with Pydantic?":
- System searches indexed Pydantic documentation
- Retrieves relevant chunks about validation
- Provides docs to Claude as context
- Claude generates answer with references to official docs

---

## Auto-Detection of Project Languages

PenguinCode automatically detects which languages your project uses by scanning:

### Detection Methods

1. **File Extensions**: Scans top-level and `src/` directories for language files
2. **Dependency Files**: Parses `pyproject.toml`, `package.json`, `go.mod`, `Cargo.toml`, etc.
3. **Configuration Files**: Checks for `tsconfig.json`, `ansible.cfg`, `.terraform.lock.hcl`
4. **Directory Structures**: Looks for `roles/`, `inventory/` (Ansible)

### Detection Flow

```
Project Directory
    ├── Scan file extensions (.py, .js, .ts, .go, .rs, .tf)
    ├── Parse dependency files
    ├── Check config files
    └── Build ProjectContext (languages + libraries)
```

---

## Supported Languages

| Language | Detection | Dependencies |
|----------|-----------|--------------|
| **Python** | `.py`, `pyproject.toml`, `requirements.txt` | PyDAL, FastAPI, Django, Flask, etc. |
| **JavaScript** | `.js`, `.jsx`, `package.json` | React, Express, Next.js, Axios, etc. |
| **TypeScript** | `.ts`, `.tsx`, `tsconfig.json` | Zod, Prisma, Vite, Vitest, etc. |
| **Go** | `.go`, `go.mod`, `go.sum` | Gin, Echo, GORM, Fiber, etc. |
| **Rust** | `.rs`, `Cargo.toml` | Tokio, Serde, Actix-web, Diesel, etc. |
| **HCL** | `.tf`, `.tofu`, `.terraform.lock.hcl` | AWS, Azure, Google Cloud, Kubernetes, etc. |
| **Ansible** | `ansible.cfg`, `roles/`, `requirements.yml` | Collections, roles from Ansible Galaxy |

### Python

**Detections**: Files with `.py`, `pyproject.toml`, `requirements.txt`, `setup.py`, `Pipfile`

**Libraries**: FastAPI, Django, Flask, SQLAlchemy, Pydantic, Pytest, NumPy, Pandas, Boto3, etc.

### JavaScript/TypeScript

**Detections**: Files with `.js`, `.jsx`, `.ts`, `.tsx`, `package.json`, `tsconfig.json`

**Libraries**: React, Vue, Next.js, Express, Axios, Prisma, Tailwind CSS, Vite, etc.

### Go

**Detections**: Files with `.go`, `go.mod`, `go.sum`

**Libraries**: Gin, Echo, Fiber, GORM, etc.

### Rust

**Detections**: Files with `.rs`, `Cargo.toml`

**Libraries**: Tokio, Serde, Actix-web, Reqwest, Diesel, etc.

### HCL (Terraform/OpenTofu)

**Detections**: Files with `.tf`, `.tofu`, `.terraform.lock.hcl`

**Providers**: AWS, Azure, Google Cloud, DigitalOcean, Cloudflare, Kubernetes, Docker, etc.

### Ansible

**Detections**: `ansible.cfg`, `playbook.yml`, `site.yml`, `roles/`, `inventory/`, `requirements.yml`

**Collections**: ansible.builtin, ansible.posix, community.docker, community.kubernetes, amazon.aws, azure.azcollection, etc.

---

## How Detection Works

### Python Detection

Triggers on any of:
- Files: `*.py`, `pyproject.toml`, `requirements.txt`, `setup.py`, `Pipfile`
- Parses dependencies with versions
- Extracts from `[project.dependencies]` and `[tool.poetry.dependencies]`

### JavaScript/TypeScript Detection

Triggers on:
- Files: `*.js`, `*.jsx`, `*.ts`, `*.tsx`, `package.json`
- TypeScript detected if `tsconfig.json` exists alongside JS
- Parses `dependencies` and `devDependencies`
- Filters: skips `@types/*` packages (type definitions)

### Go Detection

Triggers on:
- Files: `*.go`, `go.mod`, `go.sum`
- Parses `require` blocks and single-line `require` statements
- Includes version constraints

### Terraform/OpenTofu Detection

Triggers on:
- Files: `*.tf`, `*.tofu`, `.terraform.lock.hcl`
- Parses `provider` blocks and `required_providers`
- Extracts from lock file for locked versions

### Ansible Detection

Triggers on:
- Files: `ansible.cfg`, `playbook.yml`, `site.yml`
- Directories: `roles/`, `inventory/`
- Files: `requirements.yml`, `galaxy.yml`
- Parses collections and roles from requirements

---

## Configuration Options

All options are in `config.yaml` under the `docs_rag` section:

```yaml
docs_rag:
  enabled: true                    # Master switch (true/false)
  auto_detect_on_start: true      # Detect languages at startup
  auto_detect_on_request: true    # Detect from request content
  auto_index_on_detect: true      # Index docs when detected
  auto_index_on_request: true     # Index on-demand when needed

  # Manual language configuration
  languages_manual:
    python: false        # false = auto-detect, true = force index
    javascript: false
    typescript: false
    go: false
    rust: false
    hcl: false
    ansible: false

  # Priority libraries to always index
  libraries_manual: []
    # - fastapi
    # - pytest
    # - numpy

  # Cache settings
  cache_dir: "./.penguincode/docs"
  cache_max_age_days: 7           # TTL for cached docs
  max_pages_per_library: 50       # Max pages to fetch per library
  max_libraries_to_index: 20      # Max total libraries indexed
```

### Configuration Behaviors

**`auto_detect_on_start: true`**
- At startup, scans project directory for languages and libraries
- Builds ProjectContext from dependencies
- Prepares for auto_index_on_detect

**`auto_detect_on_request: true`**
- Analyzes user request for language keywords
- Dynamically updates library list based on request
- Overrides manual_languages if detected in request

**`auto_index_on_detect: true`**
- When language detected, automatically fetches and indexes docs
- Happens at startup if auto_detect_on_start is true
- Respects max_libraries_to_index limit

**`auto_index_on_request: true`**
- If user asks about library not yet indexed, fetches on-demand
- Useful for multi-language projects
- Prevents blocking on startup

**`languages_manual`**
- `false`: Use auto-detection (default)
- `true`: Force index language even if not detected
- Manual settings act as baseline; auto-detect adds to them

**`libraries_manual`**
- List of library names to prioritize
- Examples: `["fastapi", "pytest", "numpy"]`
- Indexed before auto-detected libraries
- Respects max_libraries_to_index limit

### Cache Settings

**`cache_dir`**: Where to store fetched documentation
- Default: `./.penguincode/docs`
- Stores HTML converted to markdown
- Maintains cache index JSON

**`cache_max_age_days`**: How long to keep cached docs
- Default: `7` days
- After N days, docs re-fetched on next request
- Ensures latest documentation is available

**`max_pages_per_library`**: Limit docs fetched per library
- Default: `50` pages
- Includes base_url, api_docs_path, and guide_path
- Balances coverage vs. storage/performance

**`max_libraries_to_index`**: Total libraries to index
- Default: `20` libraries
- Limits vector store and memory usage
- Priority: manual libraries first, then detected

---

## How Indexing Works

### Indexing Pipeline

```
1. Detect Library/Language
   └─ Parse dependency file
   └─ Extract name + version

2. Fetch Documentation
   └─ Query official source
   └─ Convert HTML to markdown
   └─ Cache locally with TTL

3. Chunk Documentation
   └─ Split into 1000-char chunks
   └─ 200-char overlap
   └─ Preserve structure

4. Generate Embeddings
   └─ Use Ollama's nomic-embed-text
   └─ Async embedding generation
   └─ Handle errors gracefully

5. Store in ChromaDB
   └─ Cosine similarity metric
   └─ Track metadata (library, version, url)
   └─ Update index metadata
```

### Caching Strategy

Documentation is cached locally to avoid re-fetching:

```
Cache Structure:
./.penguincode/docs/
├── cache_index.json          # Index of all cached pages
├── {hash}.md                 # Fetched page (markdown)
└── ...

Metadata Per Entry:
{
  "url": "https://docs.fastapi.io/",
  "fetch_time": "2025-12-28T10:30:00",
  "ttl_days": 7,
  "library": "fastapi",
  "language": "python"
}
```

### Expiration and Cleanup

- Cached entries expire after `cache_max_age_days`
- Expired entries removed on next fetch
- If library removed from project, cached docs auto-deleted
- Metadata tracked for each entry

### Storage Structure

```
Index Storage:
./.penguincode/docs_index/
├── index_metadata.json       # Libraries/languages indexed
└── [ChromaDB vector storage]

Metadata Tracks:
- Library name + version
- Index timestamp
- Chunk count per library
- Document metadata
```

---

## RAG Retrieval and Enhancement

### Search Flow

When you ask a question:

```python
1. Analyze question
   └─ Detect mentioned libraries/languages
   └─ Filter to relevant docs

2. Generate Query Embedding
   └─ Convert question to vector
   └─ Send to Ollama embedding model

3. Vector Search
   └─ Search ChromaDB for similar chunks
   └─ Use cosine similarity metric
   └─ Return top 5 results (default)

4. Rank by Relevance
   └─ Score: 1.0 - distance
   └─ Include source URL + library
   └─ Format for LLM context

5. Augment Prompt
   └─ Add retrieved docs to prompt
   └─ Claude generates grounded answer
   └─ Includes citations to sources
```

### Search Filtering

Search can filter to:
- Specific libraries: `["fastapi", "pydantic"]`
- Specific languages: `["python", "javascript"]`
- Or search all indexed docs

---

## Manual Configuration for Libraries

To always index specific libraries, add to `libraries_manual`:

```yaml
docs_rag:
  libraries_manual:
    - fastapi
    - pytest
    - numpy
    - react
    - prisma
```

For projects using many languages/libraries:

```yaml
docs_rag:
  libraries_manual:
    - fastapi        # Always index these
    - django
    - pydantic
  max_libraries_to_index: 25  # Increase limit
  cache_max_age_days: 14      # Longer cache TTL
```

To force index a language even if not detected:

```yaml
docs_rag:
  languages_manual:
    python: true     # Force Python docs
    go: false        # Let auto-detect decide
```

---

## Troubleshooting Indexing Issues

### Embedding Model Not Found

**Error**: `Embedding failed: model not found`

**Solution**:
```bash
ollama pull nomic-embed-text
ollama list | grep nomic
```

### Slow Documentation Fetching

**Problem**: Indexing takes 30+ seconds

**Solution**:
```yaml
docs_rag:
  max_pages_per_library: 30    # Reduce from 50
  max_libraries_to_index: 10   # Index fewer libraries
```

### Cache Growing Too Large

**Problem**: `.penguincode/docs/` exceeds disk space

**Solution**:
```bash
# Delete cache and re-index (fresh fetches on next request)
rm -rf ./.penguincode/docs/cache_index.json
```

Or reduce TTL:
```yaml
docs_rag:
  cache_max_age_days: 3        # Delete old docs after 3 days
```

### Library Not Found in Docs

**Problem**: Documentation for library not indexed

**Solution**:
```yaml
docs_rag:
  auto_index_on_request: true    # Enable on-demand indexing
  libraries_manual:
    - your-custom-library        # Add to manual list
```

### ChromaDB Initialization Error

**Problem**: `Failed to initialize ChromaDB`

**Solution**:
```bash
# Delete index and rebuild
rm -rf ./.penguincode/docs_index/
pip install --upgrade chromadb
```

### Out of Memory During Indexing

**Problem**: System runs out of memory while indexing

**Solution**:
```yaml
docs_rag:
  max_pages_per_library: 20     # Reduce page count
  max_libraries_to_index: 5     # Index fewer libraries
```

---

## Configuration Examples

### Minimal Setup (Single Language)

```yaml
docs_rag:
  enabled: true
  auto_detect_on_start: true
  auto_index_on_detect: true
  languages_manual:
    python: true
```

### Full-Featured Setup (Multi-Language)

```yaml
docs_rag:
  enabled: true
  auto_detect_on_start: true
  auto_detect_on_request: true
  auto_index_on_detect: true
  auto_index_on_request: true
  libraries_manual:
    - fastapi
    - react
    - pydantic
  cache_max_age_days: 14
  max_pages_per_library: 75
  max_libraries_to_index: 30
```

### Lightweight Setup (Resource-Constrained)

```yaml
docs_rag:
  enabled: true
  auto_detect_on_start: true
  auto_index_on_detect: true
  auto_index_on_request: false
  libraries_manual:
    - fastapi
  cache_max_age_days: 30
  max_pages_per_library: 20
  max_libraries_to_index: 5
```

---

## Architecture

### System Components

```
docs_rag/
├── models.py      # Data models (Language, Library, DocChunk, etc.)
├── detector.py    # Project detection from dependency files
├── sources.py     # Documentation URL mappings
├── fetcher.py     # HTTP fetching with TTL cache
├── indexer.py     # ChromaDB vector storage
└── injector.py    # Context injection for prompts
```

### Component Responsibilities

**Detector**: Scans dependency files to extract languages and libraries
- Parses pyproject.toml, package.json, go.mod, etc.
- Returns ProjectContext with detected languages and libraries

**Fetcher**: Downloads documentation from official sources
- Converts HTML to markdown
- Implements TTL-based caching
- Manages cache expiration and cleanup

**Indexer**: Stores documentation in vector database
- Chunks text into overlapping segments
- Generates embeddings via Ollama
- Stores in ChromaDB for semantic search
- Tracks indexing metadata

**Sources**: Maps libraries to documentation URLs
- LANGUAGE_DOCS: Official docs for each language
- LIBRARY_DOCS: Popular libraries and their doc sources
- get_doc_source(): Looks up source by library name

### Data Flow

```
Project Scan
    │
    ▼
ProjectDetector
    │ (returns: ProjectContext)
    ▼
DocumentationFetcher
    │ (downloads & converts to markdown)
    ▼
Cache Layer
    │ (stores with TTL metadata)
    ▼
DocumentationIndexer
    │ (chunks, embeds, stores)
    ▼
ChromaDB
    │ (vector storage)
    ▼
Vector Search
    │ (semantic similarity)
    ▼
LLM Context
    │ (injected into prompts)
    ▼
User Response
```

---

**Learn More**:
- See implementation files in `/home/penguin/code/PenguinCode/penguincode/docs_rag/`
- Configuration examples above
- Troubleshooting section for common issues

**Last Updated**: 2025-12-28
**See Also**: [USAGE.md](USAGE.md), [ARCHITECTURE.md](ARCHITECTURE.md), [STANDARDS.md](STANDARDS.md)
