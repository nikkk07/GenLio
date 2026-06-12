"""Local inline-SVG icon set for the slide design system.

Every icon is a single-color 24x24 path drawing that inherits ``currentColor``,
so CSS paints them gold (or any brand color) without per-icon variants. The
content writer references icons BY NAME from :data:`ALLOWED_ICONS`; anything
else is coerced to ``star`` before rendering, so a bad LLM icon name can never
break a render. No network is ever touched at render time.
"""

from __future__ import annotations

_VIEWBOX = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">{body}</svg>'

# Icon bodies (24x24 stroke outlines). Kept terse but hand-checked: each one
# reads as its concept at 28-44px render sizes on a dark background.
_BODIES: dict[str, str] = {
    "book": '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20V4H6.5A2.5 2.5 0 0 0 4 6.5v13z"/><path d="M4 19.5A2.5 2.5 0 0 0 6.5 22H20v-5"/>',
    "plane": '<path d="M10.5 13.5 3 11l1.5-2 6.5 1L17.5 3l2.5 1-4 8 4.5 4-1.5 2-6-3.5-3 3-1.5-.5.5-3z"/>',
    "takeoff": '<path d="M2 21h20"/><path d="M3.5 14.5 8 16l11.5-6.5c1.2-.7 1.4-1.8.6-2.5-.7-.6-1.8-.6-2.8 0l-4 2.4-7-2.4-2 1.5 5 3-3 1.8-2.5-.8-1.3 1z"/>',
    "medal": '<circle cx="12" cy="14" r="5"/><path d="m9 10.5-3-7.5h4l2 5 2-5h4l-3 7.5"/><path d="m12 12.5 1 1.8h2l-1.5 1.4.5 2-2-1.1-2 1.1.5-2L9 14.3h2z" fill="currentColor" stroke="none"/>',
    "badge": '<path d="M12 2 4.5 5v6c0 5 3.2 8.8 7.5 11 4.3-2.2 7.5-6 7.5-11V5L12 2z"/><path d="m8.8 11.8 2.2 2.2 4.2-4.5"/>',
    "chart_up": '<path d="M3 3v18h18"/><path d="m7 15 4-4 3 3 6-7"/><path d="M16 7h4v4"/>',
    "target": '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1.3" fill="currentColor" stroke="none"/>',
    "bulb": '<path d="M9 18h6"/><path d="M10 21h4"/><path d="M12 3a6 6 0 0 0-3.5 10.9c.8.6 1.2 1.3 1.4 2.1h4.2c.2-.8.6-1.5 1.4-2.1A6 6 0 0 0 12 3z"/>',
    "users": '<circle cx="9" cy="8" r="3.5"/><path d="M3 20c0-3.3 2.7-5.5 6-5.5s6 2.2 6 5.5"/><path d="M16 5a3.2 3.2 0 0 1 0 6.3"/><path d="M18 14.7c2 .8 3 2.4 3 4.3"/>',
    "globe": '<circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3a14.5 14.5 0 0 1 0 18 14.5 14.5 0 0 1 0-18z"/>',
    "trophy": '<path d="M8 21h8"/><path d="M12 17v4"/><path d="M7 4h10v5a5 5 0 0 1-10 0V4z"/><path d="M7 6H4a3 3 0 0 0 3 4.5"/><path d="M17 6h3a3 3 0 0 1-3 4.5"/>',
    "check": '<circle cx="12" cy="12" r="9"/><path d="m8 12.5 2.8 2.8L16.5 9"/>',
    "clipboard": '<rect x="6" y="4" width="12" height="17" rx="2"/><path d="M9 4a3 3 0 0 1 6 0"/><path d="M9 11h6"/><path d="M9 15h6"/>',
    "compass": '<circle cx="12" cy="12" r="9"/><path d="m15.5 8.5-2 5-5 2 2-5z"/>',
    "shield": '<path d="M12 2 4.5 5v6c0 5 3.2 8.8 7.5 11 4.3-2.2 7.5-6 7.5-11V5L12 2z"/>',
    "star": '<path d="m12 3 2.6 5.6 6.1.7-4.5 4.2 1.2 6L12 16.5 6.6 19.5l1.2-6L3.3 9.3l6.1-.7z"/>',
    "wings": '<path d="M12 13c-2-3.5-6-5-10-4.5 1.5 3.5 4.5 5.5 8 5.8"/><path d="M12 13c2-3.5 6-5 10-4.5-1.5 3.5-4.5 5.5-8 5.8"/><circle cx="12" cy="14.5" r="1.6"/><path d="M12 16.5V19"/>',
    "quote": '<path d="M5 7h5v5H6.5A4.5 4.5 0 0 0 11 16.5V18a6 6 0 0 1-6-6V7z" fill="currentColor" stroke="none"/><path d="M14 7h5v5h-3.5A4.5 4.5 0 0 0 20 16.5V18a6 6 0 0 1-6-6V7z" fill="currentColor" stroke="none"/>',
    "paper_plane": '<path d="m3 11 18-7-5.5 17-3.5-7L3 11z"/><path d="M12 14 21 4"/>',
    "graduation": '<path d="m12 4 10 4.5L12 13 2 8.5 12 4z"/><path d="M6.5 10.5V15c0 1.7 2.5 3 5.5 3s5.5-1.3 5.5-3v-4.5"/><path d="M22 8.5V14"/>',
}

ALLOWED_ICONS: frozenset[str] = frozenset(_BODIES)

FALLBACK_ICON = "star"


def normalize_icon(name: str | None) -> str:
    """Return ``name`` if it is a known icon, else :data:`FALLBACK_ICON`."""
    cleaned = (name or "").strip().lower()
    return cleaned if cleaned in ALLOWED_ICONS else FALLBACK_ICON


def icon_svg(name: str) -> str:
    """Inline SVG markup for ``name`` (unknown names render the star)."""
    return _VIEWBOX.format(body=_BODIES[normalize_icon(name)])
