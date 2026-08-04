# Auto-Reply X Bot

Automated X/Twitter engagement bot with **Telegram man-in-the-loop**, multi-provider AI (Gemini / OpenAI / Anthropic), editable niche system prompts, and an admin dashboard for credentials, drafts, and activity records.

## Features

- **Timeline + keyword engagement**: Fetch candidates, filter, generate replies/quotes
- **Man-in-the-loop (default)**: Drafts wait for Approve / Edit / Reject in Telegram or the dashboard before posting
- **Multi-AI**: Gemini, OpenAI, or Anthropic via a shared provider layer
- **Niche system prompts**: Admin-editable profiles (e.g. `web3`, `blockchain`, `default`)
- **Original tweets & threads**: Generated on schedule (also go through MITL when enabled)
- **Admin dashboard**: Password login, draft queue, full records, credentials, AI settings, **media library**
- **PostgreSQL state**: Tracks replies, tweets, quotes, drafts, AI settings, media attachments

## Prerequisites

- Python 3.8+
- PostgreSQL
- X Developer App (Read + Write)
- At least one AI API key (optional but recommended)
- Telegram bot token + your chat id (for MITL notifications) — see [`docs/TELEGRAM_SETUP.md`](docs/TELEGRAM_SETUP.md)
- Optional agent mode — [`docs/AGENT.md`](docs/AGENT.md) (`AGENT_ENABLED=true`)

## Quick start

```bash
pip install -r requirements.txt
cp env.example .env
# Edit .env: ADMIN_*, DB_*, X_*, AI keys, TELEGRAM_*
python setup.py          # optional interactive config.json
python dashboard.py      # http://localhost:5001
```

Login with `ADMIN_EMAIL` / `ADMIN_PASSWORD`. Use **Drafts** to approve content, **AI Settings** to edit system instructions.

### CLI

```bash
python main.py --run-once   # one collect+generate cycle (creates drafts if MITL on)
python main.py              # in-process morning/evening scheduler
```

## Configuration

| Source | Role |
|--------|------|
| `.env` | Secrets, admin login, Telegram, DB, AI keys |
| `config.json` | Keywords, schedule, filters, MITL flag, reply templates |
| PostgreSQL `ai_prompt_profiles` | Niche system instructions |
| PostgreSQL `ai_provider_settings` | Provider models / default |

### Important env vars

See [`env.example`](env.example).

- `MITL_ENABLED=true` (default via config) — drafts instead of live posts
- `AI_PROVIDER=gemini|openai|anthropic`
- `AI_PROMPT_PROFILE=web3` — active niche name
- `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`
- `ADMIN_EMAIL` + `ADMIN_PASSWORD`
- `CRON_SECRET` — for `/api/run-once` cron hook

## How MITL works

1. Bot collects & generates content
2. Saves a **pending draft** in PostgreSQL and notifies Telegram
3. You **Approve** (posts to X), **Edit** then approve, or **Reject**
4. Dashboard **Drafts** page does the same without Telegram

Set `man_in_the_loop.enabled` to `false` in Automation settings (or `MITL_ENABLED=false`) for auto-post (not recommended).

## Dashboard routes

| Path | Purpose |
|------|---------|
| `/login` | Admin email + password |
| `/dashboard` | Overview + pending draft count |
| `/records/drafts` | Approve / edit / reject |
| `/records/replies` | All reply history |
| `/records/tweets` | Posted tweets & threads |
| `/records/quotes` | Quote tweets |
| `/settings/ai` | Provider + niche system prompts |
| `/settings/media` | Upload image library (JPG/PNG/GIF/WEBP) |
| `/settings/credentials` | X + AI API keys (masked) |
| `/api/run-once?secret=...` | Cron trigger |

### Posting with images

1. Open **Media** and upload images (max 5 MB each).
2. Open **Drafts**, select up to **4** images on a pending draft.
3. **Approve & Post** — images are uploaded to X and attached to the reply/tweet/quote (first tweet of a thread).

### Telegram compose (photo + tip)

1. Message your Telegram bot (authorized `TELEGRAM_CHAT_ID` only).
2. Send a **photo with a caption tip**, an **album (up to 4 photos)** with a tip on one caption, or photo first then tip.
3. The bot expands your tip into a full post, attaches the image(s), and sends an **image preview** plus **Approve / Edit / Rewrite / Reject**.
4. `/rewrite [guidance]` rewrites the last compose draft with AI; `/rewrite 123 make it shorter` targets draft #123.
5. Approve posts to X. Same draft appears in the dashboard **Drafts** page.

### Safety & quality

Dashboard **Safety** page controls:

- **Daily / monthly post limits** (replies + tweets + quotes counted)
- **Max pending drafts**
- **Text gates**: min/max length, hashtag/link caps, ALL-CAPS, duplicates
- **Risky tip refusal** for Telegram compose (guarantees, seed phrases, etc.)
- Custom block phrases + recent block log (`safety_events`)

Env overrides: `SAFETY_ENABLED`, `DAILY_POST_LIMIT`, `MONTHLY_POST_LIMIT`.

## Deploy

See [`DEPLOY.md`](DEPLOY.md) for Nginx + Gunicorn + cron. Keep **one** Gunicorn worker (`-w 1`) so Telegram polling and bot runs stay single-process.

## Safety

- Start with low `max_replies_per_run` and MITL on
- Respect X rate limits and automation rules
- Keep secrets out of git (`.env`, `config.json` are gitignored)

## License / Disclaimer

Personal project. Use at your own risk and in compliance with X and AI provider terms.
