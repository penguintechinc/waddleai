# === Kubernetes Deployment (microk8s + Helm v3) ===
PROJECT_NAME := $(shell basename $(CURDIR))
HELM_DIR := k8s/helm/$(PROJECT_NAME)

# bandit/pip-audit MUST run under the same interpreter version this package
# targets (`requires-python = ">=3.13"`, pyproject.toml). A machine-/CI-wide
# `bandit`/`pip-audit` install can be shebang-pinned to a different system
# python (observed: /usr/bin/python3 == 3.12 on a dev box that also has
# 3.13 available) -- when that happens pip-audit's own dependency-resolution
# venv silently ignores every PyPI release whose `Requires-Python` excludes
# that older interpreter (e.g. quart==0.23.1 needs >=3.13) and fails with a
# "Could not find a version that satisfies..." resolution error that reads
# like a bad pin or a yanked release, when it is neither. This venv is
# scoped to this package only (Makefile-local, gitignored) and pins the
# tools to the one interpreter version this package actually requires, so
# `test-security` below is reproducible regardless of what else happens to
# be on PATH.
SECURITY_VENV := .venv

security-venv:
	@if [ ! -x $(SECURITY_VENV)/bin/python ]; then uv venv -p 3.13 $(SECURITY_VENV); fi
	@if [ ! -x $(SECURITY_VENV)/bin/bandit ] || [ ! -x $(SECURITY_VENV)/bin/pip-audit ]; then \
	  uv pip install --python $(SECURITY_VENV)/bin/python bandit==1.8.3 pip-audit==2.10.1; \
	fi

k8s-alpha-deploy:
	@./tests/k8s/alpha/run-all-alpha.sh

k8s-beta-deploy:
	@./tests/k8s/beta/run-all-beta.sh

k8s-prod-deploy:
	@read -p "Deploy to PRODUCTION? (yes/NO): " c && [ "$$c" = "yes" ]
	@helm upgrade --install $(PROJECT_NAME) ./$(HELM_DIR) --namespace $(PROJECT_NAME) --create-namespace --values ./$(HELM_DIR)/production.yml --wait --timeout 10m

k8s-alpha-test:
	@./tests/k8s/alpha/run-all-alpha.sh

k8s-beta-test:
	@./tests/k8s/beta/run-all-beta.sh

# Namespace = product name only, never env-suffixed (env lives in the values
# filename, not the namespace) -- see devops-kubernetes.md Core Rules.
k8s-cleanup:
	@helm uninstall $(PROJECT_NAME) -n $(PROJECT_NAME) 2>/dev/null || true
	@kubectl delete namespace $(PROJECT_NAME) 2>/dev/null || true

helm-lint:
	@helm lint ./$(HELM_DIR)

helm-template:
	@helm template $(PROJECT_NAME) ./$(HELM_DIR) --values ./$(HELM_DIR)/alpha.yml

# === gRPC Proto Codegen ===
# Regenerates every penguincode .proto (existing + new) into its committed
# location, deterministically: grpc_tools.protoc's raw codegen needs two
# fixups before it's importable as part of this package / ruff-clean (see
# scripts/fix_proto_codegen.py's module docstring for the full why):
#   1. *_pb2_grpc.py's own-module import made package-relative
#      (`from . import X_pb2 as ...`) -- protoc emits an absolute import in
#      both the flat (penguincode.proto) and nested (knowledge/v1/*) cases.
#   2. *_pb2.py's cross-proto dependency imports (e.g. importing
#      google/protobuf/struct.proto) relocated to the top import block and
#      `# noqa: F401`-tagged -- otherwise `ruff check --fix` deletes them as
#      "unused" (the alias is only referenced by the descriptor pool's own
#      import graph), silently breaking the module at import time.
# `ruff check --fix` + `ruff format` then bring the generated files in line
# with this repo's canonical (and only) lint/format tool -- protoc's raw
# output on its own is not ruff-clean (unused `import warnings`,
# `class Foo(object):`, stray f-string prefixes all trip `ruff check`).
#
# Run twice with no diff (`git diff --stat` empty on the second run) to
# confirm this target is deterministic; see tests/test_proto_knowledge.py
# for the corresponding import/message smoke test.
PROTO_DIR := penguincode_cli/proto
PROTO_FILES := $(PROTO_DIR)/penguincode.proto $(PROTO_DIR)/knowledge/v1/knowledge.proto $(PROTO_DIR)/lessons/v1/lessons.proto

