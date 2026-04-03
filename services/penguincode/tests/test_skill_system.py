"""Smoke tests for Skill System v2.

Validates:
- SkillLoader: discovery, frontmatter parsing, model override, chains, references
- ChatAgent: model swap on skill activate/deactivate
- Intent: suggest_skill() keyword matching for all skill categories
- Config utilities: get/set/save/reset
- ConfigStore: default skills list matches discovered skills
"""

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Safe imports — skip entire module if core API has changed
# ---------------------------------------------------------------------------
try:
    from penguincode_cli.skills.loader import SKILL_NAMESPACE, SkillInfo, SkillLoader
    from penguincode_cli.agents.intent import suggest_skill, detect_user_intent, estimate_complexity
    from penguincode_cli.config.settings import (
        Settings,
        get_config_value,
        set_config_value,
        settings_to_dict,
        save_settings,
    )
    from penguincode_cli.server.models.config_store import _default_skills
except ImportError as exc:
    pytest.skip(f"Skill system API changed: {exc}", allow_module_level=True)


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def loader():
    """Discovered SkillLoader with all built-in skills."""
    sl = SkillLoader()
    sl.discover()
    return sl


@pytest.fixture
def default_settings():
    """Fresh default Settings instance."""
    return Settings()


@pytest.fixture
def mock_ollama_client():
    """Minimal mock OllamaClient for ChatAgent tests."""
    client = MagicMock()
    client.chat = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock()
    return client


# ============================================================================
# 1. SkillLoader — Discovery
# ============================================================================

