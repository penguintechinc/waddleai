---
name: code-generation
description: "Scaffolding, boilerplate generation, and template-based code creation"
model: qwen2.5-coder:7b
---

# Code Generation

## Overview
Generate boilerplate code, scaffolding, and templates to accelerate development.

## What to Generate
- **CRUD endpoints** — standard REST operations for a resource
- **Database models** — table definitions with fields and relationships
- **Test files** — test scaffolding with common fixtures
- **Configuration** — env templates, Docker configs, CI workflows
- **Documentation** — docstrings, API docs, README sections

## Generation Approach
1. **Understand the pattern** — what existing code follows this pattern?
2. **Identify variables** — what differs between instances?
3. **Generate** — create new files following the pattern
4. **Customize** — adapt generated code to specific needs
5. **Verify** — ensure generated code compiles and tests pass

## CRUD Endpoint Pattern
For a new resource (e.g., "Widget"):
1. Model: `models/widget.py`
2. Routes: `routes/widgets.py` (GET list, GET one, POST, PUT, DELETE)
3. Tests: `tests/test_widgets.py`
4. Migration: add table definition

## Best Practices
- Generated code should follow project conventions
- Always review and customize generated code
- Don't over-generate — only create what's needed
- Use consistent naming across generated files
- Add appropriate tests for generated code
