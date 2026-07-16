"""Tests for the _field_alias override-loader seam.

``jobcannon/engine/_field_alias.py`` exposes ``set_override_loader()`` as the
host-injectable seam that replaces the private repo's
``job_finder.web.autoheal.override_loader`` module. These tests pin the
seam's three documented contracts (the module docstring's own claim: "With
no override file present these are byte-identical to the canonical
extract_field / find_job_array calls"):

  (a) no loader registered -> resolve_title / resolve_url / resolve_job_array
      produce canonical results, identical to extract_field / find_job_array.
  (b) a loader whose ats_alias() returns a recipe -> the override is actually
      consulted (verified via a call-recording stub) and its extras are
      applied, with canonical keys still winning first-match-wins.
  (c) a registered loader whose ats_alias() returns None for the queried
      platform key -> identical to the canonical (no-loader) path.

``_override_loader`` is module-global mutable state. The autouse fixture
below saves/restores it around every test so ordering and pytest-xdist
workers stay isolated.
"""

from __future__ import annotations

import pytest

from jobcannon.engine import _field_alias


class _StubRecipe:
    """Minimal stand-in for the host's ats_alias() override recipe."""

    def __init__(
        self,
        title_fields: list[str] | None = None,
        url_fields: list[str] | None = None,
        array_keys: list[str] | None = None,
    ) -> None:
        self.title_fields = title_fields or []
        self.url_fields = url_fields or []
        self.array_keys = array_keys or []


class _StubLoader:
    """Minimal stand-in for the host's autoheal override loader."""

    def __init__(self, recipes: dict[str, _StubRecipe] | None = None) -> None:
        self._recipes = recipes or {}

    def ats_alias(self, key: str):
        return self._recipes.get(key)


