# === Kubernetes Deployment (microk8s + Helm v3) ===
PROJECT_NAME := $(shell basename $(CURDIR))
HELM_DIR := k8s/helm/$(PROJECT_NAME)

k8s-alpha-deploy:
	@./tests/k8s/alpha/run-all-alpha.sh

k8s-beta-deploy:
	@./tests/k8s/beta/run-all-beta.sh

k8s-prod-deploy:
	@read -p "Deploy to PRODUCTION? (yes/NO): " c && [ "$$c" = "yes" ]
	@helm upgrade --install $(PROJECT_NAME) ./$(HELM_DIR) --namespace $(PROJECT_NAME)-prod --create-namespace --values ./$(HELM_DIR)/values.yaml --wait --timeout 10m

k8s-alpha-test:
	@./tests/k8s/alpha/run-all-alpha.sh

k8s-beta-test:
	@./tests/k8s/beta/run-all-beta.sh

k8s-cleanup:
	@helm uninstall $(PROJECT_NAME) -n $(PROJECT_NAME)-alpha 2>/dev/null || true
	@helm uninstall $(PROJECT_NAME) -n $(PROJECT_NAME)-beta 2>/dev/null || true
	@kubectl delete namespace $(PROJECT_NAME)-alpha 2>/dev/null || true
	@kubectl delete namespace $(PROJECT_NAME)-beta 2>/dev/null || true

helm-lint:
	@helm lint ./$(HELM_DIR)

helm-template:
	@helm template $(PROJECT_NAME) ./$(HELM_DIR) --values ./$(HELM_DIR)/values-alpha.yaml

# === Standard Development Commands ===
dev:
	@docker-compose up

test:
	@$(MAKE) test-unit

test-unit:
	@python3 -m pytest tests/ -v

test-integration:
	@echo "No integration tests defined"

test-e2e:
	@echo "No e2e tests defined"

test-functional:
	@echo "No functional tests defined"

test-security:
	@echo "=== Security Scans ==="
	@if command -v bandit >/dev/null 2>&1; then echo "-- bandit --"; bandit -r . -x ./tests,./venv,./.git --quiet || true; fi
	@if command -v pip-audit >/dev/null 2>&1; then echo "-- pip-audit --"; find . -name "requirements.txt" -not -path "*/.git/*" -not -path "*/venv/*" | xargs -I{} pip-audit -r {} 2>/dev/null || true; fi
	@if command -v gosec >/dev/null 2>&1; then echo "-- gosec --"; find . -name "go.mod" -not -path "*/.git/*" -not -path "*/vendor/*" | xargs -I{} dirname {} | xargs -I{} sh -c 'cd {} && gosec ./... || true'; fi
	@if command -v govulncheck >/dev/null 2>&1; then echo "-- govulncheck --"; find . -name "go.mod" -not -path "*/.git/*" -not -path "*/vendor/*" | xargs -I{} dirname {} | xargs -I{} sh -c 'cd {} && govulncheck ./... || true'; fi
	@find . -name "package.json" -not -path "*/.git/*" -not -path "*/node_modules/*" -maxdepth 3 | xargs -I{} dirname {} | xargs -I{} sh -c 'cd {} && npm audit 2>/dev/null || true'
	@if command -v gitleaks >/dev/null 2>&1; then echo "-- gitleaks --"; gitleaks detect --source . --no-git 2>/dev/null || true; fi

smoke-test:
	@echo "No smoke tests defined"

lint:
	@echo "=== Linting ==="
	@if command -v flake8 >/dev/null 2>&1; then echo "-- flake8 --"; python3 -m flake8 . --max-line-length=120 --exclude=.git,__pycache__,venv,node_modules || true; fi
	@if command -v black >/dev/null 2>&1; then echo "-- black --"; black --check . --exclude '/(\.git|venv|__pycache__|node_modules)/' || true; fi
	@if command -v isort >/dev/null 2>&1; then echo "-- isort --"; isort --check-only . || true; fi
	@if command -v mypy >/dev/null 2>&1; then echo "-- mypy --"; python3 -m mypy . --ignore-missing-imports || true; fi
	@if command -v golangci-lint >/dev/null 2>&1; then echo "-- golangci-lint --"; find . -name "go.mod" -not -path "*/.git/*" -not -path "*/vendor/*" | xargs -I{} dirname {} | xargs -I{} sh -c 'cd {} && golangci-lint run || true'; fi
	@if command -v hadolint >/dev/null 2>&1; then echo "-- hadolint --"; find . -name "Dockerfile*" -not -path "*/.git/*" | xargs hadolint || true; fi
	@if command -v shellcheck >/dev/null 2>&1; then echo "-- shellcheck --"; find . -name "*.sh" -not -path "*/.git/*" | xargs shellcheck || true; fi

build:
	@pip3 install -e .

docker-build:
	@docker-compose build

docker-push:
	@echo "Push images - use CI pipeline"

deploy-dev:
	@echo "Deploy to dev - see k8s/kustomize/overlays/alpha"

deploy-prod:
	@echo "Deploy to prod - see k8s/helm/"

seed-mock-data:
	@echo "No mock data seeding defined"

clean:
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

pre-commit:
	@echo "=== Pre-commit checks ==="
	@$(MAKE) lint
	@$(MAKE) test-security
	@$(MAKE) test
	@echo "=== Pre-commit complete ==="
