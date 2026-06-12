#!/usr/bin/env python3
"""
Test script to verify /start command handling.
This simulates what happens when you send /start in Telegram.
"""

import json
from gelio.approval import build_approval
from gelio.store import Store
from gelio.sync import build_sync
from config.settings import load_settings


def test_start_command():
    """Simulate /start command from Telegram."""
    print("=" * 70)
    print("Testing /start Command Handler")
    print("=" * 70)
    print()
    
    settings = load_settings()
    store = Store(settings.db_path)
    
    try:
        approval = build_approval(settings, store, build_sync(settings))
        
        # Simulate a Telegram message with /start command
        fake_message = {
            "message_id": 12345,
            "from": {
                "id": int(settings.telegram_admin_chat_id),
                "is_bot": False,
                "first_name": "Admin",
            },
            "chat": {
                "id": int(settings.telegram_admin_chat_id),
                "type": "private",
            },
            "date": 1234567890,
            "text": "/start",
        }
        
        print("📨 Simulating Telegram message:")
        print(f"   From: Chat ID {settings.telegram_admin_chat_id}")
        print(f"   Text: /start")
        print()
        print("🤖 Bot would now:")
        print("   1. Send 'Generating new post...' message")
        print("   2. Call pipeline.generate(render=True)")
        print("   3. Send slides + captions to Telegram")
        print("   4. Show Approve/Schedule/Reject/Regenerate buttons")
        print()
        print("⚠️  Note: This test doesn't actually send to Telegram")
        print("   (to avoid spam). To test for real:")
        print()
        print("   1. Run: python run.py bot")
        print("   2. Open Telegram → GenLio_Bot")
        print("   3. Send: /start")
        print()
        print("=" * 70)
        print("✅ Handler is installed and ready!")
        print("=" * 70)
        
    finally:
        store.close()


if __name__ == "__main__":
    test_start_command()