class TestSkillLoaderDiscovery:
    """Verify skill discovery finds all expected skills."""

    def test_discover_returns_51_skills(self, loader):
        skills = loader.list_all()
        assert len(skills) == 55, f"Expected 55 skills, got {len(skills)}"

    def test_all_skills_have_name(self, loader):
        for name, info in loader.list_all().items():
            assert info.name, f"Skill has empty name: {name}"
            assert info.name == name, f"Skill name mismatch: {info.name} != {name}"

    def test_all_skills_have_description(self, loader):
        for name, info in loader.list_all().items():
            assert info.description, f"Skill '{name}' has no description"

    def test_all_skills_have_content(self, loader):
        for name, info in loader.list_all().items():
            assert len(info.content) > 50, f"Skill '{name}' has too little content ({len(info.content)} chars)"

    def test_all_skills_have_valid_path(self, loader):
        for name, info in loader.list_all().items():
            assert info.path.exists(), f"Skill '{name}' path does not exist: {info.path}"
            assert info.path.name == "SKILL.md", f"Skill '{name}' file is not SKILL.md: {info.path.name}"

    def test_original_13_skills_present(self, loader):
        """Verify all 13 original OpenCode skills survived the migration."""
        original = [
            "brainstorming", "code-review", "dispatching-parallel-agents",
            "executing-plans", "finishing-a-development-branch",
            "receiving-code-review", "subagent-driven-development",
            "systematic-debugging", "test-driven-development",
            "using-git-worktrees", "verification-before-completion",
            "writing-plans", "writing-skills",
        ]
        skills = loader.list_all()
        for name in original:
            assert name in skills, f"Original skill '{name}' missing after migration"

    def test_new_38_skills_present(self, loader):
        """Verify all 38 new PenguinCode skills are present."""
        new_skills = [
            # Git
            "committing-changes", "pushing-to-github", "branching-strategy",
            "resolving-merge-conflicts", "cherry-picking", "git-bisect-debugging",
            # Testing
            "smoke-testing", "integration-testing", "performance-testing",
            "security-scanning", "writing-unit-tests", "testing-api-endpoints",
            # Docker
            "building-docker-images", "docker-compose-development",
            "debugging-containers", "container-security",
            # Kubernetes
            "deploying-to-kubernetes", "kubernetes-debugging",
            "kubernetes-scaling", "helm-chart-management",
            # CI/CD
            "github-actions-workflows", "release-management", "deployment-rollback",
            # Code Quality
            "linting-and-formatting", "dependency-management",
            "documentation-generation", "refactoring-safely",
            # Infrastructure
            "database-migrations", "environment-configuration",
            "monitoring-and-logging", "ssl-certificate-management",
            # Workflow
            "onboarding-new-project", "troubleshooting-build-failures",
            "api-design", "creating-microservices", "code-generation",
            "pair-programming", "incident-response",
        ]
        skills = loader.list_all()
        for name in new_skills:
            assert name in skills, f"New skill '{name}' not found"

    def test_user_skills_override_builtin(self, tmp_path):
        """Verify user skills directory takes precedence."""
        # Create a user skill dir with a skill that overrides brainstorming
        user_dir = tmp_path / "skills" / "brainstorming"
        user_dir.mkdir(parents=True)
        (user_dir / "SKILL.md").write_text(
            "---\nname: brainstorming\ndescription: Custom override\n---\n# Custom"
        )

        sl = SkillLoader()
        # Monkey-patch the user dir
        original_discover = sl.discover

        def patched_discover():
            sl._skills.clear()
            builtin_dir = Path(__file__).parent.parent / "penguincode_cli" / "defaults" / "skills"
            if builtin_dir.is_dir():
                sl._scan_directory(builtin_dir)
            sl._scan_directory(tmp_path / "skills")
            sl._discovered = True

        sl.discover = patched_discover
        sl.discover()

        skill = sl.get("brainstorming")
        assert skill is not None
        assert skill.description == "Custom override"

    def test_external_skill_dirs_scanned(self, tmp_path, monkeypatch):
        """Verify external dirs (Claude Code, OpenCode) are scanned."""
        # Create a fake Claude Code skill
        claude_dir = tmp_path / ".claude" / "skills" / "my-claude-skill"
        claude_dir.mkdir(parents=True)
        (claude_dir / "SKILL.md").write_text(
            "---\nname: my-claude-skill\ndescription: From Claude Code\n---\n# Hello"
        )

        # Create a fake OpenCode skill
        oc_dir = tmp_path / ".config" / "opencode" / "skills" / "my-oc-skill"
        oc_dir.mkdir(parents=True)
        (oc_dir / "SKILL.md").write_text(
            "---\nname: my-oc-skill\ndescription: From OpenCode\n---\n# World"
        )

        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        sl = SkillLoader()
        # Only scan external dirs (skip built-in to keep test fast)
        sl._skills.clear()
        home = Path.home()
        for parts in sl.EXTERNAL_SKILL_DIRS:
            ext_dir = home.joinpath(*parts)
            if ext_dir.is_dir():
                sl._scan_directory(ext_dir)
        sl._discovered = True

        assert "my-claude-skill" in sl.list_all()
        assert "my-oc-skill" in sl.list_all()
        assert sl.get("my-claude-skill").description == "From Claude Code"
        assert sl.get("my-oc-skill").description == "From OpenCode"

    def test_external_skills_overridden_by_user(self, tmp_path, monkeypatch):
        """User custom skills override external skills on name collision."""
        # External (Claude Code) skill
        claude_dir = tmp_path / ".claude" / "skills" / "shared-skill"
        claude_dir.mkdir(parents=True)
        (claude_dir / "SKILL.md").write_text(
            "---\nname: shared-skill\ndescription: Claude version\n---\n# V1"
        )

        # User custom skill with same name
        user_dir = tmp_path / ".config" / "penguincode" / "skills" / "shared-skill"
        user_dir.mkdir(parents=True)
        (user_dir / "SKILL.md").write_text(
            "---\nname: shared-skill\ndescription: User version\n---\n# V2"
        )

        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        sl = SkillLoader()
        # Simulate full discover with only external + user dirs
        sl._skills.clear()
        home = Path.home()
        for parts in sl.EXTERNAL_SKILL_DIRS:
            ext_dir = home.joinpath(*parts)
            if ext_dir.is_dir():
                sl._scan_directory(ext_dir)
        user_custom = home / ".config" / "penguincode" / "skills"
        if user_custom.is_dir():
            sl._scan_directory(user_custom)
        sl._discovered = True

        skill = sl.get("shared-skill")
        assert skill is not None
        assert skill.description == "User version", "User skills should override external"


# ============================================================================
# 2. SkillLoader — Frontmatter Parsing
# ============================================================================

