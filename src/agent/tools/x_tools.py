"""X (Twitter) LangChain tools for the agent."""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from .. import memory as agent_memory
from ..policy import classify_tool_risk

logger = logging.getLogger(__name__)


class _ToolContext:
    """Mutable context shared by tool closures for one agent turn."""

    def __init__(self):
        self.db = None
        self.x_api = None
        self.config = None
        self.session_id: Optional[int] = None
        self.writes_this_turn = 0
        self.pending_approvals: List[Dict[str, Any]] = []
        self.telegram = None  # TelegramApprover for notify
        self.media_paths: List[str] = []


CTX = _ToolContext()


def reset_context(
    *,
    db,
    x_api,
    config,
    session_id: Optional[int],
    telegram=None,
    media_paths: Optional[List[str]] = None,
) -> None:
    CTX.db = db
    CTX.x_api = x_api
    CTX.config = config
    CTX.session_id = session_id
    CTX.writes_this_turn = 0
    CTX.pending_approvals = []
    CTX.telegram = telegram
    CTX.media_paths = list(media_paths or [])


def _safety():
    if CTX.config:
        return CTX.config.get_safety_config()
    return None


def _record(tool_name: str, args: dict, risk: str, status: str, draft_id=None, result=None):
    if CTX.db is None:
        return 0
    try:
        return agent_memory.create_action(
            CTX.db,
            session_id=CTX.session_id,
            tool_name=tool_name,
            args=args,
            risk_level=risk,
            status=status,
            draft_id=draft_id,
            result=result,
        )
    except Exception as e:
        logger.warning(f"Failed to record agent action: {e}")
        return 0


def _queue_mitl(
    *,
    tool_name: str,
    args: dict,
    kind: str,
    text: str,
    target_tweet_id: Optional[str] = None,
    thread_texts: Optional[List[str]] = None,
    risk_reason: str = "",
) -> str:
    """Create content_draft + pending agent_action and notify Telegram."""
    draft_id = None
    if CTX.db is not None:
        data = {
            "kind": kind,
            "status": "pending",
            "generated_text": text,
            "target_tweet_id": target_tweet_id,
            "source": "agent",
            "keyword": risk_reason[:200] if risk_reason else "agent_action",
            "thread_texts": json.dumps(thread_texts) if thread_texts else None,
            "provider": "agent",
            "model": "langgraph",
        }
        draft_id = CTX.db.create_draft(data)
        if CTX.media_paths and draft_id:
            # Attach already-known media assets if stored as ids later — skip paths here
            pass
        action_id = _record(
            tool_name,
            args,
            "risky",
            "pending_approval",
            draft_id=draft_id,
            result=risk_reason,
        )
        pending = {
            "action_id": action_id,
            "draft_id": draft_id,
            "tool_name": tool_name,
            "reason": risk_reason,
            "text": text,
        }
        CTX.pending_approvals.append(pending)
        if CTX.telegram and draft_id:
            try:
                draft = CTX.db.get_draft(draft_id)
                CTX.telegram.notify_draft(draft_id, draft or data)
            except Exception as e:
                logger.warning(f"notify_draft failed: {e}")
        return json.dumps(
            {
                "status": "pending_approval",
                "draft_id": draft_id,
                "action_id": action_id,
                "reason": risk_reason,
                "message": "Queued for Telegram Approve before posting.",
            }
        )
    return json.dumps({"status": "error", "error": "database unavailable"})


# --- Tool argument schemas ---


class SearchInput(BaseModel):
    query: str = Field(description="X search query (keywords, from:user, etc.)")
    count: int = Field(default=10, description="Max tweets to return (1-25)")


class TimelineInput(BaseModel):
    count: int = Field(default=15, description="How many home timeline tweets (1-40)")


class MentionsInput(BaseModel):
    count: int = Field(default=15, description="Recent mention-like tweets to scan from timeline")


class DraftTextInput(BaseModel):
    brief: str = Field(description="What the tweet/reply should say")
    style: str = Field(default="clear and human", description="Optional style notes")


class TweetInput(BaseModel):
    text: str = Field(description="Tweet text, max 280 chars")


class ReplyInput(BaseModel):
    text: str = Field(description="Reply text, max 280 chars")
    tweet_id: str = Field(description="Target tweet id to reply to")


class QuoteInput(BaseModel):
    text: str = Field(description="Quote commentary")
    tweet_id: str = Field(description="Tweet id to quote")


