"""Telegram man-in-the-loop: notify, approve/edit/reject, and photo+tip compose."""
import asyncio
import hashlib
import logging
import os
import threading
import time
from datetime import datetime
from typing import Any, Dict, Optional

from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

_PLACEHOLDER_TOKENS = {
    "",
    "your_telegram_bot_token",
    "changeme",
    "change-me",
}
_PLACEHOLDER_CHAT_IDS = {
    "",
    "your_telegram_chat_id",
    "changeme",
}

# Process-local guard (gunicorn reload / double import)
_polling_start_lock = threading.Lock()
_polling_started = False


class DraftPoster:
    """Posts approved drafts to X and updates tracking tables."""

    def __init__(self, config=None, db=None, x_api=None):
        self.config = config
        self.db = db
        self.x_api = x_api

    def _ensure(self):
        if self.db is None or self.x_api is None:
            from .config import Config
            from .database import Database
            from .x_api import XAPI

            if self.config is None:
                self.config = Config()
            if self.db is None:
                self.db = Database()
            if self.x_api is None:
                self.x_api = XAPI(self.config.get_x_api_credentials())

    def approve_and_post(self, draft_id: int, edited_text: Optional[str] = None) -> Dict[str, Any]:
        """Approve a draft (optionally with edited text) and post it."""
        self._ensure()
        draft = self.db.get_draft(draft_id)
        if not draft:
            return {"ok": False, "error": "Draft not found"}
        if draft.get("status") not in ("pending", "approved"):
            return {"ok": False, "error": f"Draft status is {draft.get('status')}"}

        if edited_text is not None and edited_text.strip():
            self.db.update_draft(draft_id, {"edited_text": edited_text.strip()})
            draft = self.db.get_draft(draft_id)

        text = self.db.get_draft_final_text(draft)
        kind = draft.get("kind")
        try:
            from .quality_safety import validate_before_post, log_safety_event
            from .config import Config

            if self.config is None:
                self.config = Config()
            safety = self.config.get_safety_config()
            ok, reason = validate_before_post(self.db, text, safety)
            if not ok:
                log_safety_event(
                    self.db,
                    "post_blocked",
                    reason,
                    {"draft_id": draft_id, "kind": kind},
                )
                return {"ok": False, "error": reason, "blocked": True}

            x_media_ids = []
            assets = self.db.get_draft_media(draft_id)
            if assets:
                from .media_store import absolute_path_for
                from pathlib import Path

                paths = []
                for asset in assets:
                    try:
                        paths.append(str(absolute_path_for(asset["filename"])))
                    except FileNotFoundError:
                        p = Path(asset.get("file_path") or "")
                        if p.exists():
                            paths.append(str(p))
                x_media_ids = self.x_api.upload_media_files(paths)

            posted_id = None
            if kind == "reply":
                if not draft.get("target_tweet_id") or not text:
                    raise ValueError("Missing reply text or target tweet")
                posted_id = self.x_api.post_reply(
                    text, draft["target_tweet_id"], media_ids=x_media_ids or None
                )
                if posted_id:
                    self.db.mark_tweet_replied(
                        draft["target_tweet_id"],
                        posted_id,
                        source=draft.get("source") or "unknown",
                        keyword=draft.get("keyword"),
                    )
            elif kind == "quote":
                if not draft.get("target_tweet_id") or not text:
                    raise ValueError("Missing quote text or target tweet")
                posted_id = self.x_api.quote_tweet(
                    text, draft["target_tweet_id"], media_ids=x_media_ids or None
                )
                if posted_id:
                    self.db.mark_tweet_replied(
                        draft["target_tweet_id"],
                        posted_id,
                        source=draft.get("source") or "unknown",
                        keyword=draft.get("keyword"),
                    )
                    self.db.mark_quote_retweet_posted(
                        posted_id, draft["target_tweet_id"], text
                    )
            elif kind == "tweet":
                if not text:
                    raise ValueError("Missing tweet text")
                posted_id = self.x_api.post_tweet(text, media_ids=x_media_ids or None)
                if posted_id:
                    self.db.mark_tweet_posted(posted_id, text)
            elif kind == "thread":
                import json

                texts = draft.get("thread_texts")
                if isinstance(texts, str):
                    texts = json.loads(texts)
                if not texts:
                    raise ValueError("Missing thread texts")
                if x_media_ids and texts:
                    first_id = self.x_api.post_tweet(texts[0], media_ids=x_media_ids)
                    if not first_id:
                        raise ValueError("Failed to post thread opener with media")
                    rest = texts[1:]
                    if rest:
                        ids = [first_id]
                        prev = first_id
                        for t in rest:
                            rid = self.x_api.post_reply(t, prev)
                            if not rid:
                                break
                            ids.append(rid)
                            prev = rid
                        self.db.mark_thread_posted(ids, texts[: len(ids)])
                        posted_id = ids[0]
                    else:
                        self.db.mark_thread_posted([first_id], texts[:1])
                        posted_id = first_id
                else:
                    ids = self.x_api.post_thread(texts)
                    if ids:
                        self.db.mark_thread_posted(ids, texts)
                        posted_id = ids[0]
            else:
                raise ValueError(f"Unknown draft kind: {kind}")

            if not posted_id:
                self.db.update_draft(
                    draft_id,
                    {
                        "status": "failed",
                        "error": "X API returned no post id",
                        "resolved_at": datetime.now(),
                    },
                )
                return {"ok": False, "error": "Failed to post to X"}

            self.db.update_draft(
                draft_id,
                {
                    "status": "posted",
                    "posted_tweet_id": str(posted_id),
                    "resolved_at": datetime.now(),
                    "error": None,
                },
            )
            return {"ok": True, "posted_tweet_id": str(posted_id)}
        except Exception as e:
            logger.error(f"Failed to approve draft {draft_id}: {e}", exc_info=True)
            self.db.update_draft(
                draft_id,
                {
                    "status": "failed",
                    "error": str(e)[:500],
                    "resolved_at": datetime.now(),
                },
            )
            return {"ok": False, "error": str(e)}

    def reject(self, draft_id: int) -> Dict[str, Any]:
        self._ensure()
        draft = self.db.get_draft(draft_id)
        if not draft:
            return {"ok": False, "error": "Draft not found"}
        if draft.get("status") != "pending":
            return {"ok": False, "error": f"Draft status is {draft.get('status')}"}
        self.db.update_draft(
            draft_id,
            {"status": "rejected", "resolved_at": datetime.now()},
        )
        return {"ok": True}

    def save_edit(self, draft_id: int, text: str) -> Dict[str, Any]:
        self._ensure()
        draft = self.db.get_draft(draft_id)
        if not draft:
            return {"ok": False, "error": "Draft not found"}
        if draft.get("status") != "pending":
            return {"ok": False, "error": f"Draft status is {draft.get('status')}"}
        self.db.update_draft(draft_id, {"edited_text": text.strip()})
        return {"ok": True}


