#!/usr/bin/env python3
"""
IST to UTC converter for scheduling posts.
Usage: python ist_to_utc.py "2026-06-13 18:00"
"""

import sys
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc


def ist_to_utc(ist_str: str) -> str:
    """Convert IST datetime string to UTC ISO format for scheduling."""
    try:
        # Try parsing with various formats
        for fmt in ["%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y %H:%M"]:
            try:
                ist_dt = datetime.strptime(ist_str, fmt)
                ist_dt = ist_dt.replace(tzinfo=IST)
                utc_dt = ist_dt.astimezone(UTC)
                
                print(f"\n✅ Conversion successful!")
                print(f"📍 IST: {ist_dt.strftime('%Y-%m-%d %I:%M %p IST')}")
                print(f"🌍 UTC: {utc_dt.strftime('%Y-%m-%d %I:%M %p UTC')}")
                print(f"\n📋 Use this for scheduling:")
                print(f"   {utc_dt.strftime('%Y-%m-%dT%H:%M:%SZ')}")
                print(f"\n💻 Command:")
                print(f"   python run.py schedule POST-ID \"{utc_dt.strftime('%Y-%m-%dT%H:%M:%SZ')}\"")
                return utc_dt.isoformat()
            except ValueError:
                continue
        
        raise ValueError("Invalid format")
    except Exception as e:
        print(f"\n❌ Error: {e}")
        print(f"\n📝 Supported formats:")
        print(f"   YYYY-MM-DD HH:MM")
        print(f"   YYYY-MM-DD HH:MM:SS")
        print(f"   DD-MM-YYYY HH:MM")
        print(f"\n💡 Examples:")
        print(f"   python ist_to_utc.py \"2026-06-13 18:00\"")
        print(f"   python ist_to_utc.py \"13-06-2026 18:00\"")
        return ""


def show_common_times():
    """Show common IST times and their UTC equivalents."""
    print("\n⏰ Common IST to UTC Conversions:")
    print("=" * 60)
    
    times = [
        ("06:00 AM", 6, 0),
        ("09:00 AM", 9, 0),
        ("12:00 PM", 12, 0),
        ("03:00 PM", 15, 0),
        ("06:00 PM", 18, 0),
        ("09:00 PM", 21, 0),
    ]
    
    today = datetime.now(IST).date()
    
    for label, hour, minute in times:
        ist_dt = datetime(today.year, today.month, today.day, hour, minute, tzinfo=IST)
        utc_dt = ist_dt.astimezone(UTC)
        print(f"IST {label:12} → UTC {utc_dt.strftime('%I:%M %p'):12} ({utc_dt.strftime('%H:%M')})")
    
    print("\n💡 IST is 5 hours 30 minutes ahead of UTC")
    print("   Subtract 5:30 from IST to get UTC")


if __name__ == "__main__":
    print("=" * 60)
    print("🇮🇳 IST to UTC Converter for GenLio")
    print("=" * 60)
    
    if len(sys.argv) > 1:
        ist_time_str = " ".join(sys.argv[1:])
        ist_to_utc(ist_time_str)
    else:
        print("\n📅 Current Time:")
        now_ist = datetime.now(IST)
        now_utc = datetime.now(UTC)
        print(f"   IST: {now_ist.strftime('%Y-%m-%d %I:%M:%S %p')}")
        print(f"   UTC: {now_utc.strftime('%Y-%m-%d %I:%M:%S %p')}")
        
        show_common_times()
        
        print(f"\n" + "=" * 60)
        print(f"📝 Usage:")
        print(f"   python ist_to_utc.py \"YYYY-MM-DD HH:MM\"")
        print(f"\n💡 Example:")
        print(f"   python ist_to_utc.py \"2026-06-13 18:00\"")
        print(f"   python ist_to_utc.py \"{(now_ist + timedelta(days=1)).strftime('%Y-%m-%d')} 10:00\"")
        print("=" * 60)
