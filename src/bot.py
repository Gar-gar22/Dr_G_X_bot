"""Main bot logic that orchestrates fetching, generating, and posting replies."""
import logging
import random
import time
from typing import List, Dict, Any, Optional
from datetime import datetime

from .config import Config
from .database import Database
from .x_api import XAPI
from .reply_generator import ReplyGenerator
from .tweet_generator import TweetGenerator
from .telegram_approver import get_telegram_approver

logger = logging.getLogger(__name__)


class AutoReplyBot:
    """Main bot class that orchestrates the auto-reply functionality."""

    def __init__(self, config: Config):
        """Initialize the bot with configuration."""
        self.config = config
        self.db = Database()
        self.x_api = XAPI(config.get_x_api_credentials())
        ai_config = config.get_ai_config()
        profile = config.get_prompt_profile_name()
        self.reply_generator = ReplyGenerator(
            config.get_reply_templates(),
            ai_config,
            db=self.db,
            prompt_profile_name=profile,
        )
        self.tweet_generator = TweetGenerator(
            config.get_tweet_templates(),
            ai_config,
            db=self.db,
            prompt_profile_name=profile,
        )
        self.reply_settings = config.get_reply_settings()
        self.tweet_settings = config.get_tweet_settings()
        self.filters = config.get_filters()
        self.mitl_enabled = config.get_mitl_enabled()
        self.telegram = get_telegram_approver()
        self.safety = config.get_safety_config()

    def _passes_safety(self, text: str, *, for_post: bool = True) -> bool:
        from .quality_safety import (
            validate_before_post,
            check_text_quality,
            check_duplicate_text,
            check_rate_budget,
            log_safety_event,
        )

        if for_post:
            ok, reason = validate_before_post(self.db, text, self.safety)
        else:
            ok, reason = check_rate_budget(self.db, self.safety)
            if ok:
                ok, reason = check_text_quality(text, self.safety)
            if ok:
                ok, reason = check_duplicate_text(self.db, text, self.safety)
        if not ok:
            log_safety_event(self.db, "bot_blocked", reason, {"preview": (text or "")[:120]})
            logger.warning(f"Safety blocked: {reason}")
            return False
        return True

    def run(self, dry_run: bool = False) -> Dict[str, Any]:
        """Execute one bot run: fetch tweets, generate replies, draft or post them."""
        logger.info(
            "Starting bot run..."
            + (" (dry run)" if dry_run else "")
            + (" (MITL)" if self.mitl_enabled and not dry_run else "")
        )
        start_time = datetime.now()

        stats = {
            "tweets_fetched": 0,
            "tweets_filtered": 0,
            "replies_generated": 0,
            "replies_posted": 0,
            "drafts_created": 0,
            "blocked": 0,
            "errors": 0,
            "preview": [] if dry_run else None,
            "mitl": self.mitl_enabled and not dry_run,
        }

        try:
            if not dry_run:
                from .quality_safety import check_rate_budget, log_safety_event

                ok, reason = check_rate_budget(self.db, self.safety)
                if not ok:
                    log_safety_event(self.db, "run_blocked", reason, {})
                    stats["blocked"] += 1
                    stats["errors"] += 1
                    logger.warning(f"Bot run aborted: {reason}")
                    return stats

            logger.info("Collecting tweet candidates...")
            candidates = self._collect_tweet_candidates()
            stats["tweets_fetched"] = len(candidates)
            logger.info(f"Found {len(candidates)} tweet candidates")

            if len(candidates) == 0:
                logger.warning("No tweet candidates found.")
                if dry_run and "preview" in stats:
                    stats["preview"] = []
                return stats

            logger.info("Filtering tweet candidates...")
            filtered = self._filter_tweets(candidates)
            stats["tweets_filtered"] = len(filtered)
            logger.info(f"After filtering: {len(filtered)} tweets remain")

            if len(filtered) == 0:
                if dry_run and "preview" in stats:
                    stats["preview"] = []
                return stats

            max_replies = self.reply_settings.get("max_replies_per_run", 20)
            if (
                "min_replies_per_run" in self.reply_settings
                and "max_replies_per_run" in self.reply_settings
            ):
                min_replies = self.reply_settings.get("min_replies_per_run", 5)
                max_replies = self.reply_settings.get("max_replies_per_run", 50)
                max_replies = random.randint(min_replies, max_replies)
            delay_min = self.reply_settings.get("delay_minutes_min", 5)
            delay_max = self.reply_settings.get("delay_minutes_max", 15)

            selected = filtered[:max_replies]
            logger.info(f"Selected {len(selected)} tweets to reply to (target: {max_replies})")

            use_delays = not dry_run and not self.mitl_enabled
            if use_delays and len(selected) > 1:
                time_per_reply = (delay_min * 60) / len(selected)
                time_per_reply = random.uniform(time_per_reply * 0.8, time_per_reply * 1.2)
            else:
                time_per_reply = delay_min * 60

            quote_tweet_ratio = self.reply_settings.get("quote_tweet_ratio", 0.3)

            for idx, tweet in enumerate(selected):
                try:
                    use_quote_tweet = random.random() < quote_tweet_ratio
                    kind = "quote" if use_quote_tweet else "reply"
                    content_type = "quote" if use_quote_tweet else "reply"

                    text = self.reply_generator.generate_reply(
                        tweet, keyword=tweet.get("keyword"), content_type=content_type
                    )
                    if not text:
                        logger.warning(f"Failed to generate {kind} for tweet {tweet['id']}")
                        stats["errors"] += 1
                        continue

                    stats["replies_generated"] += 1
                    logger.info(f"Generated {kind} for tweet {tweet['id']}: {text[:50]}...")

                    if not dry_run and not self._passes_safety(text, for_post=not self.mitl_enabled):
                        stats["blocked"] += 1
                        continue

                    if dry_run:
                        stats["preview"].append(
                            {
                                "tweet_id": tweet["id"],
                                "type": kind,
                                "text": text,
                                "keyword": tweet.get("keyword"),
                                "source": tweet.get("source", "unknown"),
                            }
                        )
                        stats["replies_posted"] += 1
                    elif self.mitl_enabled:
                        draft_id = self._create_engagement_draft(kind, tweet, text)
                        if draft_id:
                            stats["drafts_created"] += 1
                        else:
                            stats["errors"] += 1
                    else:
                        ok = self._post_engagement(kind, tweet, text)
                        if ok:
                            stats["replies_posted"] += 1
                        else:
                            stats["errors"] += 1

                    if use_delays and idx < len(selected) - 1:
                        delay_variation = time_per_reply * 0.2
                        delay_seconds = int(
                            time_per_reply + random.uniform(-delay_variation, delay_variation)
                        )
                        delay_seconds = max(delay_seconds, 5 * 60)
                        delay_seconds = min(delay_seconds, 45 * 60)
                        logger.info(f"Waiting {delay_seconds // 60} minutes before next reply...")
                        time.sleep(delay_seconds)

                except Exception as e:
                    logger.error(f"Error processing tweet {tweet.get('id', 'unknown')}: {e}")
                    stats["errors"] += 1
                    continue

            duration = (datetime.now() - start_time).total_seconds()
            logger.info(f"Bot run completed in {duration:.1f} seconds. Stats: {stats}")
            if dry_run and stats.get("preview") is None:
                stats["preview"] = []
            return stats

        except Exception as e:
            logger.error(f"Error in bot run: {e}", exc_info=True)
            stats["errors"] += 1
            return stats

    def _create_engagement_draft(
        self, kind: str, tweet: Dict[str, Any], text: str
    ) -> Optional[int]:
        try:
            draft_id = self.db.create_draft(
                {
                    "kind": kind,
                    "status": "pending",
                    "target_tweet_id": tweet["id"],
                    "target_tweet_text": tweet.get("text"),
                    "target_author": tweet.get("author_username"),
                    "keyword": tweet.get("keyword"),
                    "source": tweet.get("source", "unknown"),
                    "generated_text": text,
                    "provider": self.reply_generator.last_provider,
                    "model": self.reply_generator.last_model,
                    "prompt_profile_id": self.reply_generator.last_profile_id,
                }
            )
            draft = self.db.get_draft(draft_id)
            self.telegram.notify_draft(draft_id, draft or {})
            return draft_id
        except Exception as e:
            logger.error(f"Failed to create draft: {e}", exc_info=True)
            return None

    def _post_engagement(self, kind: str, tweet: Dict[str, Any], text: str) -> bool:
        try:
            if kind == "quote":
                quote_id = self.x_api.quote_tweet(text, tweet["id"])
                if not quote_id:
                    return False
                self.db.mark_tweet_replied(
                    tweet["id"],
                    quote_id,
                    source=tweet.get("source", "unknown"),
                    keyword=tweet.get("keyword"),
                )
                self.db.mark_quote_retweet_posted(quote_id, tweet["id"], text)
                return True

            reply_id = self.x_api.post_reply(text, tweet["id"])
            if not reply_id:
                return False
            self.db.mark_tweet_replied(
                tweet["id"],
                reply_id,
                source=tweet.get("source", "unknown"),
                keyword=tweet.get("keyword"),
            )
            return True
        except Exception as e:
            logger.error(f"Post engagement failed: {e}", exc_info=True)
            return False

    def _collect_tweet_candidates(self) -> List[Dict[str, Any]]:
        """Collect tweet candidates from timeline and keyword searches."""
        candidates = []
        replied_ids = self.db.get_replied_tweet_ids()

        # Also exclude tweets that already have a pending draft
        try:
            pending = self.db.list_drafts(status="pending", limit=500)
            pending_targets = {
                d["target_tweet_id"] for d in pending if d.get("target_tweet_id")
            }
            replied_ids = replied_ids | pending_targets
        except Exception:
            pass

        timeline_count = self.reply_settings.get("timeline_count", 50)
        timeline_tweets = self.x_api.get_home_timeline(count=timeline_count)
        for tweet in timeline_tweets:
            if tweet["id"] not in replied_ids:
                tweet["source"] = "timeline"
                candidates.append(tweet)

        keywords = self.config.get_keywords()
        keyword_count = self.reply_settings.get("keyword_search_count", 20)

        for keyword in keywords:
            search_tweets = self.x_api.search_tweets(keyword, count=keyword_count)
            for tweet in search_tweets:
                if tweet["id"] not in replied_ids:
                    tweet["source"] = "keyword_search"
                    tweet["keyword"] = keyword
                    candidates.append(tweet)

        seen_ids = set()
        unique_candidates = []
        for tweet in candidates:
            if tweet["id"] not in seen_ids:
                seen_ids.add(tweet["id"])
                unique_candidates.append(tweet)

        return unique_candidates

    def _filter_tweets(self, tweets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Filter tweets based on configuration filters."""
        filtered = []
        user_id = str(self.x_api.user_id) if self.x_api.user_id else None
        min_followers = self.filters.get("min_followers", 0)

        for tweet in tweets:
            if self.filters.get("exclude_retweets", True) and tweet.get("is_retweet"):
                continue
            if self.filters.get("exclude_own_tweets", True) and user_id:
                if tweet.get("author_id") == user_id:
                    continue
            if min_followers > 0:
                author_followers = tweet.get("author_followers_count", 0)
                if author_followers < min_followers:
                    continue
            if self.filters.get("exclude_replied_tweets", True):
                if self.db.is_tweet_replied(tweet["id"]):
                    continue
            filtered.append(tweet)

        random.shuffle(filtered)
        return filtered

    def post_tweet(self) -> Dict[str, Any]:
        """Post or draft a single original tweet based on keywords."""
        logger.info("Posting a new tweet..." + (" (MITL draft)" if self.mitl_enabled else ""))
        stats = {"tweet_posted": False, "draft_id": None, "error": None}

        try:
            keywords = self.config.get_keywords()
            tweet_text = self.tweet_generator.generate_tweet(keywords=keywords)
            if not tweet_text:
                stats["error"] = "Failed to generate tweet"
                return stats

            if not self._passes_safety(tweet_text, for_post=not self.mitl_enabled):
                stats["error"] = "Blocked by safety gates"
                return stats

            if self.mitl_enabled:
                draft_id = self.db.create_draft(
                    {
                        "kind": "tweet",
                        "status": "pending",
                        "generated_text": tweet_text,
                        "provider": self.tweet_generator.last_provider,
                        "model": self.tweet_generator.last_model,
                        "prompt_profile_id": self.tweet_generator.last_profile_id,
                        "source": "scheduled_tweet",
                    }
                )
                draft = self.db.get_draft(draft_id)
                self.telegram.notify_draft(draft_id, draft or {})
                stats["draft_id"] = draft_id
                stats["tweet_posted"] = False
                return stats

            tweet_id = self.x_api.post_tweet(tweet_text)
            if tweet_id:
                self.db.mark_tweet_posted(tweet_id, tweet_text)
                stats["tweet_posted"] = True
                stats["tweet_id"] = tweet_id
            else:
                stats["error"] = "Failed to post tweet"
        except Exception as e:
            logger.error(f"Error posting tweet: {e}", exc_info=True)
            stats["error"] = str(e)
        return stats

    def post_thread_tweet(self, num_tweets: int = 3) -> Dict[str, Any]:
        """Post or draft a thread of tweets based on keywords."""
        logger.info(
            f"Posting a new thread with {num_tweets} tweets..."
            + (" (MITL draft)" if self.mitl_enabled else "")
        )
        stats = {"thread_posted": False, "draft_id": None, "error": None}

        try:
            import json

            keywords = self.config.get_keywords()
            tweet_texts = self.tweet_generator.generate_thread(
                keywords=keywords, num_tweets=num_tweets
            )
            if not tweet_texts:
                stats["error"] = "Failed to generate thread"
                return stats

            if self.mitl_enabled:
                draft_id = self.db.create_draft(
                    {
                        "kind": "thread",
                        "status": "pending",
                        "generated_text": tweet_texts[0] if tweet_texts else "",
                        "thread_texts": tweet_texts,
                        "provider": self.tweet_generator.last_provider,
                        "model": self.tweet_generator.last_model,
                        "prompt_profile_id": self.tweet_generator.last_profile_id,
                        "source": "scheduled_thread",
                    }
                )
                draft = self.db.get_draft(draft_id)
                self.telegram.notify_draft(draft_id, draft or {})
                stats["draft_id"] = draft_id
                return stats

            thread_ids = self.x_api.post_thread(tweet_texts)
            if thread_ids:
                self.db.mark_thread_posted(thread_ids, tweet_texts)
                stats["thread_posted"] = True
                stats["thread_ids"] = thread_ids
                stats["num_tweets"] = len(thread_ids)
            else:
                stats["error"] = "Failed to post thread"
        except Exception as e:
            logger.error(f"Error posting thread: {e}", exc_info=True)
            stats["error"] = str(e)
        return stats

    def close(self) -> None:
        """Clean up resources."""
        self.db.close()