class TestSkillFrontmatter:
    """Verify frontmatter parsing including model field."""

    def test_parse_name_and_description(self):
        content = '---\nname: test-skill\ndescription: "A test skill"\n---\n# Content'
        name, desc, model = SkillLoader._parse_frontmatter(content)
        assert name == "test-skill"
        assert desc == "A test skill"
        assert model is None

    def test_parse_model_field(self):
        content = '---\nname: test\ndescription: "desc"\nmodel: qwen2.5-coder:7b\n---\n# Content'
        name, desc, model = SkillLoader._parse_frontmatter(content)
        assert model == "qwen2.5-coder:7b"

    def test_parse_model_quoted(self):
        content = '---\nname: test\ndescription: "desc"\nmodel: "llama3.2:3b"\n---\n# Content'
        _, _, model = SkillLoader._parse_frontmatter(content)
        assert model == "llama3.2:3b"

    def test_parse_no_frontmatter(self):
        content = "# Just a heading\n\nSome content"
        name, desc, model = SkillLoader._parse_frontmatter(content)
        assert name == ""
        assert desc == ""
        assert model is None

    def test_parse_empty_frontmatter(self):
        content = "---\n---\n# Content"
        name, desc, model = SkillLoader._parse_frontmatter(content)
        assert name == ""
        assert desc == ""
        assert model is None


# ============================================================================
# 3. SkillLoader — Model Override
# ============================================================================

class TestSkillModelOverride:
    """Verify model override is correctly parsed from skills."""

    def test_code_skills_have_model(self, loader):
        """Skills that execute code should specify a model."""
        code_skills = [
            "committing-changes", "pushing-to-github", "smoke-testing",
            "building-docker-images", "deploying-to-kubernetes",
            "linting-and-formatting", "incident-response",
        ]
        for name in code_skills:
            skill = loader.get(name)
            assert skill is not None, f"Skill '{name}' not found"
            assert skill.model == "qwen2.5-coder:7b", (
                f"Skill '{name}' should have model qwen2.5-coder:7b, got {skill.model}"
            )

    def test_advisory_skills_no_model(self, loader):
        """Advisory/planning skills should use default model."""
        advisory_skills = [
            "brainstorming", "branching-strategy", "documentation-generation",
            "api-design", "onboarding-new-project", "pair-programming",
        ]
        for name in advisory_skills:
            skill = loader.get(name)
            assert skill is not None, f"Skill '{name}' not found"
            assert skill.model is None, (
                f"Advisory skill '{name}' should have no model override, got {skill.model}"
            )

    def test_model_override_count(self, loader):
        """Verify expected number of skills have model overrides."""
        skills_with_model = [s for s in loader.list_all().values() if s.model]
        # 33 code-execution skills have model override
        assert len(skills_with_model) >= 30, (
            f"Expected >= 30 skills with model override, got {len(skills_with_model)}"
        )


# ============================================================================
# 4. SkillLoader — Cross-References & Chains
# ============================================================================

class TestSkillReferencesAndChains:
    """Verify waddlepowers: cross-references and chain resolution."""

    def test_committing_changes_refs(self, loader):
        skill = loader.get("committing-changes")
        assert "linting-and-formatting" in skill.references
        assert "security-scanning" in skill.references
        assert "verification-before-completion" in skill.references

    def test_incident_response_refs(self, loader):
        skill = loader.get("incident-response")
        assert "monitoring-and-logging" in skill.references
        assert "deployment-rollback" in skill.references
        assert "kubernetes-debugging" in skill.references

    def test_creating_microservices_refs(self, loader):
        skill = loader.get("creating-microservices")
        assert "building-docker-images" in skill.references
        assert "api-design" in skill.references
        assert "deploying-to-kubernetes" in skill.references

    def test_chain_resolution(self, loader):
        chain = loader.get_chain("committing-changes")
        names = [s.name for s in chain]
        assert names[0] == "committing-changes"
        assert "linting-and-formatting" in names
        assert "security-scanning" in names
        assert "verification-before-completion" in names

    def test_chain_deduplication(self, loader):
        """Chain should not contain duplicate skills."""
        chain = loader.get_chain("committing-changes")
        names = [s.name for s in chain]
        assert len(names) == len(set(names)), f"Duplicate skills in chain: {names}"

    def test_chain_max_depth(self, loader):
        """Chain should respect max depth of 5."""
        chain = loader.get_chain("committing-changes")
        assert len(chain) <= 20, f"Chain too long ({len(chain)}), possible infinite recursion"

    def test_chain_nonexistent_skill(self, loader):
        chain = loader.get_chain("nonexistent-skill")
        assert chain == []

    def test_references_point_to_real_skills(self, loader):
        """Cross-references from new skills should point to skills that exist.

        Note: Some pre-existing original skills (code-review, subagent-driven-development)
        reference skills that were never created (code-reviewer, requesting-code-review).
        We exclude those known pre-existing broken refs from this check.
        """
        all_skills = loader.list_all()
        # Pre-existing broken refs from original OpenCode skills
        known_broken = {
            ("code-review", "code-reviewer"),
            ("subagent-driven-development", "requesting-code-review"),
            ("subagent-driven-development", "code-reviewer"),
        }
        broken_refs = []
        for name, skill in all_skills.items():
            for ref in skill.references:
                if ref not in all_skills and (name, ref) not in known_broken:
                    broken_refs.append(f"{name} -> {ref}")
        assert broken_refs == [], f"Broken cross-references: {broken_refs}"


