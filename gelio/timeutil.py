"""IST ⇄ UTC conversion for scheduled posting.

gelio stores every ``scheduled_time`` as an ISO-8601 UTC timestamp; admins
think in IST. ``parse_schedule_input`` is the single entry point: naive inputs
("2026-06-13 18:00") are interpreted as IST, while inputs carrying an explicit
offset or ``Z`` are respected as-is. Run ``python -m gelio.timeutil`` for the
interactive converter (replaces the old root-level ``ist_to_utc.py``).
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

# Naive formats accepted from admins (interpreted as IST).
_NAIVE_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y %H:%M")


class ScheduleParseError(ValueError):
    """Raised when a schedule string matches no supported format."""


def ist_to_utc(ist_str: str) -> datetime:
    """Parse a naive IST datetime string and return it as aware UTC."""
    for fmt in _NAIVE_FORMATS:
        try:
            return datetime.strptime(ist_str, fmt).replace(tzinfo=IST).astimezone(UTC)
        except ValueError:
            continue
    raise ScheduleParseError(
        f"unrecognized IST datetime {ist_str!r}; "
        "use YYYY-MM-DD HH:MM, YYYY-MM-DD HH:MM:SS, or DD-MM-YYYY HH:MM"
    )


def parse_schedule_input(text: str) -> str:
    """Turn an admin-supplied schedule string into a stored UTC ISO timestamp.

    * ``2026-06-13 18:00`` (naive)            → treated as IST, converted.
    * ``2026-06-13T12:30:00Z`` / ``+05:30``    → explicit offset, respected.
    * ``2026-06-13T12:30:00`` (naive ISO)      → treated as IST, converted.
    """
    text = text.strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return ist_to_utc(text).isoformat()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(UTC).isoformat()


def utc_to_ist(utc_iso: str) -> datetime:
    """Render a stored UTC ISO timestamp as aware IST (for admin-facing text)."""
    dt = datetime.fromisoformat(utc_iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(IST)


def _show_common_times() -> None:
    print("\n⏰ Common IST to UTC Conversions:")
    print("=" * 60)
    today = datetime.now(IST).date()
    for hour in (6, 9, 12, 15, 18, 21):
        ist_dt = datetime(today.year, today.month, today.day, hour, 0, tzinfo=IST)
        utc_dt = ist_dt.astimezone(UTC)
        print(
            f"IST {ist_dt.strftime('%I:%M %p'):12} → "
            f"UTC {utc_dt.strftime('%I:%M %p'):12} ({utc_dt.strftime('%H:%M')})"
        )
    print("\n💡 IST is 5 hours 30 minutes ahead of UTC")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    print("=" * 60)
    print("🇮🇳 IST to UTC Converter for gelio")
    print("=" * 60)
    if argv:
        raw = " ".join(argv)
        try:
            utc_iso = parse_schedule_input(raw)
        except ScheduleParseError as exc:
            print(f"\n❌ Error: {exc}")
            print('\n💡 Example: python -m gelio.timeutil "2026-06-13 18:00"')
            return 1
        utc_dt = datetime.fromisoformat(utc_iso)
        print(f"\n✅ IST {raw} → UTC {utc_dt.strftime('%Y-%m-%d %I:%M %p')}")
        print(f"\n📋 Use this for scheduling:\n   {utc_iso}")
        print(f'\n💻 Command:\n   python run.py schedule POST-ID "{raw}"')
        return 0
    now_ist, now_utc = datetime.now(IST), datetime.now(UTC)
    print(f"\n📅 Current Time:")
    print(f"   IST: {now_ist.strftime('%Y-%m-%d %I:%M:%S %p')}")
    print(f"   UTC: {now_utc.strftime('%Y-%m-%d %I:%M:%S %p')}")
    _show_common_times()
    print('\n📝 Usage: python -m gelio.timeutil "YYYY-MM-DD HH:MM"  (IST)')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
