"""Project language and library detection.

Detects which languages and libraries a project uses by parsing
dependency files. Only detected libraries will have docs indexed,
preventing RAG bloat.
"""

import re
from pathlib import Path

from .models import Language, Library, ProjectContext

# File patterns that indicate a language
LANGUAGE_INDICATORS: dict[Language, list[str]] = {
    Language.PYTHON: ["*.py", "pyproject.toml", "requirements.txt", "setup.py", "Pipfile"],
    Language.JAVASCRIPT: ["*.js", "*.jsx", "*.mjs", "package.json"],
    Language.TYPESCRIPT: ["*.ts", "*.tsx", "tsconfig.json"],
    Language.GO: ["*.go", "go.mod", "go.sum"],
    Language.RUST: ["*.rs", "Cargo.toml"],
    Language.HCL: ["*.tf", "*.tofu", ".terraform.lock.hcl"],
    Language.ANSIBLE: ["ansible.cfg", "playbook.yml", "playbook.yaml", "site.yml", "site.yaml"],
    Language.RUBY: ["Gemfile", "Rakefile", ".ruby-version", "config.ru"],
    Language.PHP: ["composer.json", "artisan", "index.php", ".php-version"],
    Language.DART: ["pubspec.yaml", "analysis_options.yaml", ".dart_tool"],
}

# Dependency files and their languages
DEPENDENCY_FILES: dict[str, Language] = {
    "pyproject.toml": Language.PYTHON,
    "requirements.txt": Language.PYTHON,
    "setup.py": Language.PYTHON,
    "Pipfile": Language.PYTHON,
    "package.json": Language.JAVASCRIPT,  # Also covers TypeScript
    "go.mod": Language.GO,
    "Cargo.toml": Language.RUST,
    "Gemfile": Language.RUBY,
    "Gemfile.lock": Language.RUBY,
    "composer.json": Language.PHP,
    "composer.lock": Language.PHP,
    "pubspec.yaml": Language.DART,
    "pubspec.lock": Language.DART,
}


