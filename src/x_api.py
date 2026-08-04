"""X (Twitter) API integration using Tweepy."""
import logging
import time
from typing import List, Dict, Any, Optional
import tweepy

logger = logging.getLogger(__name__)


class XAPI:
    """Handles all interactions with X (Twitter) API."""
    
    def __init__(self, credentials: Dict[str, str]):
        """Initialize X API client with credentials."""
        self.client = None
        self.api = None
        self.user_id = None
        self._authenticate(credentials)
    
    def _authenticate(self, credentials: Dict[str, str]) -> None:
        """Authenticate with X API using OAuth 1.0a and API v2."""
        try:
            # OAuth 1.0a for write operations (API v1.1)
            auth = tweepy.OAuth1UserHandler(
                credentials["consumer_key"],
                credentials["consumer_secret"],
                credentials["access_token"],
                credentials["access_token_secret"]
            )
            self.api = tweepy.API(auth, wait_on_rate_limit=True)
            
            # API v2 client for read operations
            self.client = tweepy.Client(
                bearer_token=credentials.get("bearer_token"),
                consumer_key=credentials["consumer_key"],
                consumer_secret=credentials["consumer_secret"],
                access_token=credentials["access_token"],
                access_token_secret=credentials["access_token_secret"],
                wait_on_rate_limit=True
            )
            
            # Get authenticated user ID
            me = self.api.verify_credentials()
            self.user_id = me.id
            logger.info(f"Authenticated as @{me.screen_name} (ID: {me.id})")
        
        except Exception as e:
            logger.error(f"Authentication failed: {e}")
            raise
    
    def get_home_timeline(self, count: int = 800) -> List[Dict[str, Any]]:
        """Fetch tweets from home timeline (what shows in your feed).
        
        Uses pagination to fetch more tweets from your timeline.
        """
        try:
            result = []
            max_per_page = 200
            pages_needed = (count + max_per_page - 1) // max_per_page
            
            # Use Cursor to paginate through home timeline
            for page in tweepy.Cursor(
                self.api.home_timeline,
                tweet_mode='extended',
                count=max_per_page
            ).pages(pages_needed):
                for tweet in page:
                    result.append({
                        "id": str(tweet.id),
                        "text": tweet.full_text,
                        "author_id": str(tweet.user.id),
                        "author_username": tweet.user.screen_name,
                        "author_followers_count": tweet.user.followers_count,
                        "created_at": tweet.created_at,
                        "is_retweet": hasattr(tweet, 'retweeted_status'),
                        "in_reply_to_status_id": str(tweet.in_reply_to_status_id) if tweet.in_reply_to_status_id else None
                    })
                    
                    if len(result) >= count:
                        break
                
                if len(result) >= count:
                    break
            
            logger.info(f"Fetched {len(result)} tweets from home timeline")
            return result
        
        except Exception as e:
            logger.error(f"Error fetching home timeline: {e}")
            return []

    def get_user_tweets(self, count: int = 3200) -> List[Dict[str, Any]]:
        """Fetch tweets from the authenticated user's own timeline (both recent and old).
        
        Uses pagination to fetch up to 3200 tweets (Twitter API v1.1 limit).
        """
        try:
            if not self.user_id:
                logger.error("User ID not available. Cannot fetch user tweets.")
                return []
            
            result = []
            max_per_page = 200
            pages_needed = (count + max_per_page - 1) // max_per_page
            
            logger.info(f"Fetching tweets for user ID: {self.user_id}, target count: {count}")
            
            # Use Cursor to paginate through older tweets
            # Explicitly use user_id to ensure we get the authenticated user's tweets
            page_count = 0
            for page in tweepy.Cursor(
                self.api.user_timeline,
                user_id=self.user_id,
                tweet_mode="extended",
                include_rts=True,
                exclude_replies=False,
                count=max_per_page
            ).pages(pages_needed):
                page_count += 1
                
                if not page:
                    logger.warning(f"No tweets returned on page {page_count}")
                    break
                
                for tweet in page:
                    # Handle both text and full_text attributes
                    tweet_text = getattr(tweet, 'full_text', None) or getattr(tweet, 'text', None) or ""
                    
                    if not tweet_text:
                        logger.warning(f"Tweet {tweet.id} has no text, skipping")
                        continue
                    
                    result.append(
                        {
                            "id": str(tweet.id),
                            "text": tweet_text,
                            "author_id": str(tweet.user.id),
                            "author_username": tweet.user.screen_name,
                            "author_followers_count": tweet.user.followers_count,
                            "created_at": tweet.created_at,
                            "is_retweet": hasattr(tweet, "retweeted_status"),
                            "in_reply_to_status_id": str(tweet.in_reply_to_status_id)
                            if tweet.in_reply_to_status_id
                            else None,
                        }
                    )
                    
                    if len(result) >= count:
                        break
                
                if len(result) >= count:
                    break
                
                # If we got fewer tweets than expected, we might have reached the end
                if page and len(page) < max_per_page:
                    logger.info(f"Reached end of timeline (got {len(page)} tweets on last page)")
                    break

            logger.info(f"Fetched {len(result)} tweets from authenticated user timeline")
            if len(result) == 0:
                logger.warning("No tweets fetched. This might indicate an API issue or the user has no tweets.")
            return result

        except tweepy.Unauthorized as e:
            logger.error(f"Unauthorized to fetch user tweets: {e}. Check your API credentials.")
            return []
        except tweepy.NotFound as e:
            logger.error(f"User not found when fetching tweets: {e}")
            return []
        except Exception as e:
            logger.error(f"Error fetching user tweets: {e}", exc_info=True)
            return []

    def get_liked_tweets(self, count: int = 3200) -> List[Dict[str, Any]]:
        """Fetch tweets liked by the authenticated user (both recent and old).
        
        Uses pagination to fetch up to 3200 liked tweets (Twitter API v1.1 limit).
        """
        try:
            result = []
            max_per_page = 200
            pages_needed = (count + max_per_page - 1) // max_per_page

            # Use Cursor to paginate through older liked tweets
            for page in tweepy.Cursor(
                self.api.get_favorites,
                count=max_per_page
            ).pages(pages_needed):
                for tweet in page:
                    result.append(
                        {
                            "id": str(tweet.id),
                            "text": getattr(tweet, "full_text", tweet.text),
                            "author_id": str(tweet.user.id),
                            "author_username": tweet.user.screen_name,
                            "author_followers_count": tweet.user.followers_count,
                            "created_at": tweet.created_at,
                            "is_retweet": hasattr(tweet, "retweeted_status"),
                            "in_reply_to_status_id": str(tweet.in_reply_to_status_id)
                            if tweet.in_reply_to_status_id
                            else None,
                        }
                    )
                    
                    if len(result) >= count:
                        break
                
                if len(result) >= count:
                    break

            logger.info(f"Fetched {len(result)} liked tweets from authenticated user (including older likes)")
            return result

        except Exception as e:
            logger.error(f"Error fetching liked tweets: {e}")
            return []
    
    def search_tweets(self, query: str, count: int = 20) -> List[Dict[str, Any]]:
        """Search for tweets matching a query."""
        try:
            # Use API v2 for search
            tweets = self.client.search_recent_tweets(
                query=query,
                max_results=min(count, 100),  # API v2 limit is 100 per request
                tweet_fields=['created_at', 'author_id', 'in_reply_to_user_id', 'public_metrics'],
                expansions=['author_id'],
                user_fields=['username', 'public_metrics']
            )
            
            if not tweets.data:
                return []
            
            # Create user lookup dict with follower counts
            users = {}
            for user in tweets.includes.get('users', []):
                users[user.id] = {
                    'username': user.username,
                    'followers_count': user.public_metrics.get('followers_count', 0) if hasattr(user, 'public_metrics') and user.public_metrics else 0
                }
            
            result = []
            for tweet in tweets.data:
                user_data = users.get(tweet.author_id, {'username': 'unknown', 'followers_count': 0})
                result.append({
                    "id": str(tweet.id),
                    "text": tweet.text,
                    "author_id": str(tweet.author_id),
                    "author_username": user_data['username'],
                    "author_followers_count": user_data['followers_count'],
                    "created_at": tweet.created_at,
                    "is_retweet": False,  # v2 search doesn't return retweets by default
                    "in_reply_to_status_id": str(tweet.in_reply_to_user_id) if hasattr(tweet, 'in_reply_to_user_id') and tweet.in_reply_to_user_id else None
                })
            
            logger.info(f"Found {len(result)} tweets for query: {query}")
            return result
        
        except Exception as e:
            logger.error(f"Error searching tweets for '{query}': {e}")
            return []
    
    def upload_media(self, file_path: str) -> Optional[str]:
        """Upload an image file via API v1.1; returns media_id string."""
        try:
            media = self.api.media_upload(filename=file_path)
            media_id = str(media.media_id)
            logger.info(f"Uploaded media {media_id} from {file_path}")
            return media_id
        except Exception as e:
            logger.error(f"Error uploading media: {e}", exc_info=True)
            return None

    def upload_media_files(self, file_paths: list) -> list:
        """Upload multiple files; returns list of media_id strings (skips failures)."""
        ids = []
        for path in (file_paths or [])[:4]:
            mid = self.upload_media(path)
            if mid:
                ids.append(mid)
        return ids

    def post_reply(
        self,
        text: str,
        in_reply_to_tweet_id: str,
        media_ids: Optional[list] = None,
    ) -> Optional[str]:
        """Post a reply to a tweet (optionally with images)."""
        try:
            kwargs = {
                "status": text,
                "in_reply_to_status_id": in_reply_to_tweet_id,
                "auto_populate_reply_metadata": True,
            }
            if media_ids:
                kwargs["media_ids"] = [str(m) for m in media_ids[:4]]
            status = self.api.update_status(**kwargs)
            reply_id = str(status.id)
            logger.info(f"Posted reply {reply_id} to tweet {in_reply_to_tweet_id}")
            return reply_id

        except tweepy.TooManyRequests:
            logger.warning("Rate limit exceeded. Waiting...")
            time.sleep(900)
            return None
        except Exception as e:
            logger.error(f"Error posting reply: {e}")
            return None

    def post_tweet(self, text: str, media_ids: Optional[list] = None) -> Optional[str]:
        """Post an original tweet (optionally with images)."""
        try:
            media_ids = [str(m) for m in (media_ids or [])[:4]] or None

            if self.client:
                try:
                    kwargs = {"text": text}
                    if media_ids:
                        kwargs["media_ids"] = media_ids
                    response = self.client.create_tweet(**kwargs)
                    if response and response.data:
                        tweet_id = str(response.data["id"])
                        logger.info(f"Posted tweet {tweet_id} via API v2: {text[:50]}...")
                        return tweet_id
                except Exception as v2_error:
                    logger.warning(f"API v2 failed, trying v1.1: {v2_error}")

            if self.api:
                kwargs = {"status": text}
                if media_ids:
                    kwargs["media_ids"] = media_ids
                status = self.api.update_status(**kwargs)
                tweet_id = str(status.id)
                logger.info(f"Posted tweet {tweet_id} via API v1.1: {text[:50]}...")
                return tweet_id

            logger.error("No API client available for posting tweet")
            return None

        except tweepy.TooManyRequests:
            logger.warning("Rate limit exceeded. Waiting...")
            time.sleep(900)
            return None
        except Exception as e:
            logger.error(f"Error posting tweet: {e}")
            return None
    
    def post_thread(self, tweet_texts: List[str]) -> Optional[List[str]]:
        """Post a thread of tweets. Each tweet replies to the previous one."""
        try:
            if not tweet_texts:
                logger.error("No tweet texts provided for thread")
                return None
            
            thread_ids = []
            previous_tweet_id = None
            
            for idx, text in enumerate(tweet_texts):
                try:
                    if previous_tweet_id:
                        # Post as reply to previous tweet (try API v2 first, fallback to v1.1)
                        if self.client:
                            try:
                                response = self.client.create_tweet(
                                    text=text,
                                    in_reply_to_tweet_id=previous_tweet_id
                                )
                                if response and response.data:
                                    tweet_id = str(response.data['id'])
                                    logger.info(f"Posted thread tweet {idx + 1}/{len(tweet_texts)}: {tweet_id} via API v2 (replying to {previous_tweet_id})")
                                else:
                                    raise Exception("No data in API v2 response")
                            except Exception as v2_error:
                                logger.warning(f"API v2 reply failed, trying v1.1: {v2_error}")
                                # Fallback to API v1.1
                                if self.api:
                                    status = self.api.update_status(
                                        status=text,
                                        in_reply_to_status_id=previous_tweet_id,
                                        auto_populate_reply_metadata=True
                                    )
                                    tweet_id = str(status.id)
                                    logger.info(f"Posted thread tweet {idx + 1}/{len(tweet_texts)}: {tweet_id} via API v1.1 (replying to {previous_tweet_id})")
                                else:
                                    raise Exception("No API client available")
                        else:
                            # Use API v1.1 if v2 client not available
                            if self.api:
                                status = self.api.update_status(
                                    status=text,
                                    in_reply_to_status_id=previous_tweet_id,
                                    auto_populate_reply_metadata=True
                                )
                                tweet_id = str(status.id)
                                logger.info(f"Posted thread tweet {idx + 1}/{len(tweet_texts)}: {tweet_id} via API v1.1 (replying to {previous_tweet_id})")
                            else:
                                raise Exception("No API client available")
                    else:
                        # Post first tweet (try API v2 first, fallback to v1.1)
                        if self.client:
                            try:
                                response = self.client.create_tweet(text=text)
                                if response and response.data:
                                    tweet_id = str(response.data['id'])
                                    logger.info(f"Posted thread tweet 1/{len(tweet_texts)}: {tweet_id} via API v2")
                                else:
                                    raise Exception("No data in API v2 response")
                            except Exception as v2_error:
                                logger.warning(f"API v2 failed for first tweet, trying v1.1: {v2_error}")
                                status = self.api.update_status(status=text)
                                tweet_id = str(status.id)
                                logger.info(f"Posted thread tweet 1/{len(tweet_texts)}: {tweet_id} via API v1.1")
                        else:
                            status = self.api.update_status(status=text)
                            tweet_id = str(status.id)
                            logger.info(f"Posted thread tweet 1/{len(tweet_texts)}: {tweet_id} via API v1.1")
                    
                    thread_ids.append(tweet_id)
                    previous_tweet_id = tweet_id
                    
                    # Small delay between tweets in thread (except after last one)
                    if idx < len(tweet_texts) - 1:
                        time.sleep(2)  # 2 second delay between tweets
                
                except tweepy.TooManyRequests:
                    logger.warning("Rate limit exceeded while posting thread. Waiting...")
                    time.sleep(900)  # Wait 15 minutes
                    # Return partial thread if we got some tweets posted
                    if thread_ids:
                        logger.warning(f"Thread partially posted: {len(thread_ids)}/{len(tweet_texts)} tweets")
                        return thread_ids
                    return None
                except Exception as e:
                    logger.error(f"Error posting thread tweet {idx + 1}: {e}")
                    # If we got at least one tweet posted, return partial thread
                    if thread_ids:
                        logger.warning(f"Thread partially posted: {len(thread_ids)}/{len(tweet_texts)} tweets")
                        return thread_ids
                    return None
            
            logger.info(f"Successfully posted thread with {len(thread_ids)} tweets")
            return thread_ids
        
        except Exception as e:
            logger.error(f"Error posting thread: {e}")
            return None
    
    def retweet(self, tweet_id: str) -> Optional[str]:
        """Retweet (repost) a tweet (using API v1.1)."""
        try:
            # Use API v1.1 for retweeting
            status = self.api.retweet(id=tweet_id)
            retweet_id = str(status.id) if status else tweet_id
            logger.info(f"Retweeted tweet {tweet_id} as {retweet_id}")
            return retweet_id
        except tweepy.TooManyRequests:
            logger.warning("Rate limit exceeded. Waiting...")
            time.sleep(900)  # Wait 15 minutes
            return None
        except tweepy.Forbidden as e:
            # Already retweeted or not allowed
            logger.warning(f"Cannot retweet {tweet_id}: {e}")
            return None
        except Exception as e:
            logger.error(f"Error retweeting tweet {tweet_id}: {e}")
            return None
    
    def quote_tweet(
        self, text: str, tweet_id: str, media_ids: Optional[list] = None
    ) -> Optional[str]:
        """Post a quote tweet (optionally with images)."""
        try:
            media_ids = [str(m) for m in (media_ids or [])[:4]] or None

            if self.client:
                try:
                    kwargs = {"text": text, "quote_tweet_id": tweet_id}
                    if media_ids:
                        kwargs["media_ids"] = media_ids
                    response = self.client.create_tweet(**kwargs)
                    if response and response.data:
                        quote_id = str(response.data["id"])
                        logger.info(
                            f"Posted quote tweet {quote_id} via API v2 quoting tweet {tweet_id}"
                        )
                        return quote_id
                except Exception as v2_error:
                    logger.warning(f"API v2 quote tweet failed, trying v1.1: {v2_error}")

            if self.api:
                tweet_url = f"https://twitter.com/i/web/status/{tweet_id}"
                max_text_len = 280 - len(tweet_url) - 1
                if len(text) > max_text_len:
                    text = text[: max_text_len - 3] + "..."
                quote_text = f"{text} {tweet_url}"
                kwargs = {"status": quote_text}
                if media_ids:
                    kwargs["media_ids"] = media_ids
                status = self.api.update_status(**kwargs)
                quote_id = str(status.id)
                logger.info(
                    f"Posted quote tweet {quote_id} via API v1.1 quoting tweet {tweet_id}"
                )
                return quote_id

            logger.error("No API client available for posting quote tweet")
            return None
        except tweepy.TooManyRequests:
            logger.warning("Rate limit exceeded. Waiting...")
            time.sleep(900)
            return None
        except Exception as e:
            logger.error(f"Error posting quote tweet: {e}")
            return None
    
    def get_user_info(self) -> Dict[str, Any]:
        """Get authenticated user information."""
        try:
            me = self.api.verify_credentials()
            return {
                "id": me.id,
                "username": me.screen_name,
                "name": me.name,
                "followers_count": me.followers_count,
                "friends_count": me.friends_count
            }
        except Exception as e:
            logger.error(f"Error getting user info: {e}")
            return {}

