# gelio — AI content agent (Phases 1–5)

`gelio` is a production-grade, **free-tier-only** AI content agent for the
aviation training academy **We One Aviation**. Each run maps a psychology
principle (Decision Fatigue, Impostor Syndrome, Spotlight Effect, …) onto
aviation / DGCA / pilot-training life, writes a full 8–10 slide carousel script
plus per-platform captions, and saves everything as **schema-validated JSON
artifacts**.

The full pipeline: content (Phase 1) → branded slide visuals + PDF (Phase 2)
→ Telegram approval with multi-admin buttons and IST scheduling (Phase 3) →
publishing to X and Instagram automatically, plus a semi-manual LinkedIn PDF
hand-off (Phase 4) → a **$0 always-on deployment** on GitHub Actions cron +
a serverless Telegram webhook + Supabase-authoritative shared state (Phase 5).

See the **[Phase 5 go-live runbook](#phase-5--0-go-live-runbook)** to deploy.

---

## Setup

Requires **Python 3.11+**.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python run.py setup-assets       # downloads brand fonts + installs Chromium
```

`setup-assets` fetches the Montserrat/Inter variable fonts and runs
`playwright install chromium` (the compositor renders slides as HTML screenshots).
On Linux/CI also install the browser's system libraries:

```bash
python run.py setup-assets --with-deps        # or: playwright install --with-deps chromium
```

Configure secrets (keys live only in `.env`, which is git-ignored):

```bash
cp .env.example .env
# then edit .env and paste your free-tier keys
```

### Environment variables (`.env`)

| Variable          | Required | Default                    | Notes                                            |
| ----------------- | -------- | -------------------------- | ------------------------------------------------ |
| `GELIO_PROVIDER`  | no       | `groq`                     | Primary provider: `groq` or `gemini`.            |
| `GROQ_API_KEY`    | yes\*    | —                          | Free key: https://console.groq.com/keys          |
| `GROQ_MODEL`      | no       | `llama-3.3-70b-versatile`  | Any current free Llama 3.x model.                |
| `GEMINI_API_KEY`  | yes\*    | —                          | Free key (starts with `AIza`): https://aistudio.google.com/app/apikey |
| `GEMINI_MODEL`    | no       | `gemini-2.0-flash`         | Gemini free-tier model.                          |
| `CLOUDFLARE_ACCOUNT_ID` | for AI images | —              | Workers AI account id (Phase 3.5 primary).       |
| `CLOUDFLARE_API_TOKEN`  | for AI images | —              | Token with the **Workers AI** permission.        |
| `CLOUDFLARE_IMAGE_MODEL`| no       | `@cf/black-forest-labs/flux-1-schnell` | Override the image model.     |
| `TOGETHER_API_KEY`| no       | —                          | Optional fallback image provider (FLUX schnell). |
| `TOGETHER_IMAGE_MODEL` | no  | `black-forest-labs/FLUX.1-schnell-Free` | Override Together model.       |
| `POLLINATIONS_TOKEN` | no    | —                          | Legacy (Phase 2); no longer in the image chain.  |
| `TELEGRAM_BOT_TOKEN` | for approval | —                  | BotFather token (Phase 3).                       |
| `TELEGRAM_ADMIN_CHAT_ID` | for approval | —              | Admin chat id(s); comma-separate for multiple admins, e.g. `111,222`. |
| `SUPABASE_URL`    | no\*\*   | —                          | Bare project URL (`https://<ref>.supabase.co`, no `/rest/v1`); blank = sync disabled. |
| `SUPABASE_SERVICE_KEY` | no  | —                          | service_role key (server-side only).             |
| `SUPABASE_BUCKET` | no       | `slides`                   | Public storage bucket for slides + PDF.          |
| `X_CONSUMER_KEY` / `X_CONSUMER_SECRET` | for X | —        | X app keys (Read+Write app, OAuth 1.0a). All 4 X keys required or X is skipped. |
| `X_ACCESS_TOKEN` / `X_ACCESS_TOKEN_SECRET` | for X | —    | User-context access token + secret.              |
| `X_MAX_IMAGES`    | no       | `4`                        | Slides per tweet (X hard cap is 4; first N slides are posted). |
| `IG_USER_ID`      | for IG   | —                          | IG Business/Creator user id (linked to a FB Page). |
| `IG_ACCESS_TOKEN` | for IG   | —                          | Long-lived Graph API token. Missing keys → IG skipped. |

\*\* **Instagram requires Supabase**: the Graph API fetches slides from public
URLs, which sync uploads to the public bucket. Without `SUPABASE_URL`, the IG
publisher skips gracefully ("Instagram requires Supabase slide URLs — enable
sync") and the rest of the publish run completes.

\* You need at least one working LLM key. Groq is primary; Gemini is the
automatic fallback. Providers with a missing key are skipped.

---

## Usage

### Phase 1 — content

```bash
# Print a would-be Brief + Content to stdout; writes nothing.
python run.py generate --dry-run

# Generate, validate, and persist artifacts + a DRAFTED record.
python run.py generate

# Choose a slide count (default 9).
python run.py generate --slides 10
```

### Phase 2 — visuals + composition

```bash
# One-time: download the brand fonts into assets/fonts/.
python run.py setup-assets

# Phase 1 + Phase 2 in one run (content -> slides -> carousel.pdf).
python run.py generate --render

# Render visuals for an artifact dir that already exists.
python run.py render 2026-06-11-decision-fatigue

# Re-render over existing slides (otherwise a complete slides/ dir is skipped).
python run.py render 2026-06-11-decision-fatigue --force
python run.py generate --render --force

# Verify the AI image chain with one live call (saves output/test-image.png).
python run.py test-image
```

### Series mode (Phase 3.7)

```bash
# A numbered-steps carousel: slide N is step N; the last step is the closing/CTA.
python run.py generate --series "10 Steps to Become a Pilot in India" --slides 10 --render
```

Every slide gets a `step_number` and the top-right badge renders **STEP N**
(non-series carousels show **SLIDE N** in the same style). The Telegram
Regenerate flow also accepts series topics: type a title shaped like
`10 Steps to ...` (auto-detected, one slide per step) or force it with
`series: My Title`.

If fewer than ~7/9 of a render's slides got a real AI photo (the rest fell
back to the branded gradient), the render is flagged **DEGRADED** — a warning
is logged and printed so you can check provider keys/quota before approving.

### Phase 3 — Telegram approval gate

```bash
# Generate → render → send the album + captions + buttons to the admin.
python run.py generate --render --approve

# Send an already-rendered post for approval.
python run.py send-approval 2026-06-11-decision-fatigue

# Run the long-poll listener that handles Approve/Reject/Regenerate taps.
python run.py bot
```

Flow: every admin in `TELEGRAM_ADMIN_CHAT_ID` (comma-separated for multiple)
gets the slides as a Telegram album, the three captions + hashtags, and inline
buttons. **✅ Approve Now** → `APPROVED` and (Phase 4) publishes immediately,
**⏰ Schedule** → the bot asks (via ForceReply) for a time in **IST** (e.g.
`2026-06-13 18:00`; explicit `…Z`/offset also accepted), stores it as UTC, and
approves — `publish-due` posts it when the time comes. **❌ Reject** →
`REJECTED`, **🔄 Regenerate** → the bot asks for a topic (or `auto`); it
rejects the current post and renders a fresh one on that topic, linked by
`parent_id`. Buttons are idempotent **across admins**: the first tap wins and
later taps answer "Already handled by <admin>". Action results are broadcast
to all admins. Regenerations are capped at 3/day. Nothing publishes without an
explicit Approve.

### Phase 4 — publishers (X, Instagram, LinkedIn)

```bash
# Publish one APPROVED post right now (all enabled platforms).
python run.py publish 2026-06-11-decision-fatigue

# Only specific platforms (also how you retry a failed platform).
python run.py publish 2026-06-11-decision-fatigue --platforms x,instagram

# Cron entry point: publish APPROVED posts whose schedule is due or unset.
# Idempotent — safe to run every few minutes.
python run.py publish-due

# Schedule from the CLI (naive time = IST, converted to UTC for storage).
python run.py schedule 2026-06-11-decision-fatigue "2026-06-13 18:00"
python -m gelio.timeutil "2026-06-13 18:00"   # standalone IST→UTC converter
```

Per-platform behavior:

- **X (Twitter)** — OAuth 1.0a user-context (hand-rolled HMAC-SHA1, no extra
  deps). Each slide is uploaded via the v1.1 media endpoint, then one tweet is
  created via `POST /2/tweets`. **X allows max 4 images per tweet**, so the
  first `X_MAX_IMAGES` (default 4: hook + 3 insights) slides are posted with
  the X caption; hashtags are appended only while the text stays ≤ 280 chars.
- **Instagram** — Graph API carousel: one item container per slide
  (`is_carousel_item`), a `CAROUSEL` container with the IG caption + hashtags,
  status polled until `FINISHED`, then `media_publish`. Requires IG
  Business/Creator + `IG_USER_ID`/`IG_ACCESS_TOKEN` **and Supabase sync** (the
  API fetches slides from the public bucket URLs).
- **LinkedIn (semi-manual by design)** — every admin receives `carousel.pdf`
  as a Telegram document plus the ready-to-paste LinkedIn caption + hashtags
  and a **"✅ Mark LinkedIn posted"** button. Upload the PDF manually, then tap
  the button (idempotent across admins) to confirm.

**Completion semantics:** per-platform results are tracked in dedicated
columns (`x_status`/`x_post_id`, `ig_status`/`ig_media_id`,
`linkedin_status`). When every **enabled** platform has succeeded (disabled
platforms — missing keys — are excluded), the post transitions to `COMPLETE`
and syncs. A platform failure marks `FAILED_X` / `FAILED_IG` (or
`FAILED_POST`) but the post stays resumable: `publish <id>` retries **only**
the failed/unfinished platforms and never re-posts to one already marked
posted. Transient errors (429/5xx/network) are retried with backoff;
permanent 4xx fail fast with the (token-redacted) response body logged.

Each non-dry run writes:

```
output/<id>/
├── brief.json, content.json     (Phase 1)
├── backgrounds/slide_1.png …    (raw AI/fallback backgrounds)
├── slides/slide_1.png …         (final 1080×1350 composited slides)
└── carousel.pdf                 (LinkedIn-ready)
```

and inserts a `DRAFTED` row into `data/gelio.db` (stamped with `rendered_at`
after a successful render — the state stays `DRAFTED`; approval is Phase 3).

**Architecture rule:** the AI image model **never renders text** — it only
generates a photographic background from each slide's `image_prompt`. All
typography (eyebrow, gold-highlighted headline, body, logo/wordmark, Swipe pill,
counter, CTA contact block) is stamped by the **HTML/CSS compositor** rendered
with **Playwright/Chromium**, so branding is pixel-perfect and identical every
day. Templates live in `gelio/templates/` (`base` + `hook`/`insight`/`cta` +
`styles.css`); fonts are embedded as `@font-face` for offline determinism.

**Image provider chain (Phase 3.5):** each is feature-flagged on its env keys and
tried in order — **Cloudflare Workers AI** (`flux-1-schnell`) → **Together AI**
(`FLUX.1-schnell-Free`) → a locally generated **branded gradient** that never
fails. Each slide logs `source=cloudflare|together|gradient`. With no image keys
set, every slide uses the gradient and the run still completes.

**No-repeat guarantee:** concepts already used (tracked in SQLite) are excluded.
When the 30-concept bank is exhausted, gelio asks the LLM for 10 fresh concepts,
validates they're new, appends them to `data/topic_bank.json`, and continues.

**Idempotency:** if a run re-selects an already-`DRAFTED` id, gelio detects the
existing record and reports its location instead of writing duplicates.

---

## Telegram & Supabase setup (Phase 3)

### Telegram bot
1. In Telegram, message **@BotFather** → `/newbot` → copy the token into
   `TELEGRAM_BOT_TOKEN`.
2. Get your numeric chat id: message **@userinfobot**, or message your new bot
   then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and read
   `message.chat.id`. Put it in `TELEGRAM_ADMIN_CHAT_ID` — comma-separate for
   multiple admins (the bot ignores every other sender; every listed admin
   receives previews and may tap buttons).
3. Run `python run.py bot` to handle button taps, and
   `python run.py generate --render --approve` to push a post for review.

**Security notes**
- Never commit logs: `bot.log` is git-ignored. Log output passes through a
  redaction filter (`gelio/redact.py`) so a Telegram URL or exception can
  never leak the bot token into a log line.
- If the token was ever exposed (e.g. a log committed to a public repo),
  revoke it via **@BotFather → /revoke** and paste the new token into `.env` —
  no code change needed; the token is only read from the environment.

### Supabase (optional dashboard sync)
1. Create a free project; copy the **Project URL** → `SUPABASE_URL` and the
   **service_role** key → `SUPABASE_SERVICE_KEY` (server-side only).
2. SQL editor → run `supabase/schema.sql` (creates `public.posts` + RLS).
3. Storage → new **public** bucket named `slides`.
4. Leave `SUPABASE_URL` blank to disable sync entirely — gelio runs fine without
   it; sync is best-effort and never blocks the approval flow.

## Tests

```bash
pytest
```

All external services (LLM, Telegram, Supabase, image API) are mocked — tests
require **no network and no API keys**.

---

## Architecture

```
config/settings.py    typed settings from env + brand.json
config/brand.json     brand kit (name, CTA, contact, visual tokens, hashtags)
data/topic_bank.json  topics by category (myth_busting/career_truth/process/psychology)
data/gelio.db         SQLite (created at runtime)

gelio/schemas.py        Pydantic contracts: Brief, Slide, Content, PostRecord
gelio/store.py          SQLite: topic dedup, run history, post state machine
gelio/llm.py            provider abstraction: Groq primary + Gemini fallback,
                        retry/backoff, JSON-mode, fence-stripping
gelio/topic_engine.py   weighted category pick -> angle/hook/eyebrow -> Brief
gelio/content_writer.py Brief -> Content (+image_prompt/highlight), retry loop
gelio/validators.py     business rules (slide roles, limits, CTA slide)
gelio/visual_gen.py     AI backgrounds: Cloudflare -> Together -> gradient (3.5)
gelio/compositor.py     HTML/CSS templates -> Playwright screenshot (3.5)
gelio/templates/        Jinja2 base + hook/insight/cta + styles.css (3.5)
gelio/pdf_builder.py    composited slides -> carousel.pdf (Phase 2)
gelio/assets.py         download brand fonts (setup-assets)
gelio/approval.py       Telegram approval gate: preview, buttons, regen,
                        scheduling, LinkedIn confirm (Phases 3–4)
gelio/publisher.py      X / Instagram / LinkedIn publishers + PublishService (4)
gelio/timeutil.py       IST ⇄ UTC schedule parsing (+ python -m gelio.timeutil)
gelio/redact.py         token redaction helper + logging filter
gelio/sync.py           best-effort Supabase sync for the dashboard (Phase 3)
gelio/pipeline.py       one end-to-end run (idempotent, dry-run + render aware)
run.py                  CLI
supabase/schema.sql     dashboard table + RLS to run in the Supabase SQL editor
```

### Reliability

- All LLM calls retry transient errors (transport, 429, 5xx) with exponential
  backoff (tenacity). Groq's strict-JSON `400 json_validate_failed` (a
  stochastic bad generation) is treated as retryable and re-rolled.