proto:
	@echo "=== Regenerating gRPC stubs (grpcio-tools) ==="
	python3 -m grpc_tools.protoc \
		-I $(PROTO_DIR) \
		--python_out=$(PROTO_DIR) \
		--grpc_python_out=$(PROTO_DIR) \
		$(PROTO_FILES)
	@echo "-- fixing package-relative + dependency imports in generated code --"
	python3 scripts/fix_proto_codegen.py $(PROTO_DIR)
	@echo "-- ruff check --fix + ruff format (this repo's sole lint/format tool) --"
	ruff check --fix --quiet $(PROTO_DIR)
	ruff format --quiet $(PROTO_DIR)
	@echo "=== proto regeneration complete ==="

# === Standard Development Commands ===
# Docker Compose is DEPRECATED for all environments (devops.md) -- local dev
# runs on Kubernetes via the alpha Helm values (see tests/k8s/alpha/run-all-alpha.sh).
dev:
	@$(MAKE) k8s-alpha-deploy

test:
	@$(MAKE) test-coverage

test-unit:
	@python3 -m pytest tests/ -v

# Coverage-gated test run (CI's test-penguincode job and pre-commit both call
# this -- `--no-cov` must never appear anywhere in this chain again, see
# .github/workflows/docker-build.yml test-penguincode). Two sequential
# recipe lines, each a separate make-invoked shell: pytest's own non-zero
# exit already fails `make` on its own (no pipe to mask it), then
# coverage_gate.py enforces two independent thresholds against the data
# file pytest left behind: 90% line+branch on the knowledge-platform
# modules (Tier A), and the whole-package ratchet floor in
# .coverage-floor (Tier B) -- see that script's module docstring.
test-coverage:
	python3 -m pytest tests/ -v --cov --cov-branch --cov-report=term-missing
	python3 scripts/coverage_gate.py

test-integration:
	@echo "No integration tests defined"

test-e2e:
	@echo "No e2e tests defined"

test-functional:
	@echo "No functional tests defined"

# Real gate -- every scanner below used to be piped through `|| true`,
# so a HIGH bandit finding or a known-CVE dependency could merge unnoticed
# (critical-rules.md Verification Integrity: a gate that cannot fail is not
# a gate). No `|| true` anywhere in this chain; a missing tool or a 0-item
# scan counts as FAILURE, not a silent skip.
#
# bandit gains --severity-level medium: unfiltered, this codebase alone
# reports ~180 findings, nearly all LOW/Confidence noise that nobody can
# act on; medium+ is the real, actionable denominator (0 HIGH/0 MEDIUM as
# of this gate's un-masking -- see git history for the fixes/justified
# `# nosec` lines).
#
# pip-audit/bandit run from `security-venv` (see above) rather than
# whatever's on PATH, to avoid the python-version resolution trap documented
# there.
test-security: security-venv
	@echo "=== Security Scans ==="
	@fail=0; \
	echo "-- bandit --"; \
	$(SECURITY_VENV)/bin/bandit -r . -x ./tests,./venv,./$(SECURITY_VENV),./.git --severity-level medium --quiet || fail=1; \
	echo "-- pip-audit --"; \
	reqs=$$(find . -name "requirements.txt" -not -path "*/.git/*" -not -path "*/venv/*" -not -path "./$(SECURITY_VENV)/*"); \
	req_count=$$(printf '%s\n' "$$reqs" | grep -c . || true); \
	echo "pip-audit: $$req_count requirements.txt file(s) found"; \
	if [ "$$req_count" -eq 0 ]; then \
	  echo "!! pip-audit: 0 requirements.txt found -- counting as FAILURE"; fail=1; \
	else \
	  printf '%s\n' "$$reqs" | xargs -I{} $(SECURITY_VENV)/bin/pip-audit -r {} || fail=1; \
	fi; \
	if [ -n "$$(find . -name go.mod -not -path './.git/*' -not -path '*/vendor/*')" ]; then \
	  for t in gosec govulncheck; do \
	    command -v $$t >/dev/null 2>&1 || { echo "!! Go present but $$t MISSING -- FAILURE"; fail=1; }; \
	  done; \
	  for t in gosec govulncheck; do \
	    command -v $$t >/dev/null 2>&1 && find . -name go.mod -not -path './.git/*' -not -path '*/vendor/*' \
	      | xargs -I{} dirname {} | xargs -I{} sh -c "cd {} && $$t ./..." || fail=1; \
	  done; \
	else echo "-- gosec/govulncheck -- (no go.mod; skipped legitimately)"; fi; \
	echo "-- npm audit -- (no package.json in this service; skipped legitimately)"; \
	if command -v gitleaks >/dev/null 2>&1; then \
	  echo "-- gitleaks --"; \
	  gitleaks detect --source . --no-git --redact --config .gitleaks.toml --exit-code 1 --log-level error || fail=1; \
	else echo "!! MISSING TOOL: gitleaks -- cannot verify, counting as FAILURE"; fail=1; fi; \
	[ $$fail -eq 0 ] || { echo "=== SECURITY SCANS FAILED ==="; exit 1; }; \
	echo "=== security scans clean ==="