class ThreadInput(BaseModel):
    texts: List[str] = Field(description="Ordered list of tweets in the thread")


def _x_search_tweets(query: str, count: int = 10) -> str:
    if not CTX.x_api:
        return json.dumps({"error": "X API not configured"})
    count = max(1, min(int(count or 10), 25))
    tweets = CTX.x_api.search_tweets(query, count=count)
    slim = [
        {
            "id": t.get("id"),
            "text": (t.get("text") or "")[:280],
            "author": t.get("author_username"),
        }
        for t in (tweets or [])[:count]
    ]
    _record("x_search_tweets", {"query": query, "count": count}, "safe", "executed", result=str(len(slim)))
    return json.dumps({"tweets": slim, "count": len(slim)})


def _x_home_timeline(count: int = 15) -> str:
    if not CTX.x_api:
        return json.dumps({"error": "X API not configured"})
    count = max(1, min(int(count or 15), 40))
    tweets = CTX.x_api.get_home_timeline(count=count)
    slim = [
        {
            "id": t.get("id"),
            "text": (t.get("text") or "")[:280],
            "author": t.get("author_username"),
        }
        for t in (tweets or [])[:count]
    ]
    _record("x_home_timeline", {"count": count}, "safe", "executed", result=str(len(slim)))
    return json.dumps({"tweets": slim, "count": len(slim)})


def _x_get_mentions(count: int = 15) -> str:
    """Approximate mentions by scanning timeline for @ replies / mentions of self."""
    if not CTX.x_api:
        return json.dumps({"error": "X API not configured"})
    count = max(1, min(int(count or 15), 40))
    me = None
    try:
        info = CTX.x_api.get_user_info() if hasattr(CTX.x_api, "get_user_info") else None
        me = (info or {}).get("username") if info else None
    except Exception:
        me = None
    tweets = CTX.x_api.get_home_timeline(count=min(80, count * 4))
    hits = []
    for t in tweets or []:
        text = t.get("text") or ""
        if me and f"@{me}".lower() in text.lower():
            hits.append(t)
        elif t.get("in_reply_to_status_id"):
            hits.append(t)
        if len(hits) >= count:
            break
    slim = [
        {
            "id": t.get("id"),
            "text": (t.get("text") or "")[:280],
            "author": t.get("author_username"),
        }
        for t in hits[:count]
    ]
    _record("x_get_mentions", {"count": count}, "safe", "executed", result=str(len(slim)))
    return json.dumps({"tweets": slim, "count": len(slim), "note": "Best-effort from timeline"})


def _draft_tweet_text(brief: str, style: str = "clear and human") -> str:
    """Generate draft text via AI without posting."""
    from ...ai_provider import generate_with_resolved_provider

    try:
        cfg = CTX.config.config if CTX.config and hasattr(CTX.config, "config") else None
        text, _, _ = generate_with_resolved_provider(
            (
                "Write a single X/Twitter post. Stay under 280 characters. "
                f"Style: {style}. No hashtag spam."
            ),
            brief,
            db=CTX.db,
            config=cfg,
            max_tokens=200,
        )
        text = (text or "").strip()[:280]
        if not text:
            return json.dumps({"error": "AI returned empty draft"})
        _record("draft_tweet_text", {"brief": brief}, "safe", "executed", result=text[:100])
        return json.dumps({"draft": text, "chars": len(text)})
    except Exception as e:
        return json.dumps({"error": str(e)})


