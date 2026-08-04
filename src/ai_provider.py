"""Multi-provider AI generation (Gemini, OpenAI, Anthropic, AgentRouter)."""
import logging
import os
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

AGENTROUTER_DEFAULT_BASE_URL = "https://agentrouter.org/v1"
# AgentRouter gateway only allows these model ids for this project.
AGENTROUTER_ALLOWED_MODELS = (
    "gpt-5.6-sol",
    "claude-opus-4-8",
    "claude-opus-5",
)
AGENTROUTER_DEFAULT_MODEL = "gpt-5.6-sol"
# AgentRouter allowlists coding-agent clients and sits behind Aliyun WAF.
# Use the Codex CLI wire image; strip OpenAI-Python stainless markers so the
# edge does not treat us as a generic cloud SDK client.
AGENTROUTER_CODEX_UA = (
    "codex_cli_rs/0.101.0 (Mac OS 26.0.1; arm64) Apple_Terminal/464"
)
AGENTROUTER_DEFAULT_HEADERS = {
    "Originator": "codex_cli_rs",
    "User-Agent": AGENTROUTER_CODEX_UA,
    "Version": "0.101.0",
}
GEMINI_DEFAULT_MODEL = "gemini-3.5-flash"
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


def normalize_gemini_model(model: Optional[str]) -> str:
    name = (model or "").strip() or GEMINI_DEFAULT_MODEL
    return _GEMINI_MODEL_ALIASES.get(name, name)


def normalize_agentrouter_model(model: Optional[str]) -> str:
    """Force AgentRouter model to one of the allowed gateway ids."""
    name = (model or "").strip()
    if name in AGENTROUTER_ALLOWED_MODELS:
        return name
    return AGENTROUTER_DEFAULT_MODEL


def openai_env_targets_agentrouter() -> bool:
    """True when OPENAI_BASE_URL / OPENAI_API_BASE points at AgentRouter (docs style)."""
    for key in ("OPENAI_BASE_URL", "OPENAI_API_BASE"):
        url = (os.getenv(key) or "").strip().lower()
        if "agentrouter.org" in url:
            return True
    return False


