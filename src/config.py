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
            "filters": {"exclude_retweets": True, "exclude_own_tweets": True, "min_followers": 0},
            "tweet_settings": {"enabled": True, "tweets_per_run": 1, "threads_per_run": 0, "thread_tweet_count": 3},
        }

    def load_config(self) -> None:
        """Load configuration from JSON file. Creates default if missing (for first deploy)."""
        if self.config_path.exists():
            with open(self.config_path, 'r', encoding='utf-8') as f:
                self.config = json.load(f)
        else:
            self.config = self._default_config()
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=2)
    
    def load_from_database(self) -> None:
        """Load API credentials from database if available."""
        if not self.use_database:
            return
        
        try:
            from .database import Database
            db = Database()
            
            # Load X API credentials
            x_creds = db.get_api_credentials("x_api")
            if x_creds:
                self.config.setdefault("x_api", {})
                if x_creds.get("consumer_key"):
                    self.config["x_api"]["consumer_key"] = x_creds["consumer_key"]
                if x_creds.get("consumer_secret"):
                    self.config["x_api"]["consumer_secret"] = x_creds["consumer_secret"]
                if x_creds.get("access_token"):
                    self.config["x_api"]["access_token"] = x_creds["access_token"]
                if x_creds.get("access_token_secret"):
                    self.config["x_api"]["access_token_secret"] = x_creds["access_token_secret"]
                if x_creds.get("bearer_token"):
                    self.config["x_api"]["bearer_token"] = x_creds["bearer_token"]
            
            # Load Gemini API credentials
            gemini_creds = db.get_api_credentials("gemini")
            if gemini_creds:
                self.config.setdefault("gemini", {})
                if gemini_creds.get("api_key"):
                    self.config["gemini"]["api_key"] = gemini_creds["api_key"]
                if gemini_creds.get("enabled") is not None:
                    self.config["gemini"]["enabled"] = gemini_creds["enabled"]
            
            db.close()
        except Exception:
            pass
    
    def save_to_database(self, force: bool = False) -> None:
        """Save API credentials to database.
        
        Args:
            force: If True, save even if use_database is False
        """
        if not self.use_database and not force:
            return
        
        try:
            from .database import Database
            db = Database()
            
            # Save X API credentials
            x_api = self.config.get("x_api", {})
            if x_api:
                db.save_api_credentials("x_api", {
                    "consumer_key": x_api.get("consumer_key"),
                    "consumer_secret": x_api.get("consumer_secret"),
                    "access_token": x_api.get("access_token"),
                    "access_token_secret": x_api.get("access_token_secret"),
                    "bearer_token": x_api.get("bearer_token"),
                    "enabled": True
                })
            
            # Save Gemini API credentials
            gemini = self.config.get("gemini", {})
            if gemini.get("api_key"):
                db.save_api_credentials("gemini", {
                    "api_key": gemini.get("api_key"),
                    "enabled": gemini.get("enabled", True)
                })
            
            db.close()
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Error saving credentials to database: {e}")
    
    def override_with_env(self) -> None:
        """Override config values with environment variables if present."""
        # Ensure x_api dict exists
        self.config.setdefault("x_api", {})
        
        # X API credentials
        if os.getenv("X_CONSUMER_KEY"):
            self.config["x_api"]["consumer_key"] = os.getenv("X_CONSUMER_KEY")
        if os.getenv("X_CONSUMER_SECRET"):
            self.config["x_api"]["consumer_secret"] = os.getenv("X_CONSUMER_SECRET")
        if os.getenv("X_ACCESS_TOKEN"):
            self.config["x_api"]["access_token"] = os.getenv("X_ACCESS_TOKEN")
        if os.getenv("X_ACCESS_TOKEN_SECRET"):
            self.config["x_api"]["access_token_secret"] = os.getenv("X_ACCESS_TOKEN_SECRET")
        if os.getenv("X_BEARER_TOKEN"):
            self.config["x_api"]["bearer_token"] = os.getenv("X_BEARER_TOKEN")
        
        # Gemini API
        self.config.setdefault("gemini", {})
        if os.getenv("GEMINI_API_KEY"):
            self.config["gemini"]["api_key"] = os.getenv("GEMINI_API_KEY")
    
    def get(self, key: str, default: Any = None) -> Any:
        """Get configuration value by dot-notation key (e.g., 'x_api.consumer_key')."""
        keys = key.split('.')
        value = self.config
        try:
            for k in keys:
                value = value[k]
            return value
        except (KeyError, TypeError):
            return default
    
    def get_x_api_credentials(self) -> Dict[str, str]:
        """Get X API credentials."""
        return {
            "consumer_key": self.get("x_api.consumer_key"),
            "consumer_secret": self.get("x_api.consumer_secret"),
            "access_token": self.get("x_api.access_token"),
            "access_token_secret": self.get("x_api.access_token_secret"),
            "bearer_token": self.get("x_api.bearer_token")
        }
    
    def get_gemini_config(self) -> Dict[str, Any]:
        """Get Google Gemini configuration."""
        return self.get("gemini", {})
    
    def get_schedule_config(self) -> Dict[str, str]:
        """Get schedule configuration."""
        return self.get("schedule", {})
    
    def get_reply_settings(self) -> Dict[str, Any]:
        """Get reply settings."""
        return self.get("reply_settings", {})
    
    def get_keywords(self) -> list:
        """Get list of keywords to search for."""
        return self.get("keywords", [])
    
    def get_reply_templates(self) -> list:
        """Get list of reply templates."""
        return self.get("reply_templates", [])
    
    def get_tweet_templates(self) -> list:
        """Get list of tweet templates."""
        return self.get("tweet_templates", [
            "Sharing some thoughts on AI and technology today...",
            "Just reflecting on the latest developments in tech.",
            "Interesting day for innovation and creativity."
        ])
    
    def get_tweet_settings(self) -> Dict[str, Any]:
        """Get tweet posting settings."""
        # Support both interval_minutes and interval_hours for backward compatibility
        default = {"enabled": True, "interval_minutes": 30}
        settings = self.get("tweet_settings", default)
        # Convert interval_hours to interval_minutes if needed
        if "interval_hours" in settings and "interval_minutes" not in settings:
            settings["interval_minutes"] = settings["interval_hours"] * 60
        return settings
    
    def get_filters(self) -> Dict[str, Any]:
        """Get filter settings."""
        return self.get("filters", {})

