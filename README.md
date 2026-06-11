# gelio — Content Intelligence Core (Phase 1)

`gelio` is a production-grade, **free-tier-only** AI content agent for the
aviation training academy **We One Aviation**. Each run maps a psychology
principle (Decision Fatigue, Impostor Syndrome, Spotlight Effect, …) onto
aviation / DGCA / pilot-training life, writes a full 8–10 slide carousel script
plus per-platform captions, and saves everything as **schema-validated JSON
artifacts** that later phases consume unchanged.

Phase 1 produces clean, schema-locked outputs and reaches the `DRAFTED` state.
Image generation, Telegram approval, and auto-posting come in later phases.

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
| `TELEGRAM_ADMIN_CHAT_ID` | for approval | —              | Only this chat may drive the bot (Phase 3).      |
| `SUPABASE_URL`    | no       | —                          | Set to enable dashboard sync; blank = disabled.  |
| `SUPABASE_SERVICE_KEY` | no  | —                          | service_role key (server-side only).             |
| `SUPABASE_BUCKET` | no       | `slides`                   | Public storage bucket for slides + PDF.          |

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
```

### Phase 3 — Telegram approval gate

```bash
# Generate → render → send the album + captions + buttons to the admin.
python run.py generate --render --approve

# Send an already-rendered post for approval.
python run.py send-approval 2026-06-11-decision-fatigue

# Run the long-poll listener that handles Approve/Reject/Regenerate taps.
python run.py bot
```

Flow: the admin gets the slides as a Telegram album, the three captions +
hashtags, and inline buttons. **✅ Approve** → `APPROVED`, **❌ Reject** →
`REJECTED`, **🔄 Regenerate** → the bot asks (via ForceReply) for a topic (or
`auto`); it rejects the current post and renders a fresh one on that topic,
linked by `parent_id`. The bot only obeys `TELEGRAM_ADMIN_CHAT_ID`, button taps
are idempotent (double-tap → "already handled"), and regenerations are capped at
3/day. Nothing publishes without an explicit Approve.

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
   `message.chat.id`. Put it in `TELEGRAM_ADMIN_CHAT_ID` (the bot ignores every
   other sender).
3. Run `python run.py bot` to handle button taps, and
   `python run.py generate --render --approve` to push a post for review.

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
gelio/approval.py       Telegram approval gate: preview, buttons, regen (Phase 3)
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
    {"index": 1, "role": "hook", "headline": "<= 60 chars",
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
last, caption limits, and 5–10 hashtags.

### `brand.json` → `contact` + `visual` (Phase 3.5)

```json
{
  "contact": {
    "name": "We One Aviation",
    "email": "info.weoneaviation@gmail.com",
    "phone": "+91-9667370747",
    "address": "C-404, Ramphal Chowk, Dwarka Sector 7, Delhi"
  },
  "visual": {
    "navy": "#0A1F3D", "navy_panel": "#0A1A33", "blue": "#0B3D91",
    "gold": "#E8B33D", "text": "#FFFFFF", "muted": "#C9D4E5",
    "slide_size": [1080, 1350],
    "logo_path": "assets/logo.png",
    "headline_font": "Montserrat", "body_font": "Inter"
  }
}
```

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
plus REJECTED and FAILED_* states.
```
Phase 1 only reaches `DRAFTED`; the full table and transitions exist now so
later phases never migrate the schema.

---

## Phase 4 hand-off (publisher)

Phase 3 hands the publisher a clean queue: a post in state **`APPROVED`** is
cleared to go live. Everything needed is on disk + DB, keyed by `<id>`:

- **`output/<id>/slides/slide_1.png … slide_N.png`** — final 1080×1350 slides.
- **`output/<id>/carousel.pdf`** — the LinkedIn-ready carousel.
- **`output/<id>/content.json`** — `captions` (linkedin / instagram / x) and
  `hashtags` to attach when posting.
- SQLite `posts.<id>` is `APPROVED` (with `rendered_at`, and `parent_id` /
  `regeneration_count` if it came from a regenerate).
- If Supabase is configured, the same row + public asset URLs are mirrored to
  `public.posts` for the dashboard.

Phase 4 should poll for `APPROVED` posts, publish per platform, and advance the
existing state machine: `APPROVED → POSTED_X / POSTED_IG / LINKEDIN_PENDING →
COMPLETE` (or `FAILED_POST`) via `Store.transition(...)`, calling
`SupabaseSync.push_state(...)` after each change. The `PostState` machine and all
columns already exist, so **no schema migration is required**.
