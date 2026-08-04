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
    """Build a chat model from dashboard/DB provider (not stuck on env AI_PROVIDER)."""
    cfg = None
    cfg_dict: Dict[str, Any] = {}
    try:
        from ..config import Config

        cfg = Config()
        cfg_dict = cfg.config or {}
    except Exception:
        pass

    provider = (
        (cfg_dict.get("ai") or {}).get("provider")
        or os.getenv("AI_PROVIDER")
        or "openai"
    ).strip().lower()

    def _openai_key() -> Optional[str]:
        return os.getenv("OPENAI_API_KEY") or (cfg_dict.get("openai") or {}).get("api_key")

    def _anthropic_key() -> Optional[str]:
        return os.getenv("ANTHROPIC_API_KEY") or (cfg_dict.get("anthropic") or {}).get(
            "api_key"
        )

    def _gemini_key() -> Optional[str]:
        return os.getenv("GEMINI_API_KEY") or (cfg_dict.get("gemini") or {}).get("api_key")

    # Legacy AgentRouter selection remaps to OpenAI.
    if provider in ("agentrouter", "agent_router"):
        logger.warning("AgentRouter was removed; using OpenAI instead")
        provider = "openai"

    if provider == "openai":
        from langchain_openai import ChatOpenAI
        from ..ai_provider import normalize_openai_model

        api_key = _openai_key()
        model_name = normalize_openai_model(
            (cfg_dict.get("openai") or {}).get("model") or os.getenv("OPENAI_MODEL"),
            for_agent=True,
        )
        if not api_key:
            raise RuntimeError("OpenAI selected but OPENAI_API_KEY is missing")
        kwargs: Dict[str, Any] = {
            "model": model_name,
            "api_key": api_key,
            "temperature": 0.4,
        }
        base = (
            os.getenv("OPENAI_BASE_URL")
            or os.getenv("OPENAI_API_BASE")
            or (cfg_dict.get("openai") or {}).get("base_url")
            or ""
        ).strip()
        # Ignore leftover AgentRouter / tunnel hosts; use official OpenAI.
        base_l = base.lower()
        if base and "agentrouter.org" not in base_l and "trycloudflare.com" not in base_l:
            # Only allow explicit OpenAI (or documented Azure) hosts for agents.
            if "api.openai.com" in base_l or "openai.azure.com" in base_l:
                kwargs["base_url"] = base.rstrip("/")
            else:
                logger.warning(
                    "Ignoring non-OpenAI OPENAI_BASE_URL %r (agent needs api.openai.com)",
                    base,
                )
        logger.info("Agent chat model: openai/%s", model_name)
        return ChatOpenAI(**kwargs)

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        api_key = _anthropic_key()
        model_name = (
            (cfg_dict.get("anthropic") or {}).get("model")
            or "claude-3-5-haiku-latest"
        )
        if not api_key:
            raise RuntimeError("Anthropic selected but ANTHROPIC_API_KEY is missing")
        return ChatAnthropic(model=model_name, api_key=api_key, temperature=0.4)

    if provider == "gemini":
        api_key = _gemini_key()
        if not api_key:
            raise RuntimeError("Gemini selected but GEMINI_API_KEY is missing")
        from langchain_google_genai import ChatGoogleGenerativeAI
        from ..ai_provider import normalize_gemini_model

        model_name = normalize_gemini_model(
            (cfg_dict.get("gemini") or {}).get("model")
            or os.getenv("GEMINI_MODEL")
            or None
        )
        return ChatGoogleGenerativeAI(
            model=model_name,
            google_api_key=api_key,
            temperature=0.4,
        )

    raise RuntimeError(
        f"Unknown AI provider '{provider}'. Choose openai, anthropic, or gemini."
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

    # Research only when user clearly wants discovery — not on every "post a tweet"
    # (X Free tier has no search/timeline; researching first only wastes turns).
    if research_hints:
        try:
            research_agent = build_react_agent(build_read_tools(), RESEARCH_SYSTEM)
            r_state = research_agent.invoke(
                {
                    "messages": hist_msgs
                    + [
                        HumanMessage(
                            content=(
                                f"User request:\n{user_text}\n\n"
                                "Gather relevant tweets with ids. Be concise. "
                                "If search/timeline tools return tier errors, say so and stop."
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
