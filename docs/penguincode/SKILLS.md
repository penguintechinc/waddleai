# PenguinCode Skill System

Skills are reusable instruction sets that guide the AI agent through specific tasks. They provide structured workflows, best practices, and domain-specific knowledge without writing code.

## Quick Reference

```bash
# List all available skills
/skill

# Activate a skill
/skill smoke-testing

# Deactivate current skill
/skill off

# Chain: activates root skill + transitive dependencies
/skill creating-microservices
```

## Skill Locations

PenguinCode scans multiple directories for skills, in order of increasing priority (later overrides earlier on name collision):

| Priority | Location | Purpose |
|---|---|---|
| 1 (lowest) | `penguincode_cli/defaults/skills/` | Built-in package skills (51 skills) |
| 2 | `~/.claude/skills/` | Claude Code personal skills |
| 3 | `~/.config/opencode/skills/` | OpenCode personal skills |
| 4 (highest) | `~/.config/penguincode/skills/` | PenguinCode user custom skills |

**Override behavior**: If the same skill name exists in multiple directories, the highest-priority version wins. This lets you customize any built-in skill by placing a modified version in your user directory.

### Adding External Skills

Drop a skill into any of the above directories and PenguinCode will discover it automatically on next startup:

```bash
# Create a custom skill for Claude Code (also visible to PenguinCode)
mkdir -p ~/.claude/skills/my-workflow
cat > ~/.claude/skills/my-workflow/SKILL.md << 'EOF'
---
name: my-workflow
description: "My custom development workflow"
---

# My Workflow
Steps go here...
EOF
```

## Skill Format

Each skill lives in a subdirectory containing a `SKILL.md` file with YAML frontmatter:

```
skills/
  smoke-testing/
    SKILL.md          # Main skill file (required)
    examples.md       # Supporting files (optional, auto-appended)
    checklists.md     # Additional context (optional, auto-appended)
```

### SKILL.md Structure

```markdown
---
name: smoke-testing
description: "Quick build, run, and API verification in under 2 minutes"
model: qwen2.5-coder:7b
---

# Smoke Testing

## Overview
What this skill does and when to use it.

## Steps
1. Step one
2. Step two

## Rules
- Constraint one
- Constraint two
```

### Frontmatter Fields

| Field | Required | Description |
|---|---|---|
| `name` | Yes | Unique skill identifier (kebab-case) |
| `description` | Yes | One-line summary used for skill selection |
| `model` | No | LLM model override (e.g., `qwen2.5-coder:7b` for code-execution skills) |

### Model Override

Skills can specify a preferred LLM model in frontmatter. When activated, PenguinCode temporarily switches to that model. When deactivated (`/skill off`), the original model is restored.

```yaml
model: qwen2.5-coder:7b    # Code-execution skills
model: llama3.2:3b          # Lightweight advisory skills
# (omit for default model)  # Uses current orchestration model
```

**Convention**: Skills that execute code (run commands, edit files) should use `qwen2.5-coder:7b`. Advisory skills (brainstorming, planning) use the default model.

## Cross-References (Skill Chaining)

Skills can reference other skills using the `waddlepowers:` namespace:

```markdown
After building, verify with `waddlepowers:smoke-testing`.
For security checks, see `waddlepowers:security-scanning`.
```

When a skill with references is activated, PenguinCode resolves the full dependency chain (depth-first, max depth 5) and appends all referenced skill content to the active context.

## Built-in Skills (51)

### Git Operations (6)
| Skill | Description | Model |
|---|---|---|
| `committing-changes` | Pre-commit checks, conventional commits | `qwen2.5-coder:7b` |
| `pushing-to-github` | Push workflow, PR creation with gh CLI | `qwen2.5-coder:7b` |
| `branching-strategy` | Branch naming, feature/hotfix flows | default |
| `resolving-merge-conflicts` | Conflict resolution workflow | `qwen2.5-coder:7b` |
| `cherry-picking` | Cherry-pick with verification | `qwen2.5-coder:7b` |
| `git-bisect-debugging` | Regression hunting with git bisect | `qwen2.5-coder:7b` |

### Testing (6)
| Skill | Description | Model |
|---|---|---|
| `smoke-testing` | Quick build+run+API check, <2min | `qwen2.5-coder:7b` |
| `integration-testing` | Cross-service testing patterns | `qwen2.5-coder:7b` |
| `performance-testing` | Load testing, benchmarking | `qwen2.5-coder:7b` |
| `security-scanning` | SAST, dependency audit, OWASP | `qwen2.5-coder:7b` |
| `writing-unit-tests` | Unit test best practices, mocking | `qwen2.5-coder:7b` |
| `testing-api-endpoints` | API contract testing, status codes | `qwen2.5-coder:7b` |