# Real smoke gate (S1 fix -- was `echo "No smoke tests defined"`, an
# unconditional-pass stub that meant the org's every-commit smoke gate never
# actually ran for penguincode). `scripts/smoke_test.py` import-smokes the
# package + generated protos, starts a real gRPC server against an ephemeral
# pgvector container, exercises Health + one KnowledgeService RPC with a
# WaddleAI dev token, and asserts real OTel spans/metrics/logs were emitted --
# see that script's module docstring for the full gate list. `bash -euo
# pipefail` (not the Makefile's default `/bin/sh` recipe shell) so a failure
# anywhere in the pipeline is never masked; `timeout 110` keeps this under the
# <2min smoke-test budget even if a container/RPC hangs instead of failing
# outright. No `|| true` anywhere in this chain -- a non-zero exit here must
# fail `make smoke-test`.
smoke-test:
	@bash -euo pipefail -c 'timeout 110 python3 scripts/smoke_test.py'

# Ratchet lint/type gate (S2 fix -- every tool below used to be piped
# through `|| true`, including a flake8/black/isort stack that isn't even
# this repo's configured linter, and `mypy --ignore-missing-imports`
# instead of this package's own `[tool.mypy] strict = true`. A gate that
# can never fail is not a gate (critical-rules.md Verification Integrity).
# `scripts/lint_gate.py` runs the real tools (`ruff check`, `ruff format
# --check`, `mypy --strict`) and fails hard on any finding not already in
# the committed `.ruff-baseline.txt` / `.mypy-baseline.txt` -- see that
# script's module docstring for scope/denominator detail and
# docs/penguincode/CONTRIBUTING.md's "Lint Gate" section for how to ratchet
# the baselines down. No `|| true` anywhere in this chain.
lint:
	@echo "=== Linting (ratchet gate: fails on NEW ruff/mypy findings only) ==="
	python3 scripts/lint_gate.py

build:
	@pip3 install -e .

docker-build:
	@docker build -f Dockerfile.server -t penguincode/server:local .

docker-push:
	@echo "Push images - use CI pipeline"

deploy-dev:
	@echo "Deploy to dev - see k8s/helm/penguincode (make k8s-alpha-deploy)"

deploy-prod:
	@echo "Deploy to prod - see k8s/helm/penguincode (make k8s-prod-deploy)"

seed-mock-data:
	@echo "No mock data seeding defined"

clean:
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

pre-commit:
	@echo "=== Pre-commit checks ==="
	@$(MAKE) lint
	@$(MAKE) test-security
	@$(MAKE) smoke-test
	@$(MAKE) test
	@echo "=== Pre-commit complete ==="
