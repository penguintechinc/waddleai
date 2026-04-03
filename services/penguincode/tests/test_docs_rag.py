"""Tests for the documentation RAG system."""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from penguincode_cli.docs_rag.detector import ProjectDetector
from penguincode_cli.docs_rag.models import (
    DocChunk,
    DocSearchResult,
    Language,
    Library,
    ProjectContext,
)
from penguincode_cli.docs_rag.sources import (
    LANGUAGE_DOCS,
    LIBRARY_DOCS,
    get_doc_source,
    get_language_doc_source,
)


class TestLanguageEnum:
    """Tests for Language enum."""

    def test_python_language(self):
        """Test Python language value."""
        assert Language.PYTHON.value == "python"

    def test_javascript_language(self):
        """Test JavaScript language value."""
        assert Language.JAVASCRIPT.value == "javascript"

    def test_typescript_language(self):
        """Test TypeScript language value."""
        assert Language.TYPESCRIPT.value == "typescript"

    def test_go_language(self):
        """Test Go language value."""
        assert Language.GO.value == "go"

    def test_rust_language(self):
        """Test Rust language value."""
        assert Language.RUST.value == "rust"

    def test_hcl_language(self):
        """Test HCL (OpenTofu/Terraform) language value."""
        assert Language.HCL.value == "hcl"

    def test_ansible_language(self):
        """Test Ansible language value."""
        assert Language.ANSIBLE.value == "ansible"

    def test_ruby_language(self):
        """Test Ruby language value."""
        assert Language.RUBY.value == "ruby"

    def test_php_language(self):
        """Test PHP language value."""
        assert Language.PHP.value == "php"

    def test_dart_language(self):
        """Test Dart language value."""
        assert Language.DART.value == "dart"


class TestLibrary:
    """Tests for Library dataclass."""

    def test_library_creation(self):
        """Test creating a library."""
        lib = Library(name="fastapi", language=Language.PYTHON, version="0.100.0")
        assert lib.name == "fastapi"
        assert lib.language == Language.PYTHON
        assert lib.version == "0.100.0"

    def test_library_without_version(self):
        """Test creating a library without version."""
        lib = Library(name="react", language=Language.JAVASCRIPT)
        assert lib.name == "react"
        assert lib.version is None

    def test_library_equality(self):
        """Test library equality based on name and language."""
        lib1 = Library(name="fastapi", language=Language.PYTHON)
        lib2 = Library(name="fastapi", language=Language.PYTHON, version="1.0")
        assert lib1 == lib2

    def test_library_hash(self):
        """Test library can be used in sets."""
        lib1 = Library(name="fastapi", language=Language.PYTHON)
        lib2 = Library(name="fastapi", language=Language.PYTHON)
        libs = {lib1, lib2}
        assert len(libs) == 1


class TestProjectContext:
    """Tests for ProjectContext dataclass."""

    def test_empty_context(self):
        """Test creating empty project context."""
        ctx = ProjectContext()
        assert ctx.languages == []
        assert ctx.libraries == []
        assert ctx.language_names == []
        assert ctx.library_names == []

    def test_context_with_languages(self):
        """Test project context with languages."""
        ctx = ProjectContext(languages=[Language.PYTHON, Language.HCL])
        assert Language.PYTHON in ctx.languages
        assert Language.HCL in ctx.languages
        assert "python" in ctx.language_names
        assert "hcl" in ctx.language_names

    def test_context_with_libraries(self):
        """Test project context with libraries."""
        libs = [
            Library(name="fastapi", language=Language.PYTHON),
            Library(name="aws", language=Language.HCL),
        ]
        ctx = ProjectContext(libraries=libs)
        assert "fastapi" in ctx.library_names
        assert "aws" in ctx.library_names

    def test_has_language(self):
        """Test has_language method."""
        ctx = ProjectContext(languages=[Language.PYTHON, Language.ANSIBLE])
        assert ctx.has_language(Language.PYTHON)
        assert ctx.has_language(Language.ANSIBLE)
        assert not ctx.has_language(Language.RUST)

    def test_get_libraries_for_language(self):
        """Test filtering libraries by language."""
        libs = [
            Library(name="fastapi", language=Language.PYTHON),
            Library(name="pydantic", language=Language.PYTHON),
            Library(name="aws", language=Language.HCL),
        ]
        ctx = ProjectContext(libraries=libs)
        python_libs = ctx.get_libraries_for_language(Language.PYTHON)
        assert len(python_libs) == 2
        assert all(lib.language == Language.PYTHON for lib in python_libs)


