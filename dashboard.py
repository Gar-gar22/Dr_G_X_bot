from flask import Flask, render_template_string, request, redirect, url_for, flash, session, jsonify, send_from_directory
import json
import re
import logging
import threading
import time
import os
from collections import Counter
from pathlib import Path
from typing import List, Optional, Tuple
from functools import wraps
from datetime import datetime

from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
import schedule

from src.config import Config
from src.x_api import XAPI
from src.bot import AutoReplyBot
from src.scheduler import BotScheduler
from src.database import Database
from src.telegram_approver import DraftPoster, get_telegram_approver
from src.media_store import media_root, save_upload, delete_file

logger = logging.getLogger(__name__)

# Bot status tracking
bot_status = {
    "running": False,
    "last_run": None,
    "next_run": None,
    "current_run_stats": None,
    "scheduler_thread": None,
    "bot_instance": None
}
bot_status_lock = threading.Lock()

# Global database instance
db_instance = None

app = Flask(__name__)
# Use environment variable for secret key in production, fallback for development
app.secret_key = os.getenv("FLASK_SECRET_KEY", "change-this-secret-key-in-production")

# Render (and similar hosts) terminate TLS and forward HTTP to gunicorn.
# Without ProxyFix, redirects/cookies use http:// and sessions fail after login.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
_is_render = bool(os.getenv("RENDER") or os.getenv("RENDER_EXTERNAL_URL"))
_prefer_https = _is_render or os.getenv("FORCE_HTTPS", "").lower() in ("1", "true", "yes")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=_prefer_https,
    PREFERRED_URL_SCHEME="https" if _prefer_https else "http",
)

ADMIN_EMAIL = (os.getenv("ADMIN_EMAIL") or "").strip().lower()
ADMIN_PASSWORD = (os.getenv("ADMIN_PASSWORD") or "").strip()
_ADMIN_PASSWORD_HASH = (
    generate_password_hash(ADMIN_PASSWORD) if ADMIN_PASSWORD else None
)


def _extract_profile_keywords(texts: List[str], max_keywords: int = 50) -> List[str]:
    """Derive keyword candidates from a list of tweet texts.
    
    Extracts hashtags, important words, and common phrases to build a profile.
    """
    if not texts:
        return []

    combined = " ".join(texts)
    
    # Remove URLs to clean up text
    combined = re.sub(r"https?://\S+", "", combined)
    combined = re.sub(r"www\.\S+", "", combined)

    keywords: List[str] = []

    # 1. Collect hashtags (without #) - these are usually important
    hashtags = [h[1:].lower().strip() for h in re.findall(r"#\w+", combined)]
    if hashtags:
        hashtag_counts = Counter(hashtags)
        # Get top hashtags (appearing at least 2 times or top 20)
        for tag, count in hashtag_counts.most_common(20):
            if len(tag) >= 2 and tag not in keywords:
                keywords.append(tag)
        # Also include hashtags that appear multiple times
        for tag, count in hashtag_counts.items():
            if count >= 2 and tag not in keywords and len(tag) >= 3:
                keywords.append(tag)

    # 2. Extract important words (3+ characters, not stopwords)
    words = [
        w.lower().strip()
        for w in re.findall(r"\b[A-Za-z][A-Za-z0-9_]{2,}\b", combined)
    ]

    stopwords = {
        "the", "and", "for", "with", "this", "that", "from", "your", "you",
        "have", "has", "are", "was", "were", "just", "about", "into", "http",
        "https", "rt", "co", "amp", "com", "www", "can", "but", "not", "all",
        "get", "got", "will", "would", "could", "should", "what", "when",
        "where", "why", "how", "who", "which", "their", "they", "them",
        "these", "those", "been", "being", "than", "then", "only", "more",
        "most", "some", "much", "many", "such", "also", "like", "make",
        "time", "very", "just", "now", "may", "way", "see", "know", "want",
        "use", "new", "old", "good", "bad", "right", "left", "well", "best",
        "first", "last", "great", "really", "still", "even", "back", "come",
        "go", "say", "said", "think", "take", "give", "look", "find", "work",
    }

    filtered_words = [w for w in words if w not in stopwords and len(w) >= 3]
    
    if filtered_words:
        word_counts = Counter(filtered_words)
        # Get words that appear multiple times or are in top 30
        for word, count in word_counts.most_common(30):
            if word not in keywords and len(word) >= 3:
                keywords.append(word)
        
        # Add words that appear at least 3 times
        for word, count in word_counts.items():
            if count >= 3 and word not in keywords and len(word) >= 3:
                keywords.append(word)

    # 3. Extract 2-word phrases (bigrams) that are common
    phrases = []
    for text in texts:
        # Clean text
        text = re.sub(r"https?://\S+", "", text)
        text = re.sub(r"#\w+", "", text)  # Remove hashtags for phrase extraction
        words_in_text = [
            w.lower() 
            for w in re.findall(r"\b[A-Za-z][A-Za-z0-9_]{2,}\b", text)
            if w.lower() not in stopwords and len(w) >= 3
        ]
        # Create bigrams
        for i in range(len(words_in_text) - 1):
            phrase = f"{words_in_text[i]} {words_in_text[i+1]}"
            if len(phrase) >= 6 and len(phrase) <= 40:  # Reasonable phrase length
                phrases.append(phrase)
    
    if phrases:
        phrase_counts = Counter(phrases)
        # Get phrases that appear at least 2 times
        for phrase, count in phrase_counts.items():
            if count >= 2 and phrase not in keywords:
                keywords.append(phrase)

    # Limit to max_keywords
    return keywords[:max_keywords]


def get_db_connection():
    """Get PostgreSQL database connection using Database class."""
    global db_instance
    if db_instance is None:
        try:
            db_instance = Database()
        except Exception as e:
            logger.error(f"Failed to create database connection: {e}", exc_info=True)
            raise
    return db_instance


def login_required(f):
    """Decorator to require login for routes."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please log in to access this page.', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


def verify_admin(email: str, password: str) -> bool:
    """Verify admin credentials from environment."""
    if not ADMIN_EMAIL or not _ADMIN_PASSWORD_HASH:
        logger.error("ADMIN_EMAIL / ADMIN_PASSWORD not configured")
        return False
    if email.strip().lower() != ADMIN_EMAIL:
        return False
    return check_password_hash(_ADMIN_PASSWORD_HASH, password)


ADMIN_HEAD = """
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{{ page_title }} · Dr G</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.css" rel="stylesheet">
    <link href="{{ url_for('static', filename='style.css') }}" rel="stylesheet">
"""

ADMIN_SIDEBAR = """
      <nav class="sidebar text-white">
        <div class="brand">
          <div class="brand-mark">DG</div>
          <div class="brand-text">
            <strong>Dr G</strong>
            <span>Control center</span>
          </div>
        </div>
        <div class="nav-section">Workspace</div>
        <ul class="nav nav-pills flex-column mb-auto">
          <li class="nav-item">
            <a href="{{ url_for('dashboard_overview') }}" class="nav-link {% if active=='overview' %}active{% endif %}">
              <i class="bi bi-speedometer2 me-2"></i> Overview
            </a>
          </li>
          <li>
            <a href="{{ url_for('settings_automation') }}" class="nav-link {% if active=='automation' %}active{% endif %}">
              <i class="bi bi-gear me-2"></i> Automation
            </a>
          </li>
          <li>
            <a href="{{ url_for('records_drafts') }}" class="nav-link {% if active=='drafts' %}active{% endif %}">
              <i class="bi bi-hourglass-split me-2"></i> Drafts
            </a>
          </li>
          <li>
            <a href="{{ url_for('agent_chat') }}" class="nav-link {% if active=='chat' %}active{% endif %}">
              <i class="bi bi-chat-dots me-2"></i> Chat
            </a>
          </li>
          <li>
            <a href="{{ url_for('records_agent') }}" class="nav-link {% if active=='agent' %}active{% endif %}">
              <i class="bi bi-cpu me-2"></i> Agent
            </a>
          </li>
          <li>
            <a href="{{ url_for('records_replies') }}" class="nav-link {% if active in ('replies','tweets','quotes') %}active{% endif %}">
              <i class="bi bi-chat-left-text me-2"></i> Records
            </a>
          </li>
          <div class="nav-section">Configure</div>
          <li>
            <a href="{{ url_for('settings_keywords') }}" class="nav-link {% if active=='keywords' %}active{% endif %}">
              <i class="bi bi-filter-circle me-2"></i> Keywords
            </a>
          </li>
          <li>
            <a href="{{ url_for('settings_credentials') }}" class="nav-link {% if active=='credentials' %}active{% endif %}">
              <i class="bi bi-key me-2"></i> Credentials
            </a>
          </li>
          <li>
            <a href="{{ url_for('settings_ai') }}" class="nav-link {% if active=='ai' %}active{% endif %}">
              <i class="bi bi-robot me-2"></i> AI Settings
            </a>
          </li>
          <li>
            <a href="{{ url_for('settings_media') }}" class="nav-link {% if active=='media' %}active{% endif %}">
              <i class="bi bi-image me-2"></i> Media
            </a>
          </li>
          <li>
            <a href="{{ url_for('settings_safety') }}" class="nav-link {% if active=='safety' %}active{% endif %}">
              <i class="bi bi-shield-check me-2"></i> Safety
            </a>
          </li>
          <li>
            <a href="{{ url_for('settings_logs') }}" class="nav-link {% if active=='logs' %}active{% endif %}">
              <i class="bi bi-journal-text me-2"></i> Logs
            </a>
          </li>
        </ul>
        <div class="sidebar-foot">
          <div class="user-email">{{ session.get('user_email', 'Admin') }}</div>
          <a href="{{ url_for('logout') }}" class="btn btn-outline-light btn-sm w-100">
            <i class="bi bi-box-arrow-right me-1"></i> Logout
          </a>
        </div>
      </nav>
"""

ADMIN_SHELL = (
    """<!doctype html>
<html lang="en">
  <head>
"""
    + ADMIN_HEAD
    + """
  </head>
  <body>
    <div class="mobile-topbar px-3 py-2 d-md-none">
      <button class="btn btn-outline-light btn-sm" type="button" onclick="toggleSidebar()" aria-label="Menu">
        <i class="bi bi-list" id="navToggleIcon"></i>
      </button>
      <span class="fw-semibold">Dr G</span>
    </div>
    <div class="d-flex">
"""
    + ADMIN_SIDEBAR
    + """
      <main class="flex-grow-1 p-4">
        {% with messages = get_flashed_messages(with_categories=true) %}
          {% if messages %}
            {% for category, message in messages %}
              <div class="alert alert-{{ category }} alert-dismissible fade show" role="alert">
                {{ message }}
                <button type="button" class="btn-close btn-close-white" data-bs-dismiss="alert"></button>
              </div>
            {% endfor %}
          {% endif %}
        {% endwith %}
        {{ body|safe }}
      </main>
    </div>
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
    <script>
      function toggleSidebar() {
        var sidebar = document.querySelector('.sidebar');
        var icon = document.getElementById('navToggleIcon');
        if (sidebar) {
          var isOpen = sidebar.classList.toggle('sidebar-open');
          if (icon) {
            icon.classList.toggle('bi-list', !isOpen);
            icon.classList.toggle('bi-x-lg', isOpen);
          }
        }
      }
    </script>
  </body>
