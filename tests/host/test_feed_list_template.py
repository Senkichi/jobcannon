"""Template-source pins for _feed_list.html (spec §3 click delegate + §4
sort-select removal). Reads the template file directly — the same
source-inspection style tests/host/test_touch_targets.py uses — so it needs
no app and no DB, and cannot collide with test_feed_page.py's rendered-HTML
ownership (Task 8's file)."""

from pathlib import Path

_TEMPLATE = (
    Path(__file__).resolve().parents[2] / "jobcannon" / "web" / "templates" / "_feed_list.html"
).read_text(encoding="utf-8")


def test_sort_select_is_gone():
    assert 'name="sort"' not in _TEMPLATE
    assert "sort_tokens" not in _TEMPLATE


def test_expand_delegate_binds_once_at_document_level():
    # Must survive #feed-content outerHTML swaps and Load-more appends:
    # bound on document, guarded by a window flag so re-included fragments
    # never double-bind.
    assert "window.jcExpandBound" in _TEMPLATE
    assert "document.addEventListener('click'" in _TEMPLATE


def test_expand_delegate_ignores_interactive_targets():
    assert "[data-posting-actions]" in _TEMPLATE
    assert "[data-posting-detail]" in _TEMPLATE
    assert "getSelection" in _TEMPLATE