def compose_post_from_tip(tip: str) -> Dict[str, Any]:
    """
    Expand a short tip into a full X post using configured AI + niche profile.
    Returns {ok, text, provider, model, profile_id, error}.
    """
    from .config import Config
    from .database import Database
    from .ai_provider import generate_with_resolved_provider

    tip = (tip or "").strip()
    if not tip:
        return {"ok": False, "error": "Empty tip"}

    config = Config()
    db = Database()
    try:
        profile_name = config.get_prompt_profile_name()
        profile = db.get_prompt_profile(profile_name, "tweet")
        system = (
            (profile or {}).get("system_instruction")
            or "You write natural X/Twitter posts under 280 characters."
        )
        # Strengthen instruction for tip expansion
        system = (
            system
            + "\n\nThe user will give a short tip or brief about what the post "
            "should say, often paired with an image they will attach. Expand their "
            "tip into ONE complete, ready-to-publish post. Follow their intent "
            "closely. Do not invent unrelated topics."
        )
        user = f"""Turn this tip into a full X/Twitter post that will be published with an image.

Tip / brief from the admin:
\"\"\"{tip}\"\"\"

Rules:
- Write the complete post text only (no quotes, no "here's a post")
- Stay under 280 characters
- Follow the tip's meaning and tone
- Sound human, specific, and useful
- Do not mention that you are an AI
"""
        text, provider, model = generate_with_resolved_provider(
            system,
            user,
            db=db,
            config=config.get_ai_config(),
            max_tokens=220,
        )
        if not text:
            # Fallback: lightly expand tip without AI
            text = tip if len(tip) <= 280 else tip[:277] + "..."
            return {
                "ok": True,
                "text": text,
                "provider": None,
                "model": None,
                "profile_id": (profile or {}).get("id"),
                "error": None,
                "fallback": True,
            }
        if len(text) > 280:
            text = text[:277] + "..."
        return {
            "ok": True,
            "text": text.strip(),
            "provider": provider,
            "model": model,
            "profile_id": (profile or {}).get("id"),
            "error": None,
        }
    except Exception as e:
        logger.error(f"compose_post_from_tip failed: {e}", exc_info=True)
        return {"ok": False, "error": str(e)}
    finally:
        db.close()


