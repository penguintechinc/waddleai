"""Skill discovery, loading, and chaining for PenguinCode.

Scans multiple locations for skills (later sources override earlier):
1. Built-in: penguincode_cli/defaults/skills/ (package data)
2. External: Claude Code (~/.claude/skills/), OpenCode (~/.config/opencode/skills/)
3. User custom: ~/.config/penguincode/skills/ (highest priority)

Handles both skill formats:
- Subdirectory: skill-name/SKILL.md (OpenCode-compatible)
- Flat file: skill-name.md (simple)
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

# Namespace prefix for skill cross-references
SKILL_NAMESPACE = "waddlepowers:"


@dataclass
class SkillInfo:
    """Loaded skill with parsed metadata and content."""

    name: str
    description: str
    content: str  # Full SKILL.md content (with supporting files appended)
    path: Path
    references: list[str] = field(default_factory=list)  # Other skill names referenced
    model: str | None = None  # Optional LLM model override for this skill


class SkillLoader:
    """Discovers and loads skills from built-in and user directories."""

    def __init__(self) -> None:
        self._skills: dict[str, SkillInfo] = {}
        self._discovered = False

    # External skill directories to auto-import from (in scan order).
    # Later entries override earlier ones on name collision.
    EXTERNAL_SKILL_DIRS: list[tuple[str, ...]] = [
        (".claude", "skills"),  # Claude Code
        (".config", "opencode", "skills"),  # OpenCode
    ]

    def discover(self) -> None:
        """Scan built-in, external, and user skill directories.

        Scan order (later overrides earlier on name collision):
        1. Built-in package skills
        2. External tool skills (Claude Code, OpenCode)
        3. User custom PenguinCode skills (highest priority)
        """
        self._skills.clear()

        # 1. Built-in skills (package data)
        builtin_dir = Path(__file__).parent.parent / "defaults" / "skills"
        if builtin_dir.is_dir():
            self._scan_directory(builtin_dir)

        # 2. External skill directories (Claude Code, OpenCode, etc.)
        home = Path.home()
        for parts in self.EXTERNAL_SKILL_DIRS:
            ext_dir = home.joinpath(*parts)
            if ext_dir.is_dir():
                self._scan_directory(ext_dir)

        # 3. User custom skills (highest priority, override everything)
        user_dir = home / ".config" / "penguincode" / "skills"
        if user_dir.is_dir():
            self._scan_directory(user_dir)

        self._discovered = True

    def _scan_directory(self, directory: Path) -> None:
        """Scan a directory for skills in both formats."""
        for entry in sorted(directory.iterdir()):
            if entry.is_dir() and not entry.name.startswith((".", "_")):
                # Subdirectory format: skill-name/SKILL.md
                skill_file = entry / "SKILL.md"
                if skill_file.is_file():
                    skill = self._load_skill(skill_file, entry)
                    if skill:
                        self._skills[skill.name] = skill
            elif entry.is_file() and entry.suffix == ".md":
                # Flat file format: skill-name.md
                skill = self._load_skill(entry, None)
                if skill:
                    self._skills[skill.name] = skill

    def _load_skill(self, skill_file: Path, skill_dir: Path | None) -> SkillInfo | None:
        """Load a single skill from its SKILL.md (or flat .md) file.

        For subdirectory skills, appends all supporting .md files after
        the main SKILL.md content.
        """
        try:
            content = skill_file.read_text(encoding="utf-8")
        except OSError:
            return None

        # Parse YAML frontmatter
        name, description, model = self._parse_frontmatter(content)

        # Derive name from directory/filename if not in frontmatter
        if not name:
            if skill_dir:
                name = skill_dir.name
            else:
                name = skill_file.stem

        # For subdirectory skills, append supporting .md files
        if skill_dir:
            for support_file in sorted(skill_dir.iterdir()):
                if support_file.is_file() and support_file.suffix == ".md" and support_file.name != "SKILL.md":
                    try:
                        support_content = support_file.read_text(encoding="utf-8")
                        content += f"\n\n---\n\n# {support_file.stem}\n\n{support_content}"
                    except OSError:
                        continue

            # Also check subdirectories (e.g., examples/)
            for sub_entry in sorted(skill_dir.iterdir()):
                if sub_entry.is_dir() and not sub_entry.name.startswith((".", "_")):
                    for sub_file in sorted(sub_entry.iterdir()):
                        if sub_file.is_file() and sub_file.suffix == ".md":
                            try:
                                sub_content = sub_file.read_text(encoding="utf-8")
                                content += f"\n\n---\n\n# {sub_entry.name}/{sub_file.stem}" f"\n\n{sub_content}"
                            except OSError:
                                continue

        # Extract cross-references (waddlepowers:skill-name)
        references = self._extract_references(content)

        return SkillInfo(
            name=name,
            description=description,
            content=content,
            path=skill_file,
            references=references,
            model=model,
        )

    @staticmethod
    def _parse_frontmatter(content: str) -> tuple[str, str, str | None]:
        """Parse YAML frontmatter for name, description, and model.

        Returns:
            Tuple of (name, description, model). Name/description may be
            empty string; model is None when not specified.
        """
        name = ""
        description = ""
        model: str | None = None

        # Match YAML frontmatter block
        match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
        if not match:
            return name, description, model

        frontmatter = match.group(1)

        # Extract name
        name_match = re.search(r"^name:\s*(.+)$", frontmatter, re.MULTILINE)
        if name_match:
            name = name_match.group(1).strip().strip("\"'")

        # Extract description
        desc_match = re.search(r"^description:\s*(.+)$", frontmatter, re.MULTILINE)
        if desc_match:
            description = desc_match.group(1).strip().strip("\"'")

        # Extract model override
        model_match = re.search(r"^model:\s*(.+)$", frontmatter, re.MULTILINE)
        if model_match:
            model = model_match.group(1).strip().strip("\"'")

        return name, description, model

    @staticmethod
    def _extract_references(content: str) -> list[str]:
        """Extract waddlepowers: skill references from content."""
        # Match waddlepowers:skill-name patterns
        refs = re.findall(rf"{re.escape(SKILL_NAMESPACE)}([\w-]+)", content)
        # Deduplicate while preserving order
        seen = set()
        unique = []
        for ref in refs:
            if ref not in seen:
                seen.add(ref)
                unique.append(ref)
        return unique

    def get(self, name: str) -> SkillInfo | None:
        """Get a skill by name.

        Args:
            name: Skill name (e.g., "brainstorming")

        Returns:
            SkillInfo or None if not found
        """
        if not self._discovered:
            self.discover()
        return self._skills.get(name)

    def list_all(self) -> dict[str, SkillInfo]:
        """Return all discovered skills.

        Returns:
            Dict mapping skill name to SkillInfo
        """
        if not self._discovered:
            self.discover()
        return dict(self._skills)

    def get_chain(self, name: str) -> list[SkillInfo]:
        """Get a skill and all its transitive dependencies in order.

        Performs depth-first traversal with cycle detection.
        Max depth of 5 to prevent runaway chains.

        Args:
            name: Starting skill name

        Returns:
            Ordered list of SkillInfo (root skill first, then dependencies)
        """
        if not self._discovered:
            self.discover()

        result: list[SkillInfo] = []
        visited: set[str] = set()

        def _traverse(skill_name: str, depth: int) -> None:
            if depth > 5 or skill_name in visited:
                return
            visited.add(skill_name)

            skill = self._skills.get(skill_name)
            if not skill:
                return

            result.append(skill)

            for ref in skill.references:
                _traverse(ref, depth + 1)

        _traverse(name, 0)
        return result