</html>
"""
)


def render_admin(page_title: str, active: str, body: str, **ctx):
    """Render a page inside the shared admin shell."""
    return render_template_string(
        ADMIN_SHELL,
        page_title=page_title,
        active=active,
        body=body,
        **ctx,
    )


OVERVIEW_BODY = """
        {% if not has_twitter %}
        <div class="alert alert-warning d-flex flex-wrap justify-content-between align-items-center gap-3 mb-4">
          <div>
            <strong>Connect X</strong><br>
            <span class="small">Add API keys so the bot can search and post.</span>
          </div>
          <a href="{{ url_for('settings_credentials') }}" class="btn btn-sm btn-primary">Open credentials</a>
        </div>
        {% endif %}
        <div class="page-header">
          <h2>Overview</h2>
          <p>Live activity, connection health, and pending MITL drafts.</p>
        </div>
        {% if twitter_status or gemini_status is not none %}
        <div class="status-bar mb-4">
          {% if twitter_status %}
          <span class="status-pill">
            <span class="status-dot {% if twitter_status == 'ok' %}ok{% elif twitter_status == 'error' %}err{% endif %}"></span>
            X · {{ 'Connected' if twitter_status == 'ok' else twitter_message or 'Error' if twitter_status == 'error' else 'Not configured' }}
          </span>
          {% endif %}
          {% if gemini_status is not none %}
          <span class="status-pill">
            <span class="status-dot {% if gemini_status == 'ok' %}ok{% elif gemini_status == 'configured' %}warn{% endif %}"></span>
            AI · {{ 'On' if gemini_status == 'ok' else 'Configured (off)' if gemini_status == 'configured' else 'Off' }}
          </span>
          {% endif %}
        </div>
        {% endif %}
        <div class="row g-3 mb-4">
          <div class="col-6 col-md-4 col-xl-2">
            <div class="card stat-card h-100"><div class="card-body">
              <h6 class="card-title">Replies</h6>
              <p class="display-6 mb-0">{{ total_replied }}</p>
            </div></div>
          </div>
          <div class="col-6 col-md-4 col-xl-2">
            <div class="card stat-card h-100"><div class="card-body">
              <h6 class="card-title">Tweets</h6>
              <p class="display-6 mb-0">{{ total_tweets_posted }}</p>
            </div></div>
          </div>
          <div class="col-6 col-md-4 col-xl-2">
            <div class="card stat-card h-100"><div class="card-body">
              <h6 class="card-title">Quotes</h6>
              <p class="display-6 mb-0">{{ total_quote_retweets }}</p>
            </div></div>
          </div>
          <div class="col-6 col-md-4 col-xl-2">
            <div class="card stat-card h-100"><div class="card-body">
              <h6 class="card-title">Drafts</h6>
              <p class="display-6 mb-0">{{ pending_drafts }}</p>
              <a href="{{ url_for('records_drafts') }}" class="small">Review</a>
            </div></div>
          </div>
          <div class="col-6 col-md-4 col-xl-2">
            <div class="card stat-card h-100"><div class="card-body">
              <h6 class="card-title">Blocked</h6>
              <p class="display-6 mb-0">{{ blocked_today }}</p>
              <a href="{{ url_for('settings_safety') }}" class="small">Safety</a>
            </div></div>
          </div>
          <div class="col-6 col-md-4 col-xl-2">
            <div class="card stat-card h-100"><div class="card-body">
              <h6 class="card-title">Last reply</h6>
              <p class="h5 mb-0 mt-2" style="font-size:0.95rem;">{{ last_reply or '—' }}</p>
            </div></div>
          </div>
        </div>

        <div class="d-flex justify-content-between align-items-end mb-3">
          <h4 class="mb-0">Recent replies</h4>
          <a href="{{ url_for('records_replies') }}" class="small">View all</a>
        </div>
        <div class="card"><div class="card-body p-0">
            {% if recent_replies %}
              <div class="table-responsive">
                <table class="table table-sm align-middle mb-0">
                  <thead>
                    <tr>
                      <th>Tweet</th>
                      <th>Reply</th>
                      <th>Source</th>
                      <th>Keyword</th>
                      <th>When</th>
                    </tr>
                  </thead>
                  <tbody>
                    {% for row in recent_replies %}
                    <tr>
                      <td class="font-monospace small">{{ row['tweet_id'] }}</td>
                      <td class="font-monospace small">{{ row['reply_tweet_id'] }}</td>
                      <td><span class="badge bg-secondary">{{ row['source'] }}</span></td>
                      <td>{{ row['keyword'] or '—' }}</td>
                      <td class="small text-muted">{{ row['replied_at'] }}</td>
                    </tr>
                    {% endfor %}
                  </tbody>
                </table>
              </div>
            {% else %}
              <p class="mb-0 text-muted p-4">No replies recorded yet.</p>
            {% endif %}
        </div></div>
"""

HOME_TEMPLATE = OVERVIEW_BODY

LANDING_PAGE_TEMPLATE = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Dr G — X engagement control</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.css" rel="stylesheet">
    <link href="{{ url_for('static', filename='style.css') }}" rel="stylesheet">
  </head>
  <body>
    <nav class="navbar navbar-expand-lg navbar-dark landing-nav">
      <div class="container">
        <a class="navbar-brand d-flex align-items-center gap-2" href="/">
          <span class="brand-mark" style="width:32px;height:32px;border-radius:9px;display:grid;place-items:center;font-size:0.75rem;font-weight:700;color:#041512;background:linear-gradient(145deg,#3ecfbe,#2a9d8f);">DG</span>
          <span class="fw-semibold">Dr G</span>
        </a>
        <a href="{{ url_for('login') }}" class="btn btn-primary btn-sm">Sign in</a>
      </div>
    </nav>

    <section class="landing-hero">
      <div class="container">
        <p class="eyebrow">X · Telegram · AI</p>
        <h1>Operate engagement with judgment.</h1>
        <p class="lede">
          Drafts, agent tools, and man-in-the-loop approvals — so nothing ships to X
          without your review when it matters.
        </p>
        <div class="cta-row">
          <a href="{{ url_for('login') }}" class="btn btn-primary btn-lg">Open dashboard</a>
        </div>
      </div>
    </section>

    <section class="landing-panel" id="features">
      <div class="container">
        <h2>What you control</h2>
        <p class="text-muted mb-0">One place for automation, drafts, and Telegram-driven agent actions.</p>
        <div class="feature-row">
          <div class="feature-item">
            <h3>Man-in-the-loop</h3>
            <p>Approve, edit, or reject before anything posts — from Telegram or Drafts.</p>
          </div>
          <div class="feature-item">
            <h3>Agent + tools</h3>
            <p>Chat naturally; the agent searches and posts via X tools under safety policy.</p>
          </div>
          <div class="feature-item">
            <h3>Safety budgets</h3>
            <p>Daily and monthly caps, quality gates, and an audit trail of blocked events.</p>
          </div>
        </div>
      </div>
    </section>

    <footer class="landing-footer">
      <div class="container d-flex flex-wrap justify-content-between gap-2">
        <span>Dr G · control center</span>
        <span>A project by <a href="https://x.com/ohakwengr" target="_blank" rel="noopener noreferrer">Abdul IB</a></span>
      </div>
    </footer>
  </body>
</html>
"""

LOGIN_TEMPLATE = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Sign in · Dr G</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.css" rel="stylesheet">
    <link href="{{ url_for('static', filename='style.css') }}" rel="stylesheet">
  </head>
  <body class="page-login">
    <div class="login-card">
      <div class="login-brand">
        <div class="brand-mark">DG</div>
        <h1 class="login-title">Dr G</h1>
        <p class="login-sub">Admin sign-in</p>
      </div>

      {% with messages = get_flashed_messages(with_categories=true) %}
        {% if messages %}
          {% for category, message in messages %}
            <div class="alert alert-{{ category }} alert-dismissible fade show" role="alert">
              {{ message }}
              <button type="button" class="btn-close btn-close-white" data-bs-dismiss="alert"></button>
            </div>
          {% endfor %}
        {% endif %}
      {% endwith %}

      <form method="post">
        <div class="mb-3">
          <label class="form-label">Email</label>
          <input type="email" name="email" class="form-control" required autofocus placeholder="you@example.com" autocomplete="username">
        </div>
        <div class="mb-4">
          <label class="form-label">Password</label>
          <input type="password" name="password" class="form-control" required placeholder="••••••••" autocomplete="current-password">
        </div>
        <button type="submit" class="btn btn-primary w-100 mb-3">
          Continue
        </button>
        <div class="text-center">
          <a href="{{ url_for('landing') }}" class="text-muted small text-decoration-none">
            ← Back
          </a>
        </div>
      </form>
    </div>
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
  </body>
</html>
"""

AUTOMATION_BODY = """
<div class="page-narrow">
<h2 class="mb-4">Automation Settings</h2>

          {% if config_error %}
            <div class="alert alert-danger">{{ config_error }}</div>
          {% endif %}

          <!-- Bot Status Card -->
          <div class="card shadow-sm mb-4">
            <div class="card-body">
              <h5 class="card-title mb-3">
                <i class="bi bi-robot me-2"></i>Bot Status
              </h5>
              <div class="row align-items-center">
                <div class="col-md-6">
                  <div class="d-flex align-items-center mb-2">
                    <span class="badge bg-{{ 'success' if bot_running else 'secondary' }} me-2" style="width: 12px; height: 12px; border-radius: 50%;"></span>
                    <strong>Status:</strong>
                    <span class="ms-2">{{ 'Running' if bot_running else 'Stopped' }}</span>
                  </div>
                  {% if bot_running %}
                  <div class="small text-muted">
                    <div>Last run: {{ last_run or 'N/A' }}</div>
                    <div>Next run: {{ next_run or 'Calculating...' }}</div>
                  </div>
                  {% endif %}
                </div>
                <div class="col-md-6 text-end">
                  {% if bot_running %}
                  <form method="post" style="display: inline;">
                    <input type="hidden" name="action" value="stop">
                    <button type="submit" class="btn btn-danger" onclick="return confirm('Are you sure you want to stop the bot?')">
                      <i class="bi bi-stop-circle me-1"></i>Stop Bot
                    </button>
                  </form>
                  {% else %}
                  <form method="post" style="display: inline;">
                    <input type="hidden" name="action" value="start">
                    <button type="submit" class="btn btn-success">
                      <i class="bi bi-play-circle me-1"></i>Start Bot
                    </button>
                  </form>
                  <form method="post" style="display: inline;" class="ms-2">
                    <input type="hidden" name="action" value="run_once">
                    <button type="submit" class="btn btn-outline-primary">
                      <i class="bi bi-arrow-right-circle me-1"></i>Run Once
                    </button>
                  </form>
                  <form method="post" style="display: inline;" class="ms-2">
                    <input type="hidden" name="action" value="run_once_preview">
                    <button type="submit" class="btn btn-outline-info">
                      <i class="bi bi-eye me-1"></i>Run Once (Preview)
                    </button>
                  </form>
                  {% endif %}
                </div>
              </div>
            </div>
          </div>

          {% if preview %}
          <div class="card shadow-sm mb-4 border-info">
            <div class="card-body">
              <h5 class="card-title mb-3">
                <i class="bi bi-eye me-2"></i>Last preview (no tweets were posted)
              </h5>
              <p class="small text-muted mb-2">
                Tweets fetched: {{ preview.stats.get('tweets_fetched', 0) }} &middot;
                After filter: {{ preview.stats.get('tweets_filtered', 0) }} &middot;
                Would post: {{ preview.stats.get('replies_posted', 0) }} replies
              </p>
              {% if preview.preview %}
              <div class="table-responsive">
                <table class="table table-sm table-hover mb-0">
                  <thead>
                    <tr>
                      <th>Type</th>
                      <th>Tweet ID</th>
                      <th>Source</th>
                      <th>Keyword</th>
                      <th>Text (snippet)</th>
                    </tr>
                  </thead>
                  <tbody>
                    {% for item in preview.preview %}
                    <tr>
                      <td><span class="badge bg-{{ 'primary' if item.type == 'reply' else 'secondary' }}">{{ item.type }}</span></td>
                      <td class="font-monospace small">{{ item.tweet_id }}</td>
                      <td>{{ item.source or '-' }}</td>
                      <td>{{ item.keyword or '-' }}</td>
                      <td class="small text-break" style="max-width: 280px;">{{ (item.text or '')[:120] }}{% if (item.text or '')|length > 120 %}...{% endif %}</td>
                    </tr>
                    {% endfor %}
                  </tbody>
                </table>
              </div>
              {% else %}
              <p class="mb-0 text-muted">No replies would have been posted (e.g. no candidates or all filtered out).</p>
              {% endif %}
            </div>
          </div>
          {% endif %}

          <!-- Schedule Settings -->
          <form method="post" class="row g-3">
            <input type="hidden" name="action" value="save_schedule">
            
            <div class="col-12">
              <h5>Schedule Settings</h5>
              <p class="text-muted small">The bot will run continuously during these time ranges. Replies are posted every 30+ minutes, and content (tweets/threads) every 15+ minutes during the ranges.</p>
            </div>

            <div class="col-md-6">
              <label class="form-label">Morning Start Time</label>
              <input type="time" name="morning_start" class="form-control" value="{{ schedule.get('morning_start', schedule.get('morning_time', '09:00')) }}" required>
            </div>

            <div class="col-md-6">
              <label class="form-label">Morning End Time</label>
              <input type="time" name="morning_end" class="form-control" value="{{ schedule.get('morning_end', '11:00') }}" required>
            </div>

            <div class="col-md-6">
              <label class="form-label">Evening Start Time</label>
              <input type="time" name="evening_start" class="form-control" value="{{ schedule.get('evening_start', schedule.get('evening_time', '18:00')) }}" required>
            </div>

            <div class="col-md-6">
              <label class="form-label">Evening End Time</label>
              <input type="time" name="evening_end" class="form-control" value="{{ schedule.get('evening_end', '20:00') }}" required>
            </div>

            <div class="col-md-6">
              <label class="form-label">Timezone</label>
              <input type="text" name="timezone" class="form-control" value="{{ schedule.get('timezone', 'UTC') }}" placeholder="UTC">
              <small class="text-muted">Use timezone names like 'UTC', 'America/New_York', etc.</small>
            </div>

            <div class="col-12 mt-4">
              <h5>Run Settings</h5>
            </div>

            <div class="col-md-6">
              <label class="form-label">Min Replies Per Run</label>
              <input type="number" name="min_replies_per_run" class="form-control" value="{{ reply_settings.get('min_replies_per_run', 20) }}" min="1" max="50" required>
              <small class="text-muted">Minimum replies per run (20-50 recommended)</small>
            </div>

            <div class="col-md-6">
              <label class="form-label">Max Replies Per Run</label>
              <input type="number" name="max_replies_per_run" class="form-control" value="{{ reply_settings.get('max_replies_per_run', 50) }}" min="1" max="50" required>
              <small class="text-muted">Maximum replies per run (20-50 recommended)</small>
            </div>

            <div class="col-md-6">
              <label class="form-label">Delay Between Replies (minutes)</label>
              <div class="input-group">
                <input type="number" name="delay_minutes_min" class="form-control" value="{{ reply_settings.get('delay_minutes_min', 5) }}" min="1" max="60" required>
                <span class="input-group-text">to</span>
                <input type="number" name="delay_minutes_max" class="form-control" value="{{ reply_settings.get('delay_minutes_max', 15) }}" min="1" max="60" required>
              </div>
              <small class="text-muted">Time between each reply (5-15 minutes recommended for 20-50 replies)</small>
            </div>

            <div class="col-12 mt-4">
              <h5>Content Posting Settings</h5>
              <p class="text-muted small">The bot will also post original tweets and threads during time ranges.</p>
            </div>

            <div class="col-md-4">
              <label class="form-label">Tweets Per Run</label>
              <input type="number" name="tweets_per_run" class="form-control" value="{{ tweet_settings.get('tweets_per_run', 1) }}" min="0" max="10" required>
              <small class="text-muted">How many original tweets to post per run</small>
            </div>

            <div class="col-md-4">
              <label class="form-label">Threads Per Run</label>
              <input type="number" name="threads_per_run" class="form-control" value="{{ tweet_settings.get('threads_per_run', 0) }}" min="0" max="5" required>
              <small class="text-muted">How many threads to post per run</small>
            </div>

            <div class="col-md-4">
              <label class="form-label">Tweets Per Thread</label>
              <input type="number" name="thread_tweet_count" class="form-control" value="{{ tweet_settings.get('thread_tweet_count', 3) }}" min="2" max="10" required>
              <small class="text-muted">Number of tweets in each thread</small>
            </div>

            <div class="col-12 mt-4">
              <h5>Man-in-the-loop</h5>
              <div class="form-check">
                <input class="form-check-input" type="checkbox" name="mitl_enabled" id="mitl_enabled" {% if mitl_enabled %}checked{% endif %}>
                <label class="form-check-label" for="mitl_enabled">
                  Require Telegram/dashboard approval before posting (recommended)
                </label>
              </div>
            </div>

            <div class="col-12 mt-4">
              <button type="submit" class="btn btn-primary">
                <i class="bi bi-save me-1"></i>Save Settings
              </button>
            </div>
          </form>

          <!-- Info Card -->
          <div class="card shadow-sm mt-4" style="background: rgba(15, 23, 42, 0.5);">
            <div class="card-body">
              <h6 class="card-title">
                <i class="bi bi-info-circle me-2"></i>How It Works
              </h6>
              <ul class="mb-0 small">
                <li>The bot runs continuously during morning and evening time ranges</li>
                <li>Replies are posted every 30+ minutes during time ranges (20-50 replies total)</li>
                <li>Original tweets and threads are posted every 15+ minutes during time ranges</li>
                <li>Replies are spaced out with random delays (5-15 minutes) to appear natural</li>
                <li>The bot automatically finds tweets matching your keywords</li>
                <li>All replies are tracked to prevent duplicates</li>
                <li>Content is posted automatically based on your settings</li>
              </ul>
            </div>
          </div>
        </div>