class TelegramApprover:
    """Telegram: MITL approve/edit/reject + photo+tip compose into drafts."""

    def __init__(self, poster: Optional[DraftPoster] = None):
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        self.poster = poster or DraftPoster()
        self._app = None
        self._thread = None
        self._awaiting_edit: Dict[int, int] = {}  # user id -> draft id
        self._awaiting_tip: Dict[int, list] = {}  # user id -> list of media_asset ids
        self._last_compose_draft: Dict[int, int] = {}  # user id -> last draft id
        self._media_group_buf: Dict[str, Dict[str, Any]] = {}
        self._media_group_lock = threading.Lock()
        self._loop = None
        self._lock_conn = None  # holds Postgres advisory lock while polling

    @property
    def enabled(self) -> bool:
        token = (self.token or "").strip()
        chat = (self.chat_id or "").strip()
        if token.lower() in _PLACEHOLDER_TOKENS or ":" not in token:
            return False
        if chat.lower() in _PLACEHOLDER_CHAT_IDS:
            return False
        return True

    def _polling_lock_id(self) -> int:
        """Stable 31-bit key so only one process polls this bot token."""
        digest = hashlib.sha256(self.token.encode("utf-8")).hexdigest()
        return int(digest[:8], 16) % (2**31 - 1)

    def _try_acquire_polling_lock(self) -> bool:
        """
        Take a session-level Postgres advisory lock.
        Prevents Render deploy overlap (old + new dyno) from dual getUpdates.
        """
        if os.getenv("TELEGRAM_SKIP_LOCK", "").lower() in ("1", "true", "yes"):
            return True
        try:
            import psycopg2
            from psycopg2.extras import RealDictCursor

            database_url = (os.getenv("DATABASE_URL") or "").strip()
            if database_url.startswith("postgres://"):
                database_url = "postgresql://" + database_url[len("postgres://") :]
            sslmode = (os.getenv("DB_SSLMODE") or "").strip() or None
            if database_url:
                if sslmode and "sslmode=" not in database_url.lower():
                    sep = "&" if "?" in database_url else "?"
                    database_url = f"{database_url}{sep}sslmode={sslmode}"
                conn = psycopg2.connect(
                    dsn=database_url,
                    cursor_factory=RealDictCursor,
                    connect_timeout=10,
                )
            else:
                kwargs = {
                    "host": os.getenv("DB_HOST", "localhost"),
                    "port": int(os.getenv("DB_PORT", "5432")),
                    "user": os.getenv("DB_USER", "postgres"),
                    "password": os.getenv("DB_PASSWORD", "") or "",
                    "dbname": os.getenv("DB_NAME", "twitter"),
                    "cursor_factory": RealDictCursor,
                    "connect_timeout": 10,
                }
                if sslmode:
                    kwargs["sslmode"] = sslmode
                conn = psycopg2.connect(**kwargs)

            conn.autocommit = True
            cur = conn.cursor()
            cur.execute(
                "SELECT pg_try_advisory_lock(%s) AS locked",
                (self._polling_lock_id(),),
            )
            row = cur.fetchone()
            cur.close()
            locked = bool(row and row.get("locked"))
            if not locked:
                conn.close()
                logger.warning(
                    "Telegram polling lock held by another process — "
                    "skipping getUpdates (normal during Render redeploy)"
                )
                return False
            self._lock_conn = conn
            logger.info("Acquired Telegram polling advisory lock")
            return True
        except Exception as e:
            # If DB lock fails, still allow polling (local without PG, etc.)
            logger.warning(f"Telegram polling lock unavailable ({e}); starting anyway")
            return True

    def _release_polling_lock(self) -> None:
        if not self._lock_conn:
            return
        try:
            cur = self._lock_conn.cursor()
            cur.execute(
                "SELECT pg_advisory_unlock(%s)",
                (self._polling_lock_id(),),
            )
            cur.close()
            self._lock_conn.close()
        except Exception:
            pass
        self._lock_conn = None

    def _authorized_chat(self, chat_id) -> bool:
        return str(chat_id) == str(self.chat_id)

    def _action_keyboard(self, draft_id: int):
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Approve", callback_data=f"approve:{draft_id}"),
                    InlineKeyboardButton("Edit", callback_data=f"edit:{draft_id}"),
                    InlineKeyboardButton("Rewrite", callback_data=f"rewrite:{draft_id}"),
                    InlineKeyboardButton("Reject", callback_data=f"reject:{draft_id}"),
                ]
            ]
        )

    def notify_draft(self, draft_id: int, draft: Dict[str, Any]) -> Optional[str]:
        """Send draft with image preview when attached. Returns message id or None."""
        if not self.enabled:
            logger.info(
                "Telegram not configured; draft %s awaits dashboard approval", draft_id
            )
            return None
        try:
            from telegram import Bot, InputMediaPhoto
            from .media_store import absolute_path_for
            from pathlib import Path

            text = self._format_message(draft_id, draft)
            keyboard = self._action_keyboard(draft_id)

            self.poster._ensure()
            assets = self.poster.db.get_draft_media(draft_id)
            paths = []
            for asset in assets[:4]:
                try:
                    paths.append(str(absolute_path_for(asset["filename"])))
                except FileNotFoundError:
                    p = Path(asset.get("file_path") or "")
                    if p.exists():
                        paths.append(str(p))

            async def _send():
                bot = Bot(token=self.token)
                caption = text[:1024]
                if len(paths) == 1:
                    with open(paths[0], "rb") as f:
                        msg = await bot.send_photo(
                            chat_id=self.chat_id,
                            photo=f,
                            caption=caption,
                            reply_markup=keyboard,
                        )
                    return str(msg.message_id)
                if len(paths) > 1:
                    from telegram import InputFile

                    media = []
                    for i, path in enumerate(paths):
                        with open(path, "rb") as f:
                            data = f.read()
                        media.append(
                            InputMediaPhoto(
                                media=InputFile(data, filename=Path(path).name),
                                caption=caption if i == 0 else None,
                            )
                        )
                    await bot.send_media_group(chat_id=self.chat_id, media=media)
                    msg = await bot.send_message(
                        chat_id=self.chat_id,
                        text=text[:4000],
                        reply_markup=keyboard,
                    )
                    return str(msg.message_id)
                msg = await bot.send_message(
                    chat_id=self.chat_id,
                    text=text[:4000],
                    reply_markup=keyboard,
                )
                return str(msg.message_id)

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop and loop.is_running():
                future = asyncio.run_coroutine_threadsafe(_send(), loop)
                message_id = future.result(timeout=60)
            else:
                message_id = asyncio.run(_send())

            self.poster.db.update_draft(draft_id, {"telegram_message_id": message_id})
            return message_id
        except Exception as e:
            logger.error(
                f"Telegram notify failed for draft {draft_id}: {e}", exc_info=True
            )
            return None

    def _format_message(self, draft_id: int, draft: Dict[str, Any]) -> str:
        kind = draft.get("kind", "?")
        status = draft.get("status", "pending")
        generated = draft.get("edited_text") or draft.get("generated_text") or ""
        target = draft.get("target_tweet_text") or ""
        author = draft.get("target_author") or ""
        tip = draft.get("keyword") if draft.get("source") == "telegram_compose" else None
        lines = [
            f"Draft #{draft_id} [{kind}] — {status}",
            f"Provider: {draft.get('provider') or 'n/a'} / {draft.get('model') or 'n/a'}",
        ]
        if tip:
            lines.append(f"\nYour tip:\n{tip[:400]}")
        if author or target:
            lines.append(f"\nOriginal (@{author}):\n{(target or '')[:500]}")
        lines.append(f"\nProposed:\n{generated[:1500]}")
        if draft.get("keyword") and draft.get("source") != "telegram_compose":
            lines.append(f"\nKeyword: {draft.get('keyword')}")
        lines.append(
            "\nApprove · Edit (manual text) · Rewrite (AI again) · Reject\n"
            "Tip: /rewrite [optional guidance] for the last compose draft."
        )
        return "\n".join(lines)

    def create_compose_draft(
        self, tip: str, media_asset_ids: Any, user_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """AI-expand tip, create tweet draft, attach image(s), notify for approve."""
        from .config import Config
        from .quality_safety import (
            validate_tip_before_compose,
            check_text_quality,
            check_duplicate_text,
            log_safety_event,
        )

        if isinstance(media_asset_ids, int):
            media_asset_ids = [media_asset_ids]
        media_asset_ids = [int(m) for m in (media_asset_ids or [])][:4]
        if not media_asset_ids:
            return {"ok": False, "error": "No images attached"}

        self.poster._ensure()
        if self.poster.config is None:
            self.poster.config = Config()
        safety = self.poster.config.get_safety_config()

        ok, reason = validate_tip_before_compose(self.poster.db, tip, safety)
        if not ok:
            log_safety_event(
                self.poster.db,
                "tip_refused",
                reason,
                {"tip": (tip or "")[:200]},
            )
            return {"ok": False, "error": reason, "blocked": True}

        composed = compose_post_from_tip(tip)
        if not composed.get("ok"):
            return {"ok": False, "error": composed.get("error") or "AI compose failed"}

        ok, reason = check_text_quality(composed["text"], safety)
        if not ok:
            log_safety_event(
                self.poster.db,
                "compose_quality",
                reason,
                {"tip": (tip or "")[:200]},
            )
            return {"ok": False, "error": reason, "blocked": True}

        ok, reason = check_duplicate_text(self.poster.db, composed["text"], safety)
        if not ok:
            log_safety_event(
                self.poster.db,
                "compose_duplicate",
                reason,
                {"tip": (tip or "")[:200]},
            )
            return {"ok": False, "error": reason, "blocked": True}

        draft_id = self.poster.db.create_draft(
            {
                "kind": "tweet",
                "status": "pending",
                "generated_text": composed["text"],
                "provider": composed.get("provider"),
                "model": composed.get("model"),
                "prompt_profile_id": composed.get("profile_id"),
                "source": "telegram_compose",
                "keyword": tip[:255],
            }
        )
        self.poster.db.set_draft_media(draft_id, media_asset_ids)
        draft = self.poster.db.get_draft(draft_id)
        self.notify_draft(draft_id, draft or {})
        if user_id is not None:
            self._last_compose_draft[user_id] = draft_id
        return {
            "ok": True,
            "draft_id": draft_id,
            "text": composed["text"],
            "fallback": composed.get("fallback", False),
        }

    def rewrite_draft(
        self, draft_id: int, guidance: str = "", user_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """Re-run AI on an existing pending draft's tip/text, keep media."""
        from .quality_safety import check_text_quality, check_duplicate_text, log_safety_event

        self.poster._ensure()
        if self.poster.config is None:
            from .config import Config

            self.poster.config = Config()
        draft = self.poster.db.get_draft(draft_id)
        if not draft:
            return {"ok": False, "error": "Draft not found"}
        if draft.get("status") != "pending":
            return {"ok": False, "error": f"Draft is {draft.get('status')}"}

        tip = (draft.get("keyword") or "").strip()
        base = (draft.get("edited_text") or draft.get("generated_text") or "").strip()
        prompt = tip or base
        if guidance.strip():
            prompt = (
                f"Original tip/brief:\n{prompt}\n\n"
                f"Rewrite guidance:\n{guidance.strip()}\n\n"
                f"Current draft text (improve it):\n{base}"
            )
        elif tip:
            prompt = (
                f"{tip}\n\nCurrent draft (rewrite a better version):\n{base}"
            )

        composed = compose_post_from_tip(prompt)
        if not composed.get("ok"):
            return {"ok": False, "error": composed.get("error") or "Rewrite failed"}

        safety = self.poster.config.get_safety_config()
        ok, reason = check_text_quality(composed["text"], safety)
        if not ok:
            log_safety_event(self.poster.db, "rewrite_quality", reason, {"draft_id": draft_id})
            return {"ok": False, "error": reason, "blocked": True}
        ok, reason = check_duplicate_text(self.poster.db, composed["text"], safety)
        if not ok:
            # Allow rewrite that matches itself — only block if DIFFERENT draft
            if composed["text"] != base:
                log_safety_event(
                    self.poster.db, "rewrite_duplicate", reason, {"draft_id": draft_id}
                )
                return {"ok": False, "error": reason, "blocked": True}

        self.poster.db.update_draft(
            draft_id,
            {
                "generated_text": composed["text"],
                "edited_text": None,
            },
        )
        # update provider fields via raw update if needed — keep generated_text
        draft = self.poster.db.get_draft(draft_id)
        # Patch provider on draft row
        try:
            cursor = self.poster.db.conn.cursor()
            cursor.execute(
                """
                UPDATE content_drafts
                SET provider = %s, model = %s
                WHERE id = %s
                """,
                (composed.get("provider"), composed.get("model"), draft_id),
            )
            self.poster.db.conn.commit()
            cursor.close()
        except Exception:
            try:
                self.poster.db.conn.rollback()
            except Exception:
                pass

        draft = self.poster.db.get_draft(draft_id)
        self.notify_draft(draft_id, draft or {})
        if user_id is not None:
            self._last_compose_draft[user_id] = draft_id
        return {"ok": True, "draft_id": draft_id, "text": composed["text"]}

    async def _save_telegram_photo(self, bot, photo_sizes) -> int:
        """Download largest Telegram photo size into media library; return asset id."""
        from .media_store import save_bytes, media_root

        photo = photo_sizes[-1]
        tg_file = await bot.get_file(photo.file_id)
        data = bytes(await tg_file.download_as_bytearray())
        stored, original, mime, size = save_bytes(
            data, original_name="telegram.jpg", mime_type="image/jpeg"
        )
        self.poster._ensure()
        path = str(media_root() / stored)
        return self.poster.db.create_media_asset(stored, original, mime, path, size)

    async def _reply_callback_result(self, query, text: str):
        """Edit caption or text depending on message type."""
        try:
            if query.message and query.message.photo:
                await query.edit_message_caption(caption=text[:1024])
            else:
                await query.edit_message_text(text[:4000])
        except Exception:
            try:
                await query.message.reply_text(text[:4000])
            except Exception:
                pass

    def start_polling(self) -> bool:
        """Start Telegram long-polling in a daemon thread (at most one process)."""
        global _polling_started

        if not self.enabled:
            logger.warning(
                "Telegram approver disabled (missing/invalid TELEGRAM_BOT_TOKEN/CHAT_ID)"
            )
            return False
        if os.getenv("TELEGRAM_POLLING", "true").lower() in ("0", "false", "no", "off"):
            logger.info("Telegram polling disabled via TELEGRAM_POLLING=false")
            return False

        with _polling_start_lock:
            if _polling_started or (self._thread and self._thread.is_alive()):
                logger.info("Telegram polling already running in this process")
                return True
            _polling_started = True

        def _run():
            global _polling_started
            try:
                retries = int(os.getenv("TELEGRAM_LOCK_RETRIES", "12"))
                delay = float(os.getenv("TELEGRAM_LOCK_RETRY_SECONDS", "10"))
                acquired = False
                for attempt in range(max(1, retries)):
                    if self._try_acquire_polling_lock():
                        acquired = True
                        break
                    logger.info(
                        f"Waiting for Telegram polling lock ({attempt + 1}/{retries})…"
                    )
                    time.sleep(delay)
                if not acquired:
                    logger.error(
                        "Could not acquire Telegram polling lock — another instance "
                        "still runs getUpdates. Ensure a single Render service uses "
                        "this bot token."
                    )
                    _polling_started = False
                    return

                from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
                from telegram.error import Conflict
                from telegram.ext import (
                    Application,
                    CallbackQueryHandler,
                    CommandHandler,
                    MessageHandler,
                    filters,
                    ContextTypes,
                )

                async def _bootstrap(application):
                    try:
                        await application.bot.delete_webhook(drop_pending_updates=True)
                    except Exception as wh_err:
                        logger.warning(f"delete_webhook skipped: {wh_err}")

                app = (
                    Application.builder()
                    .token(self.token)
                    .post_init(_bootstrap)
                    .build()
                )
                self._app = app

                async def _on_error(update, context):
                    err = context.error
                    if isinstance(err, Conflict):
                        logger.warning(
                            "Telegram Conflict (another getUpdates): %s", err
                        )
                        return
                    logger.error("Telegram handler error: %s", err, exc_info=err)

                app.add_error_handler(_on_error)

                HELP = (
                    "X Bot compose (MITL)\n\n"
                    "• Photo + caption tip → AI full post + Approve buttons\n"
                    "• Photo album (up to 4) + tip on first/last caption\n"
                    "• Photo then tip as next message\n"
                    "• /rewrite [guidance] — AI rewrite last compose draft\n"
                    "• /rewrite 123 [guidance] — rewrite draft #123\n"
                    "Buttons: Approve · Edit · Rewrite · Reject\n"
                    "Dashboard Drafts works the same."
                )

                async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
                    if not update.effective_chat or not self._authorized_chat(
                        update.effective_chat.id
                    ):
                        return
                    await update.message.reply_text(HELP)

                async def on_rewrite_cmd(
                    update: Update, context: ContextTypes.DEFAULT_TYPE
                ):
                    if not update.effective_chat or not self._authorized_chat(
                        update.effective_chat.id
                    ):
                        return
                    user_id = update.effective_user.id
                    args = context.args or []
                    draft_id = None
                    guidance_parts = []
                    if args and args[0].isdigit():
                        draft_id = int(args[0])
                        guidance_parts = args[1:]
                    else:
                        draft_id = self._last_compose_draft.get(user_id)
                        guidance_parts = args
                    if not draft_id:
                        self.poster._ensure()
                        pending = self.poster.db.list_drafts(status="pending", limit=1)
                        if pending:
                            draft_id = pending[0]["id"]
                    if not draft_id:
                        await update.message.reply_text(
                            "No draft to rewrite. Compose one first, or use /rewrite <id>."
                        )
                        return
                    guidance = " ".join(guidance_parts).strip()
                    await update.message.reply_text(f"Rewriting draft #{draft_id}…")
                    result = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: self.rewrite_draft(draft_id, guidance, user_id=user_id),
                    )
                    if result.get("ok"):
                        await update.message.reply_text(
                            f"Draft #{draft_id} rewritten — check the preview above."
                        )
                    else:
                        await update.message.reply_text(
                            f"Rewrite failed: {result.get('error')}"
                        )

                async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
                    query = update.callback_query
                    if not query:
                        return
                    chat = query.message.chat if query.message else None
                    if not chat or not self._authorized_chat(chat.id):
                        await query.answer("Unauthorized", show_alert=True)
                        return
                    await query.answer()
                    data = query.data or ""
                    if ":" not in data:
                        return
                    action, draft_id_s = data.split(":", 1)
                    try:
                        draft_id = int(draft_id_s)
                    except ValueError:
                        return
                    user_id = query.from_user.id if query.from_user else 0

                    if action == "approve":
                        result = self.poster.approve_and_post(draft_id)
                        if result.get("ok"):
                            await self._reply_callback_result(
                                query,
                                f"Draft #{draft_id} posted ({result.get('posted_tweet_id')})",
                            )
                        else:
                            await self._reply_callback_result(
                                query,
                                f"Draft #{draft_id} failed: {result.get('error')}",
                            )
                    elif action == "reject":
                        result = self.poster.reject(draft_id)
                        if result.get("ok"):
                            await self._reply_callback_result(
                                query, f"Draft #{draft_id} rejected."
                            )
                        else:
                            await self._reply_callback_result(
                                query, f"Reject failed: {result.get('error')}"
                            )
                    elif action == "edit":
                        self._awaiting_edit[user_id] = draft_id
                        self._awaiting_tip.pop(user_id, None)
                        await self._reply_callback_result(
                            query,
                            f"Draft #{draft_id}: send the edited post text as your next message.",
                        )
                    elif action == "rewrite":
                        await self._reply_callback_result(
                            query, f"Rewriting draft #{draft_id}…"
                        )
                        result = await asyncio.get_event_loop().run_in_executor(
                            None,
                            lambda: self.rewrite_draft(draft_id, "", user_id=user_id),
                        )
                        if not result.get("ok"):
                            await query.message.reply_text(
                                f"Rewrite failed: {result.get('error')}"
                            )

                async def _finish_media_group(group_id: str):
                    await asyncio.sleep(1.5)
                    with self._media_group_lock:
                        buf = self._media_group_buf.pop(group_id, None)
                    if not buf:
                        return
                    user_id = buf["user_id"]
                    chat_id = buf["chat_id"]
                    media_ids = buf["media_ids"][:4]
                    tip = (buf.get("tip") or "").strip()
                    bot = app.bot
                    if tip:
                        result = await asyncio.get_event_loop().run_in_executor(
                            None,
                            lambda: self.create_compose_draft(
                                tip, media_ids, user_id=user_id
                            ),
                        )
                        if result.get("ok"):
                            await bot.send_message(
                                chat_id=chat_id,
                                text=(
                                    f"Album draft #{result['draft_id']} ready "
                                    f"({len(media_ids)} images). Preview above."
                                ),
                            )
                        else:
                            await bot.send_message(
                                chat_id=chat_id,
                                text=f"Compose failed: {result.get('error')}",
                            )
                    else:
                        self._awaiting_tip[user_id] = media_ids
                        await bot.send_message(
                            chat_id=chat_id,
                            text=(
                                f"Saved {len(media_ids)} images. "
                                "Send a short tip for the caption."
                            ),
                        )

                async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
                    if not update.message or not update.effective_user:
                        return
                    if not self._authorized_chat(update.effective_chat.id):
                        return
                    user_id = update.effective_user.id
                    self._awaiting_edit.pop(user_id, None)

                    try:
                        media_id = await self._save_telegram_photo(
                            context.bot, update.message.photo
                        )
                    except Exception as e:
                        logger.error(f"Telegram photo save failed: {e}", exc_info=True)
                        await update.message.reply_text(f"Could not save image: {e}")
                        return

                    tip = (update.message.caption or "").strip()
                    group_id = update.message.media_group_id

                    if group_id:
                        with self._media_group_lock:
                            buf = self._media_group_buf.get(group_id)
                            if not buf:
                                self._media_group_buf[group_id] = {
                                    "user_id": user_id,
                                    "chat_id": update.effective_chat.id,
                                    "media_ids": [media_id],
                                    "tip": tip,
                                }
                                asyncio.create_task(_finish_media_group(group_id))
                            else:
                                if media_id not in buf["media_ids"]:
                                    buf["media_ids"].append(media_id)
                                if tip and not buf.get("tip"):
                                    buf["tip"] = tip
                        return

                    # Single photo
                    if tip:
                        await update.message.reply_text("Got it — writing the post…")
                        result = await asyncio.get_event_loop().run_in_executor(
                            None,
                            lambda: self.create_compose_draft(
                                tip, [media_id], user_id=user_id
                            ),
                        )
                        if result.get("ok"):
                            note = " (template fallback)" if result.get("fallback") else ""
                            await update.message.reply_text(
                                f"Draft #{result['draft_id']} ready{note}. "
                                "See the image preview + buttons above."
                            )
                        else:
                            await update.message.reply_text(
                                f"Compose failed: {result.get('error')}"
                            )
                    else:
                        self._awaiting_tip[user_id] = [media_id]
                        await update.message.reply_text(
                            "Image saved. Send a short tip — what should this post say?"
                        )

                async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
                    if not update.message or not update.effective_user:
                        return
                    if not self._authorized_chat(update.effective_chat.id):
                        return
                    user_id = update.effective_user.id
                    text = (update.message.text or "").strip()
                    if not text:
                        return

                    media_ids = self._awaiting_tip.pop(user_id, None)
                    if media_ids:
                        if isinstance(media_ids, int):
                            media_ids = [media_ids]
                        await update.message.reply_text("Writing the full post…")
                        result = await asyncio.get_event_loop().run_in_executor(
                            None,
                            lambda: self.create_compose_draft(
                                text, media_ids, user_id=user_id
                            ),
                        )
                        if result.get("ok"):
                            note = " (template fallback)" if result.get("fallback") else ""
                            await update.message.reply_text(
                                f"Draft #{result['draft_id']} ready{note}. "
                                "Use Approve / Edit / Rewrite / Reject."
                            )
                        else:
                            self._awaiting_tip[user_id] = media_ids
                            await update.message.reply_text(
                                f"Compose failed: {result.get('error')}. Send another tip."
                            )
                        return

                    draft_id = self._awaiting_edit.pop(user_id, None)
                    if not draft_id:
                        await update.message.reply_text(
                            "Send a photo (or album) + tip to compose, "
                            "or /rewrite · /start for help."
                        )
                        return

                    result = self.poster.save_edit(draft_id, text)
                    if not result.get("ok"):
                        await update.message.reply_text(
                            f"Edit failed: {result.get('error')}"
                        )
                        return
                    self.poster._ensure()
                    draft = self.poster.db.get_draft(draft_id)
                    self.notify_draft(draft_id, draft or {})
                    await update.message.reply_text(
                        f"Draft #{draft_id} updated — preview resent above."
                    )

                app.add_handler(CommandHandler("start", on_start))
                app.add_handler(CommandHandler("help", on_start))
                app.add_handler(CommandHandler("rewrite", on_rewrite_cmd))
                app.add_handler(CallbackQueryHandler(on_callback))
                app.add_handler(MessageHandler(filters.PHOTO, on_photo))
                app.add_handler(
                    MessageHandler(filters.TEXT & ~filters.COMMAND, on_text)
                )
                logger.info("Telegram polish polling started (preview/album/rewrite)")
                try:
                    app.run_polling(
                        drop_pending_updates=True,
                        stop_signals=None,
                        allowed_updates=Update.ALL_TYPES,
                    )
                except Conflict as e:
                    logger.warning(
                        "Telegram polling stopped due to Conflict "
                        "(another process owns getUpdates): %s",
                        e,
                    )
            except Exception as e:
                logger.error(f"Telegram polling crashed: {e}", exc_info=True)
            finally:
                self._release_polling_lock()
                _polling_started = False

        self._thread = threading.Thread(
            target=_run, name="telegram-approver", daemon=True
        )
        self._thread.start()
        return True


_approver: Optional[TelegramApprover] = None


def get_telegram_approver() -> TelegramApprover:
    global _approver
    if _approver is None:
        _approver = TelegramApprover()
    return _approver
