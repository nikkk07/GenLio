"""Vercel Python serverless entrypoint for the Telegram webhook.

Vercel serves an ASGI ``app`` exported from a file under ``api/``. The actual
routing + secret validation lives in :func:`gelio.webhook.build_app`; this file
only exposes it. Set the project's Environment Variables in the Vercel
dashboard (TELEGRAM_BOT_TOKEN, TELEGRAM_ADMIN_CHAT_ID, TELEGRAM_WEBHOOK_SECRET,
GELIO_STATE=supabase, SUPABASE_URL, SUPABASE_SERVICE_KEY, and any publish keys).

Routes (relative to your deployment base URL):
    GET  /api/telegram/health
    POST /api/telegram/telegram/<secret>   ← register this with setWebhook

Supabase Edge Function alternative: an Edge Function (Deno) can forward the
same JSON body to a tiny FastAPI deployment, or you can port `create_app`'s
single handler to Deno — the validation contract (path secret + header secret)
is identical. Vercel is the documented default because it runs this Python app
unchanged.
"""

from gelio.webhook import build_app

app = build_app()