def _gated_write(tool_name: str, args: dict, kind: str, text: str, **kwargs) -> str:
    risk, reason = classify_tool_risk(
        tool_name,
        args,
        db=CTX.db,
        safety=_safety(),
        writes_this_turn=CTX.writes_this_turn,
    )
    if risk == "blocked":
        _record(tool_name, args, "blocked", "rejected", result=reason)
        return json.dumps({"status": "blocked", "reason": reason})

    if risk == "risky":
        return _queue_mitl(
            tool_name=tool_name,
            args=args,
            kind=kind,
            text=text,
            target_tweet_id=kwargs.get("target_tweet_id"),
            thread_texts=kwargs.get("thread_texts"),
            risk_reason=reason,
        )

    # Safe auto-exec
    if not CTX.x_api:
        return json.dumps({"error": "X API not configured"})
    try:
        posted_id = None
        if tool_name == "x_tweet":
            posted_id = CTX.x_api.post_tweet(text)
            if posted_id:
                CTX.db.mark_tweet_posted(str(posted_id), text)
        elif tool_name == "x_reply":
            tid = kwargs.get("target_tweet_id")
            posted_id = CTX.x_api.post_reply(text, tid)
            if posted_id:
                CTX.db.mark_tweet_replied(tid, str(posted_id), source="agent")
        elif tool_name == "x_quote":
            tid = kwargs.get("target_tweet_id")
            posted_id = CTX.x_api.quote_tweet(text, tid)
            if posted_id:
                CTX.db.mark_quote_retweet_posted(str(posted_id), tid, text)
        elif tool_name == "x_thread":
            texts = kwargs.get("thread_texts") or []
            ids = CTX.x_api.post_thread(texts)
            if ids:
                CTX.db.mark_thread_posted(ids, texts)
                posted_id = ids[0]
        if not posted_id:
            _record(tool_name, args, "safe", "rejected", result="post_failed")
            return json.dumps({"status": "error", "error": "X API returned no id"})
        CTX.writes_this_turn += 1
        _record(
            tool_name,
            args,
            "safe",
            "executed",
            result=str(posted_id),
        )
        return json.dumps({"status": "posted", "tweet_id": str(posted_id)})
    except Exception as e:
        logger.error(f"Write tool {tool_name} failed: {e}", exc_info=True)
        _record(tool_name, args, "safe", "rejected", result=str(e)[:300])
        return json.dumps({"status": "error", "error": str(e)})


def _x_tweet(text: str) -> str:
    args = {"text": text}
    return _gated_write("x_tweet", args, "tweet", text)


def _x_reply(text: str, tweet_id: str) -> str:
    args = {"text": text, "tweet_id": tweet_id}
    return _gated_write(
        "x_reply", args, "reply", text, target_tweet_id=str(tweet_id)
    )


def _x_quote(text: str, tweet_id: str) -> str:
    args = {"text": text, "tweet_id": tweet_id}
    return _gated_write(
        "x_quote", args, "quote", text, target_tweet_id=str(tweet_id)
    )


def _x_thread(texts: List[str]) -> str:
    texts = [t.strip() for t in (texts or []) if t and str(t).strip()]
    args = {"texts": texts}
    joined = texts[0] if texts else ""
    return _gated_write(
        "x_thread", args, "thread", joined, thread_texts=texts
    )


def build_read_tools() -> List[StructuredTool]:
    return [
        StructuredTool.from_function(
            func=_x_search_tweets,
            name="x_search_tweets",
            description="Search recent X/Twitter posts by query.",
            args_schema=SearchInput,
        ),
        StructuredTool.from_function(
            func=_x_home_timeline,
            name="x_home_timeline",
            description="Fetch tweets from the authenticated user's home timeline.",
            args_schema=TimelineInput,
        ),
        StructuredTool.from_function(
            func=_x_get_mentions,
            name="x_get_mentions",
            description="Find recent mentions/replies involving you (best-effort).",
            args_schema=MentionsInput,
        ),
        StructuredTool.from_function(
            func=_draft_tweet_text,
            name="draft_tweet_text",
            description="Write draft tweet text with AI without posting.",
            args_schema=DraftTextInput,
        ),
    ]


def build_write_tools() -> List[StructuredTool]:
    return [
        StructuredTool.from_function(
            func=_x_tweet,
            name="x_tweet",
            description="Post an original tweet. May auto-post if safe or queue Telegram Approve if risky.",
            args_schema=TweetInput,
        ),
        StructuredTool.from_function(
            func=_x_reply,
            name="x_reply",
            description="Reply to a tweet by id. May require Telegram Approve if risky.",
            args_schema=ReplyInput,
        ),
        StructuredTool.from_function(
            func=_x_quote,
            name="x_quote",
            description="Quote-tweet a tweet by id.",
            args_schema=QuoteInput,
        ),
        StructuredTool.from_function(
            func=_x_thread,
            name="x_thread",
            description="Post a thread (always requires Approve if 3+ parts).",
            args_schema=ThreadInput,
        ),
    ]


def build_all_tools(*, include_writes: bool = True) -> List[StructuredTool]:
    tools = build_read_tools()
    if include_writes:
        tools.extend(build_write_tools())
    return tools