class ProjectDetector:
    """Detects project languages and libraries from dependency files."""

    def __init__(self, project_dir: str):
        self.project_dir = Path(project_dir)

    def detect(self) -> ProjectContext:
        """
        Detect languages and libraries in the project.

        Returns:
            ProjectContext with detected languages and libraries
        """
        languages: set[Language] = set()
        libraries: list[Library] = []
        dependency_files: dict[str, str] = {}

        # First, detect languages from file extensions
        languages.update(self._detect_languages_from_files())

        # Then parse dependency files for specific libraries
        for filename, language in DEPENDENCY_FILES.items():
            dep_file = self.project_dir / filename
            if dep_file.exists():
                try:
                    content = dep_file.read_text()
                    dependency_files[filename] = content
                    languages.add(language)

                    # Parse libraries from the dependency file
                    libs = self._parse_dependency_file(filename, content, language)
                    libraries.extend(libs)
                except Exception:
                    pass  # Skip files we can't read

        # Check for TypeScript in package.json projects
        if Language.JAVASCRIPT in languages:  # noqa: SIM102
            if (self.project_dir / "tsconfig.json").exists():
                languages.add(Language.TYPESCRIPT)

        # Check for OpenTofu/Terraform
        if any((self.project_dir / f).exists() for f in [".terraform.lock.hcl"]):
            languages.add(Language.HCL)
        elif list(self.project_dir.glob("*.tf")) or list(self.project_dir.glob("*.tofu")):
            languages.add(Language.HCL)
            libs = self._parse_hcl_providers()
            libraries.extend(libs)

        # Check for Ansible
        ansible_indicators = ["ansible.cfg", "playbook.yml", "playbook.yaml", "site.yml", "site.yaml"]
        if any((self.project_dir / f).exists() for f in ansible_indicators) or (self.project_dir / "roles").exists() or (self.project_dir / "inventory").exists():
            languages.add(Language.ANSIBLE)
            libs = self._parse_ansible_requirements()
            libraries.extend(libs)

        return ProjectContext(
            languages=list(languages),
            libraries=libraries,
            dependency_files=dependency_files,
        )

    def _detect_languages_from_files(self) -> set[Language]:
        """Detect languages from file extensions in the project."""
        languages: set[Language] = set()

        # Only scan top-level and src directories to avoid deep traversal
        scan_dirs = [self.project_dir]
        src_dir = self.project_dir / "src"
        if src_dir.exists():
            scan_dirs.append(src_dir)

        extension_to_language = {
            ".py": Language.PYTHON,
            ".js": Language.JAVASCRIPT,
            ".jsx": Language.JAVASCRIPT,
            ".ts": Language.TYPESCRIPT,
            ".tsx": Language.TYPESCRIPT,
            ".go": Language.GO,
            ".rs": Language.RUST,
            ".tf": Language.HCL,
            ".tofu": Language.HCL,
            ".rb": Language.RUBY,
            ".erb": Language.RUBY,
            ".php": Language.PHP,
            ".dart": Language.DART,
        }

        for scan_dir in scan_dirs:
            try:
                for file_path in scan_dir.iterdir():
                    if file_path.is_file():
                        ext = file_path.suffix.lower()
                        if ext in extension_to_language:
                            languages.add(extension_to_language[ext])
            except PermissionError:
                pass

        return languages

    def _parse_dependency_file(
        self, filename: str, content: str, language: Language
    ) -> list[Library]:
        """Parse a dependency file to extract libraries."""
        if filename == "pyproject.toml":
            return self._parse_pyproject_toml(content)
        elif filename == "requirements.txt":
            return self._parse_requirements_txt(content)
        elif filename == "setup.py":
            return self._parse_setup_py(content)
        elif filename == "package.json":
            return self._parse_package_json(content)
        elif filename == "go.mod":
            return self._parse_go_mod(content)
        elif filename == "Cargo.toml":
            return self._parse_cargo_toml(content)
        elif filename in ("Gemfile", "Gemfile.lock"):
            return self._parse_gemfile(content)
        elif filename in ("composer.json", "composer.lock"):
            return self._parse_composer_json(content)
        elif filename in ("pubspec.yaml", "pubspec.lock"):
            return self._parse_pubspec_yaml(content)
        return []

    def _parse_pyproject_toml(self, content: str) -> list[Library]:
        """Parse pyproject.toml for Python dependencies."""
        libraries = []

        try:
            import tomllib
            data = tomllib.loads(content)

            # Get dependencies from [project.dependencies]
            deps = data.get("project", {}).get("dependencies", [])
            for dep in deps:
                name, version = self._parse_python_requirement(dep)
                if name:
                    libraries.append(Library(
                        name=name,
                        language=Language.PYTHON,
                        version=version,
                    ))

            # Also check [tool.poetry.dependencies]
            poetry_deps = data.get("tool", {}).get("poetry", {}).get("dependencies", {})
            for name, version_spec in poetry_deps.items():
                if name.lower() != "python":
                    version = version_spec if isinstance(version_spec, str) else None
                    libraries.append(Library(
                        name=name,
                        language=Language.PYTHON,
                        version=version,
                    ))

        except Exception:
            # Fallback: regex parsing
            dep_pattern = r'^\s*"([a-zA-Z0-9_-]+)(?:\[.*\])?(?:[<>=!~]+.*)?"\s*,?\s*$'
            for line in content.split('\n'):
                match = re.match(dep_pattern, line)
                if match:
                    libraries.append(Library(
                        name=match.group(1),
                        language=Language.PYTHON,
                    ))

        return libraries

    def _parse_requirements_txt(self, content: str) -> list[Library]:
        """Parse requirements.txt for Python dependencies."""
        libraries = []

        for line in content.split('\n'):
            line = line.strip()
            if not line or line.startswith('#') or line.startswith('-'):
                continue

            name, version = self._parse_python_requirement(line)
            if name:
                libraries.append(Library(
                    name=name,
                    language=Language.PYTHON,
                    version=version,
                ))

        return libraries

    def _parse_python_requirement(self, requirement: str) -> tuple[str | None, str | None]:
        """Parse a Python requirement string like 'package>=1.0.0'."""
        # Remove comments and extras
        requirement = requirement.split('#')[0].strip()
        requirement = re.sub(r'\[.*\]', '', requirement)

        # Match package name and optional version
        match = re.match(r'^([a-zA-Z0-9_-]+)(?:([<>=!~]+)(.+))?$', requirement)
        if match:
            name = match.group(1)
            version = match.group(3).strip() if match.group(3) else None
            return name, version
        return None, None

    def _parse_setup_py(self, content: str) -> list[Library]:
        """Parse setup.py for Python dependencies (basic extraction)."""
        libraries = []

        # Look for install_requires list
        install_requires = re.search(
            r'install_requires\s*=\s*\[(.*?)\]',
            content,
            re.DOTALL
        )
        if install_requires:
            deps_str = install_requires.group(1)
            # Extract quoted strings
            deps = re.findall(r'["\']([^"\']+)["\']', deps_str)
            for dep in deps:
                name, version = self._parse_python_requirement(dep)
                if name:
                    libraries.append(Library(
                        name=name,
                        language=Language.PYTHON,
                        version=version,
                    ))

        return libraries

    def _parse_package_json(self, content: str) -> list[Library]:
        """Parse package.json for JavaScript/TypeScript dependencies."""
        libraries = []

        try:
            import json
            data = json.loads(content)

            # Get regular dependencies
            deps = data.get("dependencies", {})
            for name, version in deps.items():
                libraries.append(Library(
                    name=name,
                    language=Language.JAVASCRIPT,
                    version=version.lstrip('^~'),
                ))

            # Get dev dependencies (they're often important for tooling)
            dev_deps = data.get("devDependencies", {})
            for name, version in dev_deps.items():
                # Skip common dev-only tools that don't need docs
                if name.startswith('@types/'):
                    continue
                libraries.append(Library(
                    name=name,
                    language=Language.JAVASCRIPT,
                    version=version.lstrip('^~'),
                ))

        except Exception:
            pass

        return libraries

    def _parse_go_mod(self, content: str) -> list[Library]:
        """Parse go.mod for Go dependencies."""
        libraries = []

        # Match require blocks and single requires
        require_block = re.search(r'require\s*\((.*?)\)', content, re.DOTALL)
        if require_block:
            for line in require_block.group(1).split('\n'):
                match = re.match(r'\s*(\S+)\s+(\S+)', line)
                if match:
                    libraries.append(Library(
                        name=match.group(1),
                        language=Language.GO,
                        version=match.group(2),
                    ))

        # Also match single-line requires
        single_requires = re.findall(r'^require\s+(\S+)\s+(\S+)', content, re.MULTILINE)
        for name, version in single_requires:
            libraries.append(Library(
                name=name,
                language=Language.GO,
                version=version,
            ))

        return libraries

    def _parse_cargo_toml(self, content: str) -> list[Library]:
        """Parse Cargo.toml for Rust dependencies."""
        libraries = []

        try:
            import tomllib
            data = tomllib.loads(content)

            deps = data.get("dependencies", {})
            for name, spec in deps.items():
                if isinstance(spec, str):
                    version = spec
                elif isinstance(spec, dict):
                    version = spec.get("version")
                else:
                    version = None

                libraries.append(Library(
                    name=name,
                    language=Language.RUST,
                    version=version,
                ))

        except Exception:
            # Fallback: regex parsing
            in_deps = False
            for line in content.split('\n'):
                if line.strip() == '[dependencies]':
                    in_deps = True
                    continue
                elif line.strip().startswith('['):
                    in_deps = False
                    continue

                if in_deps:
                    match = re.match(r'^([a-zA-Z0-9_-]+)\s*=\s*"([^"]+)"', line)
                    if match:
                        libraries.append(Library(
                            name=match.group(1),
                            language=Language.RUST,
                            version=match.group(2),
                        ))

        return libraries

    def _parse_gemfile(self, content: str) -> list[Library]:
        """Parse Gemfile for Ruby dependencies."""
        libraries = []

        for line in content.split('\n'):
            line = line.strip()
            if not line or line.startswith('#'):
                continue

            # Match gem 'name' or gem "name" with optional version
            match = re.match(r"""gem\s+['"]([a-zA-Z0-9_-]+)['"]""", line)
            if match:
                libraries.append(Library(
                    name=match.group(1),
                    language=Language.RUBY,
                ))

        return libraries

    def _parse_composer_json(self, content: str) -> list[Library]:
        """Parse composer.json for PHP dependencies."""
        libraries = []

        try:
            import json
            data = json.loads(content)

            for section in ("require", "require-dev"):
                deps = data.get(section, {})
                for name, version in deps.items():
                    # Skip php itself and extensions
                    if name == "php" or name.startswith("ext-"):
                        continue
                    libraries.append(Library(
                        name=name,
                        language=Language.PHP,
                        version=version.lstrip('^~'),
                    ))

        except Exception:
            pass

        return libraries

    def _parse_pubspec_yaml(self, content: str) -> list[Library]:
        """Parse pubspec.yaml for Dart/Flutter dependencies."""
        libraries = []

        try:
            import yaml
            data = yaml.safe_load(content)

            if not data:
                return libraries

            for section in ("dependencies", "dev_dependencies"):
                deps = data.get(section, {})
                if isinstance(deps, dict):
                    for name, spec in deps.items():
                        # Skip Flutter SDK dependency
                        if isinstance(spec, dict) and "sdk" in spec:
                            continue
                        version = spec if isinstance(spec, str) else None
                        libraries.append(Library(
                            name=name,
                            language=Language.DART,
                            version=version,
                        ))

        except ImportError:
            # Fallback: simple regex parsing
            dep_pattern = r'^\s+([a-zA-Z0-9_]+):\s*'
            in_deps = False
            for line in content.split('\n'):
                if line.strip() in ('dependencies:', 'dev_dependencies:'):
                    in_deps = True
                    continue
                elif line and not line.startswith(' ') and not line.startswith('\t'):
                    in_deps = False
                    continue

                if in_deps:
                    match = re.match(dep_pattern, line)
                    if match:
                        libraries.append(Library(
                            name=match.group(1),
                            language=Language.DART,
                        ))

        except Exception:
            pass

        return libraries

    def _parse_hcl_providers(self) -> list[Library]:
        """Parse OpenTofu/Terraform files for provider dependencies."""
        libraries = []
        seen_providers: set[str] = set()

        # Look for provider blocks in .tf files
        tf_files = list(self.project_dir.glob("*.tf")) + list(self.project_dir.glob("*.tofu"))

        for tf_file in tf_files:
            try:
                content = tf_file.read_text()

                # Match provider blocks: provider "aws" { ... }
                provider_blocks = re.findall(r'provider\s+"([^"]+)"', content)
                for provider in provider_blocks:
                    if provider not in seen_providers:
                        seen_providers.add(provider)
                        libraries.append(Library(
                            name=provider,
                            language=Language.HCL,
                        ))

                # Match required_providers blocks
                # required_providers { aws = { source = "hashicorp/aws" version = "~> 5.0" } }
                req_providers = re.findall(
                    r'(\w+)\s*=\s*\{[^}]*source\s*=\s*"([^"]+)"[^}]*(?:version\s*=\s*"([^"]+)")?',
                    content,
                    re.DOTALL
                )
                for name, _source, version in req_providers:
                    if name not in seen_providers:
                        seen_providers.add(name)
                        libraries.append(Library(
                            name=name,
                            language=Language.HCL,
                            version=version if version else None,
                        ))

            except Exception:
                pass

        # Also check .terraform.lock.hcl for locked providers
        lock_file = self.project_dir / ".terraform.lock.hcl"
        if lock_file.exists():
            try:
                content = lock_file.read_text()
                # Match provider "registry.terraform.io/hashicorp/aws" { version = "5.0.0" }
                locked = re.findall(
                    r'provider\s+"[^"]*?/([^/"]+)"\s*\{[^}]*version\s*=\s*"([^"]+)"',
                    content,
                    re.DOTALL
                )
                for name, version in locked:
                    if name not in seen_providers:
                        seen_providers.add(name)
                        libraries.append(Library(
                            name=name,
                            language=Language.HCL,
                            version=version,
                        ))
            except Exception:
                pass

        return libraries

    def _parse_ansible_requirements(self) -> list[Library]:
        """Parse Ansible requirements.yml for collections and roles."""
        libraries = []

        # Check for requirements.yml (collections/roles)
        req_files = ["requirements.yml", "requirements.yaml", "collections/requirements.yml"]

        for req_file in req_files:
            req_path = self.project_dir / req_file
            if req_path.exists():
                try:
                    content = req_path.read_text()
                    libs = self._parse_ansible_requirements_content(content)
                    libraries.extend(libs)
                except Exception:
                    pass

        # Check for galaxy.yml (if this is a collection)
        galaxy_file = self.project_dir / "galaxy.yml"
        if galaxy_file.exists():
            try:
                content = galaxy_file.read_text()
                libs = self._parse_galaxy_yml(content)
                libraries.extend(libs)
            except Exception:
                pass

        return libraries

    def _parse_ansible_requirements_content(self, content: str) -> list[Library]:
        """Parse Ansible requirements.yml content."""
        libraries = []

        try:
            import yaml
            data = yaml.safe_load(content)

            if not data:
                return libraries

            # Handle collections list
            collections = data.get("collections", [])
            if isinstance(collections, list):
                for coll in collections:
                    if isinstance(coll, str):
                        libraries.append(Library(
                            name=coll,
                            language=Language.ANSIBLE,
                        ))
                    elif isinstance(coll, dict):
                        name = coll.get("name", "")
                        version = coll.get("version")
                        if name:
                            libraries.append(Library(
                                name=name,
                                language=Language.ANSIBLE,
                                version=version,
                            ))

            # Handle roles list
            roles = data.get("roles", [])
            if isinstance(roles, list):
                for role in roles:
                    if isinstance(role, str):
                        libraries.append(Library(
                            name=role,
                            language=Language.ANSIBLE,
                        ))
                    elif isinstance(role, dict):
                        name = role.get("name") or role.get("src", "")
                        version = role.get("version")
                        if name:
                            libraries.append(Library(
                                name=name,
                                language=Language.ANSIBLE,
                                version=version,
                            ))

        except ImportError:
            # Fallback: simple regex parsing
            coll_pattern = r'^\s*-\s*name:\s*([^\s#]+)'
            for match in re.finditer(coll_pattern, content, re.MULTILINE):
                libraries.append(Library(
                    name=match.group(1),
                    language=Language.ANSIBLE,
                ))
        except Exception:
            pass

        return libraries

    def _parse_galaxy_yml(self, content: str) -> list[Library]:
        """Parse galaxy.yml for collection dependencies."""
        libraries = []

        try:
            import yaml
            data = yaml.safe_load(content)

            if not data:
                return libraries

            # Get dependencies
            deps = data.get("dependencies", {})
            if isinstance(deps, dict):
                for name, version in deps.items():
                    libraries.append(Library(
                        name=name,
                        language=Language.ANSIBLE,
                        version=version if isinstance(version, str) else None,
                    ))

        except Exception:
            pass

        return libraries
