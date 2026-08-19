.PHONY: dev setup install-hooks verify-hooks test test-unit test-integration test-e2e test-functional test-security \
        test-contract smoke-test lint build docker-build docker-push deploy-dev deploy-prod \
        seed-mock-data clean pre-commit

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

lint:
	@echo "=== Linting ==="
	@if command -v ruff >/dev/null 2>&1; then echo "-- ruff check --"; ruff check . || true; echo "-- ruff format --"; ruff format --check . || true; fi
	@if command -v mypy >/dev/null 2>&1; then echo "-- mypy --"; python3 -m mypy . --ignore-missing-imports || true; fi
	@if command -v golangci-lint >/dev/null 2>&1; then echo "-- golangci-lint --"; find . -name "go.mod" -not -path "*/.git/*" -not -path "*/vendor/*" | xargs -I{} dirname {} | xargs -I{} sh -c 'cd {} && golangci-lint run || true'; fi
	@if command -v hadolint >/dev/null 2>&1; then echo "-- hadolint --"; find . -name "Dockerfile*" -not -path "*/.git/*" | xargs hadolint || true; fi
	@if command -v shellcheck >/dev/null 2>&1; then echo "-- shellcheck --"; find . -name "*.sh" -not -path "*/.git/*" | xargs shellcheck || true; fi

test:
	@$(MAKE) test-unit

test-unit:
	@echo "Running unit tests..."
	python3 -m pytest tests/unit -v

# --no-cov: a tests/integration-only run only exercises a fraction of
# shared/+services/management/app, so pytest.ini's default --cov addopts
# (60% floor, meant for the full tests/unit run above) fail every time
# regardless of whether the integration tests themselves pass -- mirrors
# test-contract's existing convention below.
test-integration:
	@echo "Running integration tests..."
	python3 -m pytest tests/integration -v --no-cov

# tests/e2e/ currently has no pytest tests (only the pre-existing Playwright
# JS suite + scaffolding; the real pytest suite is on feature/e2e-suite, not
# yet merged) -- `pytest tests/e2e` exits 5 ("no tests collected"), a hard
# failure by default. Tolerate exit 5 specifically so this target isn't
# permanently red for a suite that doesn't exist yet; once feature/e2e-suite
# merges this starts gating for real with no further Makefile change needed.
test-e2e:
	@echo "Running e2e tests..."
	@python3 -m pytest tests/e2e -v --no-cov; code=$$?; \
	if [ $$code -eq 5 ]; then \
		echo "No pytest tests collected under tests/e2e/ yet (pending merge of feature/e2e-suite) -- not a failure."; \
		exit 0; \
	fi; \
	exit $$code

test-functional:
	@echo "No functional tests defined"

test-contract:
	@echo "Running contract snapshot tests..."
	python3 -m pytest tests/contract -v --no-cov

test-security:
	@echo "=== Security Scans ==="
	@if command -v bandit >/dev/null 2>&1; then echo "-- bandit --"; bandit -r . -x ./tests,./venv,./.git --quiet || true; fi
	@if command -v pip-audit >/dev/null 2>&1; then echo "-- pip-audit --"; find . -name "requirements.txt" -not -path "*/.git/*" -not -path "*/venv/*" | xargs -I{} pip-audit -r {} 2>/dev/null || true; fi
	@if command -v gosec >/dev/null 2>&1; then echo "-- gosec --"; find . -name "go.mod" -not -path "*/.git/*" -not -path "*/vendor/*" | xargs -I{} dirname {} | xargs -I{} sh -c 'cd {} && gosec ./... || true'; fi
	@if command -v govulncheck >/dev/null 2>&1; then echo "-- govulncheck --"; find . -name "go.mod" -not -path "*/.git/*" -not -path "*/vendor/*" | xargs -I{} dirname {} | xargs -I{} sh -c 'cd {} && govulncheck ./... || true'; fi
	@find . -name "package.json" -not -path "*/.git/*" -not -path "*/node_modules/*" -maxdepth 3 | xargs -I{} dirname {} | xargs -I{} sh -c 'cd {} && npm audit 2>/dev/null || true'
	@if command -v gitleaks >/dev/null 2>&1; then echo "-- gitleaks --"; gitleaks detect --source . --no-git 2>/dev/null || true; fi
	@echo "-- pip-licenses (OSI gate) --"; bash scripts/check-licenses.sh

smoke-test:
	@echo "Running smoke tests..."
	@if [ -d tests/smoke ]; then python3 -m pytest tests/smoke -v; fi

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
