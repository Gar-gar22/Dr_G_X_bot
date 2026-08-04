"""Quality gates, rate budgets, and risky-tip refusal."""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_SAFETY: Dict[str, Any] = {
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
}

# Tip / content patterns that should not become posts
RISKY_TIP_PATTERNS: List[Tuple[str, str]] = [
    (r"\bguaranteed\s+(returns?|profit|gains?|apy)\b", "guaranteed returns claims"),
    (r"\b(100x|1000x)\b.*\b(guaranteed|sure|easy)\b", "unrealistic return promises"),
    (r"\bsend\s+(me\s+)?(eth|btc|sol|usdt|crypto)\b", "asking users to send crypto"),
    (r"\b(dm\s+me|direct\s+message).{0,40}\b(seed|private\s*key|recovery)\b", "seed/private key solicitation"),
    (r"\b(seed\s*phrase|private\s*key|recovery\s*phrase)\b", "sensitive wallet material"),
    (r"\bfree\s+airdrop\b.*\b(connect|wallet|click)\b", "suspicious airdrop CTA"),
    (r"\bimpersonat(e|ing)\b", "impersonation"),
    (r"\b(as\s+vitalik|official\s+binance|official\s+coinbase)\b", "brand impersonation risk"),
    (r"\bfinancial\s+advice\b.*\b(buy|sell|invest)\b", "financial advice framing"),
    (r"\b(pump\s+and\s+dump|exit\s+scam)\b.*\b(let'?s|we\s+should)\b", "market manipulation intent"),
    (r"\b(csam|child\s+porn)\b", "prohibited content"),
    (r"\b(kill|murder|assassinate)\b.*\b(him|her|them|people)\b", "violent content"),
]

SPAM_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.I)
HASHTAG_RE = re.compile(r"(?:^|\s)#\w+")
MENTION_RE = re.compile(r"(?:^|\s)@\w+")


