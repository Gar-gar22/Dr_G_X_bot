from flask import Flask, render_template_string, request, redirect, url_for, flash, session, jsonify
import sqlite3
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

from src.config import Config
from src.x_api import XAPI
from src.bot import AutoReplyBot
from src.scheduler import BotScheduler
from src.database import Database

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

DB_PATH = Path("bot_state.db")
AUTH_DB_PATH = Path("auth.db")

# Allowed email for registration
ALLOWED_EMAIL = "ohakwebusiness@gmail.com"


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
    """Get MySQL database connection using Database class."""
    global db_instance
    if db_instance is None:
        try:
            db_instance = Database()
        except Exception as e:
            logger.error(f"Failed to create database connection: {e}", exc_info=True)
            raise
    return db_instance


def get_auth_db_connection():
    """Get connection to authentication database (still using SQLite for auth)."""
    conn = sqlite3.connect(AUTH_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_auth_db():
    """Initialize authentication database with users table (SQLite)."""
    conn = get_auth_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_email ON users(email)")
        conn.commit()
    except Exception as e:
        logger.error(f"Error initializing auth database: {e}")
        conn.rollback()
    finally:
        cur.close()
        conn.close()


def login_required(f):
    """Decorator to require login for routes."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please log in to access this page.', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


HOME_TEMPLATE = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Auto-Reply X Bot Dashboard</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.css" rel="stylesheet">
    <link href="{{ url_for('static', filename='style.css') }}" rel="stylesheet">
  </head>
  <body>
    <div class="mobile-topbar px-3 py-2 d-md-none">
      <button class="btn btn-outline-light btn-sm" type="button" onclick="toggleSidebar()">
        <i class="bi bi-list" id="navToggleIcon"></i>
      </button>
      <span class="fw-semibold">X Bot</span>
    </div>
    <div class="d-flex">
      <nav class="sidebar bg-dark text-white p-3">
        <h5 class="mb-4">X Bot</h5>
        <ul class="nav nav-pills flex-column mb-auto">
          <li class="nav-item">
            <a href="{{ url_for('dashboard_overview') }}" class="nav-link text-white active">
              <i class="bi bi-speedometer2 me-2"></i> Overview
            </a>
          </li>
          <li>
            <a href="{{ url_for('settings_keywords') }}" class="nav-link text-white">
              <i class="bi bi-filter-circle me-2"></i> Keywords & Filters
            </a>
          </li>
          <li>
            <a href="{{ url_for('settings_credentials') }}" class="nav-link text-white">
              <i class="bi bi-person-badge me-2"></i> Credentials
            </a>
          </li>
          <li>
            <a href="{{ url_for('settings_automation') }}" class="nav-link text-white">
              <i class="bi bi-gear me-2"></i> Automation
            </a>
          </li>
          <li>
            <a href="{{ url_for('settings_logs') }}" class="nav-link text-white">
              <i class="bi bi-journal-text me-2"></i> Logs
            </a>
          </li>
        </ul>
        <div class="mt-auto pt-3 border-top">
          <div class="small text-muted mb-2">{{ session.get('user_email', 'User') }}</div>
          <a href="{{ url_for('logout') }}" class="btn btn-outline-light btn-sm w-100">
            <i class="bi bi-box-arrow-right me-1"></i> Logout
          </a>
        </div>
      </nav>
      <main class="flex-grow-1 p-4">
        {% if not has_twitter %}
        <div class="alert alert-warning d-flex justify-content-between align-items-center mb-4">
          <div>
            <strong>Connect your X (Twitter) account</strong><br>
            <span class="small">Add your API keys first so the bot can search tweets and send replies.</span>
          </div>
          <a href="{{ url_for('settings_credentials') }}" class="btn btn-sm btn-primary">
            Connect Twitter
          </a>
        </div>
        {% endif %}
        <h2 class="mb-4">Overview</h2>
        {% if twitter_status or gemini_status is not none %}
        <div class="row g-3 mb-3">
          <div class="col-12">
            <div class="card shadow-sm">
              <div class="card-body py-2">
                <h6 class="card-title text-muted small mb-2">Connection status</h6>
                <div class="d-flex flex-wrap gap-3">
                  {% if twitter_status %}
                  <span class="d-flex align-items-center">
                    <span class="badge bg-{{ 'success' if twitter_status == 'ok' else 'danger' if twitter_status == 'error' else 'secondary' }} me-2">
                      <i class="bi bi-twitter"></i> Twitter
                    </span>
                    <span class="small">{{ 'Connected' if twitter_status == 'ok' else twitter_message or 'Not connected' if twitter_status == 'error' else 'Not configured' }}</span>
                  </span>
                  {% endif %}
                  {% if gemini_status is not none %}
                  <span class="d-flex align-items-center">
                    <span class="badge bg-{{ 'success' if gemini_status == 'ok' else 'warning' if gemini_status == 'configured' else 'secondary' }} me-2">
                      <i class="bi bi-robot"></i> Gemini
                    </span>
                    <span class="small">{{ 'AI replies on' if gemini_status == 'ok' else 'Configured (off)' if gemini_status == 'configured' else 'Not configured' }}</span>
                  </span>
                  {% endif %}
                </div>
              </div>
            </div>
          </div>
        </div>
        {% endif %}
        <div class="row g-3 mb-4">
          <div class="col-md-3">
            <div class="card shadow-sm">
              <div class="card-body">
                <h6 class="card-title text-muted">Total Replies</h6>
                <p class="display-6 mb-0">{{ total_replied }}</p>
              </div>
            </div>
          </div>
          <div class="col-md-3">
            <div class="card shadow-sm">
              <div class="card-body">
                <h6 class="card-title text-muted">Total Tweets Posted</h6>
                <p class="display-6 mb-0">{{ total_tweets_posted }}</p>
              </div>
            </div>
          </div>
          <div class="col-md-3">
            <div class="card shadow-sm">
              <div class="card-body">
                <h6 class="card-title text-muted">Total Quote Retweets</h6>
                <p class="display-6 mb-0">{{ total_quote_retweets }}</p>
              </div>
            </div>
          </div>
          <div class="col-md-3">
            <div class="card shadow-sm">
              <div class="card-body">
                <h6 class="card-title text-muted">Last Reply At</h6>
                <p class="h5 mb-0">{{ last_reply or 'N/A' }}</p>
              </div>
            </div>
          </div>
        </div>

        <h4 class="mt-4">Recent Replies</h4>
        <div class="card shadow-sm">
          <div class="card-body">
            {% if recent_replies %}
              <div class="table-responsive">
                <table class="table table-sm align-middle mb-0">
                  <thead>
                    <tr>
                      <th scope="col">Tweet ID</th>
                      <th scope="col">Reply ID</th>
                      <th scope="col">Source</th>
                      <th scope="col">Keyword</th>
                      <th scope="col">Replied At</th>
                    </tr>
                  </thead>
                  <tbody>
                    {% for row in recent_replies %}
                    <tr>
                      <td>{{ row['tweet_id'] }}</td>
                      <td>{{ row['reply_tweet_id'] }}</td>
                      <td><span class="badge bg-secondary">{{ row['source'] }}</span></td>
                      <td>{{ row['keyword'] or '-' }}</td>
                      <td>{{ row['replied_at'] }}</td>
                    </tr>
                    {% endfor %}
                  </tbody>
                </table>
              </div>
            {% else %}
              <p class="mb-0 text-muted">No replies recorded yet.</p>
            {% endif %}
          </div>
        </div>
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


CREDENTIALS_TEMPLATE = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Connect Twitter & Gemini - X Bot</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.css" rel="stylesheet">
    <link href="{{ url_for('static', filename='style.css') }}" rel="stylesheet">
  </head>
  <body>
    <div class="mobile-topbar px-3 py-2 d-md-none">
      <button class="btn btn-outline-light btn-sm" type="button" onclick="toggleSidebar()">
        <i class="bi bi-list" id="navToggleIcon"></i>
      </button>
      <span class="fw-semibold">X Bot</span>
    </div>
    <div class="d-flex">
      <nav class="sidebar bg-dark text-white p-3">
        <h5 class="mb-4">X Bot</h5>
        <ul class="nav nav-pills flex-column mb-auto">
          <li class="nav-item">
            <a href="{{ url_for('dashboard_overview') }}" class="nav-link text-white">
              <i class="bi bi-speedometer2 me-2"></i> Overview
            </a>
          </li>
          <li>
            <a href="{{ url_for('settings_keywords') }}" class="nav-link text-white">
              <i class="bi bi-filter-circle me-2"></i> Keywords & Filters
            </a>
          </li>
          <li>
            <a href="{{ url_for('settings_credentials') }}" class="nav-link text-white active">
              <i class="bi bi-person-badge me-2"></i> Credentials
            </a>
          </li>
          <li>
            <a href="{{ url_for('settings_automation') }}" class="nav-link text-white">
              <i class="bi bi-gear me-2"></i> Automation
            </a>
          </li>
          <li>
            <a href="{{ url_for('settings_logs') }}" class="nav-link text-white">
              <i class="bi bi-journal-text me-2"></i> Logs
            </a>
          </li>
        </ul>
        <div class="mt-auto pt-3 border-top">
          <div class="small text-muted mb-2">{{ session.get('user_email', 'User') }}</div>
          <a href="{{ url_for('logout') }}" class="btn btn-outline-light btn-sm w-100">
            <i class="bi bi-box-arrow-right me-1"></i> Logout
          </a>
        </div>
      </nav>
      <main class="flex-grow-1 p-4">
        <div class="page-narrow">
          {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
              {% for category, message in messages %}
                <div class="alert alert-{{ category }} alert-dismissible fade show" role="alert">
                  {{ message }}
                  <button type="button" class="btn-close" data-bs-dismiss="alert" aria-label="Close"></button>
                </div>
              {% endfor %}
            {% endif %}
          {% endwith %}

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
              <h5>Twitter / X API</h5>
            </div>
            <div class="col-md-6">
              <label class="form-label">Consumer Key (API Key)</label>
              <input type="password" name="consumer_key" class="form-control" value="{% if x_api and 'consumer_key' in x_api %}{{ x_api['consumer_key'] }}{% endif %}">
            </div>
            <div class="col-md-6">
              <label class="form-label">Consumer Secret (API Secret)</label>
              <input type="password" name="consumer_secret" class="form-control" value="{% if x_api and 'consumer_secret' in x_api %}{{ x_api['consumer_secret'] }}{% endif %}">
            </div>
            <div class="col-md-6">
              <label class="form-label">Access Token</label>
              <input type="password" name="access_token" class="form-control" value="{% if x_api and 'access_token' in x_api %}{{ x_api['access_token'] }}{% endif %}">
            </div>
            <div class="col-md-6">
              <label class="form-label">Access Token Secret</label>
              <input type="password" name="access_token_secret" class="form-control" value="{% if x_api and 'access_token_secret' in x_api %}{{ x_api['access_token_secret'] }}{% endif %}">
            </div>
            <div class="col-md-6">
              <label class="form-label">Bearer Token (optional)</label>
              <input type="password" name="bearer_token" class="form-control" value="{% if x_api and 'bearer_token' in x_api %}{{ x_api['bearer_token'] }}{% endif %}">
            </div>

            <div class="col-12 mt-4">
              <h5>Google Gemini (Optional)</h5>
              <p class="text-muted small mb-2">Get your API key from <a href="https://makersuite.google.com/app/apikey" target="_blank" class="text-info">Google AI Studio</a></p>
            </div>
            <div class="col-md-8">
              <label class="form-label">Gemini API Key</label>
              <input type="password" name="gemini_api_key" class="form-control" value="{% if gemini and 'api_key' in gemini %}{{ gemini['api_key'] }}{% endif %}">
            </div>
            <div class="col-md-4 d-flex align-items-end">
              <div class="form-check">
                <input class="form-check-input" type="checkbox" name="gemini_enabled" id="gemini_enabled" {% if gemini.get('enabled') %}checked{% endif %}>
                <label class="form-check-label" for="gemini_enabled">
                  Enable AI replies
                </label>
              </div>
            </div>

            <div class="col-12 mt-4">
              <button type="submit" class="btn btn-primary">
                <i class="bi bi-link-45deg me-1"></i> Save & Test Connection
              </button>
            </div>
          </form>
        </div>
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


LANDING_PAGE_TEMPLATE = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Auto-Reply X Bot - Automated Twitter Engagement</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.css" rel="stylesheet">
    <style>
      body {
        background: linear-gradient(135deg, #0b1020 0%, #1e293b 100%);
        color: #f5f5f5;
        font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        min-height: 100vh;
      }
      .hero-section {
        padding: 80px 0;
        text-align: center;
      }
      .hero-title {
        font-size: 3.5rem;
        font-weight: 700;
        margin-bottom: 1.5rem;
        background: linear-gradient(135deg, #6366f1, #22d3ee);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        background-clip: text;
      }
      .hero-subtitle {
        font-size: 1.25rem;
        color: #94a3b8;
        margin-bottom: 2.5rem;
        max-width: 600px;
        margin-left: auto;
        margin-right: auto;
      }
      .feature-card {
        background: rgba(15, 23, 42, 0.9);
        border: 1px solid rgba(148, 163, 184, 0.2);
        border-radius: 1rem;
        padding: 2rem;
        margin-bottom: 2rem;
        transition: transform 0.3s, border-color 0.3s;
        color: #e5e7eb;
      }
      .feature-card:hover {
        transform: translateY(-5px);
        border-color: #6366f1;
      }
      .feature-card h4 {
        color: #f5f5f5;
        margin-bottom: 1rem;
      }
      .feature-card p {
        color: #94a3b8;
        margin-bottom: 0;
      }
      .feature-icon {
        font-size: 3rem;
        color: #6366f1;
        margin-bottom: 1rem;
      }
      .btn-primary-custom {
        background: linear-gradient(135deg, #6366f1, #22d3ee);
        border: none;
        padding: 0.75rem 2rem;
        font-size: 1.1rem;
        font-weight: 600;
        border-radius: 0.5rem;
        transition: transform 0.2s;
        color: white;
      }
      .btn-primary-custom:hover {
        transform: scale(1.05);
        color: white;
      }
      .navbar {
        background: rgba(15, 23, 42, 0.9);
        backdrop-filter: blur(10px);
      }
    </style>
  </head>
  <body>
    <nav class="navbar navbar-expand-lg navbar-dark">
      <div class="container">
        <a class="navbar-brand fw-bold" href="/">
          <i class="bi bi-twitter me-2"></i>X Bot
        </a>
        <div class="ms-auto">
          <a href="{{ url_for('login') }}" class="btn btn-primary-custom">Login</a>
        </div>
      </div>
    </nav>

    <div class="hero-section">
      <div class="container">
        <h1 class="hero-title">Auto-Reply X Bot</h1>
        <p class="hero-subtitle">
          Automate your Twitter engagement with intelligent replies. 
          Connect with your audience 24/7 using AI-powered responses.
        </p>
        <a href="{{ url_for('login') }}" class="btn btn-primary-custom btn-lg">
          <i class="bi bi-box-arrow-in-right me-2"></i>Login to Dashboard
        </a>
      </div>
    </div>

    <div class="container py-5" id="features">
      <div class="row">
        <div class="col-md-4">
          <div class="feature-card text-center">
            <i class="bi bi-robot feature-icon"></i>
            <h4>AI-Powered Replies</h4>
            <p>
              Generate thoughtful, contextual replies using Google Gemini AI. 
              Your bot will engage naturally with your audience.
            </p>
          </div>
        </div>
        <div class="col-md-4">
          <div class="feature-card text-center">
            <i class="bi bi-funnel feature-icon"></i>
            <h4>Smart Filtering</h4>
            <p>
              Set keywords and filters to target the right conversations. 
              Control who you engage with and when.
            </p>
          </div>
        </div>
        <div class="col-md-4">
          <div class="feature-card text-center">
            <i class="bi bi-clock-history feature-icon"></i>
            <h4>Automated Scheduling</h4>
            <p>
              Run your bot on a schedule. Set it and forget it - 
              your bot will engage automatically at optimal times.
            </p>
          </div>
        </div>
      </div>
    </div>

    <footer class="mt-5 py-5" style="background: rgba(15, 23, 42, 0.9); border-top: 1px solid rgba(148, 163, 184, 0.2);">
      <div class="container">
        <div class="row">
          <div class="col-md-4 mb-4 mb-md-0">
            <h5 class="text-light mb-3">
              <i class="bi bi-twitter me-2"></i>X Bot
            </h5>
            <p class="text-muted" style="color: #94a3b8 !important;">
              Automate your Twitter engagement with intelligent AI-powered replies. 
              Connect with your audience 24/7.
            </p>
          </div>
          <div class="col-md-4 mb-4 mb-md-0">
            <h5 class="text-light mb-3">Features</h5>
            <ul class="list-unstyled">
              <li><a href="#features" class="text-muted text-decoration-none" style="color: #94a3b8 !important;">AI-Powered Replies</a></li>
              <li><a href="#features" class="text-muted text-decoration-none" style="color: #94a3b8 !important;">Smart Filtering</a></li>
              <li><a href="#features" class="text-muted text-decoration-none" style="color: #94a3b8 !important;">Automated Scheduling</a></li>
            </ul>
          </div>
          <div class="col-md-4">
            <h5 class="text-light mb-3">Access</h5>
            <ul class="list-unstyled">
              <li><a href="{{ url_for('login') }}" class="text-muted text-decoration-none" style="color: #94a3b8 !important;">Login to Dashboard</a></li>
            </ul>
          </div>
        </div>
        <hr class="my-4" style="border-color: rgba(148, 163, 184, 0.2);">
        <div class="text-center">
          <p class="text-muted mb-0" style="color: #94a3b8 !important;">
            &copy; 2024 Auto-Reply X Bot. Built for automated Twitter engagement.
          </p>
          <p class="text-muted mb-0 mt-2" style="color: #94a3b8 !important;">
            A project by <a href="https://x.com/ohakwengr" target="_blank" rel="noopener noreferrer" class="text-decoration-none" style="color: #6366f1 !important;">Ogbonna Ohakwe</a>
          </p>
        </div>
      </div>
    </footer>

    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
  </body>
</html>
"""


LOGIN_TEMPLATE = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Login - X Bot</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.css" rel="stylesheet">
    <link href="{{ url_for('static', filename='style.css') }}" rel="stylesheet">
    <style>
      body {
        background: radial-gradient(circle at top left, #1e293b, #020617);
        min-height: 100vh;
        display: flex;
        align-items: center;
        justify-content: center;
      }
      .login-card {
        background: rgba(15, 23, 42, 0.9);
        border: 1px solid rgba(148, 163, 184, 0.2);
        border-radius: 1rem;
        padding: 2.5rem;
        max-width: 400px;
        width: 100%;
      }
      .login-title {
        color: #f5f5f5;
        margin-bottom: 1.5rem;
      }
    </style>
  </head>
  <body>
    <div class="login-card">
      <h2 class="login-title text-center mb-4">
        <i class="bi bi-twitter me-2"></i>X Bot Login
      </h2>
      
      {% with messages = get_flashed_messages(with_categories=true) %}
        {% if messages %}
          {% for category, message in messages %}
            <div class="alert alert-{{ category }} alert-dismissible fade show" role="alert">
              {{ message }}
              <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
            </div>
          {% endfor %}
        {% endif %}
      {% endwith %}

      <form method="post">
        <div class="mb-3">
          <label class="form-label text-light">Email Address</label>
          <input type="email" name="email" class="form-control" required autofocus placeholder="Enter your email">
          <small class="text-muted">Only authorized email addresses can access the dashboard.</small>
        </div>
        <button type="submit" class="btn btn-primary w-100 mb-3">
          <i class="bi bi-box-arrow-in-right me-2"></i>Login
        </button>
        <div class="text-center">
          <a href="{{ url_for('landing') }}" class="text-muted text-decoration-none">
            <i class="bi bi-arrow-left me-1"></i>Back to Home
          </a>
        </div>
      </form>
    </div>
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
  </body>
</html>
"""


AUTOMATION_TEMPLATE = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Automation Settings - X Bot</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.css" rel="stylesheet">
    <link href="{{ url_for('static', filename='style.css') }}" rel="stylesheet">
  </head>
  <body>
    <div class="mobile-topbar px-3 py-2 d-md-none">
      <button class="btn btn-outline-light btn-sm" type="button" onclick="toggleSidebar()">
        <i class="bi bi-list" id="navToggleIcon"></i>
      </button>
      <span class="fw-semibold">X Bot</span>
    </div>
    <div class="d-flex">
      <nav class="sidebar bg-dark text-white p-3">
        <h5 class="mb-4">X Bot</h5>
        <ul class="nav nav-pills flex-column mb-auto">
          <li class="nav-item">
            <a href="{{ url_for('dashboard_overview') }}" class="nav-link text-white">
              <i class="bi bi-speedometer2 me-2"></i> Overview
            </a>
          </li>
          <li>
            <a href="{{ url_for('settings_keywords') }}" class="nav-link text-white">
              <i class="bi bi-filter-circle me-2"></i> Keywords & Filters
            </a>
          </li>
          <li>
            <a href="{{ url_for('settings_credentials') }}" class="nav-link text-white">
              <i class="bi bi-person-badge me-2"></i> Credentials
            </a>
          </li>
          <li>
            <a href="{{ url_for('settings_automation') }}" class="nav-link text-white active">
              <i class="bi bi-gear me-2"></i> Automation
            </a>
          </li>
          <li>
            <a href="{{ url_for('settings_logs') }}" class="nav-link text-white">
              <i class="bi bi-journal-text me-2"></i> Logs
            </a>
          </li>
        </ul>
        <div class="mt-auto pt-3 border-top">
          <div class="small text-muted mb-2">{{ session.get('user_email', 'User') }}</div>
          <a href="{{ url_for('logout') }}" class="btn btn-outline-light btn-sm w-100">
            <i class="bi bi-box-arrow-right me-1"></i> Logout
          </a>
        </div>
      </nav>
      <main class="flex-grow-1 p-4">
        <div class="page-narrow">
          {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
              {% for category, message in messages %}
                <div class="alert alert-{{ category }} alert-dismissible fade show" role="alert">
                  {{ message }}
                  <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
                </div>
              {% endfor %}
            {% endif %}
          {% endwith %}

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
      
      // Auto-refresh status every 30 seconds if bot is running
      {% if bot_running %}
      setInterval(function() {
        location.reload();
      }, 30000);
      {% endif %}
    </script>
  </body>
</html>
"""


KEYWORDS_TEMPLATE = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Keywords & Filters - X Bot</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.css" rel="stylesheet">
    <link href="{{ url_for('static', filename='style.css') }}" rel="stylesheet">
  </head>
  <body>
    <div class="mobile-topbar px-3 py-2 d-md-none">
      <button class="btn btn-outline-light btn-sm" type="button" onclick="toggleSidebar()">
        <i class="bi bi-list"></i>
      </button>
      <span class="fw-semibold">X Bot</span>
    </div>
    <div class="d-flex">
      <nav class="sidebar bg-dark text-white p-3">
        <h5 class="mb-4">X Bot</h5>
        <ul class="nav nav-pills flex-column mb-auto">
          <li class="nav-item">
            <a href="{{ url_for('dashboard_overview') }}" class="nav-link text-white">
              <i class="bi bi-speedometer2 me-2"></i> Overview
            </a>
          </li>
          <li>
            <a href="{{ url_for('settings_keywords') }}" class="nav-link text-white active">
              <i class="bi bi-filter-circle me-2"></i> Keywords & Filters
            </a>
          </li>
          <li>
            <a href="{{ url_for('settings_credentials') }}" class="nav-link text-white">
              <i class="bi bi-person-badge me-2"></i> Credentials
            </a>
          </li>
          <li>
            <a href="{{ url_for('settings_automation') }}" class="nav-link text-white">
              <i class="bi bi-gear me-2"></i> Automation
            </a>
          </li>
          <li>
            <a href="{{ url_for('settings_logs') }}" class="nav-link text-white">
              <i class="bi bi-journal-text me-2"></i> Logs
            </a>
          </li>
        </ul>
        <div class="mt-auto pt-3 border-top">
          <div class="small text-muted mb-2">{{ session.get('user_email', 'User') }}</div>
          <a href="{{ url_for('logout') }}" class="btn btn-outline-light btn-sm w-100">
            <i class="bi bi-box-arrow-right me-1"></i> Logout
          </a>
        </div>
      </nav>
      <main class="flex-grow-1 p-4">
        <div class="page-narrow-wide">
          {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
              {% for category, message in messages %}
                <div class="alert alert-{{ category }} alert-dismissible fade show" role="alert">
                  {{ message }}
                  <button type="button" class="btn-close" data-bs-dismiss="alert" aria-label="Close"></button>
                </div>
              {% endfor %}
            {% endif %}
          {% endwith %}

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
      </main>
    </div>
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
    <script>
      function toggleSidebar() {
        var sidebar = document.querySelector('.sidebar');
        if (sidebar) {
          sidebar.classList.toggle('sidebar-open');
        }
      }
    </script>
  </body>
</html>
"""


LOGS_TEMPLATE = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Logs - X Bot</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.css" rel="stylesheet">
    <link href="{{ url_for('static', filename='style.css') }}" rel="stylesheet">
  </head>
  <body>
    <div class="mobile-topbar px-3 py-2 d-md-none">
      <button class="btn btn-outline-light btn-sm" type="button" onclick="toggleSidebar()">
        <i class="bi bi-list" id="navToggleIcon"></i>
      </button>
      <span class="fw-semibold">X Bot</span>
    </div>
    <div class="d-flex">
      <nav class="sidebar bg-dark text-white p-3">
        <h5 class="mb-4">X Bot</h5>
        <ul class="nav nav-pills flex-column mb-auto">
          <li class="nav-item">
            <a href="{{ url_for('dashboard_overview') }}" class="nav-link text-white">
              <i class="bi bi-speedometer2 me-2"></i> Overview
            </a>
          </li>
          <li>
            <a href="{{ url_for('settings_keywords') }}" class="nav-link text-white">
              <i class="bi bi-filter-circle me-2"></i> Keywords & Filters
            </a>
          </li>
          <li>
            <a href="{{ url_for('settings_credentials') }}" class="nav-link text-white">
              <i class="bi bi-person-badge me-2"></i> Credentials
            </a>
          </li>
          <li>
            <a href="{{ url_for('settings_automation') }}" class="nav-link text-white">
              <i class="bi bi-gear me-2"></i> Automation
            </a>
          </li>
          <li>
            <a href="{{ url_for('settings_logs') }}" class="nav-link text-white active">
              <i class="bi bi-journal-text me-2"></i> Logs
            </a>
          </li>
        </ul>
        <div class="mt-auto pt-3 border-top">
          <div class="small text-muted mb-2">{{ session.get('user_email', 'User') }}</div>
          <a href="{{ url_for('logout') }}" class="btn btn-outline-light btn-sm w-100">
            <i class="bi bi-box-arrow-right me-1"></i> Logout
          </a>
        </div>
      </nav>
      <main class="flex-grow-1 p-4">
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


# Initialize auth database on startup
init_auth_db()


@app.route("/")
def landing():
    """Landing page - public homepage."""
    return render_template_string(LANDING_PAGE_TEMPLATE)


@app.route("/login", methods=["GET", "POST"])
def login():
    """Login page - email only, no password required."""
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        
        if not email:
            flash("Please enter your email address.", "danger")
            return render_template_string(LOGIN_TEMPLATE)
        
        # Only allow the authorized email
        if email != ALLOWED_EMAIL:
            flash("Access denied. Only authorized email addresses can login.", "danger")
            return render_template_string(LOGIN_TEMPLATE)
        
        # Login successful - no password check needed
        session['user_id'] = 1  # Simple session ID
        session['user_email'] = email
        flash("Login successful!", "success")
        return redirect(url_for('dashboard_overview'))
    
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
    return render_template_string(
        LOGS_TEMPLATE,
        log_content=log_content,
        line_count=line_count,
        log_error=log_error,
    )


@app.route("/dashboard")
@login_required
def dashboard_overview():
    """Overview page: basic stats from database and setup status."""
    total_replied = 0
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
            return render_template_string(
                HOME_TEMPLATE,
                total_replied=0,
                total_tweets_posted=0,
                total_quote_retweets=0,
                last_reply=None,
                recent_replies=[],
                has_twitter=has_twitter,
                twitter_status=twitter_status,
                twitter_message=twitter_message,
                gemini_status=gemini_status,
            )
        
        # Now query the database
        try:
            conn = get_db_connection()
            cur = conn.conn.cursor()
        except Exception as conn_error:
            logger.error(f"Failed to get database connection: {conn_error}")
            # Return with defaults if connection fails
            return render_template_string(
                HOME_TEMPLATE,
                total_replied=0,
                total_tweets_posted=0,
                total_quote_retweets=0,
                last_reply=None,
                recent_replies=[],
                has_twitter=has_twitter,
                twitter_status=twitter_status,
                twitter_message=twitter_message,
                gemini_status=gemini_status,
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
            # PyMySQL with DictCursor already returns dicts
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

    return render_template_string(
        HOME_TEMPLATE,
        total_replied=total_replied,
        total_tweets_posted=total_tweets_posted,
        total_quote_retweets=total_quote_retweets,
        last_reply=last_reply,
        recent_replies=recent_replies,
        has_twitter=has_twitter,
        twitter_status=twitter_status,
        twitter_message=twitter_message,
        gemini_status=gemini_status,
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
        data["x_api"]["consumer_key"] = request.form.get(
            "consumer_key", ""
        ).strip()
        data["x_api"]["consumer_secret"] = request.form.get(
            "consumer_secret", ""
        ).strip()
        data["x_api"]["access_token"] = request.form.get(
            "access_token", ""
        ).strip()
        data["x_api"]["access_token_secret"] = request.form.get(
            "access_token_secret", ""
        ).strip()
        data["x_api"]["bearer_token"] = request.form.get(
            "bearer_token", ""
        ).strip()

        data.setdefault("gemini", {})
        data["gemini"]["api_key"] = request.form.get(
            "gemini_api_key", ""
        ).strip()
        data["gemini"]["enabled"] = bool(request.form.get("gemini_enabled"))
        data["gemini"]["model"] = request.form.get("gemini_model", "gemini-pro").strip()
        data["gemini"]["temperature"] = float(request.form.get("gemini_temperature", 0.7))

        Path(config.config_path).write_text(
            json.dumps(data, indent=2), encoding="utf-8"
        )

        try:
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
    
    # Check if Twitter is connected
    is_connected = False
    connected_user = None
    if config:
        try:
            xapi = XAPI(config.get_x_api_credentials())
            connected_user = xapi.get_user_info()
            if connected_user:
                is_connected = True
        except:
            is_connected = False

    return render_template_string(
        CREDENTIALS_TEMPLATE,
        config_error=error,
        x_api=x_api,
        gemini=gemini,
        is_connected=is_connected,
        connected_user=connected_user,
    )


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

            return render_template_string(
                KEYWORDS_TEMPLATE,
                config_error=error,
                keywords="\n".join(keywords),
                reply_settings=reply_settings,
                filters=filters,
            )

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

    return render_template_string(
        KEYWORDS_TEMPLATE,
        config_error=error,
        keywords="\n".join(keywords),
        reply_settings=reply_settings,
        filters=filters,
    )


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
    
    if config:
        schedule = config.get_schedule_config()
        reply_settings = config.get_reply_settings()
        tweet_settings = config.get_tweet_settings()
    
    with bot_status_lock:
        bot_running = bot_status["running"]
        last_run = bot_status.get("last_run")
        next_run = bot_status.get("next_run")
    
    preview = session.pop("last_preview", None)
    
    return render_template_string(
        AUTOMATION_TEMPLATE,
        config_error=error,
        schedule=schedule,
        reply_settings=reply_settings,
        tweet_settings=tweet_settings,
        bot_running=bot_running,
        last_run=last_run,
        next_run=next_run,
        preview=preview,
    )


if __name__ == "__main__":
    # Get port from environment variable (for production) or use default
    port = int(os.getenv("PORT", 5001))
    # Only enable debug in development
    debug = os.getenv("FLASK_DEBUG", "False").lower() == "true"
    # Use 0.0.0.0 to accept connections from all interfaces (needed for production)
    app.run(host="0.0.0.0", port=port, debug=debug)