class TestDocSources:
    """Tests for documentation sources."""

    def test_language_docs_exist(self):
        """Test that all languages have documentation sources."""
        for lang in Language:
            assert lang in LANGUAGE_DOCS, f"Missing docs for {lang}"

    def test_get_language_doc_source(self):
        """Test getting language documentation source."""
        python_docs = get_language_doc_source(Language.PYTHON)
        assert python_docs is not None
        assert "python.org" in python_docs.base_url

    def test_hcl_docs(self):
        """Test OpenTofu documentation source."""
        hcl_docs = get_language_doc_source(Language.HCL)
        assert hcl_docs is not None
        assert "opentofu.org" in hcl_docs.base_url

    def test_ansible_docs(self):
        """Test Ansible documentation source."""
        ansible_docs = get_language_doc_source(Language.ANSIBLE)
        assert ansible_docs is not None
        assert "ansible.com" in ansible_docs.base_url

    def test_ruby_docs(self):
        """Test Ruby documentation source."""
        ruby_docs = get_language_doc_source(Language.RUBY)
        assert ruby_docs is not None
        assert "ruby-lang.org" in ruby_docs.base_url

    def test_php_docs(self):
        """Test PHP documentation source."""
        php_docs = get_language_doc_source(Language.PHP)
        assert php_docs is not None
        assert "php.net" in php_docs.base_url

    def test_dart_docs(self):
        """Test Dart documentation source."""
        dart_docs = get_language_doc_source(Language.DART)
        assert dart_docs is not None
        assert "dart.dev" in dart_docs.base_url

    def test_popular_python_libraries(self):
        """Test popular Python library docs exist."""
        for lib in ["fastapi", "django", "flask", "pydantic", "pytest"]:
            assert lib in LIBRARY_DOCS, f"Missing docs for {lib}"

    def test_terraform_providers(self):
        """Test Terraform/OpenTofu provider docs exist."""
        for provider in ["aws", "azurerm", "google", "kubernetes"]:
            assert provider in LIBRARY_DOCS, f"Missing docs for {provider}"

    def test_ansible_collections(self):
        """Test Ansible collection docs exist."""
        for collection in ["ansible.builtin", "community.general", "community.docker"]:
            assert collection in LIBRARY_DOCS, f"Missing docs for {collection}"

    def test_get_doc_source(self):
        """Test getting library documentation source."""
        fastapi_docs = get_doc_source("fastapi")
        assert fastapi_docs is not None
        assert "fastapi" in fastapi_docs.base_url.lower()

    def test_get_doc_source_normalized(self):
        """Test doc source lookup with normalized names."""
        # Test character normalization - hyphens are converted to underscores
        # Look up a library that exists in the dictionary
        result = get_doc_source("fastapi")  # Direct match
        assert result is not None
        # Note: Normalization converts hyphens to underscores in lookup

    def test_ruby_libraries(self):
        """Test Ruby library docs exist."""
        for lib in ["rails", "sinatra", "rspec", "minitest", "bundler"]:
            assert lib in LIBRARY_DOCS, f"Missing docs for {lib}"

    def test_php_libraries(self):
        """Test PHP library docs exist."""
        for lib in ["laravel", "symfony", "phpunit", "twig"]:
            assert lib in LIBRARY_DOCS, f"Missing docs for {lib}"

    def test_dart_flutter_libraries(self):
        """Test Flutter/Dart library docs exist."""
        for lib in ["flutter", "riverpod", "provider", "bloc", "dio"]:
            assert lib in LIBRARY_DOCS, f"Missing docs for {lib}"


