"""Icon set tests: every allowed name renders, unknowns fall back to star."""

from __future__ import annotations

from gelio.icons import ALLOWED_ICONS, FALLBACK_ICON, icon_svg, normalize_icon

EXPECTED = {
    "book", "plane", "takeoff", "medal", "badge", "chart_up", "target", "bulb",
    "users", "globe", "trophy", "check", "clipboard", "compass", "shield",
    "star", "wings", "quote", "paper_plane", "graduation",
}


def test_icon_set_is_exactly_the_spec_list():
    assert ALLOWED_ICONS == frozenset(EXPECTED)


def test_every_icon_renders_single_color_svg():
    for name in ALLOWED_ICONS:
        svg = icon_svg(name)
        assert svg.startswith("<svg")
        assert "currentColor" in svg
        assert "http" not in svg.replace("http://www.w3.org/2000/svg", "")


def test_normalize_icon_passes_known_and_falls_back():
    assert normalize_icon("plane") == "plane"
    assert normalize_icon(" Plane ") == "plane"
    assert normalize_icon("rocketship") == FALLBACK_ICON
    assert normalize_icon("") == FALLBACK_ICON
    assert normalize_icon(None) == FALLBACK_ICON


def test_unknown_icon_renders_star_svg():
    assert icon_svg("not-a-real-icon") == icon_svg("star")