- The content writer validates every response against Pydantic **and** the
  business rules; on failure it feeds the concrete errors back into the next
  prompt, up to 3 attempts, then aborts cleanly — **bad artifacts are never
  written**.
- Permanent 4xx (bad request / auth) fail fast with the response body included.

---

## Data contracts (frozen)

### `Brief`
```json
{
  "id": "2026-06-11-decision-fatigue",
  "date": "2026-06-11",
  "concept": "Decision Fatigue",
  "aviation_angle": "Why pilots make more errors at the end of long duty days",
  "hook": "one-line scroll-stopping hook",
  "audience": "aspiring pilots / DGCA aspirants",
  "tone": "authoritative but encouraging"
}
```

### `Content`
```json
{
  "id": "2026-06-11-decision-fatigue",
  "slides": [
    {"index": 1, "role": "hook", "headline": "<= 60 chars (flat, for captions)",
     "headline_lines": [{"text": "<= 26 chars", "color": "white|gold"}],
     "subhead": "<= 140 chars",
     "panel": {"type": "checklist|grid4|quote", "title": "<= 32 chars",
               "items": [{"icon": "from the icon list", "title": "<= 28",
                          "desc": "<= 70"}],
               "quote_lines": ["2-3 short lines (quote type only)"]},
     "tip": "<= 110 chars with exactly one *gold phrase*",
     "step_number": "1..N in series mode, else absent",
     "body": "<= 220 chars", "visual_direction": "scene for image gen"}
  ],
  "captions": {"linkedin": "<= 1300", "instagram": "<= 2200", "x": "<= 280"},
  "hashtags": ["#aviation", "#pilottraining", "#DGCA", "..."],
  "cta": "from brand.json"
}
```