def merge_safety_config(raw: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = dict(DEFAULT_SAFETY)
    if raw:
        cfg.update({k: v for k, v in raw.items() if v is not None})
    # Env overrides
    if os.getenv("SAFETY_ENABLED") is not None:
        cfg["enabled"] = os.getenv("SAFETY_ENABLED", "true").lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
    if os.getenv("DAILY_POST_LIMIT"):
        try:
            cfg["daily_post_limit"] = int(os.getenv("DAILY_POST_LIMIT"))
        except ValueError:
            pass
    if os.getenv("MONTHLY_POST_LIMIT"):
        try:
            cfg["monthly_post_limit"] = int(os.getenv("MONTHLY_POST_LIMIT"))
        except ValueError:
            pass
    return cfg


def _count_posts_since(db, since: datetime) -> int:
    """Count replies + tweets + quotes since timestamp."""
    cursor = db.conn.cursor()
    total = 0
    try:
        for table, col in (
            ("replied_tweets", "replied_at"),
            ("posted_tweets", "posted_at"),
            ("quote_retweets", "posted_at"),
        ):
            try:
                cursor.execute(
                    f"SELECT COUNT(*) AS c FROM {table} WHERE {col} >= %s",
                    (since,),
                )
                row = cursor.fetchone()
                total += int(row["c"]) if row else 0
            except Exception:
                db.conn.rollback()
        return total
    finally:
        cursor.close()


def get_usage_stats(db) -> Dict[str, int]:
    """Current usage vs time windows."""
    now = datetime.now()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    pending = 0
    try:
        pending = db.count_drafts(status="pending")
    except Exception:
        pass
    return {
        "posts_today": _count_posts_since(db, day_start),
        "posts_month": _count_posts_since(db, month_start),
        "pending_drafts": pending,
    }


def check_rate_budget(db, safety: Dict[str, Any]) -> Tuple[bool, str]:
    """Return (ok, reason)."""
    if not safety.get("enabled", True):
        return True, ""
    usage = get_usage_stats(db)
    daily = int(safety.get("daily_post_limit", 30))
    monthly = int(safety.get("monthly_post_limit", 400))
    max_pending = int(safety.get("max_pending_drafts", 50))
    if daily > 0 and usage["posts_today"] >= daily:
        return False, f"Daily post limit reached ({usage['posts_today']}/{daily})"
    if monthly > 0 and usage["posts_month"] >= monthly:
        return False, f"Monthly post limit reached ({usage['posts_month']}/{monthly})"
    if max_pending > 0 and usage["pending_drafts"] >= max_pending:
        return (
            False,
            f"Too many pending drafts ({usage['pending_drafts']}/{max_pending}). "
            "Approve or reject some first.",
        )
    return True, ""


def check_text_quality(text: str, safety: Dict[str, Any]) -> Tuple[bool, str]:
    """Spam / quality gates on generated or edited post text."""
    if not safety.get("enabled", True):
        return True, ""
    text = (text or "").strip()
    min_c = int(safety.get("min_chars", 20))
    max_c = int(safety.get("max_chars", 280))
    if len(text) < min_c:
        return False, f"Text too short (min {min_c} chars)"
    if len(text) > max_c:
        return False, f"Text too long (max {max_c} chars)"

    letters = [c for c in text if c.isalpha()]
    if safety.get("block_all_caps") and letters and sum(1 for c in letters if c.isupper()) / len(letters) > 0.85:
        return False, "Blocked: mostly ALL CAPS"

    hashtags = HASHTAG_RE.findall(text)
    max_h = int(safety.get("max_hashtags", 3))
    if max_h >= 0 and len(hashtags) > max_h:
        return False, f"Too many hashtags ({len(hashtags)}/{max_h})"

    links = SPAM_URL_RE.findall(text)
    max_l = int(safety.get("max_links", 2))
    if max_l >= 0 and len(links) > max_l:
        return False, f"Too many links ({len(links)}/{max_l})"

    # Repeated character spam
    if re.search(r"(.)\1{7,}", text):
        return False, "Blocked: repeated character spam"

    for phrase in safety.get("custom_block_phrases") or []:
        p = (phrase or "").strip().lower()
        if p and p in text.lower():
            return False, f"Blocked by custom phrase: {phrase}"

    return True, ""


def check_duplicate_text(db, text: str, safety: Dict[str, Any]) -> Tuple[bool, str]:
    """Reject if same text was posted recently."""
    if not safety.get("enabled", True) or not safety.get("block_duplicates", True):
        return True, ""
    text = (text or "").strip()
    if not text:
        return True, ""
    since = datetime.now() - timedelta(days=7)
    cursor = db.conn.cursor()
    try:
        cursor.execute(
            """
            SELECT tweet_id FROM posted_tweets
            WHERE posted_at >= %s AND text = %s
            LIMIT 1
            """,
            (since, text),
        )
        if cursor.fetchone():
            return False, "Duplicate of a post from the last 7 days"
        cursor.execute(
            """
            SELECT id FROM content_drafts
            WHERE status IN ('pending', 'posted')
              AND created_at >= %s
              AND (generated_text = %s OR edited_text = %s)
            LIMIT 1
            """,
            (since, text, text),
        )
        if cursor.fetchone():
            return False, "Duplicate of an existing draft/post"
        return True, ""
    except Exception as e:
        logger.warning(f"Duplicate check failed: {e}")
        return True, ""
    finally:
        cursor.close()


def check_risky_tip(tip: str, safety: Dict[str, Any]) -> Tuple[bool, str]:
    """Refuse tips that ask for harmful / scammy / prohibited content."""
    if not safety.get("enabled", True) or not safety.get("refuse_risky_tips", True):
        return True, ""
    tip = (tip or "").strip()
    if not tip:
        return False, "Empty tip"
    if len(tip) < 3:
        return False, "Tip too short"

    lowered = tip.lower()
    for pattern, reason in RISKY_TIP_PATTERNS:
        if re.search(pattern, lowered, re.I):
            return False, f"Tip refused: {reason}"

    for phrase in safety.get("custom_block_phrases") or []:
        p = (phrase or "").strip().lower()
        if p and p in lowered:
            return False, f"Tip refused (custom phrase): {phrase}"

    return True, ""


def log_safety_event(db, event_type: str, message: str, meta: Optional[Dict] = None) -> None:
    """Persist a blocked/refusal event (best-effort)."""
    try:
        import json

        cursor = db.conn.cursor()
        cursor.execute(
            """
            INSERT INTO safety_events (event_type, message, meta_json)
            VALUES (%s, %s, %s)
            """,
            (
                event_type[:50],
                (message or "")[:1000],
                json.dumps(meta or {})[:2000],
            ),
        )
        db.conn.commit()
        cursor.close()
    except Exception as e:
        try:
            db.conn.rollback()
        except Exception:
            pass
        logger.debug(f"Could not log safety event: {e}")


def count_safety_events_today(db) -> int:
    try:
        cursor = db.conn.cursor()
        day_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        cursor.execute(
            "SELECT COUNT(*) AS c FROM safety_events WHERE created_at >= %s",
            (day_start,),
        )
        row = cursor.fetchone()
        cursor.close()
        return int(row["c"]) if row else 0
    except Exception:
        return 0


def list_safety_events(db, limit: int = 50) -> List[Dict[str, Any]]:
    try:
        cursor = db.conn.cursor()
        cursor.execute(
            """
            SELECT * FROM safety_events
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        rows = list(cursor.fetchall() or [])
        cursor.close()
        return rows
    except Exception:
        return []


def validate_before_post(db, text: str, safety: Dict[str, Any]) -> Tuple[bool, str]:
    """Combined checks before posting an approved draft."""
    ok, reason = check_rate_budget(db, safety)
    if not ok:
        return False, reason
    ok, reason = check_text_quality(text, safety)
    if not ok:
        return False, reason
    ok, reason = check_duplicate_text(db, text, safety)
    if not ok:
        return False, reason
    return True, ""


def validate_tip_before_compose(db, tip: str, safety: Dict[str, Any]) -> Tuple[bool, str]:
    """Combined checks before expanding a Telegram tip."""
    ok, reason = check_rate_budget(db, safety)
    if not ok:
        return False, reason
    ok, reason = check_risky_tip(tip, safety)
    if not ok:
        return False, reason
    return True, ""