### Docker / Containers (4)
| Skill | Description | Model |
|---|---|---|
| `building-docker-images` | Multi-arch builds, layer optimization | `qwen2.5-coder:7b` |
| `docker-compose-development` | Local dev environment setup | `qwen2.5-coder:7b` |
| `debugging-containers` | Logs, exec, inspect, networking | `qwen2.5-coder:7b` |
| `container-security` | Image scanning, non-root, secrets | `qwen2.5-coder:7b` |

### Kubernetes (4)
| Skill | Description | Model |
|---|---|---|
| `deploying-to-kubernetes` | kubectl apply, rollout strategy | `qwen2.5-coder:7b` |
| `kubernetes-debugging` | Pod logs, describe, events, exec | `qwen2.5-coder:7b` |
| `kubernetes-scaling` | HPA, VPA, resource limits | `qwen2.5-coder:7b` |
| `helm-chart-management` | Chart creation, values, upgrades | `qwen2.5-coder:7b` |

### CI/CD (3)
| Skill | Description | Model |
|---|---|---|
| `github-actions-workflows` | Workflow creation, debugging | `qwen2.5-coder:7b` |
| `release-management` | Version bumping, changelog, tags | `qwen2.5-coder:7b` |
| `deployment-rollback` | Rollback procedures, canary checks | `qwen2.5-coder:7b` |

### Code Quality (4)
| Skill | Description | Model |
|---|---|---|
| `linting-and-formatting` | Language-specific linters, autofix | `qwen2.5-coder:7b` |
| `dependency-management` | Updating, auditing, pinning deps | `qwen2.5-coder:7b` |
| `documentation-generation` | Docstrings, API docs, README | default |
| `refactoring-safely` | Incremental refactoring with tests | `qwen2.5-coder:7b` |

### Infrastructure (4)
| Skill | Description | Model |
|---|---|---|
| `database-migrations` | Schema changes, rollback, integrity | `qwen2.5-coder:7b` |
| `environment-configuration` | Env vars, .env files, secrets | `qwen2.5-coder:7b` |
| `monitoring-and-logging` | Observability, structured logging | `qwen2.5-coder:7b` |
| `ssl-certificate-management` | Cert creation, renewal, Let's Encrypt | `qwen2.5-coder:7b` |

### Workflow (7)
| Skill | Description | Model |
|---|---|---|
| `onboarding-new-project` | Project setup, understanding codebase | default |
| `troubleshooting-build-failures` | Build debugging, dep resolution | `qwen2.5-coder:7b` |
| `api-design` | REST/gRPC API design, versioning | default |
| `creating-microservices` | Service scaffold, Docker, CI | `qwen2.5-coder:7b` |
| `code-generation` | Scaffolding, boilerplate generation | `qwen2.5-coder:7b` |
| `pair-programming` | Collaborative coding, explain-as-you-go | default |
| `incident-response` | Production incident handling, postmortem | `qwen2.5-coder:7b` |

### Development Process (13 original)
| Skill | Description | Model |
|---|---|---|
| `brainstorming` | Creative exploration, approach design | default |
| `code-review` | Code review workflow | default |
| `dispatching-parallel-agents` | Parallel task execution | default |
| `executing-plans` | Plan execution with checkpoints | default |
| `finishing-a-development-branch` | Branch completion workflow | default |
| `receiving-code-review` | Handling review feedback | default |
| `subagent-driven-development` | Multi-agent development | default |
| `systematic-debugging` | Bug investigation methodology | default |
| `test-driven-development` | TDD workflow | default |
| `using-git-worktrees` | Git worktree management | default |
| `verification-before-completion` | Pre-completion checks | default |
| `writing-plans` | Implementation planning | default |
| `writing-skills` | Creating new skills | default |

## Automatic Skill Suggestions

PenguinCode analyzes user messages and suggests relevant skills via keyword matching. This is advisory only — skills are never auto-activated.

Example triggers:
- "there's a merge conflict" → suggests `resolving-merge-conflicts`
- "deploy to k8s" → suggests `deploying-to-kubernetes`
- "the build is failing" → suggests `troubleshooting-build-failures`
- "write unit tests" → suggests `writing-unit-tests`

## Creating Custom Skills

1. Create a directory in any skill location (see [Skill Locations](#skill-locations))
2. Add a `SKILL.md` with frontmatter and content
3. Optionally add supporting `.md` files (auto-appended)
4. Optionally reference other skills with `waddlepowers:skill-name`

For detailed guidance on writing effective skills, activate: `/skill writing-skills`

## Compatibility

PenguinCode skills use the same format as:
- **Claude Code** (`~/.claude/skills/`) — subdirectory + `SKILL.md`
- **OpenCode** (`~/.config/opencode/skills/`) — subdirectory + `SKILL.md`

Skills created for any of these tools are automatically discovered by PenguinCode.

## Configuration

Skills have permission settings managed by the server config store. Use `/config` to view current settings:

```bash
/config show    # See all settings including skill permissions
```

Each skill has an `enabled` flag and optional `permissions` list controlling what commands it can run.
