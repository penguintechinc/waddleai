.PHONY: dev setup install-hooks verify-hooks venv test test-unit test-integration test-e2e test-functional test-security \
        test-contract smoke-test smoke-test-production lint build docker-build docker-push deploy-dev deploy-prod \
        seed-mock-data clean pre-commit generate-openapi openapi-lint

# Every python invocation goes through the repo venv when it exists, and only
# falls back to the interpreter on PATH when it does not.
#
# Bare `python3` resolved to user-global site-packages, where penguin-libs is
# installed EDITABLE against local checkouts -- one of them a feature worktree.
# So "penguin-dal 0.4.1" locally and "penguin-dal 0.4.1" in CI were different
# code, and tests silently exercised unpublished work. The host interpreter is
# also 3.12 while CI and backend-python.md both require 3.13.
# Run `make venv` once; every target below then matches CI.
VENV := .venv

# First-party code only. Excluding these is not leniency -- including them
# is what made the old scans useless: bandit excluded ./venv (note the
# missing dot) so it walked .venv and reported 59 HIGH / 7454 LOW from
# third-party packages, burying the 0 HIGH / 2 MEDIUM that are actually
# ours. .worktrees is a second checkout of this same repo.
LINT_PATHS := proxy services shared scripts tests
SCAN_EXCLUDE := ./.venv,./.git,./.worktrees,./services/penguincode,./node_modules
PY := $(shell [ -x $(VENV)/bin/python ] && echo $(VENV)/bin/python || echo python3)

# pip-audit: advisories accepted with a written reason. This list is NOT a
# convenience hatch -- every entry needs a reason that survives review, and it
# gets re-checked whenever a fixed release appears.
#
# No accepted advisories currently. PYSEC-2026-311 (chromadb, all versions
# >=1.0.0, no fixed release) used to be ignored here; the chromadb backend
# (shared/utils/memory_integration.py's ChromaDBMemoryStore,
# shared/utils/rag_integration.py's ChromaDBRAGStore, and the chromadb
# dependency itself) was removed instead of carrying the exception forward,
# since pgvector and qdrant already cover the same ground. A config value
# that still names "chromadb" now fails fast at the create_memory_manager()/
# create_rag_manager() call site rather than resolving to a vulnerable
# dependency.
PIP_AUDIT_IGNORES :=

venv: ## Create .venv (3.13) from the hash-pinned lockfiles -- published deps only
	@uv venv -p 3.13 $(VENV)
	@uv pip install --python $(VENV)/bin/python -r requirements.txt
	@uv pip install --python $(VENV)/bin/python -r services/management/requirements.txt
	@uv pip install --python $(VENV)/bin/python -r proxy/requirements.txt
	@uv pip install --python $(VENV)/bin/python pytest pytest-asyncio pytest-cov pip-audit
	@echo "venv ready: $(VENV) ($$($(VENV)/bin/python -V))"

setup: install-hooks
	@echo "Setup complete"

install-hooks: ## Install pre-commit framework + register pre-commit and pre-push hooks
	@./scripts/install-pre-commit.sh

verify-hooks: ## Report whether pre-commit/pre-push hooks are installed and non-empty
	@./scripts/install-pre-commit.sh --verify

dev:
	docker-compose up

build:
	docker-compose build

docker-build: build

docker-push:
	@echo "Push images to registry"

# shellcheck runs at --severity=warning to match the hook in
# .pre-commit-config.yaml. Without that, `make lint` and the commit hook
# disagree and a commit can pass one while failing the other.
lint: ## Lint everything. Fails on error -- no `|| true`, no silent skips.
	@echo "=== Linting ==="
	@fail=0; \
	for t in ruff shellcheck hadolint mypy; do \
	  command -v $$t >/dev/null 2>&1 || { echo "!! MISSING TOOL: $$t -- cannot verify, counting as FAILURE"; fail=1; }; \
	done; \
	if command -v ruff >/dev/null 2>&1; then \
	  echo "-- ruff check --"; ruff check $(LINT_PATHS) || fail=1; \
	  echo "-- ruff format --"; ruff format --check $(LINT_PATHS) || fail=1; \
	fi; \
	if command -v shellcheck >/dev/null 2>&1; then \
	  echo "-- shellcheck --"; \
	  find . -name "*.sh" -not -path "./.git/*" -not -path "./.venv/*" -not -path "./.worktrees/*" -not -path "*/node_modules/*" -not -path "./services/penguincode/*" -print0 \
	    | xargs -0 -r shellcheck --severity=warning || fail=1; \
	fi; \
	if command -v hadolint >/dev/null 2>&1; then \
	  echo "-- hadolint --"; \
	  find . -name "Dockerfile*" -not -path "./.git/*" -not -path "./.venv/*" -not -path "./.worktrees/*" -not -path "./services/penguincode/*" -print0 \
	    | xargs -0 -r hadolint || fail=1; \
	fi; \
	if [ -n "$$(find . -name go.mod -not -path './.venv/*' -not -path '*/vendor/*' -not -path './.worktrees/*' -not -path './services/penguincode/*')" ]; then \
	  command -v golangci-lint >/dev/null 2>&1 || { echo "!! Go modules present but golangci-lint MISSING -- FAILURE"; fail=1; }; \
	  if command -v golangci-lint >/dev/null 2>&1; then \
	    echo "-- golangci-lint --"; \
	    find . -name go.mod -not -path './.venv/*' -not -path '*/vendor/*' -not -path './.worktrees/*' -not -path './services/penguincode/*' \
	      | xargs -r -I{} dirname {} | xargs -r -I{} sh -c 'cd {} && golangci-lint run' || fail=1; \
	  fi; \
	else echo "-- golangci-lint -- (no go.mod outside vendor; skipped legitimately)"; fi; \
	echo "-- mypy -- (gated: fails on any error not already in mypy-baseline.txt)"; \
	PY=$(PY) bash scripts/mypy-gate.sh || fail=1; \
	[ $$fail -eq 0 ] || { echo "=== LINT FAILED ==="; exit 1; }; \
	echo "=== lint clean ==="