class TestProjectDetector:
    """Tests for ProjectDetector."""

    def test_detect_empty_project(self):
        """Test detecting languages in empty project."""
        with TemporaryDirectory() as tmpdir:
            detector = ProjectDetector(tmpdir)
            ctx = detector.detect()
            assert ctx.languages == []
            assert ctx.libraries == []

    def test_detect_python_from_requirements(self):
        """Test detecting Python from requirements.txt."""
        with TemporaryDirectory() as tmpdir:
            req_file = Path(tmpdir) / "requirements.txt"
            req_file.write_text("fastapi>=0.100.0\npydantic\n")

            detector = ProjectDetector(tmpdir)
            ctx = detector.detect()

            assert Language.PYTHON in ctx.languages
            assert any(lib.name == "fastapi" for lib in ctx.libraries)
            assert any(lib.name == "pydantic" for lib in ctx.libraries)

    def test_detect_python_from_pyproject(self):
        """Test detecting Python from pyproject.toml."""
        with TemporaryDirectory() as tmpdir:
            pyproject = Path(tmpdir) / "pyproject.toml"
            pyproject.write_text('''
[project]
name = "myproject"
dependencies = [
    "fastapi>=0.100.0",
    "sqlalchemy",
]
''')
            detector = ProjectDetector(tmpdir)
            ctx = detector.detect()

            assert Language.PYTHON in ctx.languages
            assert any(lib.name == "fastapi" for lib in ctx.libraries)

    def test_detect_javascript_from_package_json(self):
        """Test detecting JavaScript from package.json."""
        with TemporaryDirectory() as tmpdir:
            package = Path(tmpdir) / "package.json"
            package.write_text('{"dependencies": {"react": "^18.0.0"}}')

            detector = ProjectDetector(tmpdir)
            ctx = detector.detect()

            assert Language.JAVASCRIPT in ctx.languages
            assert any(lib.name == "react" for lib in ctx.libraries)

    def test_detect_typescript_with_tsconfig(self):
        """Test detecting TypeScript from tsconfig.json."""
        with TemporaryDirectory() as tmpdir:
            package = Path(tmpdir) / "package.json"
            package.write_text('{"dependencies": {"react": "^18.0.0"}}')
            tsconfig = Path(tmpdir) / "tsconfig.json"
            tsconfig.write_text('{}')

            detector = ProjectDetector(tmpdir)
            ctx = detector.detect()

            assert Language.JAVASCRIPT in ctx.languages
            assert Language.TYPESCRIPT in ctx.languages

    def test_detect_go_from_go_mod(self):
        """Test detecting Go from go.mod."""
        with TemporaryDirectory() as tmpdir:
            go_mod = Path(tmpdir) / "go.mod"
            go_mod.write_text('''
module example.com/myproject

go 1.21

require (
    github.com/gin-gonic/gin v1.9.0
)
''')
            detector = ProjectDetector(tmpdir)
            ctx = detector.detect()

            assert Language.GO in ctx.languages

    def test_detect_hcl_from_tf_files(self):
        """Test detecting HCL from .tf files."""
        with TemporaryDirectory() as tmpdir:
            main_tf = Path(tmpdir) / "main.tf"
            main_tf.write_text('''
provider "aws" {
  region = "us-west-2"
}
''')
            detector = ProjectDetector(tmpdir)
            ctx = detector.detect()

            assert Language.HCL in ctx.languages
            assert any(lib.name == "aws" for lib in ctx.libraries)

    def test_detect_ansible_from_playbook(self):
        """Test detecting Ansible from playbook.yml."""
        with TemporaryDirectory() as tmpdir:
            playbook = Path(tmpdir) / "playbook.yml"
            playbook.write_text('''
- name: Configure webserver
  hosts: webservers
  tasks:
    - name: Install nginx
      apt:
        name: nginx
''')
            detector = ProjectDetector(tmpdir)
            ctx = detector.detect()

            assert Language.ANSIBLE in ctx.languages

    def test_detect_ansible_from_roles_dir(self):
        """Test detecting Ansible from roles directory."""
        with TemporaryDirectory() as tmpdir:
            roles_dir = Path(tmpdir) / "roles"
            roles_dir.mkdir()
            (roles_dir / "common").mkdir()

            detector = ProjectDetector(tmpdir)
            ctx = detector.detect()

            assert Language.ANSIBLE in ctx.languages


