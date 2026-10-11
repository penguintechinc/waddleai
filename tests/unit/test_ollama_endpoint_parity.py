"""Guards `shared/utils/ollama_endpoint.py` and PenguinCode's twin against drift.

The twin lives at
`services/penguincode/penguincode_cli/config/ollama_endpoint.py`.
PenguinCode ships its own `shared/py_libs` dependency tree and a separate
venv, so this repo's root test suite cannot import `penguincode_cli` to
compare behavior directly. Instead, this parses both files' source with
`ast` and asserts the canonical/legacy env-var constant tuples -- the actual
precedence contract -- stay byte-identical. See both modules' docstrings.
"""

from __future__ import annotations

import ast
import pathlib

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_ROOT_MODULE = _REPO_ROOT / "shared" / "utils" / "ollama_endpoint.py"
_PENGUINCODE_MODULE = (
    _REPO_ROOT / "services" / "penguincode" / "penguincode_cli" / "config" / "ollama_endpoint.py"
)

_CONSTANT_NAMES = (
    "CANONICAL_CHAT_URL_ENV",
    "CANONICAL_EMBEDDING_URL_ENV",
    "LEGACY_CHAT_URL_ENVS",
    "LEGACY_EMBEDDING_URL_ENVS",
    "DEFAULT_OLLAMA_URL",
)


def _extract_module_level_constants(path: pathlib.Path) -> dict[str, object]:
    """Return `{name: literal_value}` for module-level assignments in *path*.

    Only names listed in `_CONSTANT_NAMES` are extracted.
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    found: dict[str, object] = {}
    for node in tree.body:
        if not isinstance(node, ast.AnnAssign | ast.Assign) or node.value is None:
            continue
        targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
        for target in targets:
            if isinstance(target, ast.Name) and target.id in _CONSTANT_NAMES:
                found[target.id] = ast.literal_eval(node.value)
    return found


def test_both_modules_exist() -> None:
    """Both the root resolver and its PenguinCode twin are present on disk."""
    assert _ROOT_MODULE.is_file(), f"missing: {_ROOT_MODULE}"
    assert _PENGUINCODE_MODULE.is_file(), f"missing: {_PENGUINCODE_MODULE}"


def test_constant_names_fully_extracted_from_both_modules() -> None:
    """Catches the extractor silently finding nothing.

    A moved/renamed constant would otherwise pass on an empty, vacuous
    comparison in the test below.
    """
    root_constants = _extract_module_level_constants(_ROOT_MODULE)
    penguincode_constants = _extract_module_level_constants(_PENGUINCODE_MODULE)
    assert set(root_constants) == set(_CONSTANT_NAMES), root_constants
    assert set(penguincode_constants) == set(_CONSTANT_NAMES), penguincode_constants


def test_resolution_order_constants_are_identical() -> None:
    """The actual lockstep guarantee.

    Every precedence-defining constant must be byte-identical between the
    two modules.
    """
    root_constants = _extract_module_level_constants(_ROOT_MODULE)
    penguincode_constants = _extract_module_level_constants(_PENGUINCODE_MODULE)
    for name in _CONSTANT_NAMES:
        assert root_constants[name] == penguincode_constants[name], (
            f"{name} has drifted between shared/utils/ollama_endpoint.py "
            f"({root_constants[name]!r}) and penguincode_cli/config/ollama_endpoint.py "
            f"({penguincode_constants[name]!r}) -- keep the two resolvers in lockstep."
        )
