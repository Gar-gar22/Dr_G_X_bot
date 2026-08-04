"""Public agent runtime entrypoints."""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Stable chat_id for admin dashboard agent chat (Telegram uses numeric chat ids).
DASHBOARD_CHAT_ID = "dashboard"


def agent_enabled() -> bool:
    return os.getenv("AGENT_ENABLED", "false").lower() in ("1", "true", "yes", "on")


def _load_stack():
    from ..config import Config
    from ..database import Database
    from ..x_api import XAPI

    config = Config()
    db = Database()
    x_api = None
    try:
        creds = config.get_x_api_credentials()
        if creds.get("consumer_key") and creds.get("access_token"):
            x_api = XAPI(creds)
    except Exception as e:
        logger.warning(f"X API init for agent failed: {e}")
    return config, db, x_api


def run_agent_turn(
    chat_id: str,
    user_text: str,
    *,
    media_paths: Optional[List[str]] = None,
    telegram=None,
    include_writes: bool = True,
) -> Dict[str, Any]:
    """
    Run one agent turn for a chat source (Telegram chat_id or DASHBOARD_CHAT_ID).
    Returns {ok, reply, pending_approvals, session_id, error?}.
    """
    if not agent_enabled():
        return {"ok": False, "error": "AGENT_ENABLED is false"}

    text = (user_text or "").strip()
    if not text and not media_paths:
        return {"ok": False, "error": "Empty message"}

    if media_paths and not text:
        text = (
            "User sent image(s). Propose an on-brand X post using those images "
            "when posting tools are used; for now describe a caption suggestion."
        )

    config = db = x_api = None
    try:
        from . import memory as agent_memory
        from .graph import run_orchestrator
        from .tools.x_tools import CTX, reset_context

        config, db, x_api = _load_stack()
        session = agent_memory.get_or_create_session(db, str(chat_id))
        session_id = int(session["id"])
        agent_memory.append_message(db, session_id, "user", text)

        prefs = agent_memory.get_user_prefs(db, str(chat_id))
        prefs_note = ""
        if prefs.get("prefs"):
            prefs_note = str(prefs.get("prefs"))[:800]

        history = agent_memory.recent_messages(db, session_id, limit=16)

        reset_context(
            db=db,
            x_api=x_api,
            config=config,
            session_id=session_id,
            telegram=telegram,
            media_paths=media_paths or [],
        )

        result = run_orchestrator(
            text,
            history=history[:-1],  # exclude just-appended user msg duplicate
            include_writes=include_writes,
            prefs_note=prefs_note,
        )
        reply = (result.get("reply") or "").strip() or "OK."
        pending = list(CTX.pending_approvals)
        if pending:
            lines = [
                f"Pending Approve: draft #{p.get('draft_id')} ({p.get('reason')})"
                for p in pending
            ]
            reply = reply + "\n\n" + "\n".join(lines)

        agent_memory.append_message(
            db,
            session_id,
            "assistant",
            reply,
            tool_calls={"path": result.get("path"), "pending": pending},
        )
        return {
            "ok": True,
            "reply": reply[:4000],
            "pending_approvals": pending,
            "session_id": session_id,
            "path": result.get("path"),
        }
    except Exception as e:
        logger.error(f"run_agent_turn failed: {e}", exc_info=True)
        return {"ok": False, "error": str(e), "reply": f"Agent error: {e}"}
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass


def get_agent_status(db=None) -> Dict[str, Any]:
    """Status blurb for /status command."""
    from .policy import agent_dry_run, max_tool_steps
    from ..quality_safety import get_usage_stats, merge_safety_config

    own_db = False
    try:
        if db is None:
            from ..database import Database

            db = Database()
            own_db = True
        safety = merge_safety_config()
        try:
            from ..config import Config

            safety = Config().get_safety_config()
        except Exception:
            pass
        today = -1
        try:
            today = int(get_usage_stats(db).get("posts_today", -1))
        except Exception:
            today = -1

        from . import memory as agent_memory

        actions = agent_memory.list_recent_actions(db, limit=5)
        return {
            "enabled": agent_enabled(),
            "dry_run": agent_dry_run(),
            "max_tool_steps": max_tool_steps(),
            "posts_today": today,
            "daily_limit": safety.get("daily_post_limit"),
            "recent_actions": [
                {
                    "id": a.get("id"),
                    "tool": a.get("tool_name"),
                    "status": a.get("status"),
                    "risk": a.get("risk_level"),
                }
                for a in actions
            ],
        }
    finally:
        if own_db and db is not None:
            try:
                db.close()
            except Exception:
                pass