generate-openapi: ## Regenerate openapi/v1.yaml from the quart-schema annotations
	@$(PY) scripts/generate_openapi_spec.py

openapi-lint: ## Lint openapi/v1.yaml with spectral -- gates on error, not just warn (no || true)
	@command -v spectral >/dev/null 2>&1 || npm install -g @stoplight/spectral-cli@6.16.3
	spectral lint openapi/v1.yaml --fail-severity=error

test:
	@$(MAKE) test-unit

test-unit:
	@echo "Running unit tests..."
	@scripts/check_collected_floor.sh $(PY) tests/unit -v --cov-report=html:htmlcov

# --no-cov: a tests/integration-only run only exercises a fraction of
# shared/+services/management/app, so pytest.ini's default --cov addopts
# (60% floor, meant for the full tests/unit run above) fail every time
# regardless of whether the integration tests themselves pass -- mirrors
# test-contract's existing convention below.
test-integration:
	@echo "Running integration tests..."
	$(PY) -m pytest tests/integration -v --no-cov

# tests/e2e/ currently has no pytest tests (only the pre-existing Playwright
# JS suite + scaffolding; the real pytest suite is on feature/e2e-suite, not
# yet merged) -- `pytest tests/e2e` exits 5 ("no tests collected"), a hard
# failure by default. Tolerate exit 5 specifically so this target isn't
# permanently red for a suite that doesn't exist yet; once feature/e2e-suite
# merges this starts gating for real with no further Makefile change needed.
test-e2e:
	@echo "Running e2e tests..."
	$(PY) -m pytest tests/e2e -v --no-cov

test-functional:
	@echo "No functional tests defined"

# GPU tier: the `gpu`-marked tests that talk to a REAL Ollama-served model
# instead of a stub. Deselected from every other target by pytest.ini's marker
# convention, so they only ever run when asked for explicitly here.
#
# Pointed at the MINIMUM supported models on purpose, not the recommended ones
# -- gemma4:e4b for the routing classifier and shieldgemma:2b for the security
# auditor. If the floor works, everything above it does; testing only the
# recommendation would let the floor rot unnoticed.
#
# Against a remote box (the daemon there needs OLLAMA_HOST=0.0.0.0:11434 to
# accept non-local connections):
#   make test-gpu OLLAMA_HOST=http://gaming-laptop.local:11434
OLLAMA_HOST ?= http://localhost:11434
test-gpu: ## Run the real-model (GPU) test tier. Needs Ollama + gemma4:e4b + shieldgemma:2b.
	@echo "Running GPU-tier tests against $(OLLAMA_HOST)..."
	@WADDLEAI_GPU_TESTS=1 OLLAMA_HOST=$(OLLAMA_HOST) \
	  $(PY) -m pytest tests -m gpu -v --no-cov

test-contract:
	@echo "Running contract snapshot tests..."
	$(PY) -m pytest tests/contract -v --no-cov

test-security: ## Security scans over FIRST-PARTY code. Fails on findings.
	@echo "=== Security Scans ==="
	@fail=0; \
	command -v bandit >/dev/null 2>&1 || { echo "!! MISSING TOOL: bandit -- cannot verify, counting as FAILURE"; fail=1; }; \
	if command -v bandit >/dev/null 2>&1; then \
	  echo "-- bandit (first-party, fails on HIGH/MEDIUM) --"; \
	  bandit -r $(LINT_PATHS) --exclude services/penguincode,tests --severity-level medium --quiet || fail=1; \
	fi; \
	VENV=$(VENV) PIP_AUDIT_IGNORES="$(PIP_AUDIT_IGNORES)" bash scripts/dependency-security-scan.sh || fail=1; \
	echo "-- pip-licenses (OSI gate) --"; bash scripts/check-licenses.sh || fail=1; \
	[ $$fail -eq 0 ] || { echo "=== SECURITY SCANS FAILED ==="; exit 1; }; \
	echo "=== security scans clean ==="

smoke-test:
	@echo "Running smoke tests..."
	@bash tests/smoke/test_management_build.sh

smoke-test-production: ## Live prod checks (network + real deployment required) -- not part of pre-commit
	@echo "Running production smoke tests..."
	@bash tests/smoke/test-production.sh

seed-mock-data:
	@echo "No mock data seeding defined"

clean:
	docker-compose down -v
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true

deploy-dev:
	@echo "Deploy to dev/alpha environment"

deploy-prod:
	@echo "ERROR: deploy-prod is not implemented. See docs/docs-site/docs/deployment/kubernetes.md" >&2
	@exit 1

pre-commit:
	@echo "=== Pre-commit checks ==="
	@$(MAKE) lint
	@$(MAKE) test-security
	@$(MAKE) test
	@echo "=== Pre-commit complete ==="
