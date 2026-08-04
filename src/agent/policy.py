"""Risk policy for agent X tools — safe auto-exec vs MITL approval."""
from __future__ import annotations

import os
from typing import Any, Dict, Optional, Tuple

from ..quality_safety import (
    check_text_quality,
    merge_safety_config,
    validate_before_post,
)


READ_TOOLS = frozenset(
    {
        "x_search_tweets",
        "x_home_timeline",
        "x_get_mentions",
        "draft_tweet_text",
    }
)

WRITE_TOOLS = frozenset(
    {
        "x_tweet",
        "x_reply",
        "x_quote",
        "x_thread",
        "x_upload_media",
    }
)


def agent_dry_run() -> bool:
    return os.getenv("AGENT_DRY_RUN", "").lower() in ("1", "true", "yes", "on")


def max_tool_steps() -> int:
    try:
        return max(1, int(os.getenv("AGENT_MAX_TOOL_STEPS", "8")))
    except ValueError:
        return 8


def classify_tool_risk(
    tool_name: str,
    args: Optional[Dict[str, Any]] = None,
    *,
    db=None,
    safety: Optional[Dict[str, Any]] = None,
    writes_this_turn: int = 0,
) -> Tuple[str, str]:
    """
    Return (risk_level, reason) where risk_level is 'safe' | 'risky' | 'blocked'.
    """
    args = args or {}
    name = (tool_name or "").strip()

    if name in READ_TOOLS or name == "draft_tweet_text":
        return "safe", "read_or_draft"

    if name not in WRITE_TOOLS:
        return "risky", "unknown_tool"

    if agent_dry_run():
        return "risky", "AGENT_DRY_RUN forces approval"

    safety = merge_safety_config(safety)
    text = (args.get("text") or args.get("tweet_text") or "").strip()
    texts = args.get("texts") or args.get("thread_texts")

    if name == "x_thread" or (isinstance(texts, list) and len(texts) >= 3):
        return "risky", "thread_or_multi_part"

    if name == "x_quote" and (args.get("media_paths") or args.get("media_ids")):
        return "risky", "quote_with_media"

    if writes_this_turn >= 1 and name in ("x_reply", "x_tweet", "x_quote"):
        return "risky", "bulk_writes_same_turn"

    if text:
        ok, reason = check_text_quality(text, safety)
        if not ok:
            return "blocked", reason or "quality_gate"

    if db is not None and text and name in ("x_tweet", "x_reply", "x_quote"):
        ok, reason = validate_before_post(db, text, safety)
        if not ok:
            # Near limit or blocked → require approval or hard block
            if "limit" in (reason or "").lower():
                return "risky", reason
            return "blocked", reason or "safety_block"

    # Single short original tweet / reply — safe auto
    if name in ("x_tweet", "x_reply") and text and len(text) <= 280:
        return "safe", "single_short_post"

    if name == "x_upload_media":
        return "safe", "media_upload_only"

    return "risky", "default_write_requires_approval"