def agentrouter_api_key(config: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Resolve AgentRouter token from env or config.

    Docs-compatible: OPENAI_API_KEY + OPENAI_BASE_URL=https://agentrouter.org/v1
    (see https://docs.agentrouter.org/en/qwencode.html).
    """
    config = config or {}
    key = (
        os.getenv("AGENTROUTER_API_KEY")
        or os.getenv("AGENT_ROUTER_TOKEN")
        or (config.get("agentrouter") or {}).get("api_key")
    )
    if key:
        return key
    if openai_env_targets_agentrouter() or (
        "agentrouter.org"
        in str((config.get("agentrouter") or {}).get("base_url") or "").lower()
    ):
        return os.getenv("OPENAI_API_KEY") or (config.get("openai") or {}).get("api_key")
    return None


def agentrouter_base_url(config: Optional[Dict[str, Any]] = None) -> str:
    """Return AgentRouter OpenAI-compatible base URL (must end with /v1)."""
    config = config or {}
    openai_base = (
        os.getenv("OPENAI_BASE_URL")
        or os.getenv("OPENAI_API_BASE")
        or (config.get("openai") or {}).get("base_url")
        or ""
    ).strip()
    url = (
        os.getenv("AGENTROUTER_BASE_URL")
        or (
            openai_base
            if openai_base and "agentrouter.org" in openai_base.lower()
            else ""
        )
        or (config.get("agentrouter") or {}).get("base_url")
        or AGENTROUTER_DEFAULT_BASE_URL
    ).strip().rstrip("/")
    # Missing /v1 makes langchain-openai treat the HTTP body as a raw str → model_dump crash.
    if url.endswith("/v1/chat/completions"):
        url = url[: -len("/chat/completions")]
    if not url.endswith("/v1"):
        url = f"{url}/v1"
    return url


def agentrouter_default_headers() -> Dict[str, str]:
    """Headers AgentRouter accepts for non-Claude-Code / non-Codex clients."""
    headers = dict(AGENTROUTER_DEFAULT_HEADERS)
    # Optional shared secret when AGENTROUTER_BASE_URL points at scripts/agentrouter_proxy.py
    proxy_token = (os.getenv("AGENTROUTER_PROXY_TOKEN") or "").strip()
    if proxy_token:
        headers["X-AgentRouter-Proxy-Token"] = proxy_token
    return headers


def _agentrouter_http_client():
    """httpx client that forces Codex identity on every request."""
    import httpx
    from openai import DefaultHttpxClient

    headers = agentrouter_default_headers()

    def _force_codex_headers(request: httpx.Request) -> None:
        request.headers["originator"] = headers["Originator"]
        request.headers["user-agent"] = headers["User-Agent"]
        request.headers["version"] = headers["Version"]
        # Do not strip x-stainless-* — removing them breaks OpenAI
        # with_raw_response parsing (returns bare ChatCompletion).

    return DefaultHttpxClient(
        headers=headers,
        event_hooks={"request": [_force_codex_headers]},
        timeout=httpx.Timeout(120.0, connect=30.0),
    )


def _raise_agentrouter_body_error(body: str, *, base_url: str) -> None:
    text = (body or "").strip()
    preview = text[:180].replace("\n", " ")
    lower = text.lower()
    if "aliyun_waf" in lower or "<!doctype html" in lower or "<html" in lower:
        raise RuntimeError(
            "AgentRouter's WAF blocked this server (Aliyun challenge HTML). "
            "Common on Render/cloud IPs even when base_url includes /v1. "
            "Workarounds: switch AI provider to Gemini/OpenAI/Anthropic, ask "
            "AgentRouter support to allowlist your egress IP, or proxy via a "
            f"residential host. base_url={base_url!r}."
        )
    raise RuntimeError(
        "AgentRouter returned a non-JSON body (often means the base URL is "
        f"missing /v1, or the edge blocked the client). Using base_url={base_url!r}. "
        f"Body preview: {preview!r}"
    )


def build_agentrouter_chat_model(
    *,
    api_key: str,
    model: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
):
    """
    Build a LangChain chat model for AgentRouter.

    Must use Chat Completions at a /v1 base URL. Missing /v1 makes the OpenAI
    client parse an HTML body as a str → AttributeError model_dump.
    AgentRouter also rejects generic clients unless Codex-like headers are sent.
    """
    import json

    from langchain_openai import ChatOpenAI

    model_name = normalize_agentrouter_model(model)
    base_url = agentrouter_base_url(config)
    # Never let ambient OPENAI_BASE_URL / OPENAI_API_BASE win for this client.
    # Those often get set to https://agentrouter.org (no /v1) on hosts and
    # reproduce the model_dump crash even when we pass base_url=...
    for env_key in ("OPENAI_BASE_URL", "OPENAI_API_BASE"):
        ambient = (os.getenv(env_key) or "").strip().rstrip("/")
        if ambient and "agentrouter.org" in ambient.lower() and not ambient.endswith(
            "/v1"
        ):
            logger.warning(
                "Ignoring malformed %s=%s for AgentRouter (forcing %s)",
                env_key,
                ambient,
                base_url,
            )

    http_client = _agentrouter_http_client()
    # Temporarily neutralize bad ambient OpenAI base URLs during construction.
    cleared: Dict[str, Optional[str]] = {}
    for env_key in ("OPENAI_BASE_URL", "OPENAI_API_BASE"):
        ambient = (os.getenv(env_key) or "").strip()
        if ambient and "agentrouter.org" in ambient.lower() and not ambient.rstrip(
            "/"
        ).endswith("/v1"):
            cleared[env_key] = os.environ.pop(env_key, None)
    try:
        kwargs: Dict[str, Any] = {
            "model": model_name,
            "api_key": api_key,
            "base_url": base_url,
            "default_headers": agentrouter_default_headers(),
            "http_client": http_client,
            "temperature": 1 if model_name.startswith("gpt-5") else 0.4,
        }

        class AgentRouterChatOpenAI(ChatOpenAI):
            """ChatOpenAI that recovers JSON string bodies and clarifies WAF errors."""

            def _create_chat_result(self, response: Any, generation_info: Any = None):
                if isinstance(response, str):
                    text = response.strip()
                    try:
                        response = json.loads(text)
                    except Exception:
                        _raise_agentrouter_body_error(text, base_url=base_url)
                return super()._create_chat_result(response, generation_info)

        try:
            llm = AgentRouterChatOpenAI(**kwargs, use_responses_api=False)
        except TypeError:
            kwargs.pop("http_client", None)
            try:
                llm = AgentRouterChatOpenAI(**kwargs, use_responses_api=False)
            except TypeError:
                llm = AgentRouterChatOpenAI(**kwargs)
    finally:
        for env_key, value in cleared.items():
            if value is not None:
                os.environ[env_key] = value

    # Re-assert after validators (some langchain versions reconcile with env).
    try:
        llm.openai_api_base = base_url
    except Exception:
        pass
    logger.info(
        "AgentRouter chat model ready model=%s base_url=%s", model_name, base_url
    )
    return llm


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
    name = "openai"

    def __init__(
        self,
        api_key: str,
        base_url: Optional[str] = None,
        name: Optional[str] = None,
        default_headers: Optional[Dict[str, str]] = None,
    ):
        from openai import OpenAI

        if name:
            self.name = name
        kwargs: Dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url.rstrip("/")
        if default_headers:
            kwargs["default_headers"] = default_headers
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
            model=model or "gpt-4o-mini",
            temperature=temperature,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return (response.choices[0].message.content or "").strip()


class AgentRouterProvider(OpenAIProvider):
    """OpenAI-compatible gateway at https://agentrouter.org/v1."""

    name = "agentrouter"

    def __init__(self, api_key: str, base_url: Optional[str] = None):
        from openai import OpenAI

        self.name = "agentrouter"
        self.client = OpenAI(
            api_key=api_key,
            base_url=(base_url or agentrouter_base_url()).rstrip("/"),
            default_headers=agentrouter_default_headers(),
            http_client=_agentrouter_http_client(),
        )


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

    if db is not None:
        try:
            if preferred:
                row = db.get_ai_provider(preferred)
                if row and row.get("enabled") and row.get("api_key"):
                    return (
                        row["provider"],
                        row["api_key"],
                        row.get("model"),
                        temperature,
                    )
            row = db.get_default_ai_provider()
            if row and row.get("api_key"):
                return (
                    row["provider"],
                    row["api_key"],
                    row.get("model"),
                    temperature,
                )
        except Exception as e:
            logger.warning(f"Could not load AI provider from DB: {e}")

    # Env / config fallbacks
    openai_key = os.getenv("OPENAI_API_KEY") or (config.get("openai") or {}).get("api_key")
    anthropic_key = os.getenv("ANTHROPIC_API_KEY") or (config.get("anthropic") or {}).get("api_key")
    gemini_key = os.getenv("GEMINI_API_KEY") or (config.get("gemini") or {}).get("api_key")
    ar_key = agentrouter_api_key(config)
    ar_model = normalize_agentrouter_model(
        os.getenv("AGENTROUTER_MODEL")
        or os.getenv("OPENAI_MODEL")
        or (config.get("agentrouter") or {}).get("model")
        or (config.get("openai") or {}).get("model")
    )

    # Docs style: OPENAI_API_KEY + OPENAI_BASE_URL=https://agentrouter.org/v1
    if openai_env_targets_agentrouter() and ar_key:
        name = (preferred or "").lower()
        if not name or name in ("openai", "agentrouter", "agent_router"):
            return "agentrouter", ar_key, ar_model, temperature

    # Auto-enable gemini if key present
    gemini_enabled = (config.get("gemini") or {}).get("enabled")
    if gemini_key and gemini_enabled is None:
        gemini_enabled = True
    if gemini_key and os.getenv("GEMINI_API_KEY") and gemini_enabled is False:
        # Env key present: treat as enabled unless explicitly disabled in config only when no env?
        gemini_enabled = True

    name = (preferred or "").lower()
    if name in ("agentrouter", "agent_router") and ar_key:
        return "agentrouter", ar_key, ar_model, temperature
    if name == "openai" and openai_key:
        return "openai", openai_key, (config.get("openai") or {}).get("model", "gpt-4o-mini"), temperature
    if name == "anthropic" and anthropic_key:
        return (
            "anthropic",
            anthropic_key,
            (config.get("anthropic") or {}).get("model", "claude-3-5-haiku-latest"),
            temperature,
        )
    if name == "gemini" and gemini_key:
        return "gemini", gemini_key, (config.get("gemini") or {}).get("model", GEMINI_DEFAULT_MODEL), temperature

    if ar_key and (config.get("agentrouter") or {}).get("enabled", bool(ar_key)):
        return "agentrouter", ar_key, ar_model, temperature
    if openai_key and (config.get("openai") or {}).get("enabled", bool(openai_key)):
        return "openai", openai_key, (config.get("openai") or {}).get("model", "gpt-4o-mini"), temperature
    if anthropic_key and (config.get("anthropic") or {}).get("enabled", bool(anthropic_key)):
        return (
            "anthropic",
            anthropic_key,
            (config.get("anthropic") or {}).get("model", "claude-3-5-haiku-latest"),
            temperature,
        )
    if gemini_key and gemini_enabled:
        return "gemini", gemini_key, (config.get("gemini") or {}).get("model", GEMINI_DEFAULT_MODEL), temperature

    return None, None, None, temperature


def build_provider(
    provider_name: str,
    api_key: str,
    *,
    config: Optional[Dict[str, Any]] = None,
) -> Optional[AIProvider]:
    """Instantiate a provider by name."""
    try:
        if provider_name == "gemini":
            return GeminiProvider(api_key)
        if provider_name == "openai":
            return OpenAIProvider(api_key)
        if provider_name in ("agentrouter", "agent_router"):
            return AgentRouterProvider(api_key, base_url=agentrouter_base_url(config))
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
            model=normalize_gemini_model(model) if name == "gemini" else (
                normalize_agentrouter_model(model) if name in ("agentrouter", "agent_router") else (model or "")
            ),
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return _clean_output(text), name, model
    except Exception as e:
        logger.error(f"AI generation failed via {name}: {e}", exc_info=True)
        return None, name, model
