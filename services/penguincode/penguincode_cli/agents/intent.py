"""User intent detection and skill suggestion for the chat agent."""

import re


def detect_user_intent(user_message: str) -> str | None:
    """
    Detect user intent from their message to determine which agent to spawn.

    This is a fallback when the LLM doesn't properly call tools.

    Args:
        user_message: The user's message

    Returns:
        Agent name to spawn, or None if unclear
    """
    msg_lower = user_message.lower()

    # Explicit plan requests -> planner (must come before research and executor)
    if any(kw in msg_lower for kw in [
        "create a plan", "make a plan", "write a plan", "design a plan",
        "plan how ", "plan out ", "plan for ", "plan to ",
    ]):
        return "spawn_planner"

    # Research patterns - check before executor to avoid false positives
    # (e.g., "documentation for pytest" shouldn't trigger "pytest" -> executor)
    if any(kw in msg_lower for kw in [
        "how do i ", "how to ", "what is ", "explain ",
        "tell me about ", "difference between ", "compare ",
        "documentation", "docs for ", "tutorial ",
        "research ", "look up ",
    ]):
        return "spawn_researcher"

    # Complex task patterns -> planner
    if any(kw in msg_lower for kw in [
        "implement ", "build a ", "create a system",
        "refactor ", "redesign ", "architect ",
    ]):
        return "spawn_planner"

    # File creation/writing patterns -> executor
    # Check for "write/create ... file/script" pattern with anything in between
    if re.search(r'\b(create|write|make|add)\s+(?:a\s+)?(?:\w+\s+)?(file|script)\b', msg_lower):
        return "spawn_executor"
    # Check for file extension patterns like "testing.py", "hello.sh"
    if re.search(r'\b\w+\.(py|js|ts|sh|bash|rb|go|rs|java|c|cpp|h|txt|json|yaml|yml|md|html|css)\b', msg_lower):  # noqa: SIM102
        # Has a file extension mentioned - likely wants to create/edit
        if any(kw in msg_lower for kw in ["write", "create", "make", "add", "generate"]):
            return "spawn_executor"
    if any(kw in msg_lower for kw in [
        "save to file", "save file", "new file", "touch ", "echo ",
    ]):
        return "spawn_executor"

    # Code execution patterns -> executor
    if any(kw in msg_lower for kw in [
        "run ", "execute ", "install ", "build ", "compile ",
        "test ", "pytest", "npm ", "pip ", "cargo ",
    ]):
        return "spawn_executor"

    # File editing patterns -> executor
    if any(kw in msg_lower for kw in [
        "edit ", "modify ", "change ", "update ", "fix ",
        "add to ", "remove from ", "delete from ",
    ]):
        return "spawn_executor"

    # Reading/exploring patterns -> explorer
    if any(kw in msg_lower for kw in [
        "read ", "show ", "display ", "what's in ", "what is in ",
        "find ", "search ", "look for ", "where is ",
        "list ", "ls ", "cat ",
    ]):
        return "spawn_explorer"

    return None