<script>
  {% if bot_running %}
  setInterval(function() { location.reload(); }, 30000);
  {% endif %}
</script>
"""

KEYWORDS_BODY = """
<div class="page-narrow-wide">
<h2 class="mb-4">Keywords & Filters</h2>

          {% if config_error %}
            <div class="alert alert-danger">{{ config_error }}</div>
          {% endif %}

          <form method="post" class="row g-3">
            <div class="col-12">
              <div class="d-flex justify-content-between align-items-center">
                <div>
                  <h5 class="mb-0">Keywords to watch</h5>
                  <p class="text-muted small mb-0">
                    One keyword or phrase per line. The bot will search X (Twitter) for tweets matching these.
                  </p>
                </div>
                <button
                  type="submit"
                  name="generate_profile_keywords"
                  value="1"
                  class="btn btn-outline-light btn-sm ms-3"
                >
                  <i class="bi bi-magic me-1"></i>
                  Generate from my account
                </button>
              </div>
              <textarea name="keywords" class="form-control mt-3" rows="6">{{ keywords }}</textarea>
            </div>

            <div class="col-md-6 mt-4">
              <h5>Reply limits</h5>
              <div class="mb-3">
                <label class="form-label">Max replies per run</label>
                <input
                  type="number"
                  name="max_replies_per_run"
                  class="form-control"
                  value="{{ reply_settings.get('max_replies_per_run', 10) }}"
                >
              </div>
              <div class="row">
                <div class="col-6">
                  <label class="form-label">Min delay (minutes)</label>
                  <input
                    type="number"
                    name="delay_minutes_min"
                    class="form-control"
                    value="{{ reply_settings.get('delay_minutes_min', 30) }}"
                  >
                </div>
                <div class="col-6">
                  <label class="form-label">Max delay (minutes)</label>
                  <input
                    type="number"
                    name="delay_minutes_max"
                    class="form-control"
                    value="{{ reply_settings.get('delay_minutes_max', 40) }}"
                  >
                </div>
              </div>
            </div>

            <div class="col-md-6 mt-4">
              <h5>Filters</h5>
              <div class="form-check">
                <input
                  class="form-check-input"
                  type="checkbox"
                  name="exclude_retweets"
                  id="exclude_retweets"
                  {% if filters.get('exclude_retweets') %}checked{% endif %}
                >
                <label class="form-check-label" for="exclude_retweets">
                  Skip retweets
                </label>
              </div>
              <div class="form-check">
                <input
                  class="form-check-input"
                  type="checkbox"
                  name="exclude_own_tweets"
                  id="exclude_own_tweets"
                  {% if filters.get('exclude_own_tweets') %}checked{% endif %}
                >
                <label class="form-check-label" for="exclude_own_tweets">
                  Skip my own tweets
                </label>
              </div>
              <div class="form-check">
                <input
                  class="form-check-input"
                  type="checkbox"
                  name="exclude_replied_tweets"
                  id="exclude_replied_tweets"
                  {% if filters.get('exclude_replied_tweets') %}checked{% endif %}
                >
                <label class="form-check-label" for="exclude_replied_tweets">
                  Skip tweets that already have my reply
                </label>
              </div>
              <div class="mt-3">
                <label class="form-label">Minimum followers</label>
                <input
                  type="number"
                  name="min_followers"
                  class="form-control"
                  value="{{ filters.get('min_followers', 0) }}"
                >
              </div>
            </div>

            <div class="col-12 mt-4">
              <button type="submit" class="btn btn-primary">
                <i class="bi bi-save me-1"></i> Save Settings
              </button>
            </div>
          </form>
        </div>
"""

LOGS_BODY = """
<h2 class="mb-4">Bot Logs</h2>
        {% if log_error %}
        <div class="alert alert-warning">{{ log_error }}</div>
        {% else %}
        <p class="text-muted small mb-2">Last {{ line_count }} lines from bot.log (newest at bottom)</p>
        <div class="card shadow-sm">
          <div class="card-body p-0">
            <pre class="bg-dark text-light p-3 mb-0 small" style="max-height: 70vh; overflow-y: auto; white-space: pre-wrap; word-break: break-all;">{{ log_content }}</pre>
          </div>
        </div>
        <div class="mt-2">
          <a href="{{ url_for('settings_logs') }}" class="btn btn-outline-secondary btn-sm"><i class="bi bi-arrow-clockwise me-1"></i>Refresh</a>
        </div>
        {% endif %}
"""

CREDENTIALS_BODY = """
<div class="page-narrow">
<h2 class="mb-4">Connect Twitter & Gemini</h2>

          {% if config_error %}
            <div class="alert alert-danger">{{ config_error }}</div>
          {% endif %}

          {% if is_connected and connected_user %}
          <div class="alert alert-success d-flex justify-content-between align-items-center mb-4">
            <div>
              <strong>Connected as @{{ connected_user.get('username') }}</strong><br>
              <span class="small">Followers: {{ connected_user.get('followers_count', 'N/A') }}</span>
            </div>
            <form method="post" style="display: inline;">
              <input type="hidden" name="disconnect" value="1">
              <button type="submit" class="btn btn-outline-danger btn-sm" onclick="return confirm('Are you sure you want to disconnect your Twitter account?')">
                <i class="bi bi-x-circle me-1"></i>Disconnect
              </button>
            </form>
          </div>
          {% endif %}

          <form method="post" class="row g-3">
            <div class="col-12">
              <h5>Twitter / X API (OAuth 1.0a)</h5>
              <p class="small text-muted mb-0">
                Use <strong>Keys and tokens</strong>: API Key (= Consumer Key), API Key Secret (= Consumer Secret),
                Access Token, Access Token Secret. Optional Bearer Token for app-only reads.
                Do <strong>not</strong> use OAuth 2.0 Client ID / Client Secret here.
              </p>
            </div>
            <div class="col-md-6">
              <label class="form-label">API Key / Consumer Key</label>
              <input type="password" name="consumer_key" class="form-control" placeholder="{{ '••••••••' if x_api.get('consumer_key') else '' }}" autocomplete="off">
              <small class="text-muted">Leave blank to keep existing</small>
            </div>
            <div class="col-md-6">
              <label class="form-label">API Key Secret / Consumer Secret</label>
              <input type="password" name="consumer_secret" class="form-control" placeholder="{{ '••••••••' if x_api.get('consumer_secret') else '' }}" autocomplete="off">
            </div>
            <div class="col-md-6">
              <label class="form-label">Access Token (user context)</label>
              <input type="password" name="access_token" class="form-control" placeholder="{{ '••••••••' if x_api.get('access_token') else '' }}" autocomplete="off">
            </div>
            <div class="col-md-6">
              <label class="form-label">Access Token Secret</label>
              <input type="password" name="access_token_secret" class="form-control" placeholder="{{ '••••••••' if x_api.get('access_token_secret') else '' }}" autocomplete="off">
            </div>
            <div class="col-md-6">
              <label class="form-label">Bearer Token (optional, app-only)</label>
              <input type="password" name="bearer_token" class="form-control" placeholder="{{ '••••••••' if x_api.get('bearer_token') else '' }}" autocomplete="off">
            </div>

            <div class="col-12 mt-4">
              <h5>AI Providers</h5>
              <p class="text-muted small mb-2">Leave API key blank to keep the existing key. Manage niches/system prompts under AI Settings.</p>
            </div>
            <div class="col-md-6">
              <label class="form-label">Gemini API Key</label>
              <input type="password" name="gemini_api_key" class="form-control" placeholder="{{ '••••••••' if gemini.get('api_key') else '' }}" autocomplete="off">
            </div>
            <div class="col-md-3 d-flex align-items-end">
              <div class="form-check">
                <input class="form-check-input" type="checkbox" name="gemini_enabled" id="gemini_enabled" {% if gemini.get('enabled') %}checked{% endif %}>
                <label class="form-check-label" for="gemini_enabled">Enable Gemini</label>
              </div>
            </div>
            <div class="col-md-6">
              <label class="form-label">OpenAI API Key</label>
              <input type="password" name="openai_api_key" class="form-control" placeholder="{{ '••••••••' if openai.get('api_key') else '' }}" autocomplete="off">
            </div>
            <div class="col-md-3 d-flex align-items-end">
              <div class="form-check">
                <input class="form-check-input" type="checkbox" name="openai_enabled" id="openai_enabled" {% if openai.get('enabled') %}checked{% endif %}>
                <label class="form-check-label" for="openai_enabled">Enable OpenAI</label>
              </div>
            </div>
            <div class="col-md-6">
              <label class="form-label">Anthropic API Key</label>
              <input type="password" name="anthropic_api_key" class="form-control" placeholder="{{ '••••••••' if anthropic.get('api_key') else '' }}" autocomplete="off">
            </div>
            <div class="col-md-3 d-flex align-items-end">
              <div class="form-check">
                <input class="form-check-input" type="checkbox" name="anthropic_enabled" id="anthropic_enabled" {% if anthropic.get('enabled') %}checked{% endif %}>
                <label class="form-check-label" for="anthropic_enabled">Enable Anthropic</label>
              </div>
            </div>
            <div class="col-12 mt-2">
              <h6 class="mb-1">AgentRouter <span class="text-muted small">(agentrouter.org)</span></h6>
              <p class="text-muted small mb-2">
                OpenAI-compatible gateway. Get a key at
                <a href="https://agentrouter.org/console/token" target="_blank" rel="noopener">agentrouter.org/console/token</a>.
                Base URL defaults to <code>https://agentrouter.org/v1</code>.
              </p>
            </div>
            <div class="col-md-6">
              <label class="form-label">AgentRouter API Key</label>
              <input type="password" name="agentrouter_api_key" class="form-control" placeholder="{{ '••••••••' if agentrouter.get('api_key') else '' }}" autocomplete="off">
            </div>
            <div class="col-md-3 d-flex align-items-end">
              <div class="form-check">
                <input class="form-check-input" type="checkbox" name="agentrouter_enabled" id="agentrouter_enabled" {% if agentrouter.get('enabled') %}checked{% endif %}>
                <label class="form-check-label" for="agentrouter_enabled">Enable AgentRouter</label>
              </div>
            </div>
            <div class="col-md-6">
              <label class="form-label">AgentRouter base URL</label>
              <input type="text" name="agentrouter_base_url" class="form-control" value="{{ agentrouter.get('base_url') or 'https://agentrouter.org/v1' }}" placeholder="https://agentrouter.org/v1">
            </div>
            <div class="col-md-4">
              <label class="form-label">Default AI Provider</label>
              <select name="ai_provider" class="form-select">
                <option value="gemini" {% if ai.get('provider') == 'gemini' %}selected{% endif %}>Gemini</option>
                <option value="openai" {% if ai.get('provider') == 'openai' %}selected{% endif %}>OpenAI</option>
                <option value="anthropic" {% if ai.get('provider') == 'anthropic' %}selected{% endif %}>Anthropic</option>
                <option value="agentrouter" {% if ai.get('provider') == 'agentrouter' %}selected{% endif %}>AgentRouter</option>
              </select>
            </div>

            <div class="col-12 mt-4">
              <button type="submit" class="btn btn-primary">
                <i class="bi bi-link-45deg me-1"></i> Save & Test Connection
              </button>
            </div>
          </form>
        </div>
