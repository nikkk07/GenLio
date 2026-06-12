#!/bin/bash
# Quick start script for GenLio bot

echo "🚀 GenLio Quick Start"
echo "===================="
echo ""

# Check if we're in the right directory
if [ ! -f "run.py" ]; then
    echo "❌ Error: Not in GenLio directory"
    echo "   Run: cd /Users/nikhlbisht/Desktop/GenLio"
    exit 1
fi

# Activate virtual environment
if [ ! -d ".venv" ]; then
    echo "❌ Error: Virtual environment not found"
    echo "   Run: python3 -m venv .venv"
    exit 1
fi

echo "✅ Activating virtual environment..."
source .venv/bin/activate

echo ""
echo "📋 What would you like to do?"
echo ""
echo "1) Start Telegram Bot Server (listen for button clicks)"
echo "2) Generate new post + send to Telegram"
echo "3) List all posts"
echo "4) List scheduled posts"
echo "5) Convert IST time to UTC"
echo "6) Show all commands (open COMMANDS.txt)"
echo "7) Exit"
echo ""
read -p "Enter choice [1-7]: " choice

case $choice in
    1)
        echo ""
        echo "🤖 Starting Telegram Bot Server..."
        echo "   Press Ctrl+C to stop"
        echo ""
        python run.py bot
        ;;
    2)
        echo ""
        echo "📝 Generating new post..."
        echo ""
        python run.py generate --render --approve
        echo ""
        echo "✅ Post sent to Telegram!"
        echo "   Open GenLio_Bot in Telegram to approve"
        echo ""
        read -p "Start bot server now? (y/n): " start_bot
        if [ "$start_bot" = "y" ] || [ "$start_bot" = "Y" ]; then
            echo ""
            echo "🤖 Starting bot server..."
            python run.py bot
        fi
        ;;
    3)
        echo ""
        echo "📊 All posts:"
        echo ""
        sqlite3 data/gelio.db "SELECT id, concept, state FROM posts ORDER BY created_at DESC LIMIT 10;"
        echo ""
        ;;
    4)
        echo ""
        echo "📅 Scheduled posts:"
        echo ""
        python run.py list-scheduled
        echo ""
        read -p "Show posts before specific date? (y/n): " show_before
        if [ "$show_before" = "y" ] || [ "$show_before" = "Y" ]; then
            read -p "Enter date (YYYY-MM-DD): " date_input
            python run.py list-scheduled --before "${date_input}T23:59:59Z"
        fi
        echo ""
        ;;
    5)
        echo ""
        read -p "Enter IST time (YYYY-MM-DD HH:MM): " ist_time
        python -m gelio.timeutil "$ist_time"
        echo ""
        ;;
    6)
        echo ""
        echo "📖 Opening COMMANDS.txt..."
        if command -v open &> /dev/null; then
            open COMMANDS.txt
        elif command -v cat &> /dev/null; then
            cat COMMANDS.txt | less
        fi
        ;;
    7)
        echo ""
        echo "👋 Goodbye!"
        exit 0
        ;;
    *)
        echo ""
        echo "❌ Invalid choice"
        exit 1
        ;;
esac
