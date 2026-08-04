"""LangGraph multi-agent graph: Research → Writer → Poster (+ orchestrator react)."""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from .policy import max_tool_steps
from .prompts import (
    ORCHESTRATOR_SYSTEM,
    POSTER_SYSTEM,
    RESEARCH_SYSTEM,
    WRITER_SYSTEM,
)
from .tools.x_tools import build_all_tools, build_read_tools, build_write_tools

logger = logging.getLogger(__name__)


def _chat_model():
    """Build a chat model; prefer OpenAI tool-calling when AI_PROVIDER=openai."""
    provider = (os.getenv("AI_PROVIDER") or "openai").strip().lower()
    api_key = None
    model_name = None

    try:
        from ..config import Config

        cfg = Config()
        from ..ai_provider import resolve_provider

        # resolve may not exist — fallback
    except Exception:
        cfg = None

    if provider == "openai" or not provider:
        api_key = os.getenv("OPENAI_API_KEY")
        if cfg:
            api_key = api_key or (cfg.config.get("openai") or {}).get("api_key")
            model_name = (cfg.config.get("openai") or {}).get("model")
        model_name = model_name or os.getenv("OPENAI_MODEL") or "gpt-4o-mini"
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY required for agent tool-calling")
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model=model_name, api_key=api_key, temperature=0.4)

    if provider == "anthropic":
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if cfg:
            api_key = api_key or (cfg.config.get("anthropic") or {}).get("api_key")
            model_name = (cfg.config.get("anthropic") or {}).get("model")
        model_name = model_name or "claude-3-5-haiku-latest"
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY required")
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(model=model_name, api_key=api_key, temperature=0.4)

    # Gemini via OpenAI-compatible path is awkward; fall back to OpenAI if present
    api_key = os.getenv("OPENAI_API_KEY") or os.getenv("GEMINI_API_KEY")
    if os.getenv("OPENAI_API_KEY"):
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=os.getenv("OPENAI_MODEL") or "gpt-4o-mini",
            api_key=os.getenv("OPENAI_API_KEY"),
            temperature=0.4,
        )
    try:
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=os.getenv("GEMINI_MODEL") or "gemini-1.5-flash",
            google_api_key=os.getenv("GEMINI_API_KEY"),
            temperature=0.4,
        )
    except Exception as e:
        raise RuntimeError(
            f"Agent needs OpenAI (recommended) or Anthropic/Gemini LangChain bindings: {e}"
        )


def _last_ai_text(messages: List[Any]) -> str:
    for m in reversed(messages or []):
        if isinstance(m, AIMessage):
            content = m.content
            if isinstance(content, str) and content.strip():
                return content.strip()
            if isinstance(content, list):
                parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(block.get("text") or "")
                    elif isinstance(block, str):
                        parts.append(block)
                text = "\n".join(parts).strip()
                if text:
                    return text
    return ""


def build_react_agent(tools: List[Any], system: str):
    from langgraph.prebuilt import create_react_agent

    model = _chat_model()
    return create_react_agent(model, tools, prompt=system)


def run_orchestrator(
    user_text: str,
    *,
    history: Optional[List[Dict[str, Any]]] = None,
    include_writes: bool = True,
    prefs_note: str = "",
) -> Dict[str, Any]:
    """
    Multi-agent pipeline:
    1) Research (read tools) when query looks like discovery
    2) Writer for draft-oriented asks
    3) Full orchestrator react agent with all tools for general / post intents
    """
    history = history or []
    hist_msgs = []
    for h in history[-12:]:
        role = (h.get("role") or "").lower()
        content = h.get("content") or ""
        if not content:
            continue
        if role in ("user", "human"):
            hist_msgs.append(HumanMessage(content=content))
        elif role in ("assistant", "ai", "agent"):
            hist_msgs.append(AIMessage(content=content))

    lower = (user_text or "").lower()
    research_hints = any(
        k in lower
        for k in (
            "search",
            "find",
            "timeline",
            "mention",
            "what's on",
            "what is on",
            "look up",
            "trending",
        )
    )
    draft_hints = any(
        k in lower for k in ("draft", "write me", "compose", "brainstorm", "word this")
    ) and not any(k in lower for k in ("post it", "publish", "tweet it", "reply now"))
    post_hints = any(
        k in lower
        for k in ("post", "tweet", "reply", "quote", "publish", "thread", "send it")
    )

    notes: List[str] = []
    research_blob = ""

    # --- Research specialist ---
    if research_hints or post_hints:
        try:
            research_agent = build_react_agent(build_read_tools(), RESEARCH_SYSTEM)
            r_state = research_agent.invoke(
                {
                    "messages": hist_msgs
                    + [
                        HumanMessage(
                            content=(
                                f"User request:\n{user_text}\n\n"
                                "Gather relevant tweets with ids. Be concise."
                            )
                        )
                    ]
                },
                config={"recursion_limit": max_tool_steps() + 4},
            )
            research_blob = _last_ai_text(r_state.get("messages") or [])
            if research_blob:
                notes.append(f"Research:\n{research_blob}")
        except Exception as e:
            logger.warning(f"Research agent failed: {e}")
            notes.append(f"Research skipped: {e}")

    # --- Writer specialist (draft-only path) ---
    if draft_hints and not post_hints:
        try:
            writer = build_react_agent(
                [t for t in build_read_tools() if t.name == "draft_tweet_text"],
                WRITER_SYSTEM,
            )
            w_state = writer.invoke(
                {
                    "messages": [
                        HumanMessage(
                            content=(
                                f"User brief:\n{user_text}\n\n"
                                f"Context:\n{research_blob or '(none)'}\n"
                                f"Prefs:\n{prefs_note or '(none)'}\n"
                                "Produce a draft; do not post."
                            )
                        )
                    ]
                },
                config={"recursion_limit": 6},
            )
            draft_out = _last_ai_text(w_state.get("messages") or [])
            return {
                "reply": draft_out or "No draft produced.",
                "path": "writer",
                "notes": notes,
            }
        except Exception as e:
            logger.warning(f"Writer agent failed: {e}")

    # --- Poster / full orchestrator with write tools ---
    system = ORCHESTRATOR_SYSTEM
    if prefs_note:
        system += f"\n\nUser preferences:\n{prefs_note}"
    if research_blob:
        system += f"\n\nResearch findings (use tweet ids from here):\n{research_blob}"

    tools = build_all_tools(include_writes=include_writes)
    if post_hints:
        # Prefer poster framing when intending to write
        system = POSTER_SYSTEM + "\n\n" + system

    agent = build_react_agent(tools, system)
    messages = hist_msgs + [HumanMessage(content=user_text)]
    state = agent.invoke(
        {"messages": messages},
        config={"recursion_limit": max_tool_steps() + 6},
    )
    reply = _last_ai_text(state.get("messages") or []) or "Done."
    if notes:
        reply = reply + "\n\n—" + "\n".join(notes[:2])
    return {"reply": reply, "path": "orchestrator", "notes": notes}
