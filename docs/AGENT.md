# Agentic chat + X tools

Natural-language chat drives a LangGraph multi-agent system from **two sources**:

| Source | Entry |
|--------|--------|
| Telegram | Free-text in the authorized chat |
| Admin dashboard | **Chat** page (`/agent/chat`) |

X search/post APIs are **tools**. Safe writes can auto-post; risky ones need Approve (Telegram buttons and/or **Drafts**).

Sessions are keyed by `chat_id`: Telegram uses the numeric chat id; the dashboard uses `dashboard` (isolated history). Both share the same agent tools and MITL drafts.

**Provider switching:** Choose the default provider under **Credentials** or **AI Settings** (saved to DB). That choice drives the agent — Render’s `AI_PROVIDER` env only seeds the default when no provider is stored yet. Put a real key for each provider you want to test (`OPENAI_API_KEY`, `AGENTROUTER_API_KEY`, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`).

## Enable

Set on Render / `.env`:

```env
AGENT_ENABLED=true
AI_PROVIDER=agentrouter
AGENTROUTER_API_KEY=sk-...
# Allowed models: gpt-5.6-sol | claude-opus-4-8 | claude-opus-5
# AGENTROUTER_MODEL=gpt-5.6-sol
# Optional: AGENTROUTER_BASE_URL=https://agentrouter.org/v1
# Or use direct OpenAI instead:
# AI_PROVIDER=openai
# OPENAI_API_KEY=sk-...
# AGENT_DRY_RUN=true          # force all writes to Approve
# AGENT_MAX_TOOL_STEPS=8
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
MITL_ENABLED=true
```

**AgentRouter** ([agentrouter.org](https://agentrouter.org)) is an OpenAI-compatible gateway (`https://agentrouter.org/v1`). Create a token at https://agentrouter.org/console/token, then set `AI_PROVIDER=agentrouter` and `AGENTROUTER_API_KEY` (alias: `AGENT_ROUTER_TOKEN`).

Allowed AgentRouter models for this bot: `gpt-5.6-sol` (default), `claude-opus-4-8`, `claude-opus-5`. Pick one under Credentials / AI Settings.

AgentRouter rejects generic OpenAI SDK clients (`unauthorized client detected`). This app sends Codex-compatible `Originator` / `User-Agent` headers on AgentRouter requests so chat and tweet generation work.

OpenAI or AgentRouter are recommended for tool-calling. Anthropic / Gemini work via LangChain when configured.

Keep **one** Gunicorn worker:

```bash
gunicorn -w 1 -b 0.0.0.0:$PORT --timeout 120 dashboard:app
```

## How to use

### Dashboard
1. Open **Chat** in the admin sidebar (`/agent/chat`).
2. Type naturally (same prompts as Telegram).
3. Use **New chat** to archive the current dashboard session and start fresh.
4. Risky writes → **Drafts** (and Telegram Approve if Telegram is configured).

### Telegram
1. Message your bot (authorized chat only).
2. `/start` — help  
3. `/status` — agent flag, daily post count, recent tool actions  
4. Free text examples:
   - `search web3 on my timeline`
   - `draft a tweet about SOL fees`
   - `reply to tweet 123456 saying thanks`
5. If a write is **risky**, you get Approve / Reject (same MITL buttons as drafts).
6. Dashboard → **Agent** shows sessions and actions; **Drafts** for pending approvals.

When `AGENT_ENABLED=false`, classic photo+tip compose and batch MITL still work.

## Architecture

| Piece | Role |
|-------|------|
| Research agent | Read-only X search / timeline |
| Writer agent | Draft text only (`draft_tweet_text`) |
| Orchestrator / Poster | Full tool set including writes |
| `src/agent/policy.py` | Safe vs risky vs blocked |
| Postgres `agent_*` tables | Sessions, messages, actions |

### Safe vs risky (summary)

- **Safe:** search, timeline, draft text, single short tweet/reply under budgets  
- **Risky:** threads (3+), quote+media, bulk writes in one turn, near rate limits, `AGENT_DRY_RUN`  
- **Blocked:** quality/safety gate failures  

## Code map

- [`src/agent/runtime.py`](../src/agent/runtime.py) — `run_agent_turn`, `DASHBOARD_CHAT_ID`, `/status` data  
- [`src/agent/graph.py`](../src/agent/graph.py) — LangGraph specialists  
- [`src/agent/tools/x_tools.py`](../src/agent/tools/x_tools.py) — X tools + MITL queue  
- [`src/telegram_approver.py`](../src/telegram_approver.py) — free-text → agent  
- Dashboard — `/agent/chat`, `POST /api/agent/chat`, `GET /api/agent/chat/history`
## Troubleshooting

| Issue | Fix |
|-------|-----|
| “AGENT_ENABLED is false” | Set env and redeploy |
| Agent error `model_dump` / AgentRouter | Almost always means base URL is `https://agentrouter.org` **without** `/v1`, or `OPENAI_BASE_URL` points there. Set `https://agentrouter.org/v1` (Credentials + env). Redeploy latest. |
| AgentRouter `unauthorized client` / 401 | Redeploy latest build (Codex client headers). Confirm `AGENTROUTER_API_KEY` is a real AgentRouter token, not an OpenAI key. |
| AgentRouter WAF HTML / `aliyun_waf` | AgentRouter's edge blocked the **server IP** (common on Render). Switch provider (Gemini/OpenAI), ask AgentRouter to allowlist egress, or proxy. |
| No posts, only drafts | Action was risky — Approve in Telegram/Drafts |
| Conflict getUpdates | Only one web service; see TELEGRAM_SETUP.md |
