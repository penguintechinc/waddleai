"""Guard against docs/ and docs/docs-site/docs/ silently forking.

The mkdocs site (docs/docs-site/) has no ``docs_dir`` override, so it
publishes its OWN copies of several pages that also live under docs/.
Nothing kept the two in step, and four of them had drifted by 275-893
lines each -- the published site was serving content roughly eight months
older than the repo, including a VS Code page describing commands that had
since been fixed and a clone URL that 404s.

These tests fail on any new divergence, so a corrected page can't land in
docs/ while the site keeps serving the stale one.
"""

from __future__ import annotations

import pathlib

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_DOCS = _REPO_ROOT / "docs"
_SITE_DOCS = _DOCS / "docs-site" / "docs"

# Pages that must stay byte-identical between the repo and the published site.
_MIRRORED = (
    "api/openai-compatible.md",
    "getting-started/installation.md",
    "integrations/claude-code.md",
    "integrations/vscode-extension.md",
    "routing/destination-failover.md",
)

# Known fork, deliberately not asserted: none currently. Add an entry here
# only when a page genuinely needs a content rewrite before it can be
# brought under the _MIRRORED guard above -- see test_known_fork_list_stays_honest.
_KNOWN_FORKED: tuple[str, ...] = ()


@pytest.mark.parametrize("relpath", _MIRRORED)
def test_site_copy_matches_repo_copy(relpath: str) -> None:
    """The site's copy of a mirrored page is byte-identical to the repo's."""
    repo_copy = _DOCS / relpath
    site_copy = _SITE_DOCS / relpath
    assert repo_copy.is_file(), f"missing repo copy: {repo_copy}"
    assert site_copy.is_file(), f"missing site copy: {site_copy}"
    assert site_copy.read_text() == repo_copy.read_text(), (
        f"{relpath} has forked between docs/ and docs/docs-site/docs/. "
        "The site publishes its own copy, so fixing only one of them ships "
        "stale docs. Copy the corrected file to both paths."
    )


def test_no_unlisted_duplicate_pages() -> None:
    """Every page duplicated across both trees is either mirrored or a known fork.

    Catches a NEW duplicate being added without being brought under the
    mirroring guard above -- that is how the original four drifted.
    """
    duplicated = {
        str(p.relative_to(_SITE_DOCS))
        for p in _SITE_DOCS.rglob("*.md")
        if (_DOCS / p.relative_to(_SITE_DOCS)).is_file()
    }
    unaccounted = duplicated - set(_MIRRORED) - set(_KNOWN_FORKED)
    assert not unaccounted, (
        f"pages exist in both docs/ and docs/docs-site/docs/ but are not "
        f"listed in _MIRRORED or _KNOWN_FORKED: {sorted(unaccounted)}"
    )


def test_known_fork_list_stays_honest() -> None:
    """A page listed as a known fork must actually still differ.

    Without this, a page could be repaired and left on the exemption list,
    quietly losing the drift protection the mirrored set provides.
    """
    for relpath in _KNOWN_FORKED:
        repo_copy = _DOCS / relpath
        site_copy = _SITE_DOCS / relpath
        if not (repo_copy.is_file() and site_copy.is_file()):
            continue
        assert site_copy.read_text() != repo_copy.read_text(), (
            f"{relpath} is listed in _KNOWN_FORKED but the two copies now "
            "match -- move it to _MIRRORED so it stays protected."
        )