Slide roles: slide 1 = `hook`, slides 2..N-1 = `insight`, last slide = `cta`
(its headline/body reference the academy and the CTA text from `brand.json`).
Validators enforce: slide count, char limits, exactly one hook first and one cta
last, caption limits, and 5–10 hashtags. Phase 3.7 adds: every non-CTA slide
must carry 2–4 `headline_lines`, a `subhead`, a `panel` and a `tip` (exactly one
`*highlighted phrase*`); the `eyebrow` must differ from the headline; panel
counts are 3–5 items (checklist), 3–4 (grid4) or 2–3 `quote_lines` (quote); in
series mode `step_number` must run 1..N.

**Icon list** (panels reference icons by name; unknown names are coerced to
`star`): `badge, book, bulb, chart_up, check, clipboard, compass, globe,
graduation, medal, paper_plane, plane, quote, shield, star, takeoff, target,
trophy, users, wings`.

### `brand.json` → `contact` + `visual` (Phase 3.5)

```json
{
  "contact": {
    "name": "We One Aviation",
    "tagline": "Guiding Aspirations, Building Careers",
    "email": "info.weoneaviation@gmail.com",
    "phone": "+91-9667370747",
    "address": "C-404, Ramphal Chowk, Dwarka Sector 7, Delhi"
  },
  "visual": {
    "navy": "#0A1F3D", "navy_panel": "#0A1A33", "blue": "#0B3D91",
    "gold": "#E8B33D", "gold_light": "#F5C96B",
    "text": "#FFFFFF", "muted": "#C9D4E5",
    "slide_size": [1080, 1350],
    "logo_path": "assets/fonts/logo.png",
    "headline_font": "Montserrat", "body_font": "Inter"
  }
}
```

