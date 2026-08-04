# Telegram bot setup — create & connect (Dr_G_X_bot / tweetpy)

This guide walks you from creating a Telegram bot to connecting it with this
app (local `.env` or Render) so you can:

- Get **Approve / Edit / Rewrite / Reject** notifications (man-in-the-loop)
- **Compose** posts by sending a photo + tip (or album) to the bot

Only **your** chat is accepted (`TELEGRAM_CHAT_ID`). Other users are ignored.

---

## What you will need

| Item | Where it goes |
|------|----------------|
| Bot token | `TELEGRAM_BOT_TOKEN` |
| Your numeric chat id | `TELEGRAM_CHAT_ID` |
| MITL on | `MITL_ENABLED=true` |
| App running | Local `dashboard.py` **or** one Render web service |

**Rules that break connection if ignored**

1. Token must look like `123456789:AAH...` — never leave `your_telegram_bot_token`.
2. Chat id must be a number like `5412345678` — never leave `your_telegram_chat_id`.
3. Only **one** process may poll the same bot (don’t run local + Render together).
4. On Render, the web service must stay **awake** (free tier sleep pauses Telegram).

---

## Part A — Create the bot (BotFather)

1. Open Telegram and search for **[@BotFather](https://t.me/BotFather)** (official blue check).
2. Send `/start`, then `/newbot`.
3. Choose a **display name** (e.g. `Dr G X Approver`).
4. Choose a **username** ending in `bot` (e.g. `DrG_X_Approver_bot`).
5. BotFather replies with a token, for example:

   ```text
   7123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw
   ```

6. Copy that token somewhere safe. Treat it like a password.

### Optional BotFather settings

| Command | Suggested |
|---------|-----------|
| `/setprivacy` | **Disable** if you want the bot to see all group messages (not required for private DMs) |
| `/setjoingroups` | Disable unless you use a group |
| `/setdescription` | Short note: “Approve X drafts for Dr G bot” |

For this project, a **private chat with you alone** is enough.

---

## Part B — Get your Chat ID

The bot only accepts messages from the chat id you configure.

### Method 1 — getUpdates (simplest)

1. In Telegram, open **your new bot** and press **Start** (or send `/start`).  
   This creates the first update; required before getUpdates shows your chat.
2. In a browser, open (replace `TOKEN` with your real token):

   ```text
   https://api.telegram.org/botTOKEN/getUpdates
   ```

3. Find a block like:

   ```json
   "chat": { "id": 5412345678, "first_name": "...", "type": "private" }
   ```

4. Copy the **`id` number** — that is `TELEGRAM_CHAT_ID`.

If the page shows `"result": []`:

- Message the bot again (`/start` or `hi`)
- Refresh the URL
- Confirm the token in the URL is correct

### Method 2 — @userinfobot style bots

You can also message a helper bot such as `@userinfobot` or `@getidsbot` and copy your id.  
Use the **same account** that will approve drafts.

### Groups (optional)

If you use a **group**:

1. Add the bot to the group.
2. Send a message in the group.
3. Call `getUpdates` again — group chat ids are usually **negative** (e.g. `-1001234567890`).
4. Put that negative id in `TELEGRAM_CHAT_ID`.

Private chat (positive id) is recommended.

---

## Part C — Connect the app (environment variables)

Set these (Render **Environment** and/or local `.env`):

```env
TELEGRAM_BOT_TOKEN=7123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw
TELEGRAM_CHAT_ID=5412345678
MITL_ENABLED=true
TELEGRAM_POLLING=true
```

| Variable | Meaning |
|----------|---------|
| `TELEGRAM_BOT_TOKEN` | From BotFather |
| `TELEGRAM_CHAT_ID` | Your numeric chat id |
| `MITL_ENABLED` | `true` = drafts need Approve before posting to X |
| `TELEGRAM_POLLING` | Leave `true` so the dashboard process listens for buttons/messages |

Do **not** commit real tokens to Git. `.env` is gitignored; set secrets on Render in the dashboard.

### Local `.env`

1. Copy from `env.example` if needed.
2. Replace the Telegram placeholders with real values (Part A + B).
3. Restart the app:

   ```bash
   pip install -r requirements.txt
   python dashboard.py
   ```

   Or:

   ```bash
   gunicorn -w 1 -b 0.0.0.0:5001 --timeout 120 dashboard:app
   ```

4. In logs you should see something like:

   - `Acquired Telegram polling advisory lock`
   - `Telegram polish polling started ...`

If you see `Telegram approver disabled`, the token/chat id are still missing or placeholders.

### Render

1. Open your Web Service → **Environment**.
2. Add / update:

   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - `MITL_ENABLED` = `true`
3. **Save** and wait for redeploy (or trigger **Manual Deploy**).
4. Open **Logs** and confirm polling started (no `InvalidToken`, no endless `Conflict`).

**Important:** use **only one** poller:

- Either Render **or** local — not both with the same token.
- Keep **one** web service; do not create a second service with the same bot token.
- Start command should stay **one worker**:

  ```bash
  gunicorn -w 1 -b 0.0.0.0:$PORT --timeout 120 dashboard:app
  ```

---

## Part D — Verify it works

### 1. Bot responds to you

1. Open the bot in Telegram.
2. Send `/start`.
3. You should get a help message about compose / Approve / Rewrite.

If nothing happens:

- App not running or asleep (Render free tier)
- Wrong token
- Wrong chat id (bot ignores other users silently)
- Another process conflicting (`Conflict: terminated by other getUpdates`)

### 2. Draft notification (MITL)

1. In the dashboard, run a preview or once cycle **or** wait for cron `/api/run-once`.
2. When a draft is created, Telegram should receive text/image + buttons:
   - **Approve** — post to X  
   - **Edit** — send new text as next message  
   - **Rewrite** — AI rewrite  
   - **Reject** — discard  

You can also approve from **Dashboard → Drafts**.

### 3. Compose from Telegram

1. Send a **photo** with a short **caption tip** (or album up to 4 photos + tip).
2. Or send photo first, then the tip as the next message.
3. Bot expands the tip into a full post draft and sends a preview + buttons.
4. Optional: `/rewrite make it shorter` or `/rewrite 123 make it punchier`.

---

## Part E — How the connection works (architecture)

```text
You  ←→  Telegram servers  ←→  long-polling (dashboard process)
                                      │
                                      ├─ Approve / Edit / Reject / Rewrite
                                      ├─ Photo + tip → draft + media
                                      └─ Notify when bot creates drafts (MITL)
```

- Polling starts when `dashboard.py` / gunicorn loads (module startup).
- Only `TELEGRAM_CHAT_ID` is authorized.
- Postgres advisory lock prevents two dynos from polling at once during redeploys.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `InvalidToken` / token `your_telegram_bot_token` rejected | Set a real BotFather token; remove placeholders |
| `/start` does nothing | Check chat id; send `/start` from the **same** account; check logs for poller |
| `Conflict: terminated by other getUpdates` | Stop local dashboard; ensure only one Render service; wait for old deploy to die |
| Works locally, silent on Render | Free instance asleep — open the site to wake, or use always-on plan |
| Unauthorized / ignored | `TELEGRAM_CHAT_ID` must match your chat exactly (string compare) |
| Buttons work in dashboard Drafts but not Telegram | Poller not running — check `TELEGRAM_POLLING`, logs, single worker |
| `getUpdates` empty | Message the bot first, then refresh the URL |
| Privacy concerns | Anyone who knows the token can control the bot — revoke via BotFather `/revoke` if leaked |

### Revoke / rotate token

1. BotFather → `/revoke` → select bot  
2. Copy the new token  
3. Update `TELEGRAM_BOT_TOKEN` on Render / `.env`  
4. Redeploy / restart  

---

## Quick checklist

- [ ] Created bot with [@BotFather](https://t.me/BotFather)
- [ ] Saved `TELEGRAM_BOT_TOKEN` (`digits:secret`)
- [ ] Sent `/start` to the bot
- [ ] Copied numeric `TELEGRAM_CHAT_ID` from `getUpdates`
- [ ] Set both vars + `MITL_ENABLED=true` in Render or `.env`
- [ ] Only one poller running (`-w 1`, no local+Render overlap)
- [ ] `/start` returns help text
- [ ] Draft notify or photo+tip compose works

---

## Related docs

- [`README.md`](../README.md) — MITL overview, compose features  
- [`AGENT.md`](AGENT.md) — Agentic Telegram + X tools  
- [`DEPLOY.md`](../DEPLOY.md) — VPS / cron  
- [`env.example`](../env.example) — all environment variables  
- Render: keep `gunicorn -w 1` so Telegram polling stays single-process  