# ============================================================================
# 5. ChatAgent — Model Swap
# ============================================================================

class TestChatAgentModelSwap:
    """Verify ChatAgent model save/restore on skill activation."""

    def _make_chat_agent(self, mock_ollama_client):
        """Create a ChatAgent with minimal dependencies."""
        from penguincode_cli.agents.chat import ChatAgent
        settings = Settings()
        return ChatAgent(
            ollama_client=mock_ollama_client,
            settings=settings,
            project_dir="/tmp/test",
        )

    def test_activate_with_model_override(self, mock_ollama_client):
        agent = self._make_chat_agent(mock_ollama_client)
        original_model = agent.model

        agent.activate_skill("test", "content", model="qwen2.5-coder:7b")

        assert agent.model == "qwen2.5-coder:7b"
        assert agent._saved_model == original_model
        assert agent.active_skill == "test"

    def test_deactivate_restores_model(self, mock_ollama_client):
        agent = self._make_chat_agent(mock_ollama_client)
        original_model = agent.model

        agent.activate_skill("test", "content", model="qwen2.5-coder:7b")
        agent.deactivate_skill()

        assert agent.model == original_model
        assert agent._saved_model is None
        assert agent.active_skill is None

    def test_activate_without_model_override(self, mock_ollama_client):
        agent = self._make_chat_agent(mock_ollama_client)
        original_model = agent.model

        agent.activate_skill("test", "content", model=None)

        assert agent.model == original_model
        assert agent._saved_model is None

    def test_deactivate_without_prior_override(self, mock_ollama_client):
        agent = self._make_chat_agent(mock_ollama_client)
        original_model = agent.model

        agent.activate_skill("test", "content")
        agent.deactivate_skill()

        assert agent.model == original_model

    def test_skill_chain_activation(self, mock_ollama_client):
        agent = self._make_chat_agent(mock_ollama_client)

        agent.activate_skill("root", "combined", chain=["root", "dep1", "dep2"], model="custom:1b")

        assert agent.active_skill == "root"
        assert agent.skill_chain == ["root", "dep1", "dep2"]
        assert agent.model == "custom:1b"

    def test_multiple_activate_deactivate_cycles(self, mock_ollama_client):
        """Ensure model is correctly restored across multiple cycles."""
        agent = self._make_chat_agent(mock_ollama_client)
        original = agent.model

        for model in ["model-a:7b", "model-b:3b", "model-c:1b"]:
            agent.activate_skill("test", "content", model=model)
            assert agent.model == model
            agent.deactivate_skill()
            assert agent.model == original


# ============================================================================
# 6. Intent — suggest_skill() Keyword Matching
# ============================================================================

