"""Tool definitions for Ollama tool calling API."""

# Ollama tool definitions for agents
TOOL_DEFINITIONS = {
    "read": {
        "type": "function",
        "function": {
            "name": "read",
            "description": "Read the contents of a file. Returns the file content with line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "The path to the file to read (absolute or relative to working directory)"
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "Optional start line number (1-indexed)"
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "Optional end line number (1-indexed, inclusive)"
                    }
                },
                "required": ["path"]
            }
        }
    },
    "write": {
        "type": "function",
        "function": {
            "name": "write",
            "description": "Write content to a file. Creates the file if it doesn't exist, overwrites if it does.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "The path to the file to write"
                    },
                    "content": {
                        "type": "string",
                        "description": "The content to write to the file"
                    }
                },
                "required": ["path", "content"]
            }
        }
    },
    "edit": {
        "type": "function",
        "function": {
            "name": "edit",
            "description": "Edit a file by replacing specific text. The old_text must match exactly.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "The path to the file to edit"
                    },
                    "old_text": {
                        "type": "string",
                        "description": "The exact text to find and replace"
                    },
                    "new_text": {
                        "type": "string",
                        "description": "The new text to replace with"
                    },
                    "replace_all": {
                        "type": "boolean",
                        "description": "Whether to replace all occurrences (default: false, only first)"
                    }
                },
                "required": ["path", "old_text", "new_text"]
            }
        }
    },
    "grep": {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search for a pattern in files. Returns matching lines with file paths and line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "The search pattern (supports regex)"
                    },
                    "path": {
                        "type": "string",
                        "description": "The file or directory to search in (default: current directory)"
                    },
                    "case_sensitive": {
                        "type": "boolean",
                        "description": "Whether search is case-sensitive (default: true)"
                    }
                },
                "required": ["pattern"]
            }
        }
    },
    "glob": {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "Find files matching a glob pattern. Returns list of matching file paths.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "The glob pattern (e.g., '**/*.py' for all Python files)"
                    },
                    "path": {
                        "type": "string",
                        "description": "The base directory to search in (default: current directory)"
                    }
                },
                "required": ["pattern"]
            }
        }
    },
    "bash": {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Execute a bash command. Returns command output (stdout and stderr).",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The bash command to execute"
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Optional timeout in seconds (default: 30)"
                    }
                },
                "required": ["command"]
            }
        }
    },
}
