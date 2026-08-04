"""Postgres-backed agent session memory."""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def get_or_create_session(db, chat_id: str) -> Dict[str, Any]:
    cursor = db.conn.cursor()
    try:
        cursor.execute(
            """
            SELECT * FROM agent_sessions
            WHERE chat_id = %s AND status = 'active'
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (str(chat_id),),
        )
        row = cursor.fetchone()
        if row:
            return dict(row)
        cursor.execute(
            """
            INSERT INTO agent_sessions (chat_id, status)
            VALUES (%s, 'active')
            RETURNING *
            """,
            (str(chat_id),),
        )
        row = cursor.fetchone()
        db.conn.commit()
        return dict(row)
    except Exception:
        db.conn.rollback()
        raise
    finally:
        cursor.close()


def append_message(
    db,
    session_id: int,
    role: str,
    content: str,
    tool_calls: Any = None,
) -> int:
    cursor = db.conn.cursor()
    try:
        tc = json.dumps(tool_calls) if tool_calls is not None else None
        cursor.execute(
            """
            INSERT INTO agent_messages (session_id, role, content, tool_calls)
            VALUES (%s, %s, %s, %s)
            RETURNING id
            """,
            (session_id, role, content or "", tc),
        )
        row = cursor.fetchone()
        cursor.execute(
            "UPDATE agent_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = %s",
            (session_id,),
        )
        db.conn.commit()
        return int(row["id"]) if row else 0
    except Exception:
        db.conn.rollback()
        raise
    finally:
        cursor.close()


def recent_messages(db, session_id: int, limit: int = 20) -> List[Dict[str, Any]]:
    cursor = db.conn.cursor()
    try:
        cursor.execute(
            """
            SELECT * FROM agent_messages
            WHERE session_id = %s
            ORDER BY id DESC
            LIMIT %s
            """,
            (session_id, limit),
        )
        rows = list(cursor.fetchall() or [])
        rows.reverse()
        return [dict(r) for r in rows]
    finally:
        cursor.close()


def create_action(
    db,
    *,
    session_id: Optional[int],
    tool_name: str,
    args: Dict[str, Any],
    risk_level: str,
    status: str,
    draft_id: Optional[int] = None,
    result: Optional[str] = None,
) -> int:
    cursor = db.conn.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO agent_actions
                (session_id, tool_name, args_json, risk_level, status, draft_id, result)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                session_id,
                tool_name,
                json.dumps(args or {}),
                risk_level,
                status,
                draft_id,
                result,
            ),
        )
        row = cursor.fetchone()
        db.conn.commit()
        return int(row["id"]) if row else 0
    except Exception:
        db.conn.rollback()
        raise
    finally:
        cursor.close()


def update_action(
    db,
    action_id: int,
    *,
    status: Optional[str] = None,
    draft_id: Optional[int] = None,
    result: Optional[str] = None,
) -> None:
    cursor = db.conn.cursor()
    try:
        sets = []
        vals: List[Any] = []
        if status is not None:
            sets.append("status = %s")
            vals.append(status)
        if draft_id is not None:
            sets.append("draft_id = %s")
            vals.append(draft_id)
        if result is not None:
            sets.append("result = %s")
            vals.append(result)
        if not sets:
            return
        sets.append("updated_at = CURRENT_TIMESTAMP")
        vals.append(action_id)
        cursor.execute(
            f"UPDATE agent_actions SET {', '.join(sets)} WHERE id = %s",
            tuple(vals),
        )
        db.conn.commit()
    except Exception:
        db.conn.rollback()
        raise
    finally:
        cursor.close()


def get_action_by_draft(db, draft_id: int) -> Optional[Dict[str, Any]]:
    cursor = db.conn.cursor()
    try:
        cursor.execute(
            "SELECT * FROM agent_actions WHERE draft_id = %s ORDER BY id DESC LIMIT 1",
            (draft_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None
    finally:
        cursor.close()


def list_recent_actions(db, limit: int = 50) -> List[Dict[str, Any]]:
    cursor = db.conn.cursor()
    try:
        cursor.execute(
            """
            SELECT * FROM agent_actions
            ORDER BY id DESC
            LIMIT %s
            """,
            (limit,),
        )
        return [dict(r) for r in (cursor.fetchall() or [])]
    finally:
        cursor.close()


def list_recent_sessions(db, limit: int = 30) -> List[Dict[str, Any]]:
    cursor = db.conn.cursor()
    try:
        cursor.execute(
            """
            SELECT * FROM agent_sessions
            ORDER BY updated_at DESC NULLS LAST, id DESC
            LIMIT %s
            """,
            (limit,),
        )
        return [dict(r) for r in (cursor.fetchall() or [])]
    finally:
        cursor.close()


def get_user_prefs(db, chat_id: str) -> Dict[str, Any]:
    cursor = db.conn.cursor()
    try:
        cursor.execute(
            "SELECT * FROM agent_user_prefs WHERE chat_id = %s LIMIT 1",
            (str(chat_id),),
        )
        row = cursor.fetchone()
        if not row:
            return {}
        prefs = dict(row)
        raw = prefs.get("prefs_json")
        if isinstance(raw, str):
            try:
                prefs["prefs"] = json.loads(raw)
            except Exception:
                prefs["prefs"] = {}
        else:
            prefs["prefs"] = raw or {}
        return prefs
    finally:
        cursor.close()
