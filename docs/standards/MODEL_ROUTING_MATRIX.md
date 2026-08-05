# Model Routing Matrix for RTX 4090

## Overview

This matrix defines model selection by **tool category**, **complexity level**, and **region** for servers with **RTX 4090 (24GB VRAM)**.

**Key Constraints:**
- All models are **EU/North American origin only** (no Chinese models)
- Models must fit within 24GB VRAM budget
- High-complexity models use 4-bit quantization to maximize capability
- Only one high-complexity model can run concurrently

---

## Base Models by Complexity

### Low Complexity (6-8GB)
| Region | Model | VRAM | Origin | Capability |
|--------|-------|------|--------|------------|
| NA | `llama3.1:8b` | 6GB | Meta (US) | 0.40 |
| EU | `mistral:7b` | 5GB | Mistral (France) | 0.38 |

### Medium Complexity (12-14GB)
| Region | Model | VRAM | Origin | Capability |
|--------|-------|------|--------|------------|
| NA | `neural-chat:13b` | 12GB | Intel (US) | 0.60 |
| EU | `mistral:13b` | 13GB | Mistral (France) | 0.62 |

### High Complexity (18-24GB, 4-bit quantized)
| Region | Model | VRAM | Origin | Capability |
|--------|-------|------|--------|------------|
| NA | `llama3.1:70b-q4_K_M` | 18GB | Meta (US) | 0.85 |
| EU | `mistral-large:123b-q4_K_M` | 24GB | Mistral (France) | 0.92 |

---

## Tool Category Routing

### 🔧 Ultra-Light Operations
**Tools:** bash, file_edit, sql

| Complexity | NA Model | EU Model | VRAM | Rationale |
|------------|----------|----------|------|-----------|
| Low | `llama3.1:8b` | `mistral:7b` | 5-6GB | Fast execution, minimal reasoning |
| Medium | `neural-chat:13b` | `mistral:13b` | 12-13GB | Better context handling |
| High | `neural-chat:13b` | `mistral:13b` | 12-13GB | Rarely needed; same as medium |

### 🔍 Research & Web Search
**Tools:** web_search, data_analysis

| Complexity | NA Model | EU Model | VRAM | Rationale |
|------------|----------|----------|------|-----------|
| Low | `mistral:7b` | `mistral:7b` | 5GB | Good at summarization |
| Medium | `neural-chat:13b` | `mistral:13b` | 12-13GB | Conversation-optimized |
| High | `mistral:13b` | `mistral:13b` | 13GB | Same as medium (research rarely needs high) |

### 💻 Code Operations
**Tools:** python, javascript, typescript, go, rust, cpp, java

| Complexity | NA Model | EU Model | VRAM | Rationale |
|------------|----------|----------|------|-----------|
| Low | `llama3.1:8b` | `mistral:7b` | 5-6GB | Basic syntax & templates |
| Medium | `neural-chat:13b` | `mistral:13b` | 12-13GB | Function-level reasoning |
| High | `llama3.1:70b-q4_K_M` | `mistral-large:123b-q4_K_M` | 18-24GB | Full module/architecture design |

### 📝 Code Review & Testing
**Tools:** code_review, debug, test_write

| Complexity | NA Model | EU Model | VRAM | Rationale |
|------------|----------|----------|------|-----------|
| Low | `llama3.1:8b` | `mistral:7b` | 5-6GB | Linting-level checks |
| Medium | `neural-chat:13b` | `mistral:13b` | 12-13GB | Detailed test generation |
| High | `llama3.1:70b-q4_K_M` | `mistral-large:123b-q4_K_M` | 18-24GB | Complex test suites & debugging |

### 📚 Documentation & Architecture
**Tools:** documentation, refactor, architecture, devops

| Complexity | NA Model | EU Model | VRAM | Rationale |
|------------|----------|----------|------|-----------|
| Low | `mistral:7b` | `mistral:7b` | 5GB | Good at explanations |
| Medium | `neural-chat:13b` | `mistral:13b` | 12-13GB | Structured documentation |
| High | `llama3.1:70b-q4_K_M` | `mistral-large:123b-q4_K_M` | 18-24GB | System design & refactoring |

### 🎯 General Purpose
**Tools:** general

| Complexity | NA Model | EU Model | VRAM | Rationale |
|------------|----------|----------|------|-----------|
| Low | `llama3.1:8b` | `mistral:7b` | 5-6GB | Balanced, instruction-following |
| Medium | `neural-chat:13b` | `mistral:13b` | 12-13GB | Better reasoning |
| High | `llama3.1:70b-q4_K_M` | `mistral-large:123b-q4_K_M` | 18-24GB | Complex multi-step reasoning |

---

## Quantization & VRAM Estimates

All "High" complexity models use 4-bit quantization (`-q4_K_M`) to fit within 24GB:

| Model | Full Precision | 4-bit (q4_K_M) | Savings |
|-------|----------------|----|---------|
| llama3.1:70b | 140GB | ~18GB | 87% |
| mistral-large:123b | 246GB | ~24GB | 90% |
| neural-chat:13b | 26GB | 13GB | 50% |

