"""Reply generation logic for tweets."""
import logging
import os
import random
import re
from typing import Any, Dict, List, Optional, Tuple

from .ai_provider import generate_with_resolved_provider

logger = logging.getLogger(__name__)


class ReplyGenerator:
    """Generates contextual replies to tweets."""

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

    def generate_reply(
        self,
        tweet: Dict[str, Any],
        keyword: Optional[str] = None,
        content_type: str = "reply",
    ) -> Optional[str]:
        """Generate a reply or quote text for a tweet."""
        text, provider, model = self._generate_ai_reply(tweet, keyword, content_type)
        if text:
            self.last_provider = provider
            self.last_model = model
            return text
        return self._generate_template_reply(tweet, keyword)

    def _get_system_instruction(self, content_type: str) -> Tuple[str, Optional[int]]:
        if self.db is not None:
            try:
                profile = self.db.get_prompt_profile(self.prompt_profile_name, content_type)
                if profile and profile.get("system_instruction"):
                    self.last_profile_id = profile.get("id")
                    return profile["system_instruction"], profile.get("id")
            except Exception as e:
                logger.warning(f"Failed to load prompt profile: {e}")
        self.last_profile_id = None
        return (
            "Write a short, clear X/Twitter reply in plain English. Under 280 characters.",
            None,
        )

    def _generate_template_reply(
        self, tweet: Dict[str, Any], keyword: Optional[str] = None
    ) -> Optional[str]:
        if not self.templates:
            logger.warning("No reply templates available")
            return None

        template = random.choice(self.templates)
        topic = keyword or self._extract_topic(tweet["text"])
        tweet_preview = tweet["text"][:60] + "..." if len(tweet["text"]) > 60 else tweet["text"]

        reply = template.replace("{context}", tweet_preview)
        reply = reply.replace("{topic}", topic or "this")
        reply = reply.replace("{related_idea}", "trying a different approach")
        reply = reply.replace(
            "{personal_insight}",
            f"I've been working on {topic} too. What's your experience?",
        )

        author_username = tweet.get("author_username", "")
        if author_username and f"@{author_username}" not in reply:
            if len(reply) < 250:
                reply = f"@{author_username} {reply}"

        if len(reply) > 280:
            reply = reply[:277] + "..."
        return reply.strip()

    def _generate_ai_reply(
        self,
        tweet: Dict[str, Any],
        keyword: Optional[str],
        content_type: str,
    ) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        system, _ = self._get_system_instruction(content_type)
        author_username = tweet.get("author_username", "user")
        keyword_context = f" (related to keyword: {keyword})" if keyword else ""
        kind_label = "quote-tweet commentary" if content_type == "quote" else "reply"

        user = f"""Write a simple, clear {kind_label} to this tweet.
Rules:
- Use simple words and short sentences
- Keep it under 280 characters
- Sound natural
- Mention @{author_username} at the start for replies (not required for quote tweets)
- Don't repeat the tweet; add your own thought
- Just the text, no quotes

Tweet:
"{tweet['text']}"
{keyword_context}
"""
        text, provider, model = generate_with_resolved_provider(
            system,
            user,
            db=self.db,
            config=self.ai_config,
            max_tokens=200,
        )
        if not text:
            return None, provider, model

        if content_type != "quote" and author_username and f"@{author_username}" not in text:
            if len(text) < 260:
                text = f"@{author_username} {text}"
        if len(text) > 280:
            text = text[:277] + "..."
        return text.strip(), provider, model

    def _extract_topic(self, text: str) -> str:
        text = re.sub(r"http\S+|www\.\S+", "", text)
        text = re.sub(r"@\w+", "", text)
        text = re.sub(r"#\w+", "", text)
        words = text.split()[:5]
        return " ".join(words) if words else "this topic"