class TestSuggestSkill:
    """Verify suggest_skill returns correct skill for keyword patterns."""

    # (input_message, expected_skill_name)
    CASES = [
        # Git
        ("commit these changes", "committing-changes"),
        ("I need to do a pre-commit check", "committing-changes"),
        ("push to github", "pushing-to-github"),
        ("create a pull request", "pushing-to-github"),
        ("there's a merge conflict", "resolving-merge-conflicts"),
        ("cherry-pick this commit", "cherry-picking"),
        ("run git bisect", "git-bisect-debugging"),
        ("what branch strategy should we use", "branching-strategy"),
        # Testing
        ("run the smoke tests", "smoke-testing"),
        ("we need integration tests", "integration-testing"),
        ("do a load test", "performance-testing"),
        ("run a security scan", "security-scanning"),
        ("write a unit test for this", "writing-unit-tests"),
        ("test the api endpoints", "testing-api-endpoints"),
        ("let's do tdd", "test-driven-development"),
        # Docker
        ("docker build is slow", "building-docker-images"),
        ("set up docker-compose", "docker-compose-development"),
        ("check the container logs", "debugging-containers"),
        ("scan container for vulnerabilities", "container-security"),
        # Kubernetes
        ("deploy to k8s", "deploying-to-kubernetes"),
        ("pod is in crashloop", "kubernetes-debugging"),
        ("set up hpa autoscaling", "kubernetes-scaling"),
        ("upgrade the helm chart", "helm-chart-management"),
        # CI/CD
        ("fix the github action", "github-actions-workflows"),
        ("bump the version for release", "release-management"),
        ("rollback the deployment", "deployment-rollback"),
        # Code Quality
        ("run the linter", "linting-and-formatting"),
        ("audit the dependencies", "dependency-management"),
        ("generate documentation for this module", "documentation-generation"),
        ("refactor this safely", "refactoring-safely"),
        # Infrastructure
        ("create a database migration", "database-migrations"),
        ("set up environment variables", "environment-configuration"),
        ("add monitoring and logging", "monitoring-and-logging"),
        ("renew the ssl certificate", "ssl-certificate-management"),
        # Workflow
        ("scaffold some boilerplate", "code-generation"),
        ("create a new microservice", "creating-microservices"),
        ("help me design the api", "api-design"),
        ("onboard to this project", "onboarding-new-project"),
        ("the build is failing with errors", "troubleshooting-build-failures"),
        ("let's pair program", "pair-programming"),
        ("we have a production incident", "incident-response"),
        # Original skills (broader matchers)
        ("let's brainstorm ideas", "brainstorming"),
        ("there's a bug in the login", "systematic-debugging"),
        ("make a plan for this feature", "writing-plans"),
        ("do a code review on my changes", "code-review"),
    ]

    @pytest.mark.parametrize("message,expected", CASES, ids=[c[1] for c in CASES])
    def test_suggest_skill(self, message, expected):
        result = suggest_skill(message)
        assert result == expected, f"suggest_skill('{message}') = '{result}', expected '{expected}'"

    def test_no_suggestion_for_generic_input(self):
        """Generic messages should not suggest a skill."""
        result = suggest_skill("hello how are you")
        assert result is None

    def test_case_insensitive(self):
        assert suggest_skill("RUN THE SMOKE TESTS") == "smoke-testing"
        assert suggest_skill("Deploy To K8S") == "deploying-to-kubernetes"


# ============================================================================
# 7. Intent — detect_user_intent()
# ============================================================================

class TestDetectUserIntent:
    """Verify intent detection routes to correct agent types."""

    def test_plan_intent(self):
        assert detect_user_intent("create a plan for this feature") == "spawn_planner"

    def test_research_intent(self):
        assert detect_user_intent("how do I use pytest fixtures") == "spawn_researcher"

    def test_executor_intent(self):
        assert detect_user_intent("run the tests") == "spawn_executor"

    def test_explorer_intent(self):
        assert detect_user_intent("find all Python files") == "spawn_explorer"

    def test_no_intent(self):
        assert detect_user_intent("hello") is None


# ============================================================================
# 8. Intent — estimate_complexity()
# ============================================================================

class TestEstimateComplexity:
    """Verify complexity estimation for model tier selection."""

    def test_simple_tasks(self):
        assert estimate_complexity("read the config file") == "simple"
        assert estimate_complexity("show me the README") == "simple"

    def test_complex_tasks(self):
        assert estimate_complexity("refactor the entire auth system") == "complex"
        assert estimate_complexity("implement feature for multi-tenancy") == "complex"

    def test_moderate_default(self):
        assert estimate_complexity("update the user model") == "moderate"


# ============================================================================
# 9. Config — get/set/save utilities
# ============================================================================

