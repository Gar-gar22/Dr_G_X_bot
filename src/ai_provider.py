"""Multi-provider AI generation (Gemini, OpenAI, Anthropic)."""
import logging
import os
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

GEMINI_DEFAULT_MODEL = "gemini-3.5-flash"
OPENAI_DEFAULT_MODEL = "gpt-4o-mini"
# Agent / tool-calling default (cheap + reliable function calling on api.openai.com)
OPENAI_AGENT_MODEL = "gpt-4o-mini"
# Retired / restricted Gemini model ids → current Flash
_GEMINI_MODEL_ALIASES = {
    "gemini-pro": GEMINI_DEFAULT_MODEL,
    "gemini-1.5-flash": GEMINI_DEFAULT_MODEL,
    "gemini-1.5-flash-latest": GEMINI_DEFAULT_MODEL,
    "gemini-1.5-pro": GEMINI_DEFAULT_MODEL,
    "gemini-1.5-pro-latest": GEMINI_DEFAULT_MODEL,
    "gemini-2.0-flash": GEMINI_DEFAULT_MODEL,
    "gemini-2.0-flash-001": GEMINI_DEFAULT_MODEL,
    "gemini-2.0-flash-lite": "gemini-3.1-flash-lite",
    "gemini-2.5-flash": GEMINI_DEFAULT_MODEL,
    "gemini-2.5-flash-lite": "gemini-3.1-flash-lite",
    "gemini-2.5-pro": GEMINI_DEFAULT_MODEL,
    "gemini-3-flash-preview": GEMINI_DEFAULT_MODEL,
}
# Leftover AgentRouter (and similar) ids → real OpenAI models
_OPENAI_MODEL_ALIASES = {
    "gpt-5.6-sol": OPENAI_AGENT_MODEL,
    "gpt-5.6": OPENAI_AGENT_MODEL,
    "claude-opus-4-8": "gpt-4o",
    "claude-opus-5": "gpt-4o",
    "claude-opus-4": "gpt-4o",
}


def normalize_gemini_model(model: Optional[str]) -> str:
    name = (model or "").strip() or GEMINI_DEFAULT_MODEL
    return _GEMINI_MODEL_ALIASES.get(name, name)


def normalize_openai_model(model: Optional[str], *, for_agent: bool = False) -> str:
    """
    Resolve an OpenAI chat model id.
    Remaps AgentRouter leftovers; defaults to gpt-4o-mini for tool-calling agents.
    """
    name = (model or "").strip()
    if not name:
        return OPENAI_AGENT_MODEL if for_agent else OPENAI_DEFAULT_MODEL
    mapped = _OPENAI_MODEL_ALIASES.get(name)
    if mapped:
        logger.warning("Remapping non-OpenAI model %r → %r", name, mapped)
        return mapped
    # Heuristic: AgentRouter-style ids that can't do /v1/chat/completions tools
    lower = name.lower()
    if lower.endswith("-sol") or "agentrouter" in lower:
        logger.warning("Remapping unsupported model %r → %r", name, OPENAI_AGENT_MODEL)
        return OPENAI_AGENT_MODEL
    return name


class AIProvider(ABC):
    """Base interface for text generation providers."""

    name: str = "base"

    @abstractmethod
    def generate(
        self,
        system: str,
        user: str,
        *,
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 400,
    ) -> str:
        """Generate text from system + user prompts."""


class GeminiProvider(AIProvider):
    name = "gemini"

    def __init__(self, api_key: str):
        from google import genai

        self.client = genai.Client(api_key=api_key)

    def generate(
        self,
        system: str,
        user: str,
        *,
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 400,
    ) -> str:
        from google.genai import types

        model_name = normalize_gemini_model(model)
        prompt = f"{system.strip()}\n\n{user.strip()}"
        response = self.client.models.generate_content(
            model=model_name,
            contents=[prompt],
            config=types.GenerateContentConfig(
                temperature=temperature,
                max_output_tokens=max_tokens,
            ),
        )
        if hasattr(response, "text") and response.text:
            return response.text.strip()
        if hasattr(response, "candidates") and response.candidates:
            return response.candidates[0].content.parts[0].text.strip()
        return str(response).strip()


