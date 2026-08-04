"""Agent conversation state helpers."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, TypedDict


class AgentState(TypedDict, total=False):
    chat_id: str
    session_id: int
    user_text: str
    media_paths: List[str]
    messages: List[Any]
    reply: str
    pending_approvals: List[Dict[str, Any]]
    dry_run: bool