- `contact.tagline` renders in small caps under the logo on every slide.

- `contact` renders on the CTA slide (email / phone / address with gold icons).
- `visual` tokens theme the templates via CSS variables; `slide_size` defaults to
  4:5 (1080×1350, best for LinkedIn + Instagram) — set `[1080, 1080]` for square.
- A fixed photographic style suffix is appended to every `image_prompt` in
  `content_writer.py` (not brand.json) so the look stays consistent.
- **Logo:** drop a PNG/SVG at `assets/logo.png` (embedded as a data URI at
  render time). If absent, the compositor renders a gold "WE ONE AVIATION"
  wordmark — slides are never unbranded and never crash.
- **CI note:** in GitHub Actions run `playwright install --with-deps chromium`
  before rendering.

### Post state machine (SQLite)
```
DRAFTED → AWAITING_APPROVAL → APPROVED
        → POSTED_X / POSTED_IG / LINKEDIN_PENDING → COMPLETE
plus REJECTED and FAILED_* (incl. FAILED_X / FAILED_IG, both resumable).
```
Once `APPROVED`, the publish-era states form a fully connected sub-graph
(platforms may succeed/fail in any order and be retried); the per-platform
`*_status` columns are the source of truth for what actually posted, and the
single `state` column is the coarse milestone shown on the dashboard.

