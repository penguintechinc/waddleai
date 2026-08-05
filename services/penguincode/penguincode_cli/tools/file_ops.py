"""File operation tools for reading, writing, and editing files."""

from pathlib import Path

import aiofiles

from .base import BaseTool, ToolResult


class ReadFileTool(BaseTool):
    """Tool for reading file contents."""

    def __init__(self):
        super().__init__("read", "Read file contents with optional line range")

    async def execute(
        self,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> ToolResult:
        """
        Read file contents.

        Args:
            path: File path
            start_line: Optional start line (1-indexed)
            end_line: Optional end line (1-indexed, inclusive)

        Returns:
            ToolResult with file contents
        """
        try:
            file_path = Path(path).expanduser().resolve()

            if not file_path.exists():
                return ToolResult(
                    success=False,
                    data=None,
                    error=f"File not found: {path}",
                )

            if not file_path.is_file():
                return ToolResult(
                    success=False,
                    data=None,
                    error=f"Not a file: {path}",
                )

            async with aiofiles.open(file_path, encoding="utf-8", errors="replace") as f:
                lines = await f.readlines()

            # Apply line range if specified
            if start_line is not None or end_line is not None:
                start = (start_line - 1) if start_line else 0
                end = end_line if end_line else len(lines)
                lines = lines[start:end]

            # Add line numbers
            if start_line:
                numbered_lines = [f"{i + start_line:6d}→{line.rstrip()}" for i, line in enumerate(lines)]
            else:
                numbered_lines = [f"{i + 1:6d}→{line.rstrip()}" for i, line in enumerate(lines)]

            content = "\n".join(numbered_lines)

            return ToolResult(
                success=True,
                data=content,
                metadata={
                    "path": str(file_path),
                    "total_lines": len(lines),
                    "start_line": start_line or 1,
                    "end_line": end_line or len(lines),
                },
            )

        except Exception as e:
            return ToolResult(
                success=False,
                data=None,
                error=f"Failed to read file: {str(e)}",
            )


class WriteFileTool(BaseTool):
    """Tool for writing file contents."""

    def __init__(self, working_dir: str | None = None):
        super().__init__("write", "Write content to a file")
        self.working_dir = working_dir

    def _validate_path(self, path: str) -> Path:
        """Validate that path is within the working directory sandbox."""
        file_path = Path(path).expanduser().resolve()
        if self.working_dir:
            working = Path(self.working_dir).expanduser().resolve()
            if not str(file_path).startswith(str(working) + "/") and file_path != working:
                raise ValueError(f"Path {file_path} is outside working directory {working}")
        return file_path

    async def execute(self, path: str, content: str, create_dirs: bool = True) -> ToolResult:
        """
        Write content to file.

        Args:
            path: File path
            content: Content to write
            create_dirs: Whether to create parent directories

        Returns:
            ToolResult with operation status
        """
        try:
            try:
                file_path = self._validate_path(path)
            except ValueError as e:
                return ToolResult(
                    success=False,
                    data=None,
                    error=str(e),
                )

            # Create parent directories if needed
            if create_dirs and not file_path.parent.exists():
                file_path.parent.mkdir(parents=True, exist_ok=True)

            # Check if file exists (for metadata)
            existed = file_path.exists()

            async with aiofiles.open(file_path, "w", encoding="utf-8") as f:
                await f.write(content)

            return ToolResult(
                success=True,
                data=f"File {'updated' if existed else 'created'}: {path}",
                metadata={
                    "path": str(file_path),
                    "existed": existed,
                    "bytes_written": len(content.encode("utf-8")),
                },
            )

        except Exception as e:
            return ToolResult(
                success=False,
                data=None,
                error=f"Failed to write file: {str(e)}",
            )


class EditFileTool(BaseTool):
    """Tool for editing files using search and replace."""

    def __init__(self, working_dir: str | None = None):
        super().__init__("edit", "Edit file using search and replace")
        self.working_dir = working_dir

    def _validate_path(self, path: str) -> Path:
        """Validate that path is within the working directory sandbox."""
        file_path = Path(path).expanduser().resolve()
        if self.working_dir:
            working = Path(self.working_dir).expanduser().resolve()
            if not str(file_path).startswith(str(working) + "/") and file_path != working:
                raise ValueError(f"Path {file_path} is outside working directory {working}")
        return file_path

    async def execute(
        self,
        path: str,
        old_text: str,
        new_text: str,
        replace_all: bool = False,
    ) -> ToolResult:
        """
        Edit file by replacing text.

        Args:
            path: File path
            old_text: Text to search for
            new_text: Replacement text
            replace_all: Replace all occurrences (default: first only)

        Returns:
            ToolResult with operation status
        """
        try:
            try:
                file_path = self._validate_path(path)
            except ValueError as e:
                return ToolResult(
                    success=False,
                    data=None,
                    error=str(e),
                )

            if not file_path.exists():
                return ToolResult(
                    success=False,
                    data=None,
                    error=f"File not found: {path}",
                )

            # Validate old_text is not empty
            if not old_text or not old_text.strip():
                return ToolResult(
                    success=False,
                    data=None,
                    error="old_text cannot be empty. Use 'write' tool to overwrite entire file.",
                )

            # Read file content
            async with aiofiles.open(file_path, encoding="utf-8", errors="replace") as f:
                content = await f.read()

            # Check if old_text exists
            if old_text not in content:
                return ToolResult(
                    success=False,
                    data=None,
                    error=f"Text not found in file: {old_text[:50]}...",
                )

            # Replace text
            if replace_all:
                count = content.count(old_text)
                new_content = content.replace(old_text, new_text)
            else:
                count = 1
                new_content = content.replace(old_text, new_text, 1)

            # Write back
            async with aiofiles.open(file_path, "w", encoding="utf-8") as f:
                await f.write(new_content)

            return ToolResult(
                success=True,
                data=f"Replaced {count} occurrence(s) in {path}",
                metadata={
                    "path": str(file_path),
                    "replacements": count,
                    "replace_all": replace_all,
                },
            )

        except Exception as e:
            return ToolResult(
                success=False,
                data=None,
                error=f"Failed to edit file: {str(e)}",
            )


class GrepTool(BaseTool):
    """Tool for searching text in files."""

    def __init__(self):
        super().__init__("grep", "Search for pattern in files")

    async def execute(
        self,
        pattern: str,
        path: str = ".",
        case_sensitive: bool = True,
        max_results: int = 100,
    ) -> ToolResult:
        """
        Search for pattern in files.

        Args:
            pattern: Search pattern (supports regex)
            path: File or directory path
            case_sensitive: Whether search is case-sensitive
            max_results: Maximum results to return

        Returns:
            ToolResult with matching lines
        """
        import re

        try:
            search_path = Path(path).expanduser().resolve()
            flags = 0 if case_sensitive else re.IGNORECASE
            regex = re.compile(pattern, flags)

            matches: list[tuple[str, int, str]] = []

            # Determine if path is file or directory
            if search_path.is_file():
                files = [search_path]
            elif search_path.is_dir():
                # Search all text files in directory recursively
                files = [f for f in search_path.rglob("*") if f.is_file() and not self._should_ignore(f)]
            else:
                return ToolResult(
                    success=False,
                    data=None,
                    error=f"Path not found: {path}",
                )

            # Search in files
            for file_path in files:
                if len(matches) >= max_results:
                    break

                try:
                    async with aiofiles.open(file_path, encoding="utf-8", errors="replace") as f:
                        lines = await f.readlines()

                    for line_num, line in enumerate(lines, 1):
                        if regex.search(line):
                            matches.append((str(file_path), line_num, line.rstrip()))
                            if len(matches) >= max_results:
                                break

                except Exception:
                    # Skip files that can't be read
                    continue

            # Format results
            if matches:
                result_lines = [f"{file}:{line_num}: {line}" for file, line_num, line in matches]
                result_text = "\n".join(result_lines)
            else:
                result_text = f"No matches found for pattern: {pattern}"

            return ToolResult(
                success=True,
                data=result_text,
                metadata={
                    "pattern": pattern,
                    "matches": len(matches),
                    "files_searched": len(files),
                    "truncated": len(matches) >= max_results,
                },
            )

        except Exception as e:
            return ToolResult(
                success=False,
                data=None,
                error=f"Search failed: {str(e)}",
            )

    @staticmethod
    def _should_ignore(path: Path) -> bool:
        """Check if path should be ignored."""
        ignore_patterns = {
            ".git",
            ".svn",
            "node_modules",
            "__pycache__",
            ".pytest_cache",
            ".mypy_cache",
            ".venv",
            "venv",
            ".env",
        }

        # Check if any part of the path matches ignore patterns
        for part in path.parts:
            if part in ignore_patterns:
                return True

        # Ignore binary file extensions
        binary_extensions = {
            ".pyc",
            ".so",
            ".o",
            ".a",
            ".exe",
            ".dll",
            ".dylib",
            ".jpg",
            ".jpeg",
            ".png",
            ".gif",
            ".pdf",
            ".zip",
            ".tar",
            ".gz",
        }

        return path.suffix.lower() in binary_extensions


class GlobTool(BaseTool):
    """Tool for finding files by pattern."""

    def __init__(self):
        super().__init__("glob", "Find files matching pattern")

    async def execute(
        self,
        pattern: str,
        path: str = ".",
        max_results: int = 100,
    ) -> ToolResult:
        """
        Find files matching glob pattern.

        Args:
            pattern: Glob pattern (e.g., "**/*.py")
            path: Base directory path
            max_results: Maximum results to return

        Returns:
            ToolResult with matching file paths
        """
        try:
            base_path = Path(path).expanduser().resolve()

            if not base_path.exists():
                return ToolResult(
                    success=False,
                    data=None,
                    error=f"Path not found: {path}",
                )

            # Find matching files
            matches = list(base_path.glob(pattern))
            matches = [m for m in matches if m.is_file()]

            # Limit results
            truncated = len(matches) > max_results
            matches = matches[:max_results]

            # Format results
            if matches:
                result_lines = [str(m.relative_to(base_path)) for m in matches]
                result_text = "\n".join(result_lines)
            else:
                result_text = f"No files found matching pattern: {pattern}"

            return ToolResult(
                success=True,
                data=result_text,
                metadata={
                    "pattern": pattern,
                    "matches": len(matches),
                    "truncated": truncated,
                },
            )

        except Exception as e:
            return ToolResult(
                success=False,
                data=None,
                error=f"Glob search failed: {str(e)}",
            )
