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
#   PYSEC-2026-311 (chromadb, all versions >=1.0.0, NO fixed release exists)
#     Pre-authentication code injection in chromadb's SERVER component. This
#     repo never runs that server: there is no chroma k8s manifest and no chroma
#     image. create_rag_manager() defaults to the pgvector backend, and
#     shared/vectorstore/ ships only pgvector and qdrant. The one live consumer,
#     shared/utils/memory_integration.py, uses chromadb.PersistentClient -- an
#     embedded local-file store with no listening socket and therefore no
#     pre-auth surface. ChromaDBRAGStore can construct an HttpClient, but only
#     when host/port are explicitly configured, and then the vulnerable
#     component belongs to whoever operates that server.
#     Revisit when chromadb publishes a fix; the right long-term move is to drop
#     the chromadb backend entirely, since pgvector and qdrant already cover it.
PIP_AUDIT_IGNORES := --ignore-vuln PYSEC-2026-311

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
	$(PY) -m pytest tests/unit -v --cov-report=html:htmlcov

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
	  gitleaks detect --source . --no-git --redact --config .gitleaks.toml \
	    --exit-code 1 --log-level error || fail=1; \
	fi; \
	echo "-- pip-audit --"; \
	if [ -x $(VENV)/bin/pip-audit ]; then \
	  for r in requirements.txt proxy/requirements.txt services/management/requirements.txt; do \
	    tmp=$$(mktemp); \
	    counts=$$(awk -v target="en-core-web-lg" -v outfile="$$tmp" '{is_start=(length($$0)>0 && substr($$0,1,1) !~ /[ \t#]/); if (is_start) {name=$$0; sub(/[ \t@=\[].*/,"",name); gsub(/_/,"-",name); name=tolower(name); skip=(name==target); if (skip) excluded++; else count++} if (!skip) print > outfile} END{print count+0, excluded+0}' $$r); \
	    set -- $$counts; audited=$$1; excluded=$$2; \
	    echo "pip-audit: $$audited requirements audited, $$excluded excluded ($$r) -- en_core_web_lg is a spaCy model wheel from github.com/explosion release, hash-pinned in $$r, no PyPI entry"; \
	    if [ "$$audited" -eq 0 ]; then echo "!! pip-audit: 0 requirements audited in $$r -- filter produced an empty file, counting as FAILURE"; fail=1; fi; \
	    $(VENV)/bin/pip-audit -r $$tmp --strict $(PIP_AUDIT_IGNORES) || fail=1; \
	    rm -f "$$tmp"; \
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
	@echo "ERROR: deploy-prod is not implemented. See docs/docs-site/docs/deployment/kubernetes.md" >&2
	@exit 1

pre-commit:
	@echo "=== Pre-commit checks ==="
	@$(MAKE) lint
	@$(MAKE) test-security
	@$(MAKE) test
	@echo "=== Pre-commit complete ==="
