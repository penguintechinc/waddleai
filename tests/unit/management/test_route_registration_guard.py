"""Data-driven guards against route-registration drift in the management API.

During a 16-branch merge the same class of bug landed four times, each only
detectable at runtime:

  1. A module was listed in ``ROUTE_MODULES`` (tests/unit/management/conftest.py)
     and imported in ``api/v1/__init__.py`` *after the module had been deleted*
     -> ``ModuleNotFoundError`` at app construction.
  2. A gutted module (no ``db`` attribute left) was re-added to
     ``ROUTE_MODULES`` -> hundreds of test errors, because the fixture patches
     ``<module>.db`` and the attribute no longer exists.
  3. ``api_v1_bp`` was registered twice -> Quart raises
     ``ValueError("The name 'api_v1' is already registered ...")``.
  4. Two modules both claimed ``GET/POST /memory-config`` -> *silent*
     collision; whichever module was imported first won, the other endpoint
     quietly never existed.

Root cause: three hand-maintained lists have to agree --

  * the ``from . import (...)`` block in ``services/management/app/api/v1/__init__.py``
  * the ``register_blueprint`` calls in ``services/management/app/__init__.py``
  * ``ROUTE_MODULES`` in ``tests/unit/management/conftest.py``

-- and nothing asserted that they did. Every check below derives its expected
set from the filesystem or the live ``app`` object (never a second
hand-maintained list), so a module added next month is covered automatically.

Deliberate exceptions must be declared in the ``*_ALLOWLIST`` dict for the
guard they're exempting, keyed by module name (or ``(rule, method)`` for the
route-collision guard) with a non-empty reason string -- an exception is then
a reviewable diff, not a silent pass.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
API_V1_DIR = REPO_ROOT / "services" / "management" / "app" / "api" / "v1"
API_V1_INIT = API_V1_DIR / "__init__.py"
ROUTE_MODULES_PREFIX = "services.management.app.api.v1."

# ---------------------------------------------------------------------------
# Allowlists -- deliberate, reviewable exceptions only. Every entry needs a
# reason string; empty is the healthy state.
# ---------------------------------------------------------------------------

#: Modules that define routes on the shared `api_v1_bp` but are intentionally
#: not imported in api/v1/__init__.py's import block.
GUARD1_SHARED_BP_NOT_REGISTERED_ALLOWLIST: dict[str, str] = {}

#: Modules that appear in conftest.ROUTE_MODULES (or should) as an exception
#: to the "has routes and imports db" rule derived below.
GUARD3_ROUTE_MODULES_ALLOWLIST: dict[str, str] = {}

#: (rule, http_method) pairs where two blueprints are deliberately allowed to
#: both claim the same path+method (e.g. served under different prefixes).
GUARD5_ROUTE_COLLISION_ALLOWLIST: dict[tuple[str, str], str] = {}


def _assert_reasons(allowlist: dict) -> None:
    for key, reason in allowlist.items():
        assert isinstance(reason, str) and reason.strip(), (
            f"allowlist entry {key!r} must carry a non-empty reason string"
        )


# ---------------------------------------------------------------------------
# Filesystem / AST derivation helpers -- no hand-maintained lists in here.
# ---------------------------------------------------------------------------


def _route_module_files() -> list[Path]:
    """Every module under api/v1/ except the package's own __init__.py."""
    return sorted(p for p in API_V1_DIR.glob("*.py") if p.stem != "__init__")


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


def _has_route_decorator(tree: ast.Module) -> bool:
    """True if some function is decorated with `<anything>.route(...)`.

    Deliberately blueprint-name-agnostic (matches both the shared `api_v1_bp`
    and any module's own `xxx_bp`) so it doesn't need to know which pattern a
    given module uses.
    """
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                if (
                    isinstance(dec, ast.Call)
                    and isinstance(dec.func, ast.Attribute)
                    and dec.func.attr == "route"
                ):
                    return True
    return False