---

## Phase 4 publishing lifecycle

A post in state **`APPROVED`** is cleared to go live. Publishing triggers:

- **immediately on ✅ Approve Now** (no schedule set) — the bot publishes and
  reports per-platform results back into the Telegram thread for all admins
  ("🐦 X ✅ · 📸 Instagram ✅ · 💼 LinkedIn PDF sent — upload manually"); or
- **at `scheduled_time`** (set via the ⏰ Schedule button or `run.py
  schedule`) — run `python run.py publish-due` from cron to pick these up
  (it also catches unscheduled APPROVED posts the bot missed).

Each platform success advances the state (`POSTED_X`, `POSTED_IG`,
`LINKEDIN_PENDING`) and records the platform post id (`x_post_id`,
`ig_media_id`) for audit; `SupabaseSync.push_state(...)` runs after every
change. When all enabled platforms are done the post reaches **`COMPLETE`**.
If you add IG keys later, nothing else changes — the next `publish <id>`
simply stops skipping Instagram.

> **Supabase note:** re-run `supabase/schema.sql` once after upgrading to
> Phase 4 — it adds the `scheduled_time` / `x_post_id` / `ig_media_id` /
> `handled_by` columns (the `alter table … if not exists` block is safe on
> existing projects).

---

## Phase 5 — $0 go-live runbook

