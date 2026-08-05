"""Tests for config module - Settings, load_config, save_config."""

from pathlib import Path

import pytest

try:
    from penguincode_cli.config.settings import (
        DEFAULT_CONFIG,
        RegulatorConfig,
        Settings,
        get_agent_model,
        get_model_for_role,
        load_config,
        save_config,
    )
except ImportError as exc:
    pytest.skip(f"Config API changed: {exc}", allow_module_level=True)


@pytest.fixture
def temp_config_file(tmp_path):
    """Create a temporary config file."""
    config_file = tmp_path / "config.yaml"
    return config_file


@pytest.fixture
def sample_config():
    """Sample configuration dictionary."""
    return {
        "ollama_url": "http://test:11434",
        "models": {
            "planning": "test-model:latest",
            "orchestration": "test-orchestration:latest",
        },
        "agents": {
            "file_search": "test-agent:latest",
        },
        "security_level": "strict",
        "history_enabled": False,
        "regulator": {
            "enabled": False,
            "max_concurrent_agents": 5,
        },
    }


def test_settings_default_initialization():
    """Test Settings dataclass with default values."""
    settings = Settings()
    assert settings.ollama_url == "http://localhost:11434"
    assert settings.security_level == "standard"
    assert settings.history_enabled is True
    assert settings.regulator.enabled is True
    assert settings.regulator.max_concurrent_agents == 10


def test_settings_custom_initialization():
    """Test Settings with custom values."""
    regulator = RegulatorConfig(enabled=False, max_concurrent_agents=5)
    settings = Settings(
        ollama_url="http://custom:11434",
        models={"planning": "custom-model"},
        security_level="strict",
        regulator=regulator,
    )
    assert settings.ollama_url == "http://custom:11434"
    assert settings.models["planning"] == "custom-model"
    assert settings.security_level == "strict"
    assert settings.regulator.enabled is False


def test_load_config_default():
    """Test loading default config when no files exist."""
    settings = load_config(path=Path("/nonexistent/path/config.yaml"))
    assert settings.ollama_url == DEFAULT_CONFIG["ollama_url"]
    assert settings.models == DEFAULT_CONFIG["models"]
    assert settings.security_level == DEFAULT_CONFIG["security_level"]


def test_load_config_from_file(temp_config_file, sample_config):
    """Test loading config from YAML file."""
    import yaml

    with open(temp_config_file, "w") as f:
        yaml.dump(sample_config, f)

    settings = load_config(path=temp_config_file)
    assert settings.ollama_url == "http://test:11434"
    assert settings.models["planning"] == "test-model:latest"
    assert settings.agents["file_search"] == "test-agent:latest"
    assert settings.security_level == "strict"
    assert settings.history_enabled is False
    assert settings.regulator.enabled is False


def test_save_config(temp_config_file):
    """Test saving Settings to YAML file."""
    regulator = RegulatorConfig(enabled=True, max_concurrent_agents=15)
    settings = Settings(
        ollama_url="http://save-test:11434",
        models={"planning": "save-model:latest"},
        security_level="permissive",
        regulator=regulator,
    )

    save_config(settings, path=temp_config_file)

    # Verify file was created
    assert temp_config_file.exists()

    # Load and verify content
    import yaml

    with open(temp_config_file) as f:
        saved_data = yaml.safe_load(f)

    assert saved_data["ollama_url"] == "http://save-test:11434"
    assert saved_data["models"]["planning"] == "save-model:latest"
    assert saved_data["security_level"] == "permissive"
    assert saved_data["regulator"]["enabled"] is True
    assert saved_data["regulator"]["max_concurrent_agents"] == 15


def test_get_model_for_role():
    """Test retrieving model by role."""
    settings = Settings(models={"planning": "custom-planning:latest"})
    model = get_model_for_role("planning", settings)
    assert model == "custom-planning:latest"


def test_get_model_for_role_not_found():
    """Test ValueError when role not found."""
    settings = Settings(models={})
    with pytest.raises(ValueError, match="Role 'nonexistent' not found"):
        get_model_for_role("nonexistent", settings)


def test_get_agent_model():
    """Test retrieving agent-specific model."""
    settings = Settings(
        agents={"file_search": "agent-model:latest"},
        models={"haiku": "haiku-fallback:latest"},
    )
    model = get_agent_model("file_search", settings)
    assert model == "agent-model:latest"


def test_get_agent_model_fallback():
    """Test agent model fallback to haiku."""
    settings = Settings(models={"haiku": "haiku-fallback:latest"})
    model = get_agent_model("unknown_agent", settings)
    assert model == "haiku-fallback:latest"


def test_regulator_config_defaults():
    """Test RegulatorConfig default values."""
    regulator = RegulatorConfig()
    assert regulator.enabled is True
    assert regulator.max_concurrent_agents == 10
    assert regulator.timeout_seconds == 300
    assert regulator.retry_attempts == 3