def _imports_shared_api_v1_bp(tree: ast.Module) -> bool:
    """True for `from . import api_v1_bp` (relative, level 1, package import)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module is None:
            if any(alias.name == "api_v1_bp" for alias in node.names):
                return True
    return False


def _imports_db_from_extensions(tree: ast.Module) -> bool:
    """True for `from ...extensions import db` (any relative depth)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "extensions":
            if any(alias.name == "db" for alias in node.names):
                return True
    return False


def _init_import_block_names() -> set[str]:
    """Names pulled in by the `from . import (...)` block in api/v1/__init__.py."""
    tree = _parse(API_V1_INIT)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module is None:
            names.update(alias.name for alias in node.names)
    return names


def _modules_registering_on_shared_blueprint() -> set[str]:
    """Modules that add routes to the shared `api_v1_bp`.

    Must be imported in api/v1/__init__.py or their endpoints silently never exist.
    """
    result = set()
    for path in _route_module_files():
        tree = _parse(path)
        if _has_route_decorator(tree) and _imports_shared_api_v1_bp(tree):
            result.add(path.stem)
    return result


def _modules_needing_db_patch() -> set[str]:
    """Modules that define routes AND import `db`.

    Exactly the set that needs `<module>.db` patched to a mock for route-level tests.
    """
    result = set()
    for path in _route_module_files():
        tree = _parse(path)
        if _has_route_decorator(tree) and _imports_db_from_extensions(tree):
            result.add(path.stem)
    return result


# ---------------------------------------------------------------------------
# Guard 1: every module registering routes on the shared api_v1_bp is
# imported in api/v1/__init__.py.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module_name", sorted(_modules_registering_on_shared_blueprint()))
def test_shared_blueprint_route_module_is_imported(module_name: str) -> None:
    """A module adding `@api_v1_bp.route(...)` handlers must be imported.

    Otherwise the decorator never runs and its routes 404 in api/v1/__init__.py.
    """
    _assert_reasons(GUARD1_SHARED_BP_NOT_REGISTERED_ALLOWLIST)
    if module_name in GUARD1_SHARED_BP_NOT_REGISTERED_ALLOWLIST:
        pytest.skip(GUARD1_SHARED_BP_NOT_REGISTERED_ALLOWLIST[module_name])

    imported = _init_import_block_names()
    assert module_name in imported, (
        f"{module_name}.py registers routes on api_v1_bp but is not imported "
        f"in api/v1/__init__.py's `from . import (...)` block -- its endpoints "
        f"will silently 404. Append it to that block (do not reorder it -- see "
        f"the block's own comment), or add a reason to "
        f"GUARD1_SHARED_BP_NOT_REGISTERED_ALLOWLIST if this is deliberate."
    )


# ---------------------------------------------------------------------------
# Guard 2: every name imported in api/v1/__init__.py exists on disk.
# ---------------------------------------------------------------------------


def test_every_init_imported_name_exists_on_disk() -> None:
    """Catches the deleted-module case.

    A name left in the import block after its file was removed raises
    ModuleNotFoundError at app construction.
    """
    imported = _init_import_block_names()
    missing = sorted(name for name in imported if not (API_V1_DIR / f"{name}.py").exists())
    assert not missing, (
        f"api/v1/__init__.py imports names with no corresponding file in "
        f"{API_V1_DIR}: {missing}. Remove the import (module was deleted) or "
        f"restore the file."
    )


# ---------------------------------------------------------------------------
# Guard 3: conftest.ROUTE_MODULES agrees, both directions, with the set of
# modules that actually define routes and import `db`.
# ---------------------------------------------------------------------------