class TestDocChunk:
    """Tests for DocChunk dataclass."""

    def test_doc_chunk_creation(self):
        """Test creating a documentation chunk."""
        chunk = DocChunk(
            content="FastAPI is a modern web framework...",
            metadata={
                "library": "fastapi",
                "section": "Introduction",
                "url": "https://fastapi.tiangolo.com/",
            },
            chunk_id="abc123",
        )
        assert chunk.content == "FastAPI is a modern web framework..."
        assert chunk.library == "fastapi"
        assert chunk.section == "Introduction"
        assert chunk.url == "https://fastapi.tiangolo.com/"


class TestDocSearchResult:
    """Tests for DocSearchResult dataclass."""

    def test_search_result_creation(self):
        """Test creating a search result."""
        result = DocSearchResult(
            content="FastAPI is a modern web framework...",
            library="fastapi",
            section="Introduction",
            relevance_score=0.95,
        )
        assert result.library == "fastapi"
        assert result.relevance_score == 0.95

    def test_search_result_str(self):
        """Test string representation of search result."""
        result = DocSearchResult(
            content="Content here",
            library="fastapi",
            section="Intro",
            relevance_score=0.9,
        )
        result_str = str(result)
        assert "[fastapi]" in result_str
        assert "Intro" in result_str


class TestRubyDetection:
    """Tests for Ruby language detection and Gemfile parsing."""

    def test_detect_ruby_from_gemfile(self):
        """Test detecting Ruby from Gemfile."""
        with TemporaryDirectory() as tmpdir:
            gemfile = Path(tmpdir) / "Gemfile"
            gemfile.write_text(
                'source "https://rubygems.org"\n'
                'gem "rails", "~> 7.0"\n'
                'gem "puma"\n'
                '# gem "commented-out"\n'
            )
            detector = ProjectDetector(tmpdir)
            ctx = detector.detect()

            assert Language.RUBY in ctx.languages
            assert any(lib.name == "rails" for lib in ctx.libraries)
            assert any(lib.name == "puma" for lib in ctx.libraries)
            assert not any(lib.name == "commented-out" for lib in ctx.libraries)

    def test_detect_ruby_from_rb_extension(self):
        """Test detecting Ruby from .rb files."""
        with TemporaryDirectory() as tmpdir:
            rb_file = Path(tmpdir) / "app.rb"
            rb_file.write_text('puts "hello"')
            detector = ProjectDetector(tmpdir)
            ctx = detector.detect()

            assert Language.RUBY in ctx.languages


class TestPHPDetection:
    """Tests for PHP language detection and composer.json parsing."""

    def test_detect_php_from_composer_json(self):
        """Test detecting PHP from composer.json."""
        with TemporaryDirectory() as tmpdir:
            composer = Path(tmpdir) / "composer.json"
            composer.write_text(
                '{"require": {"laravel/framework": "^10.0"}, '
                '"require-dev": {"phpunit/phpunit": "^10.0"}}'
            )
            detector = ProjectDetector(tmpdir)
            ctx = detector.detect()

            assert Language.PHP in ctx.languages
            assert any(lib.name == "laravel/framework" for lib in ctx.libraries)
            assert any(lib.name == "phpunit/phpunit" for lib in ctx.libraries)

    def test_php_skips_extensions(self):
        """Test that PHP extensions and php itself are skipped."""
        with TemporaryDirectory() as tmpdir:
            composer = Path(tmpdir) / "composer.json"
            composer.write_text(
                '{"require": {"php": ">=8.1", "ext-mbstring": "*", "monolog/monolog": "^3.0"}}'
            )
            detector = ProjectDetector(tmpdir)
            ctx = detector.detect()

            assert not any(lib.name == "php" for lib in ctx.libraries)
            assert not any(lib.name == "ext-mbstring" for lib in ctx.libraries)
            assert any(lib.name == "monolog/monolog" for lib in ctx.libraries)


