"""CORS allowlist resolution for the management service config classes.

regression: audit-2026-09-14 — CORS_ORIGINS defaulted to ``"*"``, so a
deployment that never set the env var served an origin-wildcard API.

The module is loaded straight from its file rather than imported as
``services.management.app.config``: the package ``__init__`` pulls in Quart and
quart_cors, which are not needed to exercise pure config resolution and would
make this regression test depend on the whole service's dependency tree.
"""

import importlib.util
import pathlib
import sys
import types

import pytest

CONFIG_PATH = (
    pathlib.Path(__file__).resolve().parents[2] / "services" / "management" / "app" / "config.py"
)


def _load_config_module(monkeypatch, env: dict[str, str | None]) -> types.ModuleType:
    """Import services/management/app/config.py fresh under a given environment.

    Config values are evaluated in the class bodies at import time, so every
    case needs its own module instance rather than a re-read attribute.
    """
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)

    spec = importlib.util.spec_from_file_location("_waddleai_mgmt_config_under_test", CONFIG_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


class TestCorsDefaultDeny:
    """regression: audit-2026-09-14 — unset CORS_ORIGINS must deny, not wildcard."""

    def test_unset_defaults_to_empty_allowlist(self, monkeypatch):
        """With CORS_ORIGINS unset, every config class resolves to an empty allowlist."""
        module = _load_config_module(monkeypatch, {"CORS_ORIGINS": None})
        for cls_name in ("Config", "ProductionConfig", "DevelopmentConfig", "TestingConfig"):
            origins = getattr(module, cls_name).CORS_ORIGINS
            assert origins == [], f"{cls_name}.CORS_ORIGINS defaulted to {origins!r}, expected []"

    def test_unset_never_yields_a_wildcard(self, monkeypatch):
        """The wildcard must not appear anywhere in the default resolution."""
        module = _load_config_module(monkeypatch, {"CORS_ORIGINS": None})
        for cls_name in ("Config", "ProductionConfig", "DevelopmentConfig", "TestingConfig"):
            assert "*" not in getattr(module, cls_name).CORS_ORIGINS

    def test_empty_string_is_deny_not_a_blank_origin(self, monkeypatch):
        """CORS_ORIGINS="" yields [] rather than [""] — "".split(",") returns [""]."""
        module = _load_config_module(monkeypatch, {"CORS_ORIGINS": ""})
        assert module.Config.CORS_ORIGINS == []

    def test_explicit_origins_are_honored_and_trimmed(self, monkeypatch):
        """Explicitly configured origins survive, whitespace and blanks are dropped."""
        module = _load_config_module(
            monkeypatch,
            {"CORS_ORIGINS": " https://a.example , ,https://b.example "},
        )
        assert module.Config.CORS_ORIGINS == ["https://a.example", "https://b.example"]


class TestProductionNeverWildcards:
    """regression: audit-2026-09-14 — ProductionConfig must never resolve to '*'."""

    def test_production_strips_a_configured_wildcard(self, monkeypatch):
        """A '*' configured in the environment is dropped in production."""
        module = _load_config_module(monkeypatch, {"CORS_ORIGINS": "*"})
        assert module.ProductionConfig.CORS_ORIGINS == []

    def test_production_keeps_real_origins_beside_a_wildcard(self, monkeypatch):
        """Stripping '*' leaves the explicitly named origins intact."""
        module = _load_config_module(monkeypatch, {"CORS_ORIGINS": "*,https://app.example"})
        assert module.ProductionConfig.CORS_ORIGINS == ["https://app.example"]

    @pytest.mark.parametrize(
        "raw",
        ["*", "*,*", " * ", "https://a.example,*", "*,https://a.example,*"],
    )
    def test_production_wildcard_is_unreachable(self, monkeypatch, raw):
        """No CORS_ORIGINS spelling gets a wildcard past the production config."""
        module = _load_config_module(monkeypatch, {"CORS_ORIGINS": raw})
        assert "*" not in module.ProductionConfig.CORS_ORIGINS

    def test_parse_helper_logs_when_dropping_a_wildcard(self, monkeypatch, caplog):
        """Dropping a configured '*' is logged, not silent."""
        import logging

        module = _load_config_module(monkeypatch, {"CORS_ORIGINS": None})
        with caplog.at_level(logging.WARNING):
            result = module._parse_cors_origins("*", allow_wildcard=False)
        assert result == []
        assert any("CORS_ORIGINS" in r.getMessage() for r in caplog.records)