def test_route_modules_list_matches_modules_needing_db_patch() -> None:
    """conftest.ROUTE_MODULES must list exactly the modules needing a db patch.

    It exists purely to patch `<module>.db` to a mock for route tests, so it must
    list exactly the modules that (a) define routes and (b) import `db` -- no
    more (a stale/reintroduced entry silently patches nothing, or patches a
    module with no `db` attribute and errors every test using the fixture), no
    less (a module needing the patch but missing from the list hits a real,
    unmocked db in tests).
    """
    _assert_reasons(GUARD3_ROUTE_MODULES_ALLOWLIST)

    from tests.unit.management.conftest import ROUTE_MODULES

    malformed = [n for n in ROUTE_MODULES if not n.startswith(ROUTE_MODULES_PREFIX)]
    assert not malformed, f"ROUTE_MODULES entries with unexpected prefix: {malformed}"

    exempt = set(GUARD3_ROUTE_MODULES_ALLOWLIST)
    declared = {n[len(ROUTE_MODULES_PREFIX) :] for n in ROUTE_MODULES} - exempt
    required = _modules_needing_db_patch() - exempt

    missing = sorted(required - declared)
    orphaned = sorted(declared - required)

    assert not missing, (
        f"conftest.ROUTE_MODULES is missing modules that define routes and "
        f"import `db`: {missing}. Their route tests are hitting a real, "
        f"unmocked db instance."
    )
    assert not orphaned, (
        f"conftest.ROUTE_MODULES lists modules that don't define routes or "
        f"don't import `db`: {orphaned}. This is exactly the 'gutted module "
        f"re-added to ROUTE_MODULES' bug -- patch(f'{{m}}.db', ...) will raise "
        f"AttributeError for every test using the flask_app fixture. Remove "
        f"the entry, or add it to GUARD3_ROUTE_MODULES_ALLOWLIST with a reason."
    )


# ---------------------------------------------------------------------------
# Guard 4: no blueprint is registered twice.
# ---------------------------------------------------------------------------


def test_register_blueprints_registers_each_blueprint_once() -> None:
    """Exercises the real `register_blueprints()` against a throwaway Quart app.

    Quart's own Blueprint.register() raises ValueError the moment a blueprint
    name is registered a second time -- this just proves the production wiring
    never does that, without needing DB/Redis mocking (route modules only bind
    `db = None` at import time; no connection is attempted until
    `init_extensions()`, which this test never calls).
    """
    from quart import Quart

    from services.management.app import register_blueprints

    app = Quart("route_registration_guard_probe")
    register_blueprints(app)  # raises ValueError on any duplicate registration

    # Belt-and-suspenders: app.blueprints is a dict so duplicate names can't
    # literally coexist, but assert distinct blueprint identities per name in
    # case a future refactor accepts `name=` overrides that could alias two
    # different blueprint objects onto the same registered name.
    seen: dict[str, object] = {}
    for name, bp in app.blueprints.items():
        assert name not in seen or seen[name] is bp, (
            f"blueprint name {name!r} is registered to two different Blueprint objects"
        )
        seen[name] = bp


# ---------------------------------------------------------------------------
# Guard 5: no two handlers claim the same (rule, method) -- enumerated from
# the live app.url_map, not parsed from source, so it reflects what Quart
# actually has (Werkzeug does not raise on this; the second registration is
# just silently unreachable).
# ---------------------------------------------------------------------------


def test_no_two_handlers_claim_the_same_rule_and_method(flask_app) -> None:
    """A rule+method claimed by two different endpoints is a silent collision.

    One of them becomes permanently unreachable -- whichever module happened
    to import first wins, with no error and no log line.
    """
    _assert_reasons(GUARD5_ROUTE_COLLISION_ALLOWLIST)

    seen: dict[tuple[str, str], str] = {}
    collisions: list[str] = []

    for rule in flask_app.url_map.iter_rules():
        methods = (rule.methods or set()) - {"HEAD", "OPTIONS"}
        for method in methods:
            key = (rule.rule, method)
            if key in GUARD5_ROUTE_COLLISION_ALLOWLIST:
                continue
            if key in seen and seen[key] != rule.endpoint:
                collisions.append(
                    f"{method} {rule.rule} claimed by both {seen[key]!r} and {rule.endpoint!r}"
                )
            else:
                seen[key] = rule.endpoint

    summary = "\n".join(collisions)
    assert not collisions, (
        f"Silent route collisions (only one handler is ever reachable):\n{summary}"
    )