Phase 5 makes gelio run **unattended at zero cost** with no always-on server:

- **GitHub Actions cron** (`.github/workflows/daily.yml`) runs the daily
  `generate --render --approve` then `publish-due` at 09:00 IST (03:30 UTC).
- **A Telegram webhook** on a free serverless function (`api/telegram.py`,
  Vercel) receives the admin's approval tap minutes/hours later and routes it to
  the *same* approval handlers — Approve-with-no-schedule publishes inline.
- **Supabase is the authoritative shared state** (`GELIO_STATE=supabase`). The
  Actions runner is ephemeral and the webhook is a separate process, so local
  SQLite can't bridge them; every read/write goes to Supabase via PostgREST.

### Architecture

```
                 ┌──────────────── GitHub Actions (cron, free) ───────────────┐
                 │  doctor → generate --render --approve → publish-due         │
                 └───────────────┬───────────────────────────────┬────────────┘
                                 │ writes state                   │ publishes due
                                 ▼                                ▼
   Telegram  ──tap──►  Vercel webhook (api/telegram.py)  ──►  Supabase (posts,
   admin     ◄─DM──    /api/telegram/<secret> + header        used_concepts)
                       routes to ApprovalService.handle_update   ▲   authoritative
                       Approve→publish inline ───────────────────┘   shared state
```

### State backends

`store.py` exposes a `StateStore` interface with two backends, selected by
`GELIO_STATE`:

| `GELIO_STATE` | Backend | Use |
|---------------|---------|-----|
| `sqlite` (default) | `SqliteStore` — local file | local dev |
| `supabase` | `SupabaseStore` — PostgREST over httpx | production (CI + webhook) |

Both enforce the **identical** state machine (`check_transition`), concept
dedup, scheduling, and completion semantics — proven by a parity test suite run
against both backends with HTTP mocked (`tests/test_supabase_store.py`).