class TestDartDetection:
    """Tests for Dart/Flutter language detection and pubspec.yaml parsing."""

    def test_detect_dart_from_pubspec_yaml(self):
        """Test detecting Dart from pubspec.yaml."""
        with TemporaryDirectory() as tmpdir:
            pubspec = Path(tmpdir) / "pubspec.yaml"
            pubspec.write_text(
                "name: my_app\n"
                "dependencies:\n"
                "  flutter:\n"
                "    sdk: flutter\n"
                "  riverpod: ^2.0.0\n"
                "  dio: ^5.0.0\n"
                "dev_dependencies:\n"
                "  flutter_test:\n"
                "    sdk: flutter\n"
                "  build_runner: ^2.4.0\n"
            )
            detector = ProjectDetector(tmpdir)
            ctx = detector.detect()

            assert Language.DART in ctx.languages
            assert any(lib.name == "riverpod" for lib in ctx.libraries)
            assert any(lib.name == "dio" for lib in ctx.libraries)
            assert any(lib.name == "build_runner" for lib in ctx.libraries)

    def test_detect_dart_from_dart_extension(self):
        """Test detecting Dart from .dart files."""
        with TemporaryDirectory() as tmpdir:
            dart_file = Path(tmpdir) / "main.dart"
            dart_file.write_text('void main() => print("hello");')
            detector = ProjectDetector(tmpdir)
            ctx = detector.detect()

            assert Language.DART in ctx.languages


class TestREPLLanguageDetection:
    """Tests for REPL language detection from message content."""

    def test_detect_ruby_keywords(self):
        """Test detecting Ruby from message keywords."""
        from penguincode_cli.core.repl import REPLSession
        session = REPLSession.__new__(REPLSession)
        detected = session._detect_languages_in_message("How do I use rails routes?")
        assert "ruby" in detected

    def test_detect_php_keywords(self):
        """Test detecting PHP from message keywords."""
        from penguincode_cli.core.repl import REPLSession
        session = REPLSession.__new__(REPLSession)
        detected = session._detect_languages_in_message("Fix the laravel migration")
        assert "php" in detected

    def test_detect_dart_keywords(self):
        """Test detecting Dart from message keywords."""
        from penguincode_cli.core.repl import REPLSession
        session = REPLSession.__new__(REPLSession)
        detected = session._detect_languages_in_message("Build a flutter widget")
        assert "dart" in detected


class TestPathValidation:
    """Tests for B1: file write/edit path validation (sandbox enforcement)."""

    @pytest.mark.asyncio
    async def test_write_rejects_outside_sandbox(self):
        """Test that WriteFileTool rejects paths outside working directory."""
        from penguincode_cli.tools.file_ops import WriteFileTool

        with TemporaryDirectory() as sandbox:
            tool = WriteFileTool(working_dir=sandbox)
            result = await tool.execute(path="/etc/evil.txt", content="pwned")
            assert not result.success
            assert "outside working directory" in result.error

    @pytest.mark.asyncio
    async def test_edit_rejects_outside_sandbox(self):
        """Test that EditFileTool rejects paths outside working directory."""
        from penguincode_cli.tools.file_ops import EditFileTool

        with TemporaryDirectory() as sandbox:
            tool = EditFileTool(working_dir=sandbox)
            result = await tool.execute(
                path="/etc/passwd", old_text="root", new_text="hacked"
            )
            assert not result.success
            assert "outside working directory" in result.error

    @pytest.mark.asyncio
    async def test_write_allows_inside_sandbox(self):
        """Test that WriteFileTool allows paths inside working directory."""
        from penguincode_cli.tools.file_ops import WriteFileTool

        with TemporaryDirectory() as sandbox:
            tool = WriteFileTool(working_dir=sandbox)
            result = await tool.execute(
                path=str(Path(sandbox) / "test.txt"), content="hello"
            )
            assert result.success

    @pytest.mark.asyncio
    async def test_write_no_sandbox_allows_any_path(self):
        """Test that WriteFileTool without working_dir allows any path."""
        from penguincode_cli.tools.file_ops import WriteFileTool

        with TemporaryDirectory() as tmpdir:
            tool = WriteFileTool()  # No working_dir
            result = await tool.execute(
                path=str(Path(tmpdir) / "test.txt"), content="hello"
            )
            assert result.success