---

## Concurrency & Resource Planning

**RTX 4090 (24GB) Running Limits:**

| Scenario | Models Running | Total VRAM | Safe |
|----------|---|---|---|
| 2× Low + 1× Medium | llama3.1:8b + mistral:7b + neural-chat:13b | 23GB | ✅ |
| 1× High alone | llama3.1:70b-q4 OR mistral-large:123b-q4 | 18-24GB | ✅ |
| 1× Medium + 1× Low | neural-chat:13b + llama3.1:8b | 18GB | ✅ |
| 2× High simultaneously | llama3.1:70b-q4 + mistral-large:123b-q4 | 42GB | ❌ NO |

**Recommendation:** Queue high-complexity requests or use a load balancer to serialize them.

---

## Pre-Execution Safety Validation

**Separate from main routing.** Every tool call is validated for safety **before execution** using a lightweight, fast model that checks the command/tool against a RAG database of unsafe patterns.

### Safety Validator Model

| Region | Model | VRAM | Response Time | Purpose |
|--------|-------|------|----------------|---------|
| NA | `llama3.1:8b` | 6GB | <500ms | Fast instruction-following, pattern matching |
| EU | `mistral:7b` | 5GB | <500ms | Quick safety assessment |

### Validation Flow

```
Tool/Command Request
    ↓
[Safety Validator] (always llama3.1:8b or mistral:7b)
    ↓
Check against RAG database of unsafe patterns:
    - Command injection attempts
    - Exfiltration attempts (sending files to external services)
    - Destructive operations (filesystem destruction, format disks)
    - Credential exposure (leaking API keys, passwords to stdout)
    - Unauthorized access attempts (privilege escalation)
    ↓
Decision: SAFE or BLOCKED
    ↓
If SAFE → Route to main matrix for execution
If BLOCKED → Reject with reason + security log
```

### Unsafe Pattern Categories

- **Command Injection**: Backticks, `$()`, pipes with user input
- **Exfiltration**: Network operations sending local files/secrets to external hosts
- **Destructive Ops**: `rm`, `mkfs`, `dd` targeting system directories
- **Credential Exposure**: Printing API keys, passwords, tokens to stdout
- **Privilege Escalation**: `sudo` for non-approved operations

### RAG Database Structure

Safety patterns stored with context:
- **Pattern** (regex or semantic description)
- **Operation Type** (destructive, exfiltration, credential-exposure, injection, privesc)
- **Severity** (BLOCK, WARN, ALLOW)
- **Reason** (why it's flagged)
- **Safe Contexts** (when the same pattern is OK)

Example entries:
```
Pattern: "rm -rf /", "mkfs", "dd to system disk"
Severity: BLOCK
Type: DESTRUCTIVE
Reason: "Filesystem destruction"
Safe Context: None

Pattern: "curl/wget to external domain with credentials"
Severity: BLOCK
Type: EXFILTRATION
Reason: "Data exfiltration with secrets"
Safe Context: None

Pattern: "SELECT * FROM users"
Severity: ALLOW
Type: DATABASE_QUERY
Safe Context: "Allowed in sql tool context"
```

### Performance SLA

- **Latency**: <500ms per request (synchronous, blocks execution)
- **Availability**: 99.9% (critical path)
- **False Positives**: <2% (acceptable to block edge cases)
- **False Negatives**: <0.5% (security-critical)

---

## Model Origin & Compliance

All models comply with supply chain restrictions:

| Model | Organization | Country | Status |
|-------|--------------|---------|--------|
| llama3.1:8b, 70b | Meta | USA | ✅ Approved |
| mistral:7b, 13b | Mistral AI | France | ✅ Approved |
| mistral-large:123b | Mistral AI | France | ✅ Approved |
| neural-chat:13b | Intel | USA | ✅ Approved |

---

## Migration from Previous Matrix

**Previous (v1.0):** Used models up to 236GB (DeepSeek, llama3.1:405b)  
**Current (v2.0):** RTX 4090-optimized with quantization

**Changes:**
- All models fit within 24GB VRAM
- High-complexity uses 4-bit quantization
- DeepSeek removed (China-based, violates supply chain rules)
- Llama 405B replaced with 70B-q4 (EU alternative: Mistral-Large-q4)
- Tool-based routing maintains same categories

---

## Usage Example

```python
# Request routing
tool_type = "python"           # Code operation
complexity = "high"            # Large refactor
region = "NA"                  # North America

# Resolves to:
# model = "llama3.1:70b-q4_K_M"
# vram_required = 18GB
# concurrency_limit = 1 (high models serialize)
```

---

## See Also

- [LANGUAGE_SELECTION.md](./LANGUAGE_SELECTION.md) — When to use which model type
- [PERFORMANCE.md](./PERFORMANCE.md) — Optimization for model inference
- [scripts/seed_routing_matrix.py](../../scripts/seed_routing_matrix.py) — Implementation
