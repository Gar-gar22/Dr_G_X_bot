"""Tweet generation logic for posting original tweets."""
import logging
import os
import random
import re
from typing import Any, Dict, List, Optional, Tuple

from .ai_provider import generate_with_resolved_provider

logger = logging.getLogger(__name__)


class TweetGenerator:
    """Generates original tweets for posting."""

    def __init__(
        self,
        templates: List[str],
        ai_config: Optional[Dict[str, Any]] = None,
        db=None,
        prompt_profile_name: Optional[str] = None,
    ):
        self.templates = templates or []
        self.ai_config = ai_config or {}
        self.db = db
        self.prompt_profile_name = (
            prompt_profile_name
            or os.getenv("AI_PROMPT_PROFILE")
            or self.ai_config.get("prompt_profile")
            or "default"
        )
        self.last_provider: Optional[str] = None
        self.last_model: Optional[str] = None
        self.last_profile_id: Optional[int] = None

    def _get_system_instruction(self, content_type: str) -> str:
        if self.db is not None:
            try:
                profile = self.db.get_prompt_profile(self.prompt_profile_name, content_type)
                if profile and profile.get("system_instruction"):
                    self.last_profile_id = profile.get("id")
                    return profile["system_instruction"]
            except Exception as e:
                logger.warning(f"Failed to load prompt profile: {e}")
        self.last_profile_id = None
        return "Write a natural X/Twitter post under 280 characters."

    def generate_tweet(self, keywords: Optional[List[str]] = None) -> Optional[str]:
        text, provider, model = self._generate_ai_tweet(keywords)
        if text:
            self.last_provider = provider
            self.last_model = model
            return text
        return self._generate_template_tweet(keywords)

    def generate_thread(
        self, keywords: Optional[List[str]] = None, num_tweets: int = 3
    ) -> Optional[List[str]]:
        texts, provider, model = self._generate_ai_thread(keywords, num_tweets)
        if texts:
            self.last_provider = provider
            self.last_model = model
            return texts
        return self._generate_template_thread(keywords, num_tweets)

    def _generate_template_tweet(self, keywords: Optional[List[str]] = None) -> Optional[str]:
        if not self.templates:
            logger.warning("No tweet templates available")
            return None

        template = random.choice(self.templates)
        if len(template) > 150:
            expanded = f"{template}\n\nWhat's your take on this?"
        elif keywords:
            expanded = (
                f"{template}\n\nThis matters for {keywords[0]}. "
                "What's been your experience?"
            )
        else:
            expanded = f"{template}\n\nWhat are your thoughts on this?"

        if len(expanded) > 280:
            expanded = expanded[:277] + "..."
        return expanded.strip()

    def _generate_ai_tweet(
        self, keywords: Optional[List[str]] = None
    ) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        system = self._get_system_instruction("tweet")
        keyword_context = ""
        if keywords:
            keyword_context = f"\nTopics to consider: {', '.join(keywords[:5])}"
        user = f"""Write one natural, authentic tweet.
- Aim for 150-280 characters
- Sound human; avoid AI cliches
- No excessive emojis
- Just the tweet text
{keyword_context}
"""
        text, provider, model = generate_with_resolved_provider(
            system, user, db=self.db, config=self.ai_config, max_tokens=200
        )
        if not text:
            return None, provider, model
        text = text.replace("\\n", "\n")
        if len(text) > 280:
            text = text[:277] + "..."
        return text.strip(), provider, model

    def _generate_template_thread(
        self, keywords: Optional[List[str]] = None, num_tweets: int = 3
    ) -> Optional[List[str]]:
        if not self.templates:
            return None
        template = random.choice(self.templates)
        keyword_str = keywords[0] if keywords else "this topic"
        thread = [
            f"{template}\n\nA short thread on {keyword_str}."[:280],
        ]
        for i in range(1, num_tweets):
            tweet = (
                f"{i}/ Consistency and clear fundamentals matter most with {keyword_str}. "
                "What has worked for you?"
            )[:280]
            thread.append(tweet)
        return thread[:num_tweets]

    def _generate_ai_thread(
        self, keywords: Optional[List[str]] = None, num_tweets: int = 3
    ) -> Tuple[Optional[List[str]], Optional[str], Optional[str]]:
        system = self._get_system_instruction("thread")
        keyword_context = ""
        if keywords:
            keyword_context = f"\nTopics: {', '.join(keywords[:5])}"
        user = f"""Write a thread of {num_tweets} connected tweets.
Each tweet under 280 characters.
Format each on its own line as: 1/ text, 2/ text, etc.
Just the numbered tweets.
{keyword_context}
"""
        text, provider, model = generate_with_resolved_provider(
            system, user, db=self.db, config=self.ai_config, max_tokens=800
        )
        if not text:
            return None, provider, model

        matches = re.findall(r"^\d+/\s*(.+?)(?=^\d+/\s*|\Z)", text, re.MULTILINE | re.DOTALL)
        if matches:
            thread = [m.strip()[:280] for m in matches]
        else:
            thread = []
            for line in text.split("\n"):
                line = re.sub(r"^\d+/\s*", "", line.strip())
                if line and len(line) > 20:
                    thread.append(line[:280])

        if not thread:
            return None, provider, model
        while len(thread) < num_tweets:
            extra = self._generate_template_thread(keywords, 1)
            if extra:
                thread.append(extra[0])
            else:
                break
        return thread[:num_tweets], provider, model