### New commands

```bash
python run.py doctor                 # preflight; non-zero exit on a missing requirement
python run.py doctor --no-probe      # skip the live Supabase connectivity probe
python run.py migrate-state          # one-time copy of local SQLite posts → Supabase
python run.py migrate-state --dry-run
python run.py set-webhook https://<app>.vercel.app/api/telegram   # register webhook
python run.py delete-webhook         # remove webhook (re-enable long-poll `bot`)
GELIO_STATE=supabase python run.py generate --render --dry-run     # CI smoke (no write/post)
```

### 🚀 GO-LIVE CHECKLIST (your manual steps)

1. **Rotate the Telegram bot token.** The old token sat in a committed
   `bot.log` on a public repo (now purged from history). In **@BotFather** →
   `/revoke` → copy the new token. Use it everywhere below.
2. **Apply the Supabase schema + bucket.** In the Supabase SQL editor run
   `supabase/schema.sql` (idempotent: creates `posts` with all state columns +
   the `used_concepts` dedup table). Then **Storage → New bucket → `slides`
   (Public)**.
3. **Add GitHub Actions secrets.** Repo → Settings → Secrets and variables →
   Actions → *New repository secret* for each (NOT committed):
   - **Required:** `GROQ_API_KEY`, `CLOUDFLARE_ACCOUNT_ID`,
     `CLOUDFLARE_API_TOKEN`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ADMIN_CHAT_ID`
     (comma-separated for multiple admins), `TELEGRAM_WEBHOOK_SECRET`,
     `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `X_CONSUMER_KEY`,
     `X_CONSUMER_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_TOKEN_SECRET`.
   - **Optional:** `IG_USER_ID`, `IG_ACCESS_TOKEN`, `TOGETHER_API_KEY`.
   - `GELIO_STATE=supabase` is already set in the workflow — no secret needed.
4. **Deploy the webhook to Vercel.** Import the repo (Vercel auto-detects
   `api/telegram.py`; `vercel.json` sets `GELIO_STATE=supabase`). Add the secret
   env vars in Vercel → Settings → Environment Variables: `TELEGRAM_BOT_TOKEN`,
   `TELEGRAM_ADMIN_CHAT_ID`, `TELEGRAM_WEBHOOK_SECRET`, `SUPABASE_URL`,
   `SUPABASE_SERVICE_KEY`, and the `X_*` / `IG_*` publish keys. Deploy; the
   function base URL is `https://<app>.vercel.app/api/telegram`.
   - **Health check:** `GET https://<app>.vercel.app/api/telegram/health` →
     `{"ok":true,"configured":true}`.
5. **Register the webhook.** Locally (with `TELEGRAM_BOT_TOKEN` +
   `TELEGRAM_WEBHOOK_SECRET` in `.env`), pass the **function base** (the secret
   is appended automatically):
   `python run.py set-webhook https://<app>.vercel.app/api/telegram`.
   Telegram will then POST to `https://<app>.vercel.app/api/telegram/<secret>`
   (no doubled `/telegram/` segment).
6. **Migrate existing state once** (only if you have local posts to keep):
   `GELIO_STATE=supabase python run.py migrate-state`.
7. **Enable + test the workflow.** Actions tab → enable workflows → run **gelio
   daily** via *Run workflow* with **dry_run = true** first (smoke test, writes
   nothing), then a real `workflow_dispatch`. Confirm the preview lands in
   Telegram, tap ✅ Approve, and watch the post publish via the webhook. The
   cron then runs daily at 09:00 IST automatically.

**Safety rails (unattended):** only `APPROVED` posts ever publish; a failed
generate writes no partial post; `publish-due` is idempotent (re-runs are
safe); a **DEGRADED** render (too many gradient fallbacks) is flagged in the
admin DM and never auto-publishes — it waits behind the approval gate; and
`doctor` fails the CI run loudly before any work if a requirement is missing.
