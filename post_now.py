#!/usr/bin/env python3
"""One-off script: post a tweet, or quote + reply to a tweet (pass tweet ID). Uses full 280-char limit."""
import sys
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.config import Config
from src.x_api import XAPI

# X (Twitter) character limit
MAX_LENGTH = 280


def _trim(text: str, max_len: int = MAX_LENGTH) -> str:
    """Ensure text is at most max_len characters."""
    if len(text) <= max_len:
        return text
    return text[: max_len - 3].rstrip() + "..."


# Full-length content with paragraph formatting (line breaks). Max 280 chars total.
TWEET_TEXT = _trim(
    "Building in public isn't just a trend—it's how we learn, connect, and grow.\n\n"
    "Every small step, every lesson, every win and fail shared openly adds up. "
    "What are you working on today? Drop it below and let's support each other.\n\n"
    "The best ideas get better when we build in the open. 🚀"
)

QUOTE_TEXT = _trim(
    "This hits different.\n\n"
    "So much of success is just showing up consistently and sharing the process. "
    "Not just the wins—the blocks, the pivots, the tiny improvements.\n\n"
    "Grateful for everyone building in public. Keep going. 💪"
)

REPLY_TEXT = _trim(
    "Absolutely. The compound effect of small steps is real—we often underestimate what we can do in a year.\n\n"
    "Thanks for sharing; good reminder to focus on the system, not just the outcome.\n\n"
    "What's one small thing you're doing this week to move the needle? Would love to hear."
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def main():
    # Optional: tweet ID to quote and reply to (e.g. python post_now.py 1234567890123456789)
    # When given: only quote + reply (no new tweet). When omitted: only post new tweet.
    target_tweet_id = sys.argv[1].strip() if len(sys.argv) > 1 else None

    try:
        config = Config()
    except FileNotFoundError:
        logger.error("Config not found. Create config.json or run python setup.py")
        sys.exit(1)

    x_api = XAPI(config.get_x_api_credentials())
    user = x_api.get_user_info()
    logger.info("Authenticated as @%s", user.get("username"))

    if not target_tweet_id:
        # Post one new tweet (full length)
        tid = x_api.post_tweet(TWEET_TEXT)
        if tid:
            logger.info("Posted new tweet (%d chars): %s", len(TWEET_TEXT), tid)
        else:
            logger.warning("Failed to post new tweet")
        logger.info("To quote + reply to a tweet, run: python post_now.py <tweet_id>")
        return

    # Quote the given tweet (full length)
    qid = x_api.quote_tweet(QUOTE_TEXT, target_tweet_id)
    if qid:
        logger.info("Posted quote tweet (%d chars): %s (quoting %s)", len(QUOTE_TEXT), qid, target_tweet_id)
    else:
        logger.warning("Failed to post quote tweet")

    # Reply to the given tweet (full length)
    rid = x_api.post_reply(REPLY_TEXT, target_tweet_id)
    if rid:
        logger.info("Posted reply (%d chars): %s (to %s)", len(REPLY_TEXT), rid, target_tweet_id)
    else:
        logger.warning("Failed to post reply")

    logger.info("Done. Check your account on X.")


if __name__ == "__main__":
    main()
