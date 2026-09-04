"""Regression test for S3: tenant-owned credentials must never enter the platform pool.

Verifies ``LLMConnectionManager._select_credential`` filters the
``provider_credentials`` pool query on ``owner_org_id IS NULL`` so a
tenant's BYOK key can never serve platform or other-org traffic.
"""

from __future__ import annotations

from shared.utils.llm_connectors import LLMConnectionManager


class _Expr:
    """Composable query-expression node, mirroring penguin-dal's ``&``-composable Expression type.

    Wraps either a leaf ``("eq", field_name, value)`` tuple or a list of child
    ``_Expr`` nodes produced by AND-composition, and supports further chaining
    via ``&`` so a three-term predicate (``A & B & C``) flattens correctly.
    """

    def __init__(self, node):
        """Store the leaf tuple or list of child expressions this node wraps."""
        self.node = node

    def __and__(self, other):
        """Combine this expression with another into an AND node."""
        return _Expr([self, other])


class _Field:
    """Minimal stand-in for a penguin-dal Field that supports ``==``."""

    def __init__(self, name):
        """Store the field name used to build comparison predicates."""
        self.name = name

    def __eq__(self, other):
        """Build a leaf predicate expression, mirroring penguin-dal's query expressions."""
        return _Expr(("eq", self.name, other))


class _CredTable:
    """Stand-in for the reflected ``provider_credentials`` table."""

    provider_id = _Field("provider_id")
    enabled = _Field("enabled")
    owner_org_id = _Field("owner_org_id")


class _ProvTable:
    """Stand-in for the reflected ``ai_providers`` table."""

    name = _Field("name")
    id = _Field("id")


class _Row:
    """Minimal attribute-bag row, mirroring a penguin-dal Row."""

    def __init__(self, **kw):
        """Populate row attributes from keyword arguments."""
        self.__dict__.update(kw)


class _Rows(list):
    """List of matched rows exposing ``.first()``, mirroring penguin-dal's Rows return type."""

    def first(self):
        """Return the first matched row, or None if no rows matched."""
        return self[0] if self else None


class _Query:
    """Stand-in for the object returned by ``db(expr)``."""

    def __init__(self, db, expr):
        """Bind the owning fake DB and the captured predicate expression."""
        self.db = db
        self.expr = expr

    def select(self):
        """Resolve the captured predicate against the fake DB's rows."""
        return self.db._resolve(self.expr)


class _FakeDB:
    """Records the predicate tree passed to db(...); returns only pool rows the predicate admits."""

    def __init__(self, cred_rows):
        """Seed the fake DB with the reflected tables and candidate credential rows."""
        self.provider_credentials = _CredTable()
        self.ai_providers = _ProvTable()
        self._cred_rows = cred_rows
        self.captured_expr = None

    def __call__(self, expr):
        """Return a query bound to this DB and the given predicate expression."""
        return _Query(self, expr)

    def _flatten(self, expr, acc):
        """Flatten a penguin-dal AND-composed predicate tree into leaf ("eq", field, val) tuples."""
        # penguin-dal ANDs compose as nested _Expr nodes via &; capture leaf ("eq", field, val)
        node = expr.node if isinstance(expr, _Expr) else expr
        if isinstance(node, tuple) and node and node[0] == "eq":
            acc.append(node)
            return
        if isinstance(node, list):
            for child in node:
                self._flatten(child, acc)

    def _resolve(self, expr):
        """Return provider lookup rows or filter credential rows per the captured predicate."""
        # provider lookup (name == "prov") returns the provider row
        leaves = []
        self._flatten(expr, leaves)
        if ("eq", "name", "prov") in [(leaf[0], leaf[1], leaf[2]) for leaf in leaves]:
            return _Rows([_Row(id=1)])
        self.captured_expr = leaves
        admits_null_only = ("eq", "owner_org_id", None) in leaves
        rows = _Rows()
        for r in self._cred_rows:
            if admits_null_only and r.owner_org_id is not None:
                continue
            rows.append(r)
        return rows


def _mk_link():
    """Build a minimal connection_links-like row for the manager under test."""
    return _Row(name="prov", api_key="platform-key", enabled=True)


def test_byok_credential_is_excluded_from_pool():
    """A tenant-owned (owner_org_id set) credential must never be selected for platform traffic."""
    rows = [
        _Row(id=10, label="byok", api_key="tenant-key", org_id="", owner_org_id=99, weight=100),
        _Row(id=11, label="platform", api_key="pool-key", org_id="", owner_org_id=None, weight=100),
    ]
    db = _FakeDB(rows)
    mgr = LLMConnectionManager.__new__(LLMConnectionManager)  # bypass _load_connectors
    mgr.db = db
    from shared.utils.llm_connectors import RoundRobinSelector

    mgr._selector = RoundRobinSelector()
    key = mgr._select_credential(_mk_link())
    assert key == "pool-key"  # never the tenant-owned key
    assert ("eq", "owner_org_id", None) in db.captured_expr  # predicate present