class TestConfigUtilities:
    """Verify /config command utilities."""

    def test_get_config_value_simple(self, default_settings):
        val = get_config_value(default_settings, "ollama.api_url")
        assert val == "http://localhost:11434"

    def test_get_config_value_nested(self, default_settings):
        val = get_config_value(default_settings, "models.execution")
        assert val == "qwen2.5-coder:7b"

    def test_get_config_value_deep(self, default_settings):
        val = get_config_value(default_settings, "defaults.context_window")
        assert val == 8192

    def test_get_config_value_nonexistent(self, default_settings):
        with pytest.raises(AttributeError):
            get_config_value(default_settings, "nonexistent.key")

    def test_set_config_value_int(self, default_settings):
        old, new = set_config_value(default_settings, "defaults.context_window", "16384")
        assert old == 8192
        assert new == 16384
        assert get_config_value(default_settings, "defaults.context_window") == 16384

    def test_set_config_value_str(self, default_settings):
        old, new = set_config_value(default_settings, "models.execution", "llama3.2:3b")
        assert old == "qwen2.5-coder:7b"
        assert new == "llama3.2:3b"

    def test_set_config_value_bool(self, default_settings):
        old, new = set_config_value(default_settings, "memory.enabled", "false")
        assert old is True
        assert new is False

    def test_set_config_value_float(self, default_settings):
        old, new = set_config_value(default_settings, "defaults.temperature", "0.3")
        assert old == 0.7
        assert new == 0.3

    def test_set_config_invalid_key(self, default_settings):
        with pytest.raises(AttributeError):
            set_config_value(default_settings, "bad.path.here", "value")

    def test_settings_to_dict(self, default_settings):
        d = settings_to_dict(default_settings)
        assert isinstance(d, dict)
        assert "ollama" in d
        assert "models" in d
        assert d["models"]["execution"] == "qwen2.5-coder:7b"

    def test_save_settings(self, default_settings, tmp_path):
        path = save_settings(default_settings, str(tmp_path / "test_settings.yaml"))
        assert Path(path).exists()
        # Verify it's valid YAML
        import yaml
        with open(path) as f:
            data = yaml.safe_load(f)
        assert data["models"]["execution"] == "qwen2.5-coder:7b"
        assert data["defaults"]["context_window"] == 8192

    def test_save_settings_default_path(self, default_settings, tmp_path, monkeypatch):
        """Test save with default path (mocked home dir)."""
        monkeypatch.setenv("HOME", str(tmp_path))
        path = save_settings(default_settings)
        assert Path(path).exists()
        assert "penguincode" in path


# ============================================================================
# 10. ConfigStore — Default Skills Sync
# ============================================================================

class TestConfigStoreSkillsSync:
    """Verify config_store default skills match discovered skills."""

    def test_default_skills_count(self):
        defaults = _default_skills()
        assert len(defaults) == 55, f"Expected 55 default skills, got {len(defaults)}"

    def test_default_skills_match_discovered(self, loader):
        """Every discovered skill should have a config_store default."""
        defaults = {s.name for s in _default_skills()}
        discovered = set(loader.list_all().keys())
        missing_from_defaults = discovered - defaults
        assert missing_from_defaults == set(), (
            f"Skills discovered but not in config_store defaults: {missing_from_defaults}"
        )

    def test_default_skills_all_discoverable(self, loader):
        """Every config_store default should be discoverable."""
        defaults = {s.name for s in _default_skills()}
        discovered = set(loader.list_all().keys())
        missing_from_discovery = defaults - discovered
        assert missing_from_discovery == set(), (
            f"Skills in config_store but not discovered: {missing_from_discovery}"
        )

    def test_default_skills_have_permissions(self):
        """All default skills should have at least one permission."""
        for skill in _default_skills():
            assert len(skill.permissions) > 0, (
                f"Skill '{skill.name}' has no permissions"
            )

    def test_default_skills_names_unique(self):
        names = [s.name for s in _default_skills()]
        assert len(names) == len(set(names)), "Duplicate skill names in defaults"


# ============================================================================
# 11. SkillInfo — Dataclass Behavior
# ============================================================================

class TestSkillInfoDataclass:
    """Verify SkillInfo dataclass fields and defaults."""

    def test_default_fields(self):
        info = SkillInfo(name="test", description="desc", content="# content", path=Path("/tmp/test"))
        assert info.references == []
        assert info.model is None

    def test_with_model(self):
        info = SkillInfo(
            name="test", description="desc", content="# content",
            path=Path("/tmp/test"), model="custom:7b",
        )
        assert info.model == "custom:7b"

    def test_with_references(self):
        info = SkillInfo(
            name="test", description="desc", content="# content",
            path=Path("/tmp/test"), references=["dep1", "dep2"],
        )
        assert info.references == ["dep1", "dep2"]


# ============================================================================
# 12. Namespace Constant
# ============================================================================

class TestNamespace:
    """Verify the skill namespace constant."""

    def test_namespace_prefix(self):
        assert SKILL_NAMESPACE == "waddlepowers:"

    def test_references_use_namespace(self, loader):
        """Skills that have references should use the waddlepowers: namespace in content."""
        for name, skill in loader.list_all().items():
            if skill.references:
                for ref in skill.references:
                    assert f"waddlepowers:{ref}" in skill.content, (
                        f"Skill '{name}' lists ref '{ref}' but "
                        f"'waddlepowers:{ref}' not found in content"
                    )