class OpenAIProvider(AIProvider):
    """Official OpenAI API (api.openai.com) via the OpenAI Python SDK."""

    name = "openai"

    def __init__(self, api_key: str, base_url: Optional[str] = None):
        from openai import OpenAI

        kwargs: Dict[str, Any] = {"api_key": api_key}
        # Optional custom host (e.g. Azure); do not use third-party gateways here.
        if base_url:
            kwargs["base_url"] = base_url.rstrip("/")
        self.client = OpenAI(**kwargs)

    def generate(
        self,
        system: str,
        user: str,
        *,
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 400,
    ) -> str:
        response = self.client.chat.completions.create(
            model=normalize_openai_model(model),
            temperature=temperature,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return (response.choices[0].message.content or "").strip()


class AnthropicProvider(AIProvider):
    name = "anthropic"

    def __init__(self, api_key: str):
        from anthropic import Anthropic

        self.client = Anthropic(api_key=api_key)

    def generate(
        self,
        system: str,
        user: str,
        *,
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 400,
    ) -> str:
        response = self.client.messages.create(
            model=model or "claude-3-5-haiku-latest",
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        parts = []
        for block in response.content:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
        return "\n".join(parts).strip()


def _clean_output(text: str) -> str:
    text = (text or "").strip()
    if text.startswith('"') and text.endswith('"'):
        text = text[1:-1]
    if text.startswith("'") and text.endswith("'"):
        text = text[1:-1]
    text = text.replace("**", "").replace("__", "")
    return text.strip()


def resolve_provider_config(
    db=None,
    config: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[str], Optional[str], Optional[str], float]:
    """
    Resolve (provider_name, api_key, model, temperature) from DB, config, env.
    Priority: enabled default DB provider → config.ai → env → legacy gemini.
    """
    config = config or {}
    temperature = float(config.get("temperature", 0.7))

    preferred = (
        config.get("provider")
        or (config.get("ai") or {}).get("provider")
        or os.getenv("AI_PROVIDER")
    )
    if (preferred or "").lower() in ("agentrouter", "agent_router"):
        logger.warning("AgentRouter was removed; falling back to openai if configured")
        preferred = "openai"

    if db is not None:
        try:
            # Prefer live env OpenAI key over a stale DB token (e.g. old gateway key).
            env_openai_key = os.getenv("OPENAI_API_KEY")

            if preferred:
                row = db.get_ai_provider(preferred)
                if row and row.get("enabled") and (row.get("api_key") or env_openai_key):
                    prov = row["provider"]
                    if prov in ("agentrouter", "agent_router"):
                        openai_row = db.get_ai_provider("openai")
                        key = env_openai_key or (
                            (openai_row or {}).get("api_key") if openai_row else None
                        )
                        if key:
                            return (
                                "openai",
                                key,
                                normalize_openai_model(
                                    (openai_row or {}).get("model") or OPENAI_DEFAULT_MODEL
                                ),
                                temperature,
                            )
                        preferred = "openai"
                    else:
                        model = row.get("model")
                        key = row.get("api_key")
                        if prov == "openai":
                            model = normalize_openai_model(model)
                            key = env_openai_key or key
                        if key:
                            return (
                                prov,
                                key,
                                model,
                                temperature,
                            )
            row = db.get_default_ai_provider()
            if row and (row.get("api_key") or env_openai_key):
                if row["provider"] in ("agentrouter", "agent_router"):
                    openai_row = db.get_ai_provider("openai")
                    key = env_openai_key or (
                        (openai_row or {}).get("api_key") if openai_row else None
                    )
                    if key:
                        return (
                            "openai",
                            key,
                            normalize_openai_model(
                                (openai_row or {}).get("model") or OPENAI_DEFAULT_MODEL
                            ),
                            temperature,
                        )
                else:
                    model = row.get("model")
                    key = row.get("api_key")
                    if row["provider"] == "openai":
                        model = normalize_openai_model(model)
                        key = env_openai_key or key
                    if key:
                        return (
                            row["provider"],
                            key,
                            model,
                            temperature,
                        )
        except Exception as e:
            logger.warning(f"Could not load AI provider from DB: {e}")

    openai_key = os.getenv("OPENAI_API_KEY") or (config.get("openai") or {}).get("api_key")
    anthropic_key = os.getenv("ANTHROPIC_API_KEY") or (config.get("anthropic") or {}).get(
        "api_key"
    )
    gemini_key = os.getenv("GEMINI_API_KEY") or (config.get("gemini") or {}).get("api_key")

    gemini_enabled = (config.get("gemini") or {}).get("enabled")
    if gemini_key and gemini_enabled is None:
        gemini_enabled = True
    if gemini_key and os.getenv("GEMINI_API_KEY") and gemini_enabled is False:
        gemini_enabled = True

    name = (preferred or "").lower()
    openai_model = normalize_openai_model(
        os.getenv("OPENAI_MODEL") or (config.get("openai") or {}).get("model")
    )
    if name == "openai" and openai_key:
        return "openai", openai_key, openai_model, temperature
    if name == "anthropic" and anthropic_key:
        return (
            "anthropic",
            anthropic_key,
            (config.get("anthropic") or {}).get("model", "claude-3-5-haiku-latest"),
            temperature,
        )
    if name == "gemini" and gemini_key:
        return (
            "gemini",
            gemini_key,
            (config.get("gemini") or {}).get("model", GEMINI_DEFAULT_MODEL),
            temperature,
        )

    if openai_key and (config.get("openai") or {}).get("enabled", bool(openai_key)):
        return "openai", openai_key, openai_model, temperature
    if anthropic_key and (config.get("anthropic") or {}).get("enabled", bool(anthropic_key)):
        return (
            "anthropic",
            anthropic_key,
            (config.get("anthropic") or {}).get("model", "claude-3-5-haiku-latest"),
            temperature,
        )
    if gemini_key and gemini_enabled:
        return (
            "gemini",
            gemini_key,
            (config.get("gemini") or {}).get("model", GEMINI_DEFAULT_MODEL),
            temperature,
        )

    return None, None, None, temperature


def build_provider(
    provider_name: str,
    api_key: str,
    *,
    config: Optional[Dict[str, Any]] = None,
) -> Optional[AIProvider]:
    """Instantiate a provider by name."""
    config = config or {}
    try:
        if provider_name == "gemini":
            return GeminiProvider(api_key)
        if provider_name == "openai":
            base = (
                os.getenv("OPENAI_BASE_URL")
                or os.getenv("OPENAI_API_BASE")
                or (config.get("openai") or {}).get("base_url")
            )
            # Ignore legacy AgentRouter base URLs
            if base and "agentrouter.org" in str(base).lower():
                base = None
            return OpenAIProvider(api_key, base_url=base or None)
        if provider_name == "anthropic":
            return AnthropicProvider(api_key)
    except Exception as e:
        logger.error(f"Failed to init provider {provider_name}: {e}")
    return None


def generate_with_resolved_provider(
    system: str,
    user: str,
    *,
    db=None,
    config: Optional[Dict[str, Any]] = None,
    max_tokens: int = 400,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Generate text using resolved provider.
    Returns (text, provider_name, model) or (None, None, None) on failure.
    """
    name, api_key, model, temperature = resolve_provider_config(db=db, config=config)
    if not name or not api_key:
        return None, None, None
    provider = build_provider(name, api_key, config=config)
    if not provider:
        return None, None, None
    try:
        text = provider.generate(
            system,
            user,
            model=normalize_gemini_model(model) if name == "gemini" else (model or ""),
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return _clean_output(text), name, model
    except Exception as e:
        logger.error(f"AI generation failed via {name}: {e}", exc_info=True)
        return None, name, model
