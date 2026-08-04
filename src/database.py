"""Database management for tracking replied tweets using PostgreSQL."""
import logging
import os
import time
from typing import Set, Optional, List, Dict, Any
from datetime import datetime
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# libpq accepts: disable, allow, prefer, require, verify-ca, verify-full
# People often typo "required" on Render — normalize common aliases.
_SSLMODE_ALIASES = {
    "required": "require",
    "req": "require",
    "verified": "verify-full",
}


def _normalize_sslmode(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    mode = value.strip().lower()
    if not mode:
        return None
    return _SSLMODE_ALIASES.get(mode, mode)


def _normalize_database_url(url: str, sslmode: Optional[str] = None) -> str:
    """Fix postgres:// scheme and invalid sslmode=required in URLs."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    # Fix typo embedded in connection string
    url = url.replace("sslmode=required", "sslmode=require")
    url = url.replace("sslmode=Required", "sslmode=require")
    if sslmode and "sslmode=" not in url.lower():
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}sslmode={sslmode}"
    return url


class Database:
    """Manages PostgreSQL database for tracking replied tweets."""

    def __init__(
        self,
        host: str = None,
        port: int = None,
        user: str = None,
        password: str = None,
        database: str = None,
    ):
        """Initialize database connection and create tables if needed."""
        self.database_url = os.getenv("DATABASE_URL", "").strip() or None
        # Render / Heroku often use postgres:// — normalize for psycopg2
        if self.database_url and self.database_url.startswith("postgres://"):
            self.database_url = "postgresql://" + self.database_url[len("postgres://") :]

        self.host = host or os.getenv("DB_HOST", "localhost")
        self.port = port or int(os.getenv("DB_PORT", "5432"))
        self.user = user or os.getenv("DB_USER", "postgres")
        db_password = password if password is not None else os.getenv("DB_PASSWORD", "")
        self.password = db_password if db_password else ""
        self.database = database or os.getenv("DB_NAME", "twitter")
        self.sslmode = _normalize_sslmode(os.getenv("DB_SSLMODE", ""))
        if self.database_url:
            self.database_url = _normalize_database_url(self.database_url, None)
            # Apply sslmode to URL only if not already present
            if self.sslmode and "sslmode=" not in self.database_url.lower():
                self.database_url = _normalize_database_url(
                    self.database_url, self.sslmode
                )
        max_retries = 3
        retry_delay = 2

        for attempt in range(max_retries):
            try:
                if not self.database_url:
                    self._ensure_database_exists()
                self.conn = self._open_connection(self.database)
                self.conn.autocommit = False
                self.create_tables()
                if self.database_url:
                    logger.info("Connected to PostgreSQL via DATABASE_URL")
                else:
                    logger.info(
                        f"Connected to PostgreSQL database '{self.database}' "
                        f"on {self.host}:{self.port}"
                    )
                break
            except psycopg2.OperationalError as e:
                if attempt < max_retries - 1:
                    logger.warning(
                        f"Connection attempt {attempt + 1} failed: {e}. "
                        f"Retrying in {retry_delay} seconds..."
                    )
                    time.sleep(retry_delay)
                    retry_delay *= 2
                else:
                    logger.error(
                        f"Failed to connect to PostgreSQL after {max_retries} attempts: {e}"
                    )
                    raise
            except Exception as e:
                logger.error(f"Error connecting to PostgreSQL database: {e}")
                raise

    def _connect_kwargs(self, dbname: Optional[str] = None) -> dict:
        """Build psycopg2.connect kwargs from DATABASE_URL or discrete env vars."""
        if self.database_url:
            dsn = _normalize_database_url(self.database_url, None)
            if self.sslmode and "sslmode=" not in dsn.lower():
                dsn = _normalize_database_url(dsn, self.sslmode)
            return {
                "dsn": dsn,
                "cursor_factory": RealDictCursor,
                "connect_timeout": 10,
            }

        kwargs = {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "password": self.password,
            "dbname": dbname or self.database,
            "cursor_factory": RealDictCursor,
            "connect_timeout": 10,
        }
        if self.sslmode:
            kwargs["sslmode"] = self.sslmode
        return kwargs

    def _open_connection(self, dbname: Optional[str] = None):
        return psycopg2.connect(**self._connect_kwargs(dbname))

    def _ensure_database_exists(self) -> None:
        """Create the application database locally if it does not exist."""
        # Connect to the default maintenance DB
        admin = psycopg2.connect(**self._connect_kwargs("postgres"))
        admin.autocommit = True
        cursor = admin.cursor()
        try:
            cursor.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (self.database,)
            )
            if cursor.fetchone() is None:
                # Identifiers cannot be parameterized; sanitize to [a-zA-Z0-9_]
                safe_name = "".join(c for c in self.database if c.isalnum() or c == "_")
                if not safe_name:
                    raise ValueError(f"Invalid database name: {self.database}")
                cursor.execute(f'CREATE DATABASE "{safe_name}"')
                logger.info(f"Created PostgreSQL database '{safe_name}'")
            else:
                logger.info(f"Database '{self.database}' already exists")
        finally:
            cursor.close()
            admin.close()

    def create_tables(self) -> None:
        """Create necessary tables if they don't exist."""
        cursor = self.conn.cursor()
        try:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS replied_tweets (
                    tweet_id VARCHAR(50) PRIMARY KEY,
                    reply_tweet_id VARCHAR(50),
                    replied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    source VARCHAR(50),
                    keyword VARCHAR(255)
                )
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_replied_at ON replied_tweets (replied_at)"
            )

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS posted_tweets (
                    tweet_id VARCHAR(50) PRIMARY KEY,
                    text TEXT,
                    posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_posted_tweets_at ON posted_tweets (posted_at)"
            )

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS posted_threads (
                    thread_id VARCHAR(50) PRIMARY KEY,
                    first_tweet_id VARCHAR(50),
                    tweet_ids TEXT,
                    texts TEXT,
                    posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_posted_threads_at ON posted_threads (posted_at)"
            )

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS quote_retweets (
                    quote_tweet_id VARCHAR(50) PRIMARY KEY,
                    original_tweet_id VARCHAR(50),
                    text TEXT,
                    posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_quote_posted_at ON quote_retweets (posted_at)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_original_tweet ON quote_retweets (original_tweet_id)"
            )

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_logs (
                    id SERIAL PRIMARY KEY,
                    log_level VARCHAR(20),
                    message TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_bot_logs_created ON bot_logs (created_at)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_bot_logs_level ON bot_logs (log_level)"
            )

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS api_credentials (
                    id SERIAL PRIMARY KEY,
                    service_name VARCHAR(50) UNIQUE NOT NULL,
                    consumer_key VARCHAR(255),
                    consumer_secret VARCHAR(255),
                    access_token VARCHAR(255),
                    access_token_secret VARCHAR(255),
                    bearer_token VARCHAR(255),
                    api_key VARCHAR(255),
                    enabled BOOLEAN DEFAULT TRUE,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_service_name ON api_credentials (service_name)"
            )

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS content_drafts (
                    id SERIAL PRIMARY KEY,
                    kind VARCHAR(20) NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'pending',
                    target_tweet_id VARCHAR(50),
                    target_tweet_text TEXT,
                    target_author VARCHAR(100),
                    keyword VARCHAR(255),
                    source VARCHAR(50),
                    generated_text TEXT,
                    edited_text TEXT,
                    thread_texts TEXT,
                    provider VARCHAR(50),
                    model VARCHAR(100),
                    prompt_profile_id INT,
                    telegram_message_id VARCHAR(50),
                    error TEXT,
                    posted_tweet_id VARCHAR(50),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    resolved_at TIMESTAMP NULL
                )
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_draft_status ON content_drafts (status)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_draft_created ON content_drafts (created_at)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_draft_kind ON content_drafts (kind)"
            )

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS ai_prompt_profiles (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(100) NOT NULL,
                    content_type VARCHAR(20) NOT NULL DEFAULT 'reply',
                    system_instruction TEXT NOT NULL,
                    user_prompt_template TEXT,
                    is_active BOOLEAN DEFAULT TRUE,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_profile_name_type UNIQUE (name, content_type)
                )
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_profile_active ON ai_prompt_profiles (is_active)"
            )

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS ai_provider_settings (
                    id SERIAL PRIMARY KEY,
                    provider VARCHAR(50) UNIQUE NOT NULL,
                    api_key VARCHAR(512),
                    model VARCHAR(100),
                    enabled BOOLEAN DEFAULT FALSE,
                    is_default BOOLEAN DEFAULT FALSE,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS media_assets (
                    id SERIAL PRIMARY KEY,
                    filename VARCHAR(255) NOT NULL,
                    original_name VARCHAR(255),
                    mime_type VARCHAR(100),
                    file_path VARCHAR(512) NOT NULL,
                    file_size INT DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_media_created ON media_assets (created_at)"
            )

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS draft_media (
                    id SERIAL PRIMARY KEY,
                    draft_id INT NOT NULL,
                    media_id INT NOT NULL,
                    sort_order INT DEFAULT 0,
                    CONSTRAINT uq_draft_media UNIQUE (draft_id, media_id)
                )
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_draft_media_draft ON draft_media (draft_id)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_draft_media_media ON draft_media (media_id)"
            )

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS safety_events (
                    id SERIAL PRIMARY KEY,
                    event_type VARCHAR(50) NOT NULL,
                    message TEXT,
                    meta_json TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_safety_created ON safety_events (created_at)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_safety_type ON safety_events (event_type)"
            )

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_sessions (
                    id SERIAL PRIMARY KEY,
                    chat_id VARCHAR(64) NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'active',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_sessions_chat ON agent_sessions (chat_id)"
            )

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_messages (
                    id SERIAL PRIMARY KEY,
                    session_id INT NOT NULL,
                    role VARCHAR(20) NOT NULL,
                    content TEXT,
                    tool_calls TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_messages_session ON agent_messages (session_id)"
            )

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_actions (
                    id SERIAL PRIMARY KEY,
                    session_id INT,
                    tool_name VARCHAR(100) NOT NULL,
                    args_json TEXT,
                    risk_level VARCHAR(20) NOT NULL DEFAULT 'safe',
                    status VARCHAR(30) NOT NULL DEFAULT 'planned',
                    draft_id INT,
                    result TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_actions_status ON agent_actions (status)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_actions_draft ON agent_actions (draft_id)"
            )

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_user_prefs (
                    id SERIAL PRIMARY KEY,
                    chat_id VARCHAR(64) UNIQUE NOT NULL,
                    prefs_json TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            self.conn.commit()
            self._seed_ai_defaults(cursor)
            self.conn.commit()
            logger.info("Database tables created/verified successfully")
        except Exception as e:
            logger.error(f"Error creating tables: {e}")
            self.conn.rollback()
            raise
        finally:
            cursor.close()

    def _seed_ai_defaults(self, cursor) -> None:
        """Seed default prompt niches and provider rows if empty."""
        cursor.execute("SELECT COUNT(*) AS c FROM ai_prompt_profiles")
        row = cursor.fetchone()
        if row and row.get("c", 0) == 0:
            defaults = [
                (
                    "default",
                    "reply",
                    "You write short, clear X/Twitter replies in plain English. "
                    "Be helpful, natural, and conversational. Stay under 280 characters. "
                    "Do not sound like a bot or use hype.",
                    None,
                ),
                (
                    "default",
                    "tweet",
                    "You write original X/Twitter posts in plain English. "
                    "Sound human, specific, and useful. Aim for 150-280 characters.",
                    None,
                ),
                (
                    "default",
                    "quote",
                    "You write short quote-tweet commentary that adds a clear take. "
                    "Stay under 280 characters. No spammy engagement bait.",
                    None,
                ),
                (
                    "default",
                    "thread",
                    "You write coherent multi-tweet threads. Each tweet under 280 chars. "
                    "Build a clear narrative across the thread.",
                    None,
                ),
                (
                    "web3",
                    "reply",
                    "You are an expert in web3, crypto markets, DEXs, wallets, and on-chain "
                    "culture. Reply helpfully and skeptically when claims are unverified. "
                    "Avoid financial advice. Keep replies under 280 characters.",
                    None,
                ),
                (
                    "web3",
                    "tweet",
                    "You post about web3, crypto infrastructure, and on-chain products. "
                    "Be accurate, avoid shilling tokens, no financial advice. "
                    "150-280 characters, natural voice.",
                    None,
                ),
                (
                    "blockchain",
                    "reply",
                    "You specialize in blockchain technology: consensus, L1/L2, smart contracts, "
                    "security, and developer tooling. Prefer precise technical language "
                    "that still reads casually. Under 280 characters.",
                    None,
                ),
                (
                    "blockchain",
                    "tweet",
                    "You write posts about blockchain tech, infrastructure, and developer "
                    "tools. Emphasize clarity over hype. 150-280 characters.",
                    None,
                ),
            ]
            for name, content_type, instruction, template in defaults:
                cursor.execute(
                    """
                    INSERT INTO ai_prompt_profiles
                        (name, content_type, system_instruction, user_prompt_template, is_active)
                    VALUES (%s, %s, %s, %s, TRUE)
                    """,
                    (name, content_type, instruction, template),
                )

        cursor.execute("SELECT COUNT(*) AS c FROM ai_provider_settings")
        row = cursor.fetchone()
        if row and row.get("c", 0) == 0:
            for provider, model, is_default in [
                ("gemini", "gemini-2.5-flash", True),
                ("openai", "gpt-4o-mini", False),
                ("anthropic", "claude-3-5-haiku-latest", False),
                ("agentrouter", "gpt-5.6-sol", False),
            ]:
                cursor.execute(
                    """
                    INSERT INTO ai_provider_settings
                        (provider, api_key, model, enabled, is_default)
                    VALUES (%s, NULL, %s, FALSE, %s)
                    """,
                    (provider, model, is_default),
                )
        else:
            # Ensure AgentRouter row exists on upgrades
            cursor.execute(
                "SELECT 1 FROM ai_provider_settings WHERE provider = %s",
                ("agentrouter",),
            )
            if not cursor.fetchone():
                cursor.execute(
                    """
                    INSERT INTO ai_provider_settings
                        (provider, api_key, model, enabled, is_default)
                    VALUES (%s, NULL, %s, FALSE, FALSE)
                    """,
                    ("agentrouter", "gpt-5.6-sol"),
                )
            else:
                # Migrate to AgentRouter-allowed model ids
                cursor.execute(
                    """
                    UPDATE ai_provider_settings
                    SET model = 'gpt-5.6-sol'
                    WHERE provider = 'agentrouter'
                      AND (
                        model IS NULL
                        OR model = ''
                        OR model NOT IN (
                          'gpt-5.6-sol',
                          'claude-opus-4-8',
                          'claude-opus-5'
                        )
                      )
                    """
                )
            # Migrate retired Gemini model ids stored in settings
            cursor.execute(
                """
                UPDATE ai_provider_settings
                SET model = 'gemini-2.5-flash'
                WHERE provider = 'gemini'
                  AND model IN (
                    'gemini-pro',
                    'gemini-1.5-flash',
                    'gemini-1.5-flash-latest',
                    'gemini-2.0-flash',
                    'gemini-2.0-flash-001'
                  )
                """
            )
            cursor.execute(
                """
                UPDATE ai_provider_settings
                SET model = 'gemini-2.5-pro'
                WHERE provider = 'gemini'
                  AND model IN ('gemini-1.5-pro', 'gemini-1.5-pro-latest')
                """
            )
    
    def _row_to_dict(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """Convert RealDictRow to a plain dict."""
        if row is None:
            return None
        return dict(row)

    def is_tweet_replied(self, tweet_id: str) -> bool:
        """Check if a tweet has already been replied to."""
        cursor = self.conn.cursor()
        try:
            cursor.execute("SELECT tweet_id FROM replied_tweets WHERE tweet_id = %s", (tweet_id,))
            result = cursor.fetchone()
            return result is not None
        finally:
            cursor.close()
    
    def mark_tweet_replied(self, tweet_id: str, reply_tweet_id: str, source: str = "timeline", keyword: Optional[str] = None) -> None:
        """Mark a tweet as replied to."""
        cursor = self.conn.cursor()
        try:
            cursor.execute("""
                INSERT INTO replied_tweets (tweet_id, reply_tweet_id, source, keyword)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (tweet_id) DO UPDATE SET
                    reply_tweet_id = EXCLUDED.reply_tweet_id,
                    source = EXCLUDED.source,
                    keyword = EXCLUDED.keyword,
                    replied_at = CURRENT_TIMESTAMP
            """, (tweet_id, reply_tweet_id, source, keyword))
            self.conn.commit()
            logger.info(f"Marked tweet {tweet_id} as replied (reply: {reply_tweet_id})")
        except Exception as e:
            logger.error(f"Error marking tweet as replied: {e}", exc_info=True)
            self.conn.rollback()
            raise
        finally:
            cursor.close()
    
    def get_replied_tweet_ids(self) -> Set[str]:
        """Get all replied tweet IDs as a set."""
        cursor = self.conn.cursor()
        try:
            cursor.execute("SELECT tweet_id FROM replied_tweets")
            rows = cursor.fetchall()
            return {row['tweet_id'] for row in rows}
        finally:
            cursor.close()
    
    def mark_tweet_posted(self, tweet_id: str, text: str) -> None:
        """Mark a tweet as posted."""
        cursor = self.conn.cursor()
        try:
            cursor.execute("""
                INSERT INTO posted_tweets (tweet_id, text)
                VALUES (%s, %s)
                ON CONFLICT (tweet_id) DO UPDATE SET
                    text = EXCLUDED.text,
                    posted_at = CURRENT_TIMESTAMP
            """, (tweet_id, text))
            self.conn.commit()
            logger.info(f"Marked tweet {tweet_id} as posted")
        except Exception as e:
            logger.error(f"Error marking tweet as posted: {e}")
            self.conn.rollback()
            raise
        finally:
            cursor.close()
    
    def mark_thread_posted(self, thread_ids: List[str], texts: List[str]) -> None:
        """Mark a thread as posted."""
        import json
        cursor = self.conn.cursor()
        try:
            thread_id = thread_ids[0] if thread_ids else None
            first_tweet_id = thread_ids[0] if thread_ids else None
            tweet_ids_json = json.dumps(thread_ids)
            texts_json = json.dumps(texts)
            
            cursor.execute("""
                INSERT INTO posted_threads (thread_id, first_tweet_id, tweet_ids, texts)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (thread_id) DO UPDATE SET
                    first_tweet_id = EXCLUDED.first_tweet_id,
                    tweet_ids = EXCLUDED.tweet_ids,
                    texts = EXCLUDED.texts,
                    posted_at = CURRENT_TIMESTAMP
            """, (thread_id, first_tweet_id, tweet_ids_json, texts_json))
            self.conn.commit()
            logger.info(f"Marked thread {thread_id} as posted ({len(thread_ids)} tweets)")
        except Exception as e:
            logger.error(f"Error marking thread as posted: {e}")
            self.conn.rollback()
            raise
        finally:
            cursor.close()
    
    def mark_quote_retweet_posted(self, quote_tweet_id: str, original_tweet_id: str, text: str) -> None:
        """Mark a quote retweet as posted."""
        cursor = self.conn.cursor()
        try:
            cursor.execute("""
                INSERT INTO quote_retweets (quote_tweet_id, original_tweet_id, text)
                VALUES (%s, %s, %s)
                ON CONFLICT (quote_tweet_id) DO UPDATE SET
                    original_tweet_id = EXCLUDED.original_tweet_id,
                    text = EXCLUDED.text,
                    posted_at = CURRENT_TIMESTAMP
            """, (quote_tweet_id, original_tweet_id, text))
            self.conn.commit()
            logger.info(f"Marked quote retweet {quote_tweet_id} (quoting {original_tweet_id}) as posted")
        except Exception as e:
            logger.error(f"Error marking quote retweet as posted: {e}")
            self.conn.rollback()
            raise
        finally:
            cursor.close()
    
    def log_event(self, level: str, message: str) -> None:
        """Log an event to the database."""
        cursor = self.conn.cursor()
        try:
            cursor.execute("""
                INSERT INTO bot_logs (log_level, message)
                VALUES (%s, %s)
            """, (level, message))
            self.conn.commit()
        except Exception as e:
            logger.error(f"Error logging event: {e}")
            self.conn.rollback()
        finally:
            cursor.close()
    
    def save_api_credentials(self, service_name: str, credentials: Dict[str, Any]) -> None:
        """Save API credentials to database."""
        cursor = self.conn.cursor()
        try:
            cursor.execute("""
                INSERT INTO api_credentials (
                    service_name, consumer_key, consumer_secret, 
                    access_token, access_token_secret, bearer_token, 
                    api_key, enabled
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (service_name) DO UPDATE SET
                    consumer_key = EXCLUDED.consumer_key,
                    consumer_secret = EXCLUDED.consumer_secret,
                    access_token = EXCLUDED.access_token,
                    access_token_secret = EXCLUDED.access_token_secret,
                    bearer_token = EXCLUDED.bearer_token,
                    api_key = EXCLUDED.api_key,
                    enabled = EXCLUDED.enabled,
                    updated_at = CURRENT_TIMESTAMP
            """, (
                service_name,
                credentials.get("consumer_key"),
                credentials.get("consumer_secret"),
                credentials.get("access_token"),
                credentials.get("access_token_secret"),
                credentials.get("bearer_token"),
                credentials.get("api_key"),
                credentials.get("enabled", True)
            ))
            self.conn.commit()
            logger.info(f"Saved API credentials for {service_name}")
        except Exception as e:
            logger.error(f"Error saving API credentials: {e}")
            self.conn.rollback()
            raise
        finally:
            cursor.close()
    
    def get_api_credentials(self, service_name: str) -> Optional[Dict[str, Any]]:
        """Get API credentials from database."""
        cursor = self.conn.cursor()
        try:
            cursor.execute("""
                SELECT consumer_key, consumer_secret, access_token, 
                       access_token_secret, bearer_token, api_key, enabled
                FROM api_credentials
                WHERE service_name = %s AND enabled = TRUE
            """, (service_name,))
            row = cursor.fetchone()
            if row:
                return {
                    "consumer_key": row.get("consumer_key"),
                    "consumer_secret": row.get("consumer_secret"),
                    "access_token": row.get("access_token"),
                    "access_token_secret": row.get("access_token_secret"),
                    "bearer_token": row.get("bearer_token"),
                    "api_key": row.get("api_key"),
                    "enabled": row.get("enabled", True)
                }
            return None
        except Exception as e:
            logger.error(f"Error getting API credentials: {e}")
            return None
        finally:
            cursor.close()
    
    def delete_api_credentials(self, service_name: str) -> None:
        """Delete API credentials from database."""
        cursor = self.conn.cursor()
        try:
            cursor.execute("DELETE FROM api_credentials WHERE service_name = %s", (service_name,))
            self.conn.commit()
            logger.info(f"Deleted API credentials for {service_name}")
        except Exception as e:
            logger.error(f"Error deleting API credentials: {e}")
            self.conn.rollback()
            raise
        finally:
            cursor.close()
    
    def list_api_services(self) -> List[str]:
        """List all API service names in database."""
        cursor = self.conn.cursor()
        try:
            cursor.execute("SELECT service_name FROM api_credentials WHERE enabled = TRUE")
            rows = cursor.fetchall()
            return [row["service_name"] for row in rows]
        except Exception as e:
            logger.error(f"Error listing API services: {e}")
            return []
        finally:
            cursor.close()
    
    # --- Content drafts (man-in-the-loop) ---

    def create_draft(self, data: Dict[str, Any]) -> int:
        """Insert a pending content draft and return its id."""
        import json
        cursor = self.conn.cursor()
        try:
            thread_texts = data.get("thread_texts")
            if isinstance(thread_texts, list):
                thread_texts = json.dumps(thread_texts)
            cursor.execute(
                """
                INSERT INTO content_drafts (
                    kind, status, target_tweet_id, target_tweet_text, target_author,
                    keyword, source, generated_text, edited_text, thread_texts,
                    provider, model, prompt_profile_id, telegram_message_id
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                RETURNING id
                """,
                (
                    data.get("kind"),
                    data.get("status", "pending"),
                    data.get("target_tweet_id"),
                    data.get("target_tweet_text"),
                    data.get("target_author"),
                    data.get("keyword"),
                    data.get("source"),
                    data.get("generated_text"),
                    data.get("edited_text"),
                    thread_texts,
                    data.get("provider"),
                    data.get("model"),
                    data.get("prompt_profile_id"),
                    data.get("telegram_message_id"),
                ),
            )
            row = cursor.fetchone()
            self.conn.commit()
            draft_id = int(row["id"]) if row else 0
            logger.info(f"Created draft {draft_id} kind={data.get('kind')}")
            return draft_id
        except Exception as e:
            self.conn.rollback()
            logger.error(f"Error creating draft: {e}", exc_info=True)
            raise
        finally:
            cursor.close()

    def get_draft(self, draft_id: int) -> Optional[Dict[str, Any]]:
        """Get a draft by id."""
        cursor = self.conn.cursor()
        try:
            cursor.execute("SELECT * FROM content_drafts WHERE id = %s", (draft_id,))
            return cursor.fetchone()
        finally:
            cursor.close()

    def list_drafts(
        self,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List drafts newest first, optionally filtered by status."""
        cursor = self.conn.cursor()
        try:
            if status:
                cursor.execute(
                    """
                    SELECT * FROM content_drafts
                    WHERE status = %s
                    ORDER BY created_at DESC
                    LIMIT %s OFFSET %s
                    """,
                    (status, limit, offset),
                )
            else:
                cursor.execute(
                    """
                    SELECT * FROM content_drafts
                    ORDER BY created_at DESC
                    LIMIT %s OFFSET %s
                    """,
                    (limit, offset),
                )
            return list(cursor.fetchall() or [])
        finally:
            cursor.close()

    def count_drafts(self, status: Optional[str] = None) -> int:
        """Count drafts, optionally by status."""
        cursor = self.conn.cursor()
        try:
            if status:
                cursor.execute(
                    "SELECT COUNT(*) AS c FROM content_drafts WHERE status = %s",
                    (status,),
                )
            else:
                cursor.execute("SELECT COUNT(*) AS c FROM content_drafts")
            row = cursor.fetchone()
            return int(row["c"]) if row else 0
        finally:
            cursor.close()

    def update_draft(self, draft_id: int, fields: Dict[str, Any]) -> None:
        """Update selected draft fields."""
        if not fields:
            return
        allowed = {
            "status",
            "edited_text",
            "generated_text",
            "telegram_message_id",
            "error",
            "posted_tweet_id",
            "resolved_at",
            "thread_texts",
        }
        sets = []
        values = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            sets.append(f"{key} = %s")
            values.append(value)
        if not sets:
            return
        values.append(draft_id)
        cursor = self.conn.cursor()
        try:
            cursor.execute(
                f"UPDATE content_drafts SET {', '.join(sets)} WHERE id = %s",
                tuple(values),
            )
            self.conn.commit()
        except Exception as e:
            self.conn.rollback()
            logger.error(f"Error updating draft {draft_id}: {e}", exc_info=True)
            raise
        finally:
            cursor.close()

    def get_draft_final_text(self, draft: Dict[str, Any]) -> str:
        """Return edited text if present, else generated text."""
        edited = (draft or {}).get("edited_text") or ""
        if edited.strip():
            return edited.strip()
        return ((draft or {}).get("generated_text") or "").strip()

    # --- AI prompt profiles ---

    def list_prompt_profiles(self, active_only: bool = False) -> List[Dict[str, Any]]:
        """List prompt profiles."""
        cursor = self.conn.cursor()
        try:
            if active_only:
                cursor.execute(
                    "SELECT * FROM ai_prompt_profiles WHERE is_active = TRUE "
                    "ORDER BY name, content_type"
                )
            else:
                cursor.execute(
                    "SELECT * FROM ai_prompt_profiles ORDER BY name, content_type"
                )
            return list(cursor.fetchall() or [])
        finally:
            cursor.close()

    def get_prompt_profile(
        self,
        name: str,
        content_type: str = "reply",
    ) -> Optional[Dict[str, Any]]:
        """Get a prompt profile by niche name and content type."""
        cursor = self.conn.cursor()
        try:
            cursor.execute(
                """
                SELECT * FROM ai_prompt_profiles
                WHERE name = %s AND content_type = %s AND is_active = TRUE
                LIMIT 1
                """,
                (name, content_type),
            )
            row = cursor.fetchone()
            if row:
                return row
            # Fallback to default niche for this content type
            cursor.execute(
                """
                SELECT * FROM ai_prompt_profiles
                WHERE name = 'default' AND content_type = %s AND is_active = TRUE
                LIMIT 1
                """,
                (content_type,),
            )
            return cursor.fetchone()
        finally:
            cursor.close()

    def get_prompt_profile_by_id(self, profile_id: int) -> Optional[Dict[str, Any]]:
        """Get prompt profile by id."""
        cursor = self.conn.cursor()
        try:
            cursor.execute(
                "SELECT * FROM ai_prompt_profiles WHERE id = %s", (profile_id,)
            )
            return cursor.fetchone()
        finally:
            cursor.close()

    def save_prompt_profile(self, data: Dict[str, Any]) -> int:
        """Insert or update a prompt profile. Returns id."""
        cursor = self.conn.cursor()
        try:
            profile_id = data.get("id")
            if profile_id:
                cursor.execute(
                    """
                    UPDATE ai_prompt_profiles SET
                        name = %s,
                        content_type = %s,
                        system_instruction = %s,
                        user_prompt_template = %s,
                        is_active = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                    """,
                    (
                        data.get("name"),
                        data.get("content_type", "reply"),
                        data.get("system_instruction", ""),
                        data.get("user_prompt_template"),
                        data.get("is_active", True),
                        profile_id,
                    ),
                )
                self.conn.commit()
                return int(profile_id)
            cursor.execute(
                """
                INSERT INTO ai_prompt_profiles
                    (name, content_type, system_instruction, user_prompt_template, is_active)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (name, content_type) DO UPDATE SET
                    system_instruction = EXCLUDED.system_instruction,
                    user_prompt_template = EXCLUDED.user_prompt_template,
                    is_active = EXCLUDED.is_active,
                    updated_at = CURRENT_TIMESTAMP
                RETURNING id
                """,
                (
                    data.get("name"),
                    data.get("content_type", "reply"),
                    data.get("system_instruction", ""),
                    data.get("user_prompt_template"),
                    data.get("is_active", True),
                ),
            )
            row = cursor.fetchone()
            self.conn.commit()
            return int(row["id"]) if row else 0
        except Exception as e:
            self.conn.rollback()
            logger.error(f"Error saving prompt profile: {e}", exc_info=True)
            raise
        finally:
            cursor.close()

    def delete_prompt_profile(self, profile_id: int) -> None:
        """Delete a prompt profile."""
        cursor = self.conn.cursor()
        try:
            cursor.execute("DELETE FROM ai_prompt_profiles WHERE id = %s", (profile_id,))
            self.conn.commit()
        except Exception as e:
            self.conn.rollback()
            logger.error(f"Error deleting prompt profile: {e}", exc_info=True)
            raise
        finally:
            cursor.close()

    # --- AI provider settings ---

    def list_ai_providers(self) -> List[Dict[str, Any]]:
        """List AI provider settings."""
        cursor = self.conn.cursor()
        try:
            cursor.execute("SELECT * FROM ai_provider_settings ORDER BY provider")
            return list(cursor.fetchall() or [])
        finally:
            cursor.close()

    def get_ai_provider(self, provider: str) -> Optional[Dict[str, Any]]:
        """Get settings for one AI provider."""
        cursor = self.conn.cursor()
        try:
            cursor.execute(
                "SELECT * FROM ai_provider_settings WHERE provider = %s",
                (provider,),
            )
            return cursor.fetchone()
        finally:
            cursor.close()

    def get_default_ai_provider(self) -> Optional[Dict[str, Any]]:
        """Get the default enabled AI provider, or any enabled provider."""
        cursor = self.conn.cursor()
        try:
            cursor.execute(
                """
                SELECT * FROM ai_provider_settings
                WHERE is_default = TRUE AND enabled = TRUE
                LIMIT 1
                """
            )
            row = cursor.fetchone()
            if row:
                return row
            cursor.execute(
                """
                SELECT * FROM ai_provider_settings
                WHERE enabled = TRUE AND api_key IS NOT NULL AND api_key != ''
                LIMIT 1
                """
            )
            return cursor.fetchone()
        finally:
            cursor.close()

    def save_ai_provider(self, data: Dict[str, Any]) -> None:
        """Upsert AI provider settings. Empty api_key keeps existing key."""
        provider = data.get("provider")
        if not provider:
            raise ValueError("provider is required")
        cursor = self.conn.cursor()
        try:
            existing = None
            cursor.execute(
                "SELECT * FROM ai_provider_settings WHERE provider = %s",
                (provider,),
            )
            existing = cursor.fetchone()
            api_key = data.get("api_key")
            if (not api_key) and existing:
                api_key = existing.get("api_key")
            is_default = bool(data.get("is_default", False))
            if is_default:
                cursor.execute(
                    "UPDATE ai_provider_settings SET is_default = FALSE"
                )
            if existing:
                cursor.execute(
                    """
                    UPDATE ai_provider_settings SET
                        api_key = %s,
                        model = %s,
                        enabled = %s,
                        is_default = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE provider = %s
                    """,
                    (
                        api_key,
                        data.get("model") or existing.get("model"),
                        data.get("enabled", existing.get("enabled")),
                        is_default if "is_default" in data else existing.get("is_default"),
                        provider,
                    ),
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO ai_provider_settings
                        (provider, api_key, model, enabled, is_default)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        provider,
                        api_key,
                        data.get("model"),
                        data.get("enabled", False),
                        is_default,
                    ),
                )
            self.conn.commit()
        except Exception as e:
            self.conn.rollback()
            logger.error(f"Error saving AI provider {provider}: {e}", exc_info=True)
            raise
        finally:
            cursor.close()

    def list_replied_tweets(self, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
        """Paginated replied tweets."""
        cursor = self.conn.cursor()
        try:
            cursor.execute(
                """
                SELECT * FROM replied_tweets
                ORDER BY replied_at DESC
                LIMIT %s OFFSET %s
                """,
                (limit, offset),
            )
            return list(cursor.fetchall() or [])
        finally:
            cursor.close()

    def list_posted_tweets(self, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
        """Paginated posted tweets."""
        cursor = self.conn.cursor()
        try:
            cursor.execute(
                """
                SELECT * FROM posted_tweets
                ORDER BY posted_at DESC
                LIMIT %s OFFSET %s
                """,
                (limit, offset),
            )
            return list(cursor.fetchall() or [])
        finally:
            cursor.close()

    def list_posted_threads(self, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
        """Paginated posted threads."""
        cursor = self.conn.cursor()
        try:
            cursor.execute(
                """
                SELECT * FROM posted_threads
                ORDER BY posted_at DESC
                LIMIT %s OFFSET %s
                """,
                (limit, offset),
            )
            return list(cursor.fetchall() or [])
        finally:
            cursor.close()

    def list_quote_retweets(self, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
        """Paginated quote tweets."""
        cursor = self.conn.cursor()
        try:
            cursor.execute(
                """
                SELECT * FROM quote_retweets
                ORDER BY posted_at DESC
                LIMIT %s OFFSET %s
                """,
                (limit, offset),
            )
            return list(cursor.fetchall() or [])
        finally:
            cursor.close()

    # --- Media library ---

    def create_media_asset(
        self,
        filename: str,
        original_name: str,
        mime_type: str,
        file_path: str,
        file_size: int = 0,
    ) -> int:
        cursor = self.conn.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO media_assets
                    (filename, original_name, mime_type, file_path, file_size)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (filename, original_name, mime_type, file_path, file_size),
            )
            row = cursor.fetchone()
            self.conn.commit()
            return int(row["id"]) if row else 0
        except Exception as e:
            self.conn.rollback()
            logger.error(f"Error creating media asset: {e}", exc_info=True)
            raise
        finally:
            cursor.close()

    def get_media_asset(self, media_id: int) -> Optional[Dict[str, Any]]:
        cursor = self.conn.cursor()
        try:
            cursor.execute("SELECT * FROM media_assets WHERE id = %s", (media_id,))
            return cursor.fetchone()
        finally:
            cursor.close()

    def list_media_assets(self, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        cursor = self.conn.cursor()
        try:
            cursor.execute(
                """
                SELECT * FROM media_assets
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                (limit, offset),
            )
            return list(cursor.fetchall() or [])
        finally:
            cursor.close()

    def delete_media_asset(self, media_id: int) -> Optional[Dict[str, Any]]:
        """Delete media row; returns the row so caller can remove the file."""
        cursor = self.conn.cursor()
        try:
            cursor.execute("SELECT * FROM media_assets WHERE id = %s", (media_id,))
            row = cursor.fetchone()
            if not row:
                return None
            cursor.execute("DELETE FROM draft_media WHERE media_id = %s", (media_id,))
            cursor.execute("DELETE FROM media_assets WHERE id = %s", (media_id,))
            self.conn.commit()
            return row
        except Exception as e:
            self.conn.rollback()
            logger.error(f"Error deleting media asset: {e}", exc_info=True)
            raise
        finally:
            cursor.close()

    def set_draft_media(self, draft_id: int, media_ids: List[int]) -> None:
        """Replace draft media attachments (max 4)."""
        media_ids = [int(m) for m in media_ids[:4]]
        cursor = self.conn.cursor()
        try:
            cursor.execute("DELETE FROM draft_media WHERE draft_id = %s", (draft_id,))
            for i, mid in enumerate(media_ids):
                cursor.execute(
                    """
                    INSERT INTO draft_media (draft_id, media_id, sort_order)
                    VALUES (%s, %s, %s)
                    """,
                    (draft_id, mid, i),
                )
            self.conn.commit()
        except Exception as e:
            self.conn.rollback()
            logger.error(f"Error setting draft media: {e}", exc_info=True)
            raise
        finally:
            cursor.close()

    def get_draft_media(self, draft_id: int) -> List[Dict[str, Any]]:
        """Media assets attached to a draft, ordered."""
        cursor = self.conn.cursor()
        try:
            cursor.execute(
                """
                SELECT m.*
                FROM draft_media dm
                JOIN media_assets m ON m.id = dm.media_id
                WHERE dm.draft_id = %s
                ORDER BY dm.sort_order ASC, dm.id ASC
                """,
                (draft_id,),
            )
            return list(cursor.fetchall() or [])
        finally:
            cursor.close()

    def close(self) -> None:
        """Close database connection."""
        if self.conn:
            self.conn.close()
            logger.info("Database connection closed")
