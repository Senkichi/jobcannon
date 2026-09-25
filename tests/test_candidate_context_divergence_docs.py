"""Docstring-divergence guard for issue #364.

Issue #364's maintainer decision was to document the deliberate divergence
between the two candidate-context renderers rather than reconcile them:

- ``jobcannon.host.candidate_context.build_candidate_context`` renders a
  ``profiles`` DB row;
- ``jobcannon.host.scoring_orchestrator.build_candidate_context`` renders
  ``config["profile"]`` plus the experience-profile dict, with
  ``_render_location_targeting`` producing its location-targeting section.

The divergence is only safe while it stays documented. A future refactor
that drops one side's note leaves a reader of the other renderer believing
it is the sole candidate-context implementation — and either could then
"clean up" the apparent duplication by delegating, silently dropping the
config-shaped path's location hierarchy, structured positions/education,
industries, and exclusions prompt inputs (the ``profiles`` table has none
of them until the schema expansion tracked in issue #420 lands).

This guard pins the three properties that keep the documentation honest:

1. each renderer's docstring cross-references the other module, so a
   reader of either one learns the second exists;
2. both name issue #420 as the unification gate, so the notes retire with
   the schema expansion instead of fossilizing;
3. ``_render_location_targeting``'s docstring points at its parent
   renderer's note, since that docstring is where a maintainer touching
   the location section actually lands.

Pure introspection — no DB, no config files, no fixtures.
"""

from __future__ import annotations


def _renderer_docstrings() -> tuple[str, str, str]:
    """Return (profiles-row renderer, config-shaped renderer, helper) docs."""
    from jobcannon.host import candidate_context, scoring_orchestrator

    return (
        " ".join((candidate_context.build_candidate_context.__doc__ or "").split()),
        " ".join((scoring_orchestrator.build_candidate_context.__doc__ or "").split()),
        " ".join((scoring_orchestrator._render_location_targeting.__doc__ or "").split()),
    )


def test_profiles_row_renderer_cross_references_config_shaped_renderer():
    row_doc, _, _ = _renderer_docstrings()
    assert "issue #364" in row_doc
    assert "scoring_orchestrator" in row_doc
    assert "issue #420" in row_doc


def test_config_shaped_renderer_cross_references_profiles_row_renderer():
    _, config_doc, _ = _renderer_docstrings()
    assert "issue #364" in config_doc
    assert "candidate_context" in config_doc
    assert "NOT delegate" in config_doc
    assert "issue #420" in config_doc


def test_location_targeting_helper_points_at_divergence_note():
    _, _, helper_doc = _renderer_docstrings()
    assert "issue #364" in helper_doc
    assert "candidate_context" in helper_doc
    assert "issue #420" in helper_doc