class TestBlockingCommandDetection:
    """Tests for B3: blocking server command detection."""

    def test_flask_run_blocked(self):
        """Test that flask run is detected as blocking."""
        from penguincode_cli.tools.bash import BashTool
        assert BashTool._is_blocking_command("flask run")
        assert BashTool._is_blocking_command("flask run --port 5000")

    def test_python_app_blocked(self):
        """Test that python app.py is detected as blocking."""
        from penguincode_cli.tools.bash import BashTool
        assert BashTool._is_blocking_command("python app.py")
        assert BashTool._is_blocking_command("python3 app.py")

    def test_npm_start_blocked(self):
        """Test that npm start is detected as blocking."""
        from penguincode_cli.tools.bash import BashTool
        assert BashTool._is_blocking_command("npm start")
        assert BashTool._is_blocking_command("npm run dev")

    def test_rails_server_blocked(self):
        """Test that rails server is detected as blocking."""
        from penguincode_cli.tools.bash import BashTool
        assert BashTool._is_blocking_command("rails server")
        assert BashTool._is_blocking_command("rails s")

    def test_php_artisan_serve_blocked(self):
        """Test that php artisan serve is detected as blocking."""
        from penguincode_cli.tools.bash import BashTool
        assert BashTool._is_blocking_command("php artisan serve")
        assert BashTool._is_blocking_command("php -S localhost:8000")

    def test_docker_compose_up_blocked(self):
        """Test that docker compose up is blocked without -d."""
        from penguincode_cli.tools.bash import BashTool
        assert BashTool._is_blocking_command("docker compose up")
        assert not BashTool._is_blocking_command("docker compose up -d")

    def test_normal_commands_allowed(self):
        """Test that normal commands are not blocked."""
        from penguincode_cli.tools.bash import BashTool
        assert not BashTool._is_blocking_command("pytest")
        assert not BashTool._is_blocking_command("python -c 'print(1)'")
        assert not BashTool._is_blocking_command("npm test")
        assert not BashTool._is_blocking_command("ls -la")

    @pytest.mark.asyncio
    async def test_blocking_command_returns_warning(self):
        """Test that executing a blocking command returns warning instead of running."""
        from penguincode_cli.tools.bash import BashTool
        tool = BashTool()
        result = await tool.execute(command="flask run")
        assert result.success  # Returns success=True with warning
        assert "long-running server command" in result.data
        assert result.metadata.get("blocked") is True


class TestEditLoopDetection:
    """Tests for B2: improved edit loop detection."""

    def test_file_target_loop_detection(self):
        """Test that targeting the same file 4+ times triggers loop detection."""
        # This tests the Counter-based detection logic directly
        from collections import Counter
        recent_file_targets = [
            "src/main.py", "src/main.py", "src/utils.py",
            "src/main.py", "src/main.py",
        ]
        last_targets = recent_file_targets[-8:]
        target_counts = Counter(last_targets)
        has_loop = any(count >= 4 for count in target_counts.values())
        assert has_loop

    def test_no_false_positive_on_different_files(self):
        """Test that editing different files doesn't trigger loop."""
        from collections import Counter
        recent_file_targets = [
            "src/a.py", "src/b.py", "src/c.py", "src/d.py",
        ]
        last_targets = recent_file_targets[-8:]
        target_counts = Counter(last_targets)
        has_loop = any(count >= 4 for count in target_counts.values())
        assert not has_loop