class _RecordingLoader:
    """Stub loader that records every ats_alias() query it receives."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    def ats_alias(self, key: str):
        self.queries.append(key)
        return None


@pytest.fixture(autouse=True)
def _restore_override_loader():
    """Save/restore the module-global override loader around every test."""
    original = _field_alias._override_loader
    yield
    _field_alias.set_override_loader(original)


# ---------------------------------------------------------------------------
# (a) No loader registered -> canonical behavior
# ---------------------------------------------------------------------------


class TestNoLoaderRegistered:
    def test_resolve_title_uses_canonical_fields(self):
        _field_alias.set_override_loader(None)
        posting = {"title": "Software Engineer"}
        assert _field_alias.resolve_title(posting, "greenhouse") == "Software Engineer"

    def test_resolve_title_unmapped_field_returns_none(self):
        _field_alias.set_override_loader(None)
        assert _field_alias.resolve_title({"unmapped_key": "x"}, "greenhouse") is None

    def test_resolve_url_uses_canonical_fields(self):
        _field_alias.set_override_loader(None)
        posting = {"url": "https://example.com/jobs/1"}
        assert _field_alias.resolve_url(posting, "greenhouse") == "https://example.com/jobs/1"

    def test_resolve_job_array_uses_canonical_keys(self):
        _field_alias.set_override_loader(None)
        data = {"jobs": [{"title": "A"}, {"title": "B"}]}
        expected = [{"title": "A"}, {"title": "B"}]
        assert _field_alias.resolve_job_array(data, "greenhouse") == expected

    def test_resolve_job_array_unrecognised_shape_returns_none(self):
        _field_alias.set_override_loader(None)
        assert _field_alias.resolve_job_array({"unmapped": []}, "greenhouse") is None


# ---------------------------------------------------------------------------
# (b) Loader with a matching recipe -> override actually consulted + applied
# ---------------------------------------------------------------------------


class TestLoaderWithMatchingRecipe:
    def test_resolve_title_falls_back_to_override_extra(self):
        loader = _StubLoader({"ats:weirdats": _StubRecipe(title_fields=["headline"])})
        _field_alias.set_override_loader(loader)
        posting = {"headline": "Staff Engineer"}
        # No canonical JOB_TITLE_FIELDS key matches; only the override extra does.
        assert _field_alias.resolve_title(posting, "weirdats") == "Staff Engineer"

    def test_resolve_title_canonical_key_still_wins_first_match(self):
        loader = _StubLoader({"ats:weirdats": _StubRecipe(title_fields=["headline"])})
        _field_alias.set_override_loader(loader)
        posting = {"title": "Canonical Title", "headline": "Override Title"}
        # first-match-wins: canonical keys precede appended override extras.
        assert _field_alias.resolve_title(posting, "weirdats") == "Canonical Title"

    def test_resolve_url_falls_back_to_override_extra(self):
        loader = _StubLoader({"ats:weirdats": _StubRecipe(url_fields=["permalink"])})
        _field_alias.set_override_loader(loader)
        posting = {"permalink": "https://weird.example/jobs/9"}
        assert _field_alias.resolve_url(posting, "weirdats") == "https://weird.example/jobs/9"

    def test_resolve_job_array_falls_back_to_override_key(self):
        loader = _StubLoader({"ats:weirdats": _StubRecipe(array_keys=["listings"])})
        _field_alias.set_override_loader(loader)
        data = {"listings": [{"title": "A"}]}
        assert _field_alias.resolve_job_array(data, "weirdats") == [{"title": "A"}]

    def test_resolve_job_array_canonical_match_short_circuits_before_override(self):
        loader = _StubLoader({"ats:weirdats": _StubRecipe(array_keys=["listings"])})
        _field_alias.set_override_loader(loader)
        data = {"jobs": [{"title": "canonical"}], "listings": [{"title": "override"}]}
        # resolve_job_array tries the canonical find_job_array() first and
        # returns immediately on a hit -- the override branch is never reached.
        assert _field_alias.resolve_job_array(data, "weirdats") == [{"title": "canonical"}]

    def test_loader_is_queried_with_platform_prefixed_key(self):
        loader = _RecordingLoader()
        _field_alias.set_override_loader(loader)
        _field_alias.resolve_title({"title": "x"}, "greenhouse")
        assert loader.queries == ["ats:greenhouse"]

    def test_loader_queried_independently_for_title_and_url(self):
        loader = _RecordingLoader()
        _field_alias.set_override_loader(loader)
        _field_alias.resolve_title({"title": "x"}, "lever")
        _field_alias.resolve_url({"url": "https://x"}, "lever")
        assert loader.queries == ["ats:lever", "ats:lever"]


# ---------------------------------------------------------------------------
# (c) Loader registered but returns None for this platform -> canonical path
# ---------------------------------------------------------------------------


class TestLoaderReturnsNoRecipeForPlatform:
    def test_resolve_title_falls_back_to_canonical(self):
        loader = _StubLoader({})  # no recipes registered -> ats_alias() always None
        _field_alias.set_override_loader(loader)
        posting = {"title": "Software Engineer"}
        assert _field_alias.resolve_title(posting, "greenhouse") == "Software Engineer"

    def test_resolve_url_falls_back_to_canonical(self):
        loader = _StubLoader({})
        _field_alias.set_override_loader(loader)
        posting = {"url": "https://example.com/jobs/1"}
        assert _field_alias.resolve_url(posting, "greenhouse") == "https://example.com/jobs/1"

    def test_resolve_job_array_falls_back_to_canonical(self):
        loader = _StubLoader({})
        _field_alias.set_override_loader(loader)
        data = {"jobs": [{"title": "A"}]}
        assert _field_alias.resolve_job_array(data, "greenhouse") == [{"title": "A"}]

    def test_resolve_job_array_no_canonical_and_no_recipe_returns_none(self):
        loader = _StubLoader({})
        _field_alias.set_override_loader(loader)
        assert _field_alias.resolve_job_array({"unmapped": []}, "greenhouse") is None

    def test_resolve_job_array_recipe_with_empty_array_keys_returns_none(self):
        loader = _StubLoader({"ats:greenhouse": _StubRecipe(array_keys=[])})
        _field_alias.set_override_loader(loader)
        assert _field_alias.resolve_job_array({"unmapped": []}, "greenhouse") is None
