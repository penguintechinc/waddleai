---
name: refactoring-safely
description: "Incremental refactoring with test coverage and verification"
model: qwen2.5-coder:7b
---

# Refactoring Safely

## Overview
Restructure code incrementally while maintaining behavior, with tests as a safety net.

## Golden Rules
1. **Never refactor and add features simultaneously**
2. **Tests must pass before AND after each change**
3. **Make small, reviewable changes**
4. **Commit after each successful refactoring step**

## Process
1. **Ensure test coverage** — write tests for the code you're about to change
2. **Run tests** — confirm they pass before starting
3. **Make one small change** — rename, extract, inline, move
4. **Run tests again** — verify nothing broke
5. **Commit** — save the working state
6. **Repeat** — continue with next small change

## Common Refactoring Patterns
- **Extract function** — move code block into its own function
- **Rename** — clarify variable/function/class names
- **Inline** — replace unnecessary abstraction with direct code
- **Move** — relocate code to more appropriate module
- **Extract class** — split a class that does too much
- **Replace conditional with polymorphism**

## Safety Checks
- Run full test suite after each step
- Use `git diff` to review each change
- Keep changes under 50 lines when possible
- If tests break, `git checkout -- .` and try a smaller step

## When NOT to Refactor
- Under time pressure for a release
- Without adequate test coverage
- Without understanding the existing code
