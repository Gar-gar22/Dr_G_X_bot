# Agentic Telegram + X tools

Natural-language Telegram chat drives a LangGraph multi-agent system.
X search/post APIs are **tools**. Safe writes can auto-post; risky ones need Telegram Approve.

## Enable

Set on Render / `.env`:

```env
AGENT_ENABLED=true
AI_PROVIDER=openai
OPENAI_API_KEY=sk-...
# Optional:
# AGENT_DRY_RUN=true          # force all writes to Approve
# AGENT_MAX_TOOL_STEPS=8
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
MITL_ENABLED=true
```

OpenAI is recommended for tool-calling. Anthropic / Gemini work via LangChain when configured; orchestrator prefers OpenAI when `AI_PROVIDER=openai`.

Keep **one** Gunicorn worker:

```bash
gunicorn -w 1 -b 0.0.0.0:$PORT --timeout 120 dashboard:app
```

## How to use

1. Message your bot in Telegram (authorized chat only).
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

- [`src/agent/runtime.py`](../src/agent/runtime.py) — `run_agent_turn`, `agent_enabled`, `/status` data  
- [`src/agent/graph.py`](../src/agent/graph.py) — LangGraph specialists  
- [`src/agent/tools/x_tools.py`](../src/agent/tools/x_tools.py) — X tools + MITL queue  
- [`src/telegram_approver.py`](../src/telegram_approver.py) — free-text → agent  

## Troubleshooting

| Issue | Fix |
|-------|-----|
| “AGENT_ENABLED is false” | Set env and redeploy |
| Agent error about OpenAI | Set `OPENAI_API_KEY` / `AI_PROVIDER=openai` |
| No posts, only drafts | Action was risky — Approve in Telegram/Drafts |
| Conflict getUpdates | Only one web service; see TELEGRAM_SETUP.md |