"""

# Start Telegram MITL poller (no-op if token/chat not set)
try:
    get_telegram_approver().start_polling()
except Exception as _tg_err:
    logger.warning(f"Telegram approver not started: {_tg_err}")


@app.route("/")
def landing():
    """Landing page - public homepage."""
    return render_template_string(LANDING_PAGE_TEMPLATE)


@app.route("/login", methods=["GET", "POST"])
def login():
    """Admin login with email + password from env."""
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not email or not password:
            flash("Email and password are required.", "danger")
            return render_template_string(LOGIN_TEMPLATE)

        if not ADMIN_EMAIL or not ADMIN_PASSWORD:
            flash(
                "Admin login is not configured. Set ADMIN_EMAIL and ADMIN_PASSWORD.",
                "danger",
            )
            return render_template_string(LOGIN_TEMPLATE)

        if not verify_admin(email, password):
            flash("Invalid email or password.", "danger")
            return render_template_string(LOGIN_TEMPLATE)

        session["user_id"] = 1
        session["user_email"] = email
        flash("Login successful!", "success")
        return redirect(url_for("dashboard_overview"))

    return render_template_string(LOGIN_TEMPLATE)




@app.route("/logout")
def logout():
    """Logout and clear session."""
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for('landing'))


@app.route("/api/run-once", methods=["GET", "POST"])
def api_run_once():
    """Trigger a one-off bot run (for cron). Requires CRON_SECRET in query or header X-Cron-Secret."""
    secret = request.args.get("secret") or request.headers.get("X-Cron-Secret") or ""
    expected = os.getenv("CRON_SECRET", "")
    if not expected or secret != expected:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    with bot_status_lock:
        if bot_status["running"]:
            return jsonify({"ok": True, "message": "Bot already running; skip this run."}), 200
        try:
            bot_status["running"] = True
            thread = threading.Thread(target=_run_bot_in_thread, daemon=True)
            thread.start()
            logger.info("Started bot run via /api/run-once (cron)")
            return jsonify({"ok": True, "message": "Bot run started."}), 200
        except Exception as e:
            logger.error(f"Error starting run via API: {e}", exc_info=True)
            bot_status["running"] = False
            return jsonify({"ok": False, "error": str(e)}), 500


def _read_log_tail(path: Path, max_lines: int = 500) -> Tuple[str, int, Optional[str]]:
    """Read last max_lines from a text file. Returns (content, line_count, error_message)."""
    try:
        if not path.exists():
            return "", 0, f"Log file not found: {path}"
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        if not lines:
            return "", 0, None
        tail = lines[-max_lines:]
        return "".join(tail), len(tail), None
    except Exception as e:
        return "", 0, str(e)


@app.route("/settings/logs")
@login_required
def settings_logs():
    """View last lines of bot.log."""
    log_path = Path("bot.log")
    log_content, line_count, log_error = _read_log_tail(log_path, max_lines=500)
    body = render_template_string(
        LOGS_BODY,
        log_content=log_content,
        line_count=line_count,
        log_error=log_error,
    )
    return render_admin("Logs", "logs", body)



@app.route("/dashboard")
@login_required
def dashboard_overview():
    """Overview page: basic stats from database and setup status."""
    total_replied = 0
    total_tweets_posted = 0
    total_quote_retweets = 0
    pending_drafts = 0
    blocked_today = 0
    last_reply = None
    recent_replies = []
    has_twitter = False
    twitter_status = None  # 'ok' | 'error' | None (not configured)
    twitter_message = None
    gemini_status = None  # 'ok' | 'configured' | None

    # Check Twitter and Gemini connection status
    try:
        config = Config()
        x_api = config.config.get("x_api", {})
        if any(x_api.get(k) for k in ["consumer_key", "consumer_secret", "access_token", "bearer_token"]):
            has_twitter = True
            try:
                xapi = XAPI(config.get_x_api_credentials())
                user = xapi.get_user_info()
                twitter_status = "ok" if user else "error"
                twitter_message = None if user else "Could not verify account"
            except Exception as e:
                twitter_status = "error"
                twitter_message = str(e)[:80]
        gemini = config.config.get("gemini", {})
        if gemini.get("api_key"):
            gemini_status = "ok" if gemini.get("enabled") else "configured"
        else:
            gemini_status = None
    except Exception:
        pass

    # Ensure database exists and has tables
    try:
        # Initialize database to create tables if they don't exist
        try:
            from src.database import Database
            db = Database()
            db.close()
        except Exception as db_init_error:
            logger.error(f"Failed to initialize database: {db_init_error}")
            # Continue with defaults - database might not be set up yet
            return render_admin(
            "Overview",
            "overview",
            render_template_string(OVERVIEW_BODY, total_replied=0,
                total_tweets_posted=0,
                total_quote_retweets=0,
                pending_drafts=0,
                blocked_today=0,
                last_reply=None,
                recent_replies=[],
                has_twitter=has_twitter,
                twitter_status=twitter_status,
                twitter_message=twitter_message,
                gemini_status=gemini_status,),
        )
        
        # Now query the database
        try:
            conn = get_db_connection()
            cur = conn.conn.cursor()
        except Exception as conn_error:
            logger.error(f"Failed to get database connection: {conn_error}")
            # Return with defaults if connection fails
            return render_admin(
            "Overview",
            "overview",
            render_template_string(OVERVIEW_BODY, total_replied=0,
                total_tweets_posted=0,
                total_quote_retweets=0,
                pending_drafts=0,
                blocked_today=0,
                last_reply=None,
                recent_replies=[],
                has_twitter=has_twitter,
                twitter_status=twitter_status,
                twitter_message=twitter_message,
                gemini_status=gemini_status,),
        )
        
        # Tables are created by Database class, no need to create here
        
        # Get total replies count
        try:
            cur.execute("SELECT COUNT(*) as count FROM replied_tweets")
            result = cur.fetchone()
            total_replied = result['count'] if result else 0
        except Exception as e:
            logger.error(f"Error getting total replies: {e}")
            total_replied = 0
        
        # Get total tweets posted count
        try:
            cur.execute("SELECT COUNT(*) as count FROM posted_tweets")
            result = cur.fetchone()
            total_tweets_posted = result['count'] if result else 0
        except Exception as e:
            logger.error(f"Error getting total tweets posted: {e}")
            total_tweets_posted = 0
        
        # Get total quote retweets count
        try:
            cur.execute("SELECT COUNT(*) as count FROM quote_retweets")
            result = cur.fetchone()
            total_quote_retweets = result['count'] if result else 0
        except Exception as e:
            logger.error(f"Error getting total quote retweets: {e}")
            total_quote_retweets = 0

        try:
            cur.execute(
                "SELECT COUNT(*) as count FROM content_drafts WHERE status = 'pending'"
            )
            result = cur.fetchone()
            pending_drafts = result["count"] if result else 0
        except Exception as e:
            logger.error(f"Error getting pending drafts: {e}")
            pending_drafts = 0

        try:
            from src.quality_safety import count_safety_events_today

            blocked_today = count_safety_events_today(conn)
        except Exception as e:
            logger.error(f"Error getting safety events: {e}")
            blocked_today = 0

        # Get last reply timestamp
        try:
            cur.execute(
                "SELECT replied_at FROM replied_tweets ORDER BY replied_at DESC LIMIT 1"
            )
            row = cur.fetchone()
            if row and row.get('replied_at'):
                # Format the timestamp nicely
                from datetime import datetime
                try:
                    replied_at = row['replied_at']
                    if isinstance(replied_at, str):
                        dt = datetime.fromisoformat(replied_at.replace('Z', '+00:00'))
                    else:
                        dt = replied_at
                    last_reply = dt.strftime("%Y-%m-%d %H:%M:%S")
                except:
                    last_reply = str(replied_at)
            else:
                last_reply = None
        except Exception as e:
            logger.error(f"Error getting last reply: {e}")
            last_reply = None

        # Get recent replies
        try:
            cur.execute(
                "SELECT tweet_id, reply_tweet_id, source, keyword, replied_at "
                "FROM replied_tweets ORDER BY replied_at DESC LIMIT 10"
            )
            rows = cur.fetchall()
            # RealDictCursor already returns dict-like rows
            recent_replies = []
            for row in rows:
                from datetime import datetime
                replied_at = row.get('replied_at')
                if replied_at:
                    try:
                        if isinstance(replied_at, str):
                            dt = datetime.fromisoformat(replied_at.replace('Z', '+00:00'))
                        else:
                            dt = replied_at
                        replied_at_str = dt.strftime("%Y-%m-%d %H:%M:%S")
                    except:
                        replied_at_str = str(replied_at)
                else:
                    replied_at_str = None
                
                recent_replies.append({
                    'tweet_id': row.get('tweet_id'),
                    'reply_tweet_id': row.get('reply_tweet_id'),
                    'source': row.get('source') or 'unknown',
                    'keyword': row.get('keyword'),
                    'replied_at': replied_at_str
                })
        except Exception as e:
            logger.error(f"Error getting recent replies: {e}")
            import traceback
            traceback.print_exc()
            recent_replies = []
        
            cur.close()
            # Don't close the global database connection
        except Exception as query_error:
            logger.error(f"Database query error: {query_error}", exc_info=True)
            # Use defaults for this query
            total_replied = 0
            total_tweets_posted = 0
            total_quote_retweets = 0
            pending_drafts = 0
            blocked_today = 0
            last_reply = None
            recent_replies = []
    except Exception as e:
        # If database operations fail, log the error but use defaults
        logger.error(f"Database error in dashboard: {e}", exc_info=True)
        import traceback
        traceback.print_exc()
        # Use defaults to prevent page crash
        total_replied = 0
        total_tweets_posted = 0
        total_quote_retweets = 0
        last_reply = None
        recent_replies = []
        pending_drafts = 0
        blocked_today = 0

    return render_admin(
            "Overview",
            "overview",
            render_template_string(OVERVIEW_BODY, total_replied=total_replied,
        total_tweets_posted=total_tweets_posted,
        total_quote_retweets=total_quote_retweets,
        pending_drafts=pending_drafts,
        blocked_today=blocked_today,
        last_reply=last_reply,
        recent_replies=recent_replies,
        has_twitter=has_twitter,
        twitter_status=twitter_status,
        twitter_message=twitter_message,
        gemini_status=gemini_status,),
        )


@app.route("/settings/credentials", methods=["GET", "POST"])
@login_required
def settings_credentials():
    """Page to view/update Twitter and Gemini credentials and test connection."""
    config = None
    error = None

    try:
        config = Config()
    except Exception as e:
        error = str(e)

    if request.method == "POST":
        # Check if disconnect was clicked
        if request.form.get("disconnect"):
            if not config:
                flash("Config file missing. Please run setup.py first.", "danger")
                return redirect(url_for("settings_credentials"))
            
            data = config.config
            data.setdefault("x_api", {})
            # Clear all Twitter credentials
            data["x_api"]["consumer_key"] = ""
            data["x_api"]["consumer_secret"] = ""
            data["x_api"]["access_token"] = ""
            data["x_api"]["access_token_secret"] = ""
            data["x_api"]["bearer_token"] = ""
            
            Path(config.config_path).write_text(
                json.dumps(data, indent=2), encoding="utf-8"
            )
            
            flash("Twitter account disconnected successfully.", "success")
            return redirect(url_for("settings_credentials"))
        
        if not config:
            flash("Config file missing. Please run setup.py first.", "danger")
            return redirect(url_for("settings_credentials"))

        data = config.config
        data.setdefault("x_api", {})
        for field in (
            "consumer_key",
            "consumer_secret",
            "access_token",
            "access_token_secret",
            "bearer_token",
        ):
            val = request.form.get(field, "").strip()
            if val:
                data["x_api"][field] = val

        data.setdefault("ai", {})
        data["ai"]["provider"] = request.form.get("ai_provider", "gemini").strip() or "gemini"

        for provider, form_key in (
            ("gemini", "gemini_api_key"),
            ("openai", "openai_api_key"),
            ("anthropic", "anthropic_api_key"),
            ("agentrouter", "agentrouter_api_key"),
        ):
            data.setdefault(provider, {})
            key_val = request.form.get(form_key, "").strip()
            if key_val:
                data[provider]["api_key"] = key_val
            data[provider]["enabled"] = bool(request.form.get(f"{provider}_enabled"))

        data.setdefault("agentrouter", {})
        ar_base = request.form.get("agentrouter_base_url", "").strip()
        if ar_base:
            data["agentrouter"]["base_url"] = ar_base.rstrip("/")

        Path(config.config_path).write_text(
            json.dumps(data, indent=2), encoding="utf-8"
        )
        try:
            config.save_to_database(force=True)
        except Exception as e:
            logger.warning(f"Could not sync credentials to DB: {e}")

        try:
            # Reload for fresh credentials after file write
            config = Config()
            xapi = XAPI(config.get_x_api_credentials())
            user = xapi.get_user_info()
            if user:
                flash(
                    f"Connected as @{user.get('username')} "
                    f"(Followers: {user.get('followers_count')})",
                    "success",
                )
            else:
                flash(
                    "Could not verify Twitter account. "
                    "Check your credentials.",
                    "danger",
                )
        except Exception as e:
            flash(f"Twitter connection failed: {e}", "danger")

        return redirect(url_for("settings_credentials"))

    x_api = config.config.get("x_api", {}) if config else {}
    gemini = config.config.get("gemini", {}) if config else {}
    openai_cfg = config.config.get("openai", {}) if config else {}
    anthropic = config.config.get("anthropic", {}) if config else {}
    agentrouter = config.config.get("agentrouter", {}) if config else {}
    ai = config.config.get("ai", {}) if config else {}
    
    # Check if Twitter is connected
    is_connected = False
    connected_user = None
    if config:
        try:
            xapi = XAPI(config.get_x_api_credentials())
            connected_user = xapi.get_user_info()
            if connected_user:
                is_connected = True
        except Exception:
            is_connected = False

    body = render_template_string(
        CREDENTIALS_BODY,
        config_error=error,
        x_api=x_api,
        gemini=gemini,
        openai=openai_cfg,
        anthropic=anthropic,
        agentrouter=agentrouter,
        ai=ai,
        is_connected=is_connected,
        connected_user=connected_user,
    )
    return render_admin("Credentials", "credentials", body)



@app.route("/settings/keywords", methods=["GET", "POST"])
@login_required
def settings_keywords():
    """Page to edit keywords, reply settings and filters."""
    config = None
    error = None

    try:
        config = Config()
    except Exception as e:
        error = str(e)

    if request.method == "POST" and config:
        # If user clicked "Generate from my account", build suggested keywords
        if request.form.get("generate_profile_keywords"):
            suggested_keywords = []
            own_tweets = []
            liked_tweets = []
            timeline_tweets = []
            
            try:
                xapi = XAPI(config.get_x_api_credentials())
                
                # Fetch comprehensive Twitter activity to build profile
                flash("Analyzing your Twitter activity... This may take a moment.", "info")
                
                # Get own tweets (what you post) - with error handling
                try:
                    own_tweets = xapi.get_user_tweets(count=3200)
                    if own_tweets:
                        flash(f"Fetched {len(own_tweets)} of your tweets", "success")
                    else:
                        flash(f"Found 0 of your tweets. This might be due to API permissions or you may not have posted tweets yet.", "warning")
                except Exception as e:
                    flash(f"Warning: Could not fetch your tweets: {str(e)}", "warning")
                    import traceback
                    logger.error(f"Error fetching user tweets: {e}\n{traceback.format_exc()}")
                
                # Get liked tweets (what you engage with) - with error handling
                try:
                    liked_tweets = xapi.get_liked_tweets(count=3200)
                    if liked_tweets:
                        flash(f"Fetched {len(liked_tweets)} liked tweets", "info")
                except Exception as e:
                    flash(f"Warning: Could not fetch liked tweets: {e}", "warning")
                
                # Get home timeline (what shows in your feed - from accounts you follow) - with error handling
                try:
                    timeline_tweets = xapi.get_home_timeline(count=800)
                    if timeline_tweets:
                        flash(f"Fetched {len(timeline_tweets)} tweets from your timeline", "info")
                except Exception as e:
                    flash(f"Warning: Could not fetch timeline tweets: {e}", "warning")

                # Combine all tweet texts for analysis
                all_tweets = own_tweets + liked_tweets + timeline_tweets
                texts = [t.get("text", "") for t in all_tweets if t.get("text")]
                
                # Debug info
                logger.info(f"Total tweets collected: {len(all_tweets)} (own: {len(own_tweets)}, liked: {len(liked_tweets)}, timeline: {len(timeline_tweets)})")
                logger.info(f"Texts extracted: {len(texts)}")
                
                if not texts:
                    error_msg = (
                        f"No Twitter activity found. "
                        f"Fetched: {len(own_tweets)} of your tweets, {len(liked_tweets)} liked tweets, {len(timeline_tweets)} timeline tweets. "
                        f"Make sure you have posted tweets, liked content, or followed accounts on X (Twitter). "
                        f"If you have tweets but none were fetched, check your API permissions."
                    )
                    flash(error_msg, "warning")
                    suggested_keywords = []
                else:
                    suggested_keywords = _extract_profile_keywords(texts)
                    
                    if not suggested_keywords:
                        flash(
                            f"Analyzed {len(texts)} tweets from your activity but couldn't extract keywords. "
                            "This might happen if your content is very diverse. "
                            "You can manually add keywords below.",
                            "warning",
                        )
                    else:
                        flash(
                            f"Generated {len(suggested_keywords)} keywords from analyzing your Twitter activity: "
                            f"{len(own_tweets)} of your tweets, {len(liked_tweets)} liked tweets, "
                            f"and {len(timeline_tweets)} from your timeline. "
                            "Review them below, adjust, and click Save Settings.",
                            "success",
                        )

            except Exception as e:
                flash(f"Error connecting to Twitter: {e}. Please check your credentials.", "danger")

            # Keep existing reply settings and filters from config
            keywords = suggested_keywords if suggested_keywords else config.get_keywords()
            reply_settings = config.get_reply_settings()
            filters = config.get_filters()

            body = render_template_string(
                KEYWORDS_BODY,
                config_error=error,
                keywords="\n".join(keywords),
                reply_settings=reply_settings,
                filters=filters,
            )
            return render_admin("Keywords", "keywords", body)

        # Default path: save settings
        if config:
            data = config.config

            keywords_text = request.form.get("keywords", "")
            keywords = [k.strip() for k in keywords_text.splitlines() if k.strip()]
            data["keywords"] = keywords

            data.setdefault("reply_settings", {})
            rs = data["reply_settings"]
            rs["max_replies_per_run"] = int(
                request.form.get("max_replies_per_run", 10)
            )
            rs["delay_minutes_min"] = int(
                request.form.get("delay_minutes_min", 30)
            )
            rs["delay_minutes_max"] = int(
                request.form.get("delay_minutes_max", 40)
            )

            data.setdefault("filters", {})
            flt = data["filters"]
            flt["exclude_retweets"] = bool(
                request.form.get("exclude_retweets")
            )
            flt["exclude_own_tweets"] = bool(
                request.form.get("exclude_own_tweets")
            )
            flt["exclude_replied_tweets"] = bool(
                request.form.get("exclude_replied_tweets")
            )
            flt["min_followers"] = int(request.form.get("min_followers", 0))

            Path(config.config_path).write_text(
                json.dumps(data, indent=2), encoding="utf-8"
            )
            flash("Settings saved.", "success")
            return redirect(url_for("settings_keywords"))

    keywords = []
    reply_settings = {}
    filters = {}

    if config:
        keywords = config.get_keywords()
        reply_settings = config.get_reply_settings()
        filters = config.get_filters()

    body = render_template_string(
        KEYWORDS_BODY,
        config_error=error,
        keywords="\n".join(keywords),
        reply_settings=reply_settings,
        filters=filters,
    )
    return render_admin("Keywords", "keywords", body)



def _run_bot_in_thread():
    """Run bot in background thread."""
    global bot_status
    try:
        config = Config()
        bot = AutoReplyBot(config)
        with bot_status_lock:
            bot_status["bot_instance"] = bot
        
        stats = bot.run()
        
        with bot_status_lock:
            bot_status["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            bot_status["current_run_stats"] = stats
            bot_status["running"] = False
        
        bot.close()
        logger.info(f"Bot run completed. Stats: {stats}")
    except Exception as e:
        logger.error(f"Error running bot: {e}", exc_info=True)
        with bot_status_lock:
            bot_status["running"] = False
            bot_status["current_run_stats"] = {"error": str(e)}


def _start_scheduler():
    """Start the bot scheduler in background thread."""
    global bot_status
    try:
        config = Config()
        bot = AutoReplyBot(config)
        scheduler = BotScheduler(
            bot.run,
            bot.post_tweet,
            bot.post_thread_tweet,
            config.get_schedule_config(),
            config.get_tweet_settings()
        )
        scheduler.setup_schedule()
        
        # Store scheduler reference for stopping later
        bot._scheduler = scheduler
        
        with bot_status_lock:
            bot_status["bot_instance"] = bot
            bot_status["running"] = True
        
        def run_scheduler():
            try:
                logger.info("Scheduler thread started, entering run_continuously loop...")
                # Make scheduler check bot_status["running"] instead of its own flag
                scheduler.running = True
                logger.info("Scheduler started. Waiting for scheduled times...")
                logger.info(f"Next scheduled runs: Morning at {config.get_schedule_config().get('morning_time')}, Evening at {config.get_schedule_config().get('evening_time')}")
                
                while scheduler.running:
                    # Check if we should stop
                    with bot_status_lock:
                        if not bot_status["running"]:
                            scheduler.running = False
                            break
                    
                    schedule.run_pending()
                    time.sleep(60)  # Check every minute
                
                logger.info("Scheduler stopped (running flag set to False)")
            except Exception as e:
                logger.error(f"Scheduler error: {e}", exc_info=True)
                with bot_status_lock:
                    bot_status["running"] = False
            finally:
                logger.info("Scheduler thread exiting")
                with bot_status_lock:
                    bot_status["running"] = False
        
        thread = threading.Thread(target=run_scheduler, daemon=True)
        thread.start()
        
        with bot_status_lock:
            bot_status["scheduler_thread"] = thread
        
        logger.info("Bot scheduler started")
    except Exception as e:
        logger.error(f"Error starting scheduler: {e}", exc_info=True)
        with bot_status_lock:
            bot_status["running"] = False


@app.route("/settings/automation", methods=["GET", "POST"])
@login_required
def settings_automation():
    """Automation settings page - start/stop bot and configure schedule."""
    config = None
    error = None
    
    try:
        config = Config()
    except Exception as e:
        error = str(e)
    
    if request.method == "POST" and config:
        action = request.form.get("action")
        
        if action == "start":
            with bot_status_lock:
                if not bot_status["running"]:
                    try:
                        _start_scheduler()
                        # Check if it actually started
                        if bot_status["running"]:
                            flash("Bot started successfully! It will run twice daily at scheduled times.", "success")
                        else:
                            flash("Failed to start bot. Check logs for errors.", "danger")
                    except Exception as e:
                        logger.error(f"Error starting bot: {e}", exc_info=True)
                        flash(f"Error starting bot: {str(e)}", "danger")
                else:
                    flash("Bot is already running.", "warning")
        
        elif action == "stop":
            with bot_status_lock:
                if bot_status["running"]:
                    # Stop the scheduler
                    bot_status["running"] = False
                    if bot_status.get("bot_instance"):
                        scheduler_obj = getattr(bot_status.get("bot_instance"), "_scheduler", None)
                        if scheduler_obj:
                            scheduler_obj.running = False
                            scheduler_obj.stop()
                    flash("Bot stopped. It will finish current run and then stop.", "success")
                else:
                    flash("Bot is not running.", "warning")
        
        elif action == "run_once":
            with bot_status_lock:
                if bot_status["running"]:
                    flash("Bot is already running. Please stop it first to run once.", "warning")
                else:
                    try:
                        bot_status["running"] = True
                        thread = threading.Thread(target=_run_bot_in_thread, daemon=True)
                        thread.start()
                        logger.info("Started 'run once' thread")
                        flash("Bot run started. Check your Twitter account in a few minutes!", "info")
                    except Exception as e:
                        logger.error(f"Error starting run_once: {e}", exc_info=True)
                        bot_status["running"] = False
                        flash(f"Error starting bot: {str(e)}", "danger")
        
        elif action == "run_once_preview":
            with bot_status_lock:
                if bot_status["running"]:
                    flash("Bot is already running. Please stop it first to run a preview.", "warning")
                else:
                    try:
                        bot = AutoReplyBot(config)
                        stats = bot.run(dry_run=True)
                        bot.close()
                        session["last_preview"] = {
                            "stats": stats,
                            "preview": stats.get("preview") or [],
                        }
                        flash("Preview run completed. No tweets were posted.", "success")
                    except Exception as e:
                        logger.error(f"Error running preview: {e}", exc_info=True)
                        flash(f"Preview failed: {str(e)}", "danger")
        
        elif action == "save_schedule":
            data = config.config
            data.setdefault("schedule", {})
            data["schedule"]["morning_start"] = request.form.get("morning_start", "09:00")
            data["schedule"]["morning_end"] = request.form.get("morning_end", "11:00")
            data["schedule"]["evening_start"] = request.form.get("evening_start", "18:00")
            data["schedule"]["evening_end"] = request.form.get("evening_end", "20:00")
            # Keep backward compatibility
            data["schedule"]["morning_time"] = data["schedule"]["morning_start"]
            data["schedule"]["evening_time"] = data["schedule"]["evening_start"]
            data["schedule"]["timezone"] = request.form.get("timezone", "UTC")
            
            data.setdefault("reply_settings", {})
            data["reply_settings"]["min_replies_per_run"] = int(request.form.get("min_replies_per_run", 20))
            data["reply_settings"]["max_replies_per_run"] = int(request.form.get("max_replies_per_run", 50))
            data["reply_settings"]["delay_minutes_min"] = int(request.form.get("delay_minutes_min", 5))
            data["reply_settings"]["delay_minutes_max"] = int(request.form.get("delay_minutes_max", 15))
            
            data.setdefault("tweet_settings", {})
            data["tweet_settings"]["enabled"] = True
            data["tweet_settings"]["tweets_per_run"] = int(request.form.get("tweets_per_run", 1))
            data["tweet_settings"]["thread_enabled"] = True
            data["tweet_settings"]["threads_per_run"] = int(request.form.get("threads_per_run", 0))
            data["tweet_settings"]["thread_tweet_count"] = int(request.form.get("thread_tweet_count", 3))

            data.setdefault("man_in_the_loop", {})
            data["man_in_the_loop"]["enabled"] = bool(request.form.get("mitl_enabled"))
            
            Path(config.config_path).write_text(
                json.dumps(data, indent=2), encoding="utf-8"
            )
            
            flash("Settings saved successfully!", "success")
            
            # Restart scheduler if running
            with bot_status_lock:
                if bot_status["running"]:
                    bot_status["running"] = False
                    # Will restart on next page load if user wants
        
        return redirect(url_for("settings_automation"))
    
    schedule = {}
    reply_settings = {}
    tweet_settings = {}
    mitl_enabled = True
    
    if config:
        schedule = config.get_schedule_config()
        reply_settings = config.get_reply_settings()
        tweet_settings = config.get_tweet_settings()
        mitl_enabled = config.get_mitl_enabled()
    
    with bot_status_lock:
        bot_running = bot_status["running"]
        last_run = bot_status.get("last_run")
        next_run = bot_status.get("next_run")
    
    preview = session.pop("last_preview", None)
    
    body = render_template_string(
        AUTOMATION_BODY,
        config_error=error,
        schedule=schedule,
        reply_settings=reply_settings,
        tweet_settings=tweet_settings,
        mitl_enabled=mitl_enabled,
        bot_running=bot_running,
        last_run=last_run,
        next_run=next_run,
        preview=preview,
    )
    return render_admin("Automation", "automation", body)



RECORDS_SHELL = ADMIN_SHELL


def _serialize_agent_message(row: dict) -> dict:
    created = row.get("created_at")
    return {
        "id": row.get("id"),
        "role": row.get("role"),
        "content": row.get("content") or "",
        "created_at": created.isoformat() if hasattr(created, "isoformat") else str(created or ""),
    }


@app.route("/agent/chat")
@login_required
def agent_chat():
    """Admin dashboard chat with the same LangGraph agent as Telegram."""
    from src.agent import agent_enabled

    enabled = agent_enabled()
    body = render_template_string(
        """
        <div class="agent-chat">
          <div class="agent-chat-header">
            <div>
              <h2 class="mb-1">Agent chat</h2>
              <p class="text-muted mb-0 small">
                Same agent as Telegram · session <code>dashboard</code>
                · {% if enabled %}<span class="text-success">AGENT_ENABLED</span>{% else %}<span class="text-warning">AGENT_ENABLED is false</span>{% endif %}
                · <a href="{{ url_for('records_drafts') }}">Drafts</a>
                · <a href="{{ url_for('records_agent') }}">Activity</a>
              </p>
            </div>
            <button type="button" class="btn btn-outline-secondary btn-sm" id="agentChatNew">
              <i class="bi bi-plus-lg me-1"></i>New chat
            </button>
          </div>

          <div class="agent-chat-panel card shadow-sm">
            <div class="agent-chat-messages" id="agentChatMessages" aria-live="polite">
              <div class="agent-chat-empty text-muted" id="agentChatEmpty">
                Send a message to search X, draft posts, or queue replies.
                Risky writes still go to Drafts / Telegram Approve.
              </div>
            </div>
            <form class="agent-chat-compose" id="agentChatForm" autocomplete="off">
              <textarea
                id="agentChatInput"
                class="form-control"
                rows="2"
                placeholder="e.g. draft a tweet about SOL fees…"
                {% if not enabled %}disabled{% endif %}
              ></textarea>
              <button type="submit" class="btn btn-primary" id="agentChatSend" {% if not enabled %}disabled{% endif %}>
                <i class="bi bi-send me-1"></i>Send
              </button>
            </form>
          </div>
        </div>

        <script>
        (function () {
          var box = document.getElementById('agentChatMessages');
          var empty = document.getElementById('agentChatEmpty');
          var form = document.getElementById('agentChatForm');
          var input = document.getElementById('agentChatInput');
          var sendBtn = document.getElementById('agentChatSend');
          var newBtn = document.getElementById('agentChatNew');
          var enabled = {{ 'true' if enabled else 'false' }};

          function esc(s) {
            var d = document.createElement('div');
            d.textContent = s == null ? '' : String(s);
            return d.innerHTML;
          }

          function renderMsg(role, content, meta) {
            if (empty) empty.style.display = 'none';
            var el = document.createElement('div');
            el.className = 'agent-chat-bubble agent-chat-' + (role === 'user' ? 'user' : 'assistant');
            var label = role === 'user' ? 'You' : 'Agent';
            el.innerHTML =
              '<div class="agent-chat-meta">' + esc(label) + (meta ? ' · ' + esc(meta) : '') + '</div>' +
              '<div class="agent-chat-text">' + esc(content).replace(/\\n/g, '<br>') + '</div>';
            box.appendChild(el);
            box.scrollTop = box.scrollHeight;
            return el;
          }

          function setBusy(busy) {
            sendBtn.disabled = busy || !enabled;
            input.disabled = busy || !enabled;
            sendBtn.innerHTML = busy
              ? '<span class="spinner-border spinner-border-sm me-1"></span>Thinking…'
              : '<i class="bi bi-send me-1"></i>Send';
          }

          function loadHistory() {
            return fetch('{{ url_for("api_agent_chat_history") }}')
              .then(function (r) { return r.json(); })
              .then(function (data) {
                box.querySelectorAll('.agent-chat-bubble').forEach(function (n) { n.remove(); });
                if (empty) empty.style.display = 'block';
                (data.messages || []).forEach(function (m) {
                  renderMsg(m.role, m.content, m.created_at);
                });
              })
              .catch(function (e) {
                renderMsg('assistant', 'Could not load history: ' + e);
              });
          }

          form.addEventListener('submit', function (ev) {
            ev.preventDefault();
            if (!enabled) return;
            var text = (input.value || '').trim();
            if (!text) return;
            renderMsg('user', text);
            input.value = '';
            setBusy(true);
            var thinking = renderMsg('assistant', 'Thinking…');
            fetch('{{ url_for("api_agent_chat") }}', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ message: text })
            })
              .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
              .then(function (res) {
                thinking.remove();
                if (res.j.ok) {
                  renderMsg('assistant', res.j.reply || 'OK.');
                } else {
                  renderMsg('assistant', res.j.error || res.j.reply || 'Agent failed');
                }
              })
              .catch(function (e) {
                thinking.remove();
                renderMsg('assistant', 'Request failed: ' + e);
              })
              .finally(function () { setBusy(false); input.focus(); });
          });

          input.addEventListener('keydown', function (ev) {
            if (ev.key === 'Enter' && !ev.shiftKey) {
              ev.preventDefault();
              form.requestSubmit();
            }
          });

          newBtn.addEventListener('click', function () {
            if (!confirm('Start a new dashboard chat? Current history stays in Agent activity.')) return;
            setBusy(true);
            fetch('{{ url_for("api_agent_chat_new") }}', { method: 'POST' })
              .then(function (r) { return r.json(); })
              .then(function () { return loadHistory(); })
              .finally(function () { setBusy(false); input.focus(); });
          });

          loadHistory();
        })();
        </script>
        """,
        enabled=enabled,
    )
    return render_admin("Chat", "chat", body)


@app.route("/api/agent/chat/history")
@login_required
def api_agent_chat_history():
    """JSON message history for the dashboard agent session."""
    from src.agent import DASHBOARD_CHAT_ID, memory as agent_memory

    db = None
    try:
        db = get_db_connection()
        session = agent_memory.get_or_create_session(db, DASHBOARD_CHAT_ID)
        rows = agent_memory.recent_messages(db, int(session["id"]), limit=80)
        return jsonify(
            {
                "ok": True,
                "session_id": session["id"],
                "chat_id": DASHBOARD_CHAT_ID,
                "messages": [_serialize_agent_message(r) for r in rows],
            }
        )
    except Exception as e:
        logger.error(f"agent chat history error: {e}", exc_info=True)
        return jsonify({"ok": False, "error": str(e), "messages": []}), 500


@app.route("/api/agent/chat", methods=["POST"])
@login_required
def api_agent_chat():
    """Send one dashboard message through run_agent_turn."""
    from src.agent import DASHBOARD_CHAT_ID, run_agent_turn

    data = request.get_json(silent=True) or {}
    text = (data.get("message") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "Empty message"}), 400

    telegram = None
    try:
        telegram = get_telegram_approver()
    except Exception:
        telegram = None

    result = run_agent_turn(DASHBOARD_CHAT_ID, text, telegram=telegram)
    status = 200 if result.get("ok") else 400
    return jsonify(result), status


@app.route("/api/agent/chat/new", methods=["POST"])
@login_required
def api_agent_chat_new():
    """Archive the active dashboard session and start fresh."""
    from src.agent import DASHBOARD_CHAT_ID, memory as agent_memory

    db = None
    try:
        db = get_db_connection()
        session = agent_memory.get_or_create_session(db, DASHBOARD_CHAT_ID)
        agent_memory.archive_session(db, int(session["id"]))
        fresh = agent_memory.get_or_create_session(db, DASHBOARD_CHAT_ID)
        return jsonify({"ok": True, "session_id": fresh["id"]})
    except Exception as e:
        logger.error(f"agent chat new error: {e}", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/records/agent")
@login_required
def records_agent():
    """Recent agent sessions and tool actions."""
    db = get_db_connection()
    sessions = []
    actions = []
    try:
        from src.agent import memory as agent_memory
        from src.agent import agent_enabled

        sessions = agent_memory.list_recent_sessions(db, limit=30)
        actions = agent_memory.list_recent_actions(db, limit=50)
        enabled = agent_enabled()
    except Exception as e:
        logger.error(f"Agent records error: {e}", exc_info=True)
        enabled = False
        flash(f"Could not load agent data: {e}", "warning")

    body = render_template_string(
        """
        <h2 class="mb-3">Agent activity</h2>
        <p class="text-muted">
          AGENT_ENABLED={{ 'true' if enabled else 'false' }}.
          Chat sources: <a href="{{ url_for('agent_chat') }}">Dashboard</a> (chat_id <code>dashboard</code>)
          and Telegram. Risky X writes appear as Drafts for Approve.
        </p>
        <div class="row g-3">
          <div class="col-md-5">
            <h5>Sessions</h5>
            <div class="table-responsive">
              <table class="table table-sm table-dark">
                <thead><tr><th>ID</th><th>Chat</th><th>Status</th><th>Updated</th></tr></thead>
                <tbody>
                {% for s in sessions %}
                  <tr>
                    <td>{{ s.id }}</td>
                    <td>{{ s.chat_id }}</td>
                    <td>{{ s.status }}</td>
                    <td>{{ s.updated_at }}</td>
                  </tr>
                {% else %}
                  <tr><td colspan="4" class="text-muted">No sessions yet</td></tr>
                {% endfor %}
                </tbody>
              </table>
            </div>
          </div>
          <div class="col-md-7">
            <h5>Actions</h5>
            <div class="table-responsive">
              <table class="table table-sm table-dark">
                <thead><tr><th>ID</th><th>Tool</th><th>Risk</th><th>Status</th><th>Draft</th><th>Result</th></tr></thead>
                <tbody>
                {% for a in actions %}
                  <tr>
                    <td>{{ a.id }}</td>
                    <td>{{ a.tool_name }}</td>
                    <td>{{ a.risk_level }}</td>
                    <td>{{ a.status }}</td>
                    <td>{{ a.draft_id or '—' }}</td>
                    <td class="small">{{ (a.result or '')[:80] }}</td>
                  </tr>
                {% else %}
                  <tr><td colspan="6" class="text-muted">No actions yet</td></tr>
                {% endfor %}
                </tbody>
              </table>
            </div>
          </div>
        </div>
        """,
        sessions=sessions,
        actions=actions,
        enabled=enabled,
    )
    return render_admin("Agent", "agent", body
    )


@app.route("/records/drafts", methods=["GET", "POST"])
@login_required
def records_drafts():
    """Pending / recent drafts with approve/reject/edit and image attach."""
    poster = DraftPoster()
    db = get_db_connection()

    if request.method == "POST":
        action = request.form.get("action")
        try:
            draft_id = int(request.form.get("draft_id", "0"))
        except ValueError:
            flash("Invalid draft id", "danger")
            return redirect(url_for("records_drafts"))

        if action == "approve":
            # Persist selected images before posting
            raw_ids = request.form.getlist("media_ids")
            try:
                media_ids = [int(x) for x in raw_ids][:4]
            except ValueError:
                media_ids = []
            db.set_draft_media(draft_id, media_ids)
            edited = request.form.get("edited_text")
            result = poster.approve_and_post(draft_id, edited_text=edited)
            flash(
                "Posted." if result.get("ok") else f"Failed: {result.get('error')}",
                "success" if result.get("ok") else "danger",
            )
        elif action == "reject":
            result = poster.reject(draft_id)
            flash(
                "Rejected." if result.get("ok") else f"Failed: {result.get('error')}",
                "success" if result.get("ok") else "danger",
            )
        elif action == "edit_save":
            raw_ids = request.form.getlist("media_ids")
            try:
                media_ids = [int(x) for x in raw_ids][:4]
            except ValueError:
                media_ids = []
            db.set_draft_media(draft_id, media_ids)
            result = poster.save_edit(draft_id, request.form.get("edited_text", ""))
            flash(
                "Edit & images saved." if result.get("ok") else f"Failed: {result.get('error')}",
                "success" if result.get("ok") else "danger",
            )
        elif action == "attach_media":
            raw_ids = request.form.getlist("media_ids")
            try:
                media_ids = [int(x) for x in raw_ids][:4]
            except ValueError:
                media_ids = []
            db.set_draft_media(draft_id, media_ids)
            flash("Images attached to draft (max 4).", "success")
        return redirect(url_for("records_drafts", status=request.args.get("status", "pending")))

    status = request.args.get("status", "pending")
    status_filter = status if status != "all" else None
    drafts = db.list_drafts(status=status_filter, limit=100)
    library = db.list_media_assets(limit=100)
    drafts_with_media = []
    for d in drafts:
        item = dict(d)
        item["attached"] = db.get_draft_media(d["id"])
        item["attached_ids"] = {a["id"] for a in item["attached"]}
        drafts_with_media.append(item)

    body = render_template_string(
        """
        <h2 class="mb-3">Content Drafts</h2>
        <p class="text-muted">Attach up to 4 images from the <a href="{{ url_for('settings_media') }}">Media library</a> before Approve &amp; Post.</p>
        <div class="mb-3">
          <a class="btn btn-sm btn-outline-primary {% if status=='pending' %}active{% endif %}" href="{{ url_for('records_drafts', status='pending') }}">Pending</a>
          <a class="btn btn-sm btn-outline-secondary {% if status=='posted' %}active{% endif %}" href="{{ url_for('records_drafts', status='posted') }}">Posted</a>
          <a class="btn btn-sm btn-outline-secondary {% if status=='rejected' %}active{% endif %}" href="{{ url_for('records_drafts', status='rejected') }}">Rejected</a>
          <a class="btn btn-sm btn-outline-secondary {% if status=='all' %}active{% endif %}" href="{{ url_for('records_drafts', status='all') }}">All</a>
        </div>
        {% if not drafts %}
          <p class="text-muted">No drafts found.</p>
        {% endif %}
        {% for d in drafts %}
          <div class="card mb-3 shadow-sm">
            <div class="card-body">
              <div class="d-flex justify-content-between">
                <h5 class="card-title">#{{ d.id }} · {{ d.kind }} · <span class="badge bg-secondary">{{ d.status }}</span></h5>
                <small class="text-muted">{{ d.created_at }}</small>
              </div>
              {% if d.target_tweet_text %}
                <p class="small text-muted mb-1">Original (@{{ d.target_author or '?' }}): {{ d.target_tweet_text[:280] }}</p>
              {% endif %}
              {% if d.attached %}
                <div class="d-flex flex-wrap gap-2 mb-2">
                  {% for a in d.attached %}
                    <img src="{{ url_for('serve_media', filename=a.filename) }}" alt="" style="height:64px;width:64px;object-fit:cover;border-radius:4px;">
                  {% endfor %}
                </div>
              {% endif %}
              <form method="post" class="mt-2">
                <input type="hidden" name="draft_id" value="{{ d.id }}">
                <textarea name="edited_text" class="form-control mb-2" rows="3">{{ d.edited_text or d.generated_text }}</textarea>
                {% if d.status == 'pending' %}
                <div class="mb-2">
                  <label class="form-label small">Attach images (max 4)</label>
                  <div class="d-flex flex-wrap gap-2" style="max-height:160px;overflow:auto;">
                    {% for m in library %}
                      <label class="border rounded p-1 text-center" style="width:76px;">
                        <img src="{{ url_for('serve_media', filename=m.filename) }}" style="height:56px;width:56px;object-fit:cover;display:block;margin:0 auto;">
                        <input type="checkbox" name="media_ids" value="{{ m.id }}" {% if m.id in d.attached_ids %}checked{% endif %}>
                      </label>
                    {% else %}
                      <span class="text-muted small">No images yet — upload in Media.</span>
                    {% endfor %}
                  </div>
                </div>
                <button name="action" value="approve" class="btn btn-success btn-sm">Approve & Post</button>
                <button name="action" value="edit_save" class="btn btn-outline-primary btn-sm">Save Edit</button>
                <button name="action" value="attach_media" class="btn btn-outline-secondary btn-sm">Save Images</button>
                <button name="action" value="reject" class="btn btn-outline-danger btn-sm">Reject</button>
                {% endif %}
              </form>
              <div class="small text-muted mt-2">{{ d.provider or '-' }} / {{ d.model or '-' }} · keyword={{ d.keyword or '-' }}</div>
            </div>
          </div>
        {% endfor %}
        """,
        drafts=drafts_with_media,
        library=library,
        status=status,
    )
    return render_admin("Drafts", "drafts", body
    )


@app.route("/media/<path:filename>")
@login_required
def serve_media(filename):
    """Serve uploaded media files (auth required)."""
    return send_from_directory(media_root(), Path(filename).name)


@app.route("/settings/media", methods=["GET", "POST"])
@login_required
def settings_media():
    """Upload and manage the image library."""
    db = get_db_connection()

    if request.method == "POST":
        action = request.form.get("action")
        if action == "upload":
            files = request.files.getlist("images")
            saved = 0
            for f in files:
                if not f or not f.filename:
                    continue
                try:
                    stored, original, mime, size = save_upload(f)
                    path = str(media_root() / stored)
                    db.create_media_asset(stored, original, mime, path, size)
                    saved += 1
                except Exception as e:
                    flash(f"{f.filename}: {e}", "danger")
            if saved:
                flash(f"Uploaded {saved} image(s).", "success")
        elif action == "delete":
            try:
                mid = int(request.form.get("media_id", "0"))
            except ValueError:
                mid = 0
            row = db.delete_media_asset(mid)
            if row:
                delete_file(row.get("file_path") or row.get("filename"))
                flash("Image deleted.", "success")
            else:
                flash("Image not found.", "warning")
        return redirect(url_for("settings_media"))

    assets = db.list_media_assets(limit=200)
    body = render_template_string(
        """
        <h2>Media library</h2>
        <p class="text-muted">Upload JPG/PNG/GIF/WEBP (max 5&nbsp;MB each). Attach up to 4 images per draft before posting.</p>
        <form method="post" enctype="multipart/form-data" class="card shadow-sm p-3 mb-4">
          <input type="hidden" name="action" value="upload">
          <input type="file" name="images" accept="image/jpeg,image/png,image/gif,image/webp" multiple class="form-control mb-2" required>
          <button class="btn btn-primary">Upload</button>
        </form>
        <div class="row g-3">
          {% for a in assets %}
          <div class="col-6 col-md-3 col-lg-2">
            <div class="card h-100">
              <img src="{{ url_for('serve_media', filename=a.filename) }}" class="card-img-top" style="height:120px;object-fit:cover;" alt="{{ a.original_name }}">
              <div class="card-body p-2">
                <div class="small text-truncate" title="{{ a.original_name }}">{{ a.original_name }}</div>
                <form method="post" class="mt-1">
                  <input type="hidden" name="action" value="delete">
                  <input type="hidden" name="media_id" value="{{ a.id }}">
                  <button class="btn btn-outline-danger btn-sm w-100" onclick="return confirm('Delete this image?')">Delete</button>
                </form>
              </div>
            </div>
          </div>
          {% else %}
          <div class="col-12"><p class="text-muted">No images uploaded yet.</p></div>
          {% endfor %}
        </div>
        """,
        assets=assets,
    )
    return render_admin("Media", "media", body
    )


@app.route("/records/replies")
@login_required
def records_replies():
    db = get_db_connection()
    page = max(int(request.args.get("page", 1)), 1)
    limit = 50
    offset = (page - 1) * limit
    rows = db.list_replied_tweets(limit=limit, offset=offset)
    quotes = db.list_quote_retweets(limit=20, offset=0)
    tweets = db.list_posted_tweets(limit=20, offset=0)
    threads = db.list_posted_threads(limit=20, offset=0)
    body = render_template_string(
        """
        <h2 class="mb-3">Activity Records</h2>
        <ul class="nav nav-tabs mb-3">
          <li class="nav-item"><a class="nav-link active" href="{{ url_for('records_replies') }}">Replies</a></li>
          <li class="nav-item"><a class="nav-link" href="{{ url_for('records_tweets') }}">Tweets & Threads</a></li>
          <li class="nav-item"><a class="nav-link" href="{{ url_for('records_quotes') }}">Quotes</a></li>
        </ul>
        <div class="table-responsive card shadow-sm">
          <table class="table table-sm mb-0">
            <thead><tr><th>Tweet</th><th>Reply</th><th>Source</th><th>Keyword</th><th>When</th></tr></thead>
            <tbody>
            {% for r in rows %}
              <tr>
                <td>{{ r.tweet_id }}</td>
                <td>{{ r.reply_tweet_id }}</td>
                <td>{{ r.source }}</td>
                <td>{{ r.keyword or '-' }}</td>
                <td>{{ r.replied_at }}</td>
              </tr>
            {% else %}
              <tr><td colspan="5" class="text-muted">No replies yet.</td></tr>
            {% endfor %}
            </tbody>
          </table>
        </div>
        <div class="mt-3">
          {% if page > 1 %}<a class="btn btn-sm btn-outline-secondary" href="{{ url_for('records_replies', page=page-1) }}">Prev</a>{% endif %}
          <a class="btn btn-sm btn-outline-secondary" href="{{ url_for('records_replies', page=page+1) }}">Next</a>
        </div>
        <h4 class="mt-4">Recent original tweets (snapshot)</h4>
        <ul>{% for t in tweets %}<li>{{ t.posted_at }} — {{ t.text[:120] }}…</li>{% else %}<li class="text-muted">None</li>{% endfor %}</ul>
        <h4 class="mt-3">Recent threads (snapshot)</h4>
        <ul>{% for t in threads %}<li>{{ t.posted_at }} — {{ t.first_tweet_id }}</li>{% else %}<li class="text-muted">None</li>{% endfor %}</ul>
        <h4 class="mt-3">Recent quotes (snapshot)</h4>
        <ul>{% for q in quotes %}<li>{{ q.posted_at }} — {{ q.text[:120] }}…</li>{% else %}<li class="text-muted">None</li>{% endfor %}</ul>
        """,
        rows=rows,
        page=page,
        tweets=tweets,
        threads=threads,
        quotes=quotes,
    )
    return render_admin("Records", "replies", body
    )


@app.route("/records/tweets")
@login_required
def records_tweets():
    db = get_db_connection()
    tweets = db.list_posted_tweets(limit=100)
    threads = db.list_posted_threads(limit=100)
    body = render_template_string(
        """
        <h2>Posted Tweets & Threads</h2>
        <h4 class="mt-3">Tweets</h4>
        <div class="table-responsive card"><table class="table table-sm mb-0">
          <thead><tr><th>ID</th><th>Text</th><th>When</th></tr></thead>
          <tbody>
          {% for t in tweets %}<tr><td>{{ t.tweet_id }}</td><td>{{ t.text }}</td><td>{{ t.posted_at }}</td></tr>
          {% else %}<tr><td colspan="3" class="text-muted">None</td></tr>{% endfor %}
          </tbody></table></div>
        <h4 class="mt-4">Threads</h4>
        <div class="table-responsive card"><table class="table table-sm mb-0">
          <thead><tr><th>Thread</th><th>First</th><th>When</th></tr></thead>
          <tbody>
          {% for t in threads %}<tr><td>{{ t.thread_id }}</td><td>{{ t.first_tweet_id }}</td><td>{{ t.posted_at }}</td></tr>
          {% else %}<tr><td colspan="3" class="text-muted">None</td></tr>{% endfor %}
          </tbody></table></div>
        """,
        tweets=tweets,
        threads=threads,
    )
    return render_admin("Tweets", "replies", body
    )


@app.route("/records/quotes")
@login_required
def records_quotes():
    db = get_db_connection()
    rows = db.list_quote_retweets(limit=100)
    body = render_template_string(
        """
        <h2>Quote Tweets</h2>
        <div class="table-responsive card"><table class="table table-sm mb-0">
          <thead><tr><th>Quote ID</th><th>Original</th><th>Text</th><th>When</th></tr></thead>
          <tbody>
          {% for r in rows %}
            <tr><td>{{ r.quote_tweet_id }}</td><td>{{ r.original_tweet_id }}</td><td>{{ r.text }}</td><td>{{ r.posted_at }}</td></tr>
          {% else %}
            <tr><td colspan="4" class="text-muted">None</td></tr>
          {% endfor %}
          </tbody></table></div>
        """,
        rows=rows,
    )
    return render_admin("Quotes", "replies", body
    )


@app.route("/settings/ai", methods=["GET", "POST"])
@login_required
def settings_ai():
    """AI provider defaults and editable niche system instructions."""
    db = get_db_connection()
    config = Config()

    if request.method == "POST":
        action = request.form.get("action")
        if action == "save_defaults":
            data = config.config
            data.setdefault("ai", {})
            data["ai"]["provider"] = request.form.get("provider", "gemini")
            data["ai"]["prompt_profile"] = request.form.get("prompt_profile", "default")
            data["ai"]["temperature"] = float(request.form.get("temperature", 0.7))
            Path(config.config_path).write_text(json.dumps(data, indent=2), encoding="utf-8")
            # Mark default provider in DB
            for p in db.list_ai_providers():
                db.save_ai_provider(
                    {
                        "provider": p["provider"],
                        "api_key": None,
                        "model": request.form.get(f"model_{p['provider']}") or p.get("model"),
                        "enabled": p.get("enabled"),
                        "is_default": p["provider"] == data["ai"]["provider"],
                    }
                )
            flash("AI defaults saved.", "success")
        elif action == "save_profile":
            profile_id = request.form.get("profile_id")
            db.save_prompt_profile(
                {
                    "id": int(profile_id) if profile_id else None,
                    "name": request.form.get("name", "").strip() or "default",
                    "content_type": request.form.get("content_type", "reply"),
                    "system_instruction": request.form.get("system_instruction", ""),
                    "user_prompt_template": request.form.get("user_prompt_template") or None,
                    "is_active": bool(request.form.get("is_active")),
                }
            )
            flash("Prompt profile saved.", "success")
        elif action == "delete_profile":
            db.delete_prompt_profile(int(request.form.get("profile_id")))
            flash("Profile deleted.", "success")
        return redirect(url_for("settings_ai"))

    profiles = db.list_prompt_profiles()
    providers = db.list_ai_providers()
    ai = config.config.get("ai", {})
    niche_names = sorted({p["name"] for p in profiles})
    body = render_template_string(
        """
        <h2>AI Settings</h2>
        <form method="post" class="card shadow-sm p-3 mb-4">
          <input type="hidden" name="action" value="save_defaults">
          <div class="row g-3">
            <div class="col-md-4">
              <label class="form-label">Default provider</label>
              <select name="provider" class="form-select">
                <option value="gemini" {% if ai.get('provider')=='gemini' %}selected{% endif %}>Gemini</option>
                <option value="openai" {% if ai.get('provider')=='openai' %}selected{% endif %}>OpenAI</option>
                <option value="anthropic" {% if ai.get('provider')=='anthropic' %}selected{% endif %}>Anthropic</option>
                <option value="agentrouter" {% if ai.get('provider')=='agentrouter' %}selected{% endif %}>AgentRouter</option>
              </select>
            </div>
            <div class="col-md-4">
              <label class="form-label">Active niche profile</label>
              <select name="prompt_profile" class="form-select">
                {% for n in niche_names %}
                  <option value="{{ n }}" {% if ai.get('prompt_profile')==n %}selected{% endif %}>{{ n }}</option>
                {% endfor %}
              </select>
            </div>
            <div class="col-md-4">
              <label class="form-label">Temperature</label>
              <input type="number" step="0.1" min="0" max="2" name="temperature" class="form-control" value="{{ ai.get('temperature', 0.7) }}">
            </div>
            {% for p in providers %}
            <div class="col-md-4">
              <label class="form-label">{{ p.provider }} model</label>
              <input name="model_{{ p.provider }}" class="form-control" value="{{ p.model or '' }}">
            </div>
            {% endfor %}
          </div>
          <button class="btn btn-primary mt-3">Save defaults</button>
        </form>

        <h3>System instruction niches</h3>
        <p class="text-muted">Edit prompts for web3, blockchain, default, or add your own niche.</p>
        {% for p in profiles %}
        <form method="post" class="card shadow-sm p-3 mb-3">
          <input type="hidden" name="action" value="save_profile">
          <input type="hidden" name="profile_id" value="{{ p.id }}">
          <div class="row g-2">
            <div class="col-md-3"><input name="name" class="form-control" value="{{ p.name }}" placeholder="niche name"></div>
            <div class="col-md-3">
              <select name="content_type" class="form-select">
                {% for t in ['reply','quote','tweet','thread'] %}
                <option value="{{ t }}" {% if p.content_type==t %}selected{% endif %}>{{ t }}</option>
                {% endfor %}
              </select>
            </div>
            <div class="col-md-3 form-check mt-2">
              <input class="form-check-input" type="checkbox" name="is_active" id="active{{ p.id }}" {% if p.is_active %}checked{% endif %}>
              <label class="form-check-label" for="active{{ p.id }}">Active</label>
            </div>
          </div>
          <textarea name="system_instruction" class="form-control mt-2" rows="4">{{ p.system_instruction }}</textarea>
          <div class="mt-2">
            <button class="btn btn-sm btn-primary">Save</button>
            <button class="btn btn-sm btn-outline-danger" name="action" value="delete_profile" onclick="return confirm('Delete?')">Delete</button>
          </div>
        </form>
        {% endfor %}

        <form method="post" class="card shadow-sm p-3">
          <h5>Add niche profile</h5>
          <input type="hidden" name="action" value="save_profile">
          <div class="row g-2">
            <div class="col-md-3"><input name="name" class="form-control" placeholder="e.g. web3" required></div>
            <div class="col-md-3">
              <select name="content_type" class="form-select">
                <option value="reply">reply</option>
                <option value="quote">quote</option>
                <option value="tweet">tweet</option>
                <option value="thread">thread</option>
              </select>
            </div>
            <div class="col-md-3 form-check mt-2">
              <input class="form-check-input" type="checkbox" name="is_active" id="newActive" checked>
              <label class="form-check-label" for="newActive">Active</label>
            </div>
          </div>
          <textarea name="system_instruction" class="form-control mt-2" rows="4" placeholder="System instruction for this niche..." required></textarea>
          <button class="btn btn-success mt-2">Create</button>
        </form>
        """,
        profiles=profiles,
        providers=providers,
        ai=ai,
        niche_names=niche_names,
    )
    return render_admin("AI Settings", "ai", body
    )


@app.route("/settings/safety", methods=["GET", "POST"])
@login_required
def settings_safety():
    """Rate budgets, quality gates, tip refusal, and recent blocks."""
    from src.quality_safety import (
        get_usage_stats,
        list_safety_events,
        count_safety_events_today,
        DEFAULT_SAFETY,
    )

    config = Config()
    db = get_db_connection()

    if request.method == "POST":
        data = config.config
        data.setdefault("safety", dict(DEFAULT_SAFETY))
        s = data["safety"]
        s["enabled"] = bool(request.form.get("enabled"))
        s["refuse_risky_tips"] = bool(request.form.get("refuse_risky_tips"))
        s["block_all_caps"] = bool(request.form.get("block_all_caps"))
        s["block_duplicates"] = bool(request.form.get("block_duplicates"))
        for key in (
            "daily_post_limit",
            "monthly_post_limit",
            "max_pending_drafts",
            "min_chars",
            "max_chars",
            "max_hashtags",
            "max_links",
        ):
            try:
                s[key] = int(request.form.get(key, s.get(key, 0)))
            except ValueError:
                pass
        phrases = request.form.get("custom_block_phrases", "")
        s["custom_block_phrases"] = [
            p.strip() for p in phrases.splitlines() if p.strip()
        ]
        Path(config.config_path).write_text(
            json.dumps(data, indent=2), encoding="utf-8"
        )
        flash("Safety settings saved.", "success")
        return redirect(url_for("settings_safety"))

    safety = config.get_safety_config()
    usage = get_usage_stats(db)
    events = list_safety_events(db, limit=40)
    blocked_today = count_safety_events_today(db)
    body = render_template_string(
        """
        <h2>Safety &amp; quality</h2>
        <p class="text-muted">Rate budgets, spam gates, and risky-tip refusal for Telegram compose and auto runs.</p>

        <div class="row g-3 mb-4">
          <div class="col-md-3"><div class="card p-3"><div class="text-muted small">Posts today</div><div class="h4 mb-0">{{ usage.posts_today }} / {{ safety.daily_post_limit }}</div></div></div>
          <div class="col-md-3"><div class="card p-3"><div class="text-muted small">Posts this month</div><div class="h4 mb-0">{{ usage.posts_month }} / {{ safety.monthly_post_limit }}</div></div></div>
          <div class="col-md-3"><div class="card p-3"><div class="text-muted small">Pending drafts</div><div class="h4 mb-0">{{ usage.pending_drafts }} / {{ safety.max_pending_drafts }}</div></div></div>
          <div class="col-md-3"><div class="card p-3"><div class="text-muted small">Blocked today</div><div class="h4 mb-0">{{ blocked_today }}</div></div></div>
        </div>

        <form method="post" class="card shadow-sm p-3 mb-4">
          <div class="form-check mb-2">
            <input class="form-check-input" type="checkbox" name="enabled" id="enabled" {% if safety.enabled %}checked{% endif %}>
            <label class="form-check-label" for="enabled">Enable safety gates</label>
          </div>
          <div class="row g-3">
            <div class="col-md-3"><label class="form-label">Daily post limit</label><input type="number" name="daily_post_limit" class="form-control" value="{{ safety.daily_post_limit }}" min="0"></div>
            <div class="col-md-3"><label class="form-label">Monthly post limit</label><input type="number" name="monthly_post_limit" class="form-control" value="{{ safety.monthly_post_limit }}" min="0"></div>
            <div class="col-md-3"><label class="form-label">Max pending drafts</label><input type="number" name="max_pending_drafts" class="form-control" value="{{ safety.max_pending_drafts }}" min="0"></div>
            <div class="col-md-3"><label class="form-label">Min chars</label><input type="number" name="min_chars" class="form-control" value="{{ safety.min_chars }}" min="1"></div>
            <div class="col-md-3"><label class="form-label">Max chars</label><input type="number" name="max_chars" class="form-control" value="{{ safety.max_chars }}" min="1" max="280"></div>
            <div class="col-md-3"><label class="form-label">Max hashtags</label><input type="number" name="max_hashtags" class="form-control" value="{{ safety.max_hashtags }}" min="0"></div>
            <div class="col-md-3"><label class="form-label">Max links</label><input type="number" name="max_links" class="form-control" value="{{ safety.max_links }}" min="0"></div>
          </div>
          <div class="form-check mt-3">
            <input class="form-check-input" type="checkbox" name="refuse_risky_tips" id="refuse_risky_tips" {% if safety.refuse_risky_tips %}checked{% endif %}>
            <label class="form-check-label" for="refuse_risky_tips">Refuse risky Telegram tips (scams, guarantees, keys, etc.)</label>
          </div>
          <div class="form-check">
            <input class="form-check-input" type="checkbox" name="block_all_caps" id="block_all_caps" {% if safety.block_all_caps %}checked{% endif %}>
            <label class="form-check-label" for="block_all_caps">Block ALL-CAPS spam</label>
          </div>
          <div class="form-check mb-3">
            <input class="form-check-input" type="checkbox" name="block_duplicates" id="block_duplicates" {% if safety.block_duplicates %}checked{% endif %}>
            <label class="form-check-label" for="block_duplicates">Block duplicate text (7 days)</label>
          </div>
          <label class="form-label">Custom block phrases (one per line)</label>
          <textarea name="custom_block_phrases" class="form-control mb-3" rows="4" placeholder="guaranteed profit">{{ phrases }}</textarea>
          <button class="btn btn-primary">Save safety settings</button>
        </form>

        <h4>Recent blocks / refusals</h4>
        <div class="table-responsive card">
          <table class="table table-sm mb-0">
            <thead><tr><th>When</th><th>Type</th><th>Message</th></tr></thead>
            <tbody>
            {% for e in events %}
              <tr><td>{{ e.created_at }}</td><td><span class="badge bg-warning text-dark">{{ e.event_type }}</span></td><td>{{ e.message }}</td></tr>
            {% else %}
              <tr><td colspan="3" class="text-muted">None yet.</td></tr>
            {% endfor %}
            </tbody>
          </table>
        </div>
        """,
        safety=safety,
        usage=usage,
        events=events,
        blocked_today=blocked_today,
        phrases="\n".join(safety.get("custom_block_phrases") or []),
    )
    return render_admin("Safety", "safety", body
    )


if __name__ == "__main__":
    # Get port from environment variable (for production) or use default
    port = int(os.getenv("PORT", 5001))
    # Only enable debug in development
    debug = os.getenv("FLASK_DEBUG", "False").lower() == "true"
    # Use 0.0.0.0 to accept connections from all interfaces (needed for production)
    app.run(host="0.0.0.0", port=port, debug=debug)


