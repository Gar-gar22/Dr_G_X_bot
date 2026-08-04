"""Configuration management for the Auto-Reply X Bot."""
import json
import os
from pathlib import Path
from typing import Dict, Any, Optional
from dotenv import load_dotenv

# Load environment variables
load_dotenv()


class Config:
    """Manages configuration for the bot."""

    def __init__(self, config_path: str = "config.json", use_database: bool = True):
        """Initialize configuration from file, database, and environment variables."""
        self.config_path = Path(config_path)
        self.config: Dict[str, Any] = {}
        self.use_database = use_database
        self.load_config()
        self.load_from_database()
        self.override_with_env()

    def _default_config(self) -> Dict[str, Any]:
        """Return minimal default config so app can start (e.g. on first deploy)."""
        return {
            "x_api": {},
            "gemini": {"enabled": False},
            "openai": {"enabled": False},
            "anthropic": {"enabled": False},
            "ai": {
                "provider": os.getenv("AI_PROVIDER", "gemini"),
                "prompt_profile": os.getenv("AI_PROMPT_PROFILE", "default"),
                "temperature": 0.7,
            },
            "man_in_the_loop": {"enabled": True},
            "safety": {
                "enabled": True,
                "daily_post_limit": 30,
                "monthly_post_limit": 400,
                "max_pending_drafts": 50,
                "min_chars": 20,
                "max_chars": 280,
                "max_hashtags": 3,
                "max_links": 2,
                "block_all_caps": True,
                "block_duplicates": True,
                "refuse_risky_tips": True,
                "custom_block_phrases": [],
            },
            "keywords": [],
            "schedule": {
                "morning_time": "09:00",
                "evening_time": "18:00",
                "morning_start": "09:00",
                "morning_end": "11:00",
                "evening_start": "18:00",
                "evening_end": "20:00",
                "timezone": "UTC",
            },
            "reply_settings": {
                "max_replies_per_run": 10,
                "min_replies_per_run": 5,
                "delay_minutes_min": 5,
                "delay_minutes_max": 15,
            },
            "filters": {
                "exclude_retweets": True,
                "exclude_own_tweets": True,
                "min_followers": 0,
            },
            "tweet_settings": {
                "enabled": True,
                "tweets_per_run": 1,
                "threads_per_run": 0,
                "thread_tweet_count": 3,
            },
        }

    def load_config(self) -> None:
        """Load configuration from JSON file. Creates default if missing."""
        if self.config_path.exists():
            with open(self.config_path, "r", encoding="utf-8") as f:
                self.config = json.load(f)
        else:
            self.config = self._default_config()
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=2)

    def load_from_database(self) -> None:
        """Load API credentials and AI settings from database if available."""
        if not self.use_database:
            return

        try:
            from .database import Database

            db = Database()

            x_creds = db.get_api_credentials("x_api")
            if x_creds:
                self.config.setdefault("x_api", {})
                for key in (
                    "consumer_key",
                    "consumer_secret",
                    "access_token",
                    "access_token_secret",
                    "bearer_token",
                ):
                    if x_creds.get(key):
                        self.config["x_api"][key] = x_creds[key]

            for provider in ("gemini", "openai", "anthropic"):
                row = db.get_ai_provider(provider)
                if row:
                    self.config.setdefault(provider, {})
                    if row.get("api_key"):
                        self.config[provider]["api_key"] = row["api_key"]
                    if row.get("model"):
                        self.config[provider]["model"] = row["model"]
                    self.config[provider]["enabled"] = bool(row.get("enabled"))
                    if row.get("is_default"):
                        self.config.setdefault("ai", {})
                        self.config["ai"]["provider"] = provider
                else:
                    # Legacy gemini credentials table
                    legacy = db.get_api_credentials(provider)
                    if legacy and legacy.get("api_key"):
                        self.config.setdefault(provider, {})
                        self.config[provider]["api_key"] = legacy["api_key"]
                        if legacy.get("enabled") is not None:
                            self.config[provider]["enabled"] = legacy["enabled"]

            # Legacy: if AgentRouter was the DB default, prefer OpenAI when available
            try:
                ar = db.get_ai_provider("agentrouter")
                if ar and ar.get("is_default"):
                    self.config.setdefault("ai", {})
                    if self.config.get("openai", {}).get("api_key"):
                        self.config["ai"]["provider"] = "openai"
                    elif (self.config.get("ai") or {}).get("provider") in (
                        "agentrouter",
                        "agent_router",
                        None,
                    ):
                        self.config["ai"]["provider"] = "openai"
            except Exception:
                pass

            db.close()
        except Exception:
            pass

    def save_to_database(self, force: bool = False) -> None:
        """Save API credentials to database."""
        if not self.use_database and not force:
            return

        try:
            from .database import Database

            db = Database()

            x_api = self.config.get("x_api", {})
            if x_api:
                db.save_api_credentials(
                    "x_api",
                    {
                        "consumer_key": x_api.get("consumer_key"),
                        "consumer_secret": x_api.get("consumer_secret"),
                        "access_token": x_api.get("access_token"),
                        "access_token_secret": x_api.get("access_token_secret"),
                        "bearer_token": x_api.get("bearer_token"),
                        "enabled": True,
                    },
                )

            default_provider = (self.config.get("ai") or {}).get("provider", "gemini")
            if default_provider in ("agentrouter", "agent_router"):
                default_provider = "openai"
            for provider in ("gemini", "openai", "anthropic"):
                block = self.config.get(provider, {})
                if block.get("api_key") or block.get("enabled"):
                    db.save_ai_provider(
                        {
                            "provider": provider,
                            "api_key": block.get("api_key"),
                            "model": block.get("model"),
                            "enabled": block.get("enabled", bool(block.get("api_key"))),
                            "is_default": provider == default_provider,
                        }
                    )
                    # Keep legacy row for gemini compatibility
                    if provider == "gemini" and block.get("api_key"):
                        db.save_api_credentials(
                            "gemini",
                            {
                                "api_key": block.get("api_key"),
                                "enabled": block.get("enabled", True),
                            },
                        )

            db.close()
        except Exception as e:
            import logging

            logging.getLogger(__name__).error(f"Error saving credentials to database: {e}")

    def override_with_env(self) -> None:
        """Override config values with environment variables if present."""
        self.config.setdefault("x_api", {})

        # OAuth 1.0a user context (required to post). Portal: API Key / API Key Secret.
        # Prefer X_CONSUMER_*; accept X_API_KEY / X_API_SECRET as aliases.
        # Do NOT map OAuth 2 Client ID/Secret — those are different credentials.
        consumer_key = os.getenv("X_CONSUMER_KEY") or os.getenv("X_API_KEY")
        consumer_secret = os.getenv("X_CONSUMER_SECRET") or os.getenv("X_API_SECRET")
        if consumer_key:
            self.config["x_api"]["consumer_key"] = consumer_key
        if consumer_secret:
            self.config["x_api"]["consumer_secret"] = consumer_secret
        if os.getenv("X_ACCESS_TOKEN"):
            self.config["x_api"]["access_token"] = os.getenv("X_ACCESS_TOKEN")
        if os.getenv("X_ACCESS_TOKEN_SECRET"):
            self.config["x_api"]["access_token_secret"] = os.getenv("X_ACCESS_TOKEN_SECRET")
        if os.getenv("X_BEARER_TOKEN"):
            self.config["x_api"]["bearer_token"] = os.getenv("X_BEARER_TOKEN")

        self.config.setdefault("gemini", {})
        if os.getenv("GEMINI_API_KEY"):
            self.config["gemini"]["api_key"] = os.getenv("GEMINI_API_KEY")
            self.config["gemini"]["enabled"] = True

        self.config.setdefault("openai", {})
        if os.getenv("OPENAI_API_KEY"):
            self.config["openai"]["api_key"] = os.getenv("OPENAI_API_KEY")
            self.config["openai"]["enabled"] = True
        openai_base = (os.getenv("OPENAI_BASE_URL") or os.getenv("OPENAI_API_BASE") or "").strip()
        base_l = openai_base.lower()
        if openai_base and "agentrouter.org" not in base_l and "trycloudflare.com" not in base_l:
            if "api.openai.com" in base_l or "openai.azure.com" in base_l:
                self.config["openai"]["base_url"] = openai_base
            else:
                self.config["openai"].pop("base_url", None)
        else:
            self.config["openai"].pop("base_url", None)
        if os.getenv("OPENAI_MODEL") or self.config["openai"].get("model"):
            from .ai_provider import normalize_openai_model

            self.config["openai"]["model"] = normalize_openai_model(
                os.getenv("OPENAI_MODEL") or self.config["openai"].get("model")
            )

        self.config.setdefault("anthropic", {})
        if os.getenv("ANTHROPIC_API_KEY"):
            self.config["anthropic"]["api_key"] = os.getenv("ANTHROPIC_API_KEY")
            self.config["anthropic"]["enabled"] = True

        # Drop any leftover AgentRouter config from file / old deploys
        self.config.pop("agentrouter", None)

        self.config.setdefault("ai", {})
        # AI_PROVIDER only seeds when no provider was chosen yet (file/DB/dashboard).
        # Otherwise env permanently overrides dashboard switches (e.g. stuck on openai).
        env_provider = (os.getenv("AI_PROVIDER") or "").strip().lower()
        if env_provider in ("agentrouter", "agent_router"):
            env_provider = "openai"
        if env_provider and not (self.config.get("ai") or {}).get("provider"):
            self.config["ai"]["provider"] = env_provider
        if (self.config.get("ai") or {}).get("provider") in ("agentrouter", "agent_router"):
            self.config["ai"]["provider"] = "openai"
        if os.getenv("AI_PROMPT_PROFILE"):
            self.config["ai"]["prompt_profile"] = os.getenv("AI_PROMPT_PROFILE")

        self.config.setdefault("man_in_the_loop", {"enabled": True})
        mitl = os.getenv("MITL_ENABLED")
        if mitl is not None:
            self.config["man_in_the_loop"]["enabled"] = mitl.strip().lower() in (
                "1",
                "true",
                "yes",
                "on",
            )

        self.config.setdefault("safety", {})
        # Ensure defaults exist if old config.json
        from .quality_safety import DEFAULT_SAFETY, merge_safety_config

        merged = merge_safety_config({**DEFAULT_SAFETY, **(self.config.get("safety") or {})})
        self.config["safety"] = merged

    def get(self, key: str, default: Any = None) -> Any:
        """Get configuration value by dot-notation key."""
        keys = key.split(".")
        value = self.config
        try:
            for k in keys:
                value = value[k]
            return value
        except (KeyError, TypeError):
            return default

    def get_x_api_credentials(self) -> Dict[str, str]:
        return {
            "consumer_key": self.get("x_api.consumer_key"),
            "consumer_secret": self.get("x_api.consumer_secret"),
            "access_token": self.get("x_api.access_token"),
            "access_token_secret": self.get("x_api.access_token_secret"),
            "bearer_token": self.get("x_api.bearer_token"),
        }

    def get_gemini_config(self) -> Dict[str, Any]:
        """Backward-compatible Gemini config merged with AI settings."""
        gemini = dict(self.get("gemini", {}) or {})
        ai = self.get("ai", {}) or {}
        merged = {
            **gemini,
            "provider": ai.get("provider", "gemini"),
            "prompt_profile": ai.get("prompt_profile", "default"),
            "temperature": ai.get("temperature", gemini.get("temperature", 0.7)),
            "openai": self.get("openai", {}),
            "anthropic": self.get("anthropic", {}),
            "gemini": gemini,
        }
        return merged

    def get_ai_config(self) -> Dict[str, Any]:
        """Full AI config for generators."""
        return self.get_gemini_config()

    def get_mitl_enabled(self) -> bool:
        return bool((self.get("man_in_the_loop") or {}).get("enabled", True))

    def get_safety_config(self) -> Dict[str, Any]:
        from .quality_safety import merge_safety_config

        return merge_safety_config(self.get("safety") or {})

    def get_prompt_profile_name(self) -> str:
        return (
            os.getenv("AI_PROMPT_PROFILE")
            or (self.get("ai") or {}).get("prompt_profile")
            or "default"
        )

    def get_schedule_config(self) -> Dict[str, str]:
        return self.get("schedule", {})

    def get_reply_settings(self) -> Dict[str, Any]:
        return self.get("reply_settings", {})

    def get_keywords(self) -> list:
        return self.get("keywords", [])

    def get_reply_templates(self) -> list:
        return self.get("reply_templates", [])

    def get_tweet_templates(self) -> list:
        return self.get(
            "tweet_templates",
            [
                "Sharing some thoughts on AI and technology today...",
                "Just reflecting on the latest developments in tech.",
                "Interesting day for innovation and creativity.",
            ],
        )

    def get_tweet_settings(self) -> Dict[str, Any]:
        default = {"enabled": True, "interval_minutes": 30}
        settings = self.get("tweet_settings", default)
        if "interval_hours" in settings and "interval_minutes" not in settings:
            settings["interval_minutes"] = settings["interval_hours"] * 60
        return settings

    def get_filters(self) -> Dict[str, Any]:
        return self.get("filters", {})