def suggest_skill(user_message: str) -> str | None:
    """Suggest a relevant skill based on the user's message.

    This is advisory only — it returns a skill name to suggest,
    never auto-activates.

    Args:
        user_message: The user's message

    Returns:
        Skill name to suggest, or None
    """
    msg = user_message.lower()

    # --- Git Operations (specific patterns first) ---
    if any(kw in msg for kw in ["cherry-pick", "cherry pick", "backport"]):
        return "cherry-picking"
    if any(kw in msg for kw in ["bisect", "regression", "which commit"]):
        return "git-bisect-debugging"
    if any(kw in msg for kw in ["merge conflict", "conflict marker"]):
        return "resolving-merge-conflicts"
    if any(kw in msg for kw in ["commit", "pre-commit", "git add"]):
        return "committing-changes"
    if any(kw in msg for kw in ["push", "pull request", "pr ", "create pr"]):
        return "pushing-to-github"
    if any(kw in msg for kw in ["branch naming", "branch strategy", "feature branch"]):
        return "branching-strategy"

    # --- Incidents & Operations ---
    if any(kw in msg for kw in ["incident", "outage", "production down", "postmortem"]):
        return "incident-response"
    if any(kw in msg for kw in ["rollback", "revert deploy", "undo deploy"]):
        return "deployment-rollback"

    # --- Build Failures (before testing to catch "build fail" patterns) ---
    if any(kw in msg for kw in [
        "build fail", "build error", "won't compile", "compile error",
        "build is fail", "build broke",
    ]):
        return "troubleshooting-build-failures"

    # --- Testing ---
    if any(kw in msg for kw in ["smoke test", "quick test", "health check"]):
        return "smoke-testing"
    if any(kw in msg for kw in ["integration test", "cross-service test"]):
        return "integration-testing"
    if any(kw in msg for kw in ["load test", "benchmark", "performance test", "perf test"]):
        return "performance-testing"
    if any(kw in msg for kw in [
        "api test", "endpoint test", "status code", "test the api",
        "test api endpoint", "test endpoint",
    ]):
        return "testing-api-endpoints"
    if any(kw in msg for kw in ["unit test", "write test", "mock", "pytest"]):
        return "writing-unit-tests"
    if any(kw in msg for kw in [
        "tdd", "test first", "test coverage", "add tests", "missing tests",
    ]):
        return "test-driven-development"

    # --- Docker / Containers ---
    if any(kw in msg for kw in [
        "docker build", "dockerfile", "multi-arch", "buildx", "container image",
    ]):
        return "building-docker-images"
    if any(kw in msg for kw in ["docker-compose", "docker compose", "dev environment"]):
        return "docker-compose-development"
    if any(kw in msg for kw in ["container log", "docker exec", "container debug"]):
        return "debugging-containers"
    if any(kw in msg for kw in [
        "container security", "image scan", "trivy", "scan container",
        "container vulnerabilit",
    ]):
        return "container-security"

    # --- Security (after container-security for specificity) ---
    if any(kw in msg for kw in [
        "security scan", "sast", "secret detection", "vulnerability scan",
    ]):
        return "security-scanning"

    # --- Kubernetes ---
    if any(kw in msg for kw in [
        "deploy to k8s", "kubectl apply", "kubernetes deploy", "k8s deploy",
    ]):
        return "deploying-to-kubernetes"
    if any(kw in msg for kw in [
        "k8s debug", "pod log", "kubectl describe", "crashloop",
    ]):
        return "kubernetes-debugging"
    if any(kw in msg for kw in ["hpa", "autoscal", "k8s scale", "replica"]):
        return "kubernetes-scaling"
    if any(kw in msg for kw in ["helm", "chart", "helm install", "helm upgrade"]):
        return "helm-chart-management"

    # --- CI/CD (use word-boundary-aware patterns) ---
    if any(kw in msg for kw in [
        "ci/cd", "ci pipeline", "github action", "workflow", "ci check",
    ]):
        return "github-actions-workflows"
    if any(kw in msg for kw in [
        "release", "version bump", "changelog", "tag release",
    ]):
        return "release-management"

    # --- Code Quality ---
    if any(kw in msg for kw in ["lint", "format", "flake8", "eslint", "prettier"]):
        return "linting-and-formatting"
    if any(kw in msg for kw in [
        "dependency", "upgrade dep", "audit dep", "outdated package",
        "audit the dep",
    ]):
        return "dependency-management"
    if any(kw in msg for kw in ["docstring", "api doc", "readme", "documentation"]):
        return "documentation-generation"
    if any(kw in msg for kw in ["refactor", "restructure", "clean up code"]):
        return "refactoring-safely"

    # --- Infrastructure ---
    if any(kw in msg for kw in ["migration", "schema change", "alter table"]):
        return "database-migrations"
    if any(kw in msg for kw in ["env var", "environment", ".env", "secrets"]):
        return "environment-configuration"
    if any(kw in msg for kw in ["monitor", "logging", "observab", "metric"]):
        return "monitoring-and-logging"
    if any(kw in msg for kw in ["ssl", "certificate", "tls", "https"]):
        return "ssl-certificate-management"

    # --- Workflow (specific patterns before broad) ---
    if any(kw in msg for kw in [
        "api design", "rest api", "endpoint design", "design the api",
        "design an api", "design our api",
    ]):
        return "api-design"
    if any(kw in msg for kw in ["scaffold", "boilerplate", "generate code"]):
        return "code-generation"
    if any(kw in msg for kw in ["microservice", "new service", "service scaffold"]):
        return "creating-microservices"
    if any(kw in msg for kw in ["onboard", "new project", "getting started"]):
        return "onboarding-new-project"
    if any(kw in msg for kw in ["pair program", "explain as you go", "teach me"]):
        return "pair-programming"

    # --- Original Skills (broadest matchers last) ---
    if any(kw in msg for kw in [
        "brainstorm", "what should", "how should", "approach",
    ]):
        return "brainstorming"
    if any(kw in msg for kw in [
        "debug", "bug", "broken", "not working", "crash",
    ]):
        return "systematic-debugging"
    if any(kw in msg for kw in ["plan", "break down", "steps to", "roadmap"]):
        return "writing-plans"
    if any(kw in msg for kw in [
        "code review", "review my", "check my code", "review this",
    ]):
        return "code-review"

    return None


def estimate_complexity(task: str) -> str:
    """
    Estimate task complexity to decide which model tier to use.

    Returns: "simple", "moderate", or "complex"
    """
    task_lower = task.lower()

    # Simple tasks - single file, basic operations
    simple_patterns = [
        "read ", "show ", "display ", "print ", "cat ",
        "find file", "list files", "what is", "where is",
        "add comment", "fix typo", "rename variable",
        "simple", "quick", "just ",
    ]
    if any(p in task_lower for p in simple_patterns):
        return "simple"

    # Complex tasks - multi-file, refactoring, features, full apps
    complex_patterns = [
        "refactor", "restructure", "redesign", "architect",
        "implement feature", "add feature", "create system",
        "multiple files", "across the codebase", "all files",
        "migrate", "upgrade", "overhaul",
        "website", "web app", "application",
        "full stack", "fullstack", "frontend and backend",
        "finish my ", "build my ", "complete the ",
    ]
    if any(p in task_lower for p in complex_patterns):
        return "complex"

    # Moderate - default for most tasks
    return "moderate"
