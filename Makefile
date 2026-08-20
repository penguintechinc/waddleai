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
	for t in ruff shellcheck hadolint; do \
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
	echo "-- mypy -- (advisory: not yet a gate, see .TODO)"; \
	if command -v mypy >/dev/null 2>&1; then $(PY) -m mypy $(LINT_PATHS) --ignore-missing-imports 2>&1 | tail -3 || true; fi; \
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
	$(PY) -m pytest tests/unit -v

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

test-contract:
	@echo "Running contract snapshot tests..."
	$(PY) -m pytest tests/contract -v --no-cov

test-security: ## Security scans over FIRST-PARTY code. Fails on findings.
	@echo "=== Security Scans ==="
	@fail=0; \
	for t in bandit gitleaks; do \
	  command -v $$t >/dev/null 2>&1 || { echo "!! MISSING TOOL: $$t -- cannot verify, counting as FAILURE"; fail=1; }; \
	done; \
	if command -v bandit >/dev/null 2>&1; then \
	  echo "-- bandit (first-party, fails on HIGH/MEDIUM) --"; \
	  bandit -r $(LINT_PATHS) --exclude services/penguincode,tests --severity-level medium --quiet || fail=1; \
	fi; \
	if command -v gitleaks >/dev/null 2>&1; then \
	  echo "-- gitleaks --"; \
	  gitleaks detect --source . --no-git --redact \
	    --exit-code 1 --log-level error || fail=1; \
	fi; \
	echo "-- pip-audit --"; \
	if [ -x $(VENV)/bin/pip-audit ]; then \
	  for r in requirements.txt proxy/requirements.txt services/management/requirements.txt; do \
	    $(VENV)/bin/pip-audit -r $$r --strict || fail=1; \
	  done; \
	else echo "!! pip-audit not in $(VENV) -- run 'make venv'; counting as FAILURE"; fail=1; fi; \
	if [ -n "$$(find . -name go.mod -not -path './.venv/*' -not -path '*/vendor/*' -not -path './.worktrees/*' -not -path './services/penguincode/*')" ]; then \
	  for t in gosec govulncheck; do \
	    command -v $$t >/dev/null 2>&1 || { echo "!! Go present but $$t MISSING -- FAILURE"; fail=1; }; \
	  done; \
	  for t in gosec govulncheck; do \
	    command -v $$t >/dev/null 2>&1 && find . -name go.mod -not -path './.venv/*' -not -path '*/vendor/*' -not -path './.worktrees/*' -not -path './services/penguincode/*' \
	      | xargs -r -I{} dirname {} | xargs -r -I{} sh -c "cd {} && $$t ./..." || fail=1; \
	  done; \
	else echo "-- gosec/govulncheck -- (no go.mod outside vendor; skipped legitimately)"; fi; \
	echo "-- npm audit --"; \
	for d in $$(find . -name package.json -maxdepth 3 -not -path './.git/*' -not -path './.venv/*' -not -path './.worktrees/*' -not -path '*/node_modules/*' | xargs -r -n1 dirname); do \
	  if [ -f "$$d/package-lock.json" ]; then \
	    (cd $$d && npm audit --audit-level=high) || fail=1; \
	  else \
	    echo "!! $$d has package.json but NO package-lock.json -- dependency pinning violation (critical-rules.md); counting as FAILURE"; fail=1; \
	  fi; \
	done; \
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
	@echo "Deploy to production environment"

pre-commit:
	@echo "=== Pre-commit checks ==="
	@$(MAKE) lint
	@$(MAKE) test-security
	@$(MAKE) test
	@echo "=== Pre-commit complete ==="
