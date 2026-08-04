#!/usr/bin/env python3
"""Interactive setup script for Auto-Reply X Bot configuration."""
import json
from pathlib import Path


def print_header():
    print("=" * 60)
    print("Auto-Reply X Bot - Configuration Setup")
    print("=" * 60)
    print()


def get_input(prompt, default=None, required=True, secret=False):
    if default:
        prompt_text = f"{prompt} [{default}]: "
    else:
        prompt_text = f"{prompt}: "

    if secret:
        import getpass

        value = getpass.getpass(prompt_text)
    else:
        value = input(prompt_text).strip()

    if not value and default:
        return default
    if not value and required:
        print("This field is required. Please enter a value.")
        return get_input(prompt, default, required, secret)
    return value if value else ""


def get_bool_input(prompt, default=True):
    default_text = "Y/n" if default else "y/N"
    response = input(f"{prompt} [{default_text}]: ").strip().lower()
    if not response:
        return default
    return response in ["y", "yes", "true", "1"]


def get_int_input(prompt, default=None, min_val=None):
    while True:
        value = get_input(prompt, str(default) if default else None, required=(default is None))
        try:
            int_value = int(value)
            if min_val is not None and int_value < min_val:
                print(f"Value must be at least {min_val}.")
                continue
            return int_value
        except ValueError:
            print("Please enter a valid number.")


def setup_config():
    config_path = Path("config.json")

    if config_path.exists():
        response = input("config.json already exists. Overwrite? [y/N]: ").strip().lower()
        if response not in ["y", "yes"]:
            print("Setup cancelled.")
            return

    config = {}

    print("\n--- X (Twitter) API Credentials ---")
    print("Get credentials from: https://developer.twitter.com/en/portal/dashboard")
    print()

    config["x_api"] = {
        "consumer_key": get_input("Consumer Key (API Key)", required=True, secret=True),
        "consumer_secret": get_input("Consumer Secret (API Secret)", required=True, secret=True),
        "access_token": get_input("Access Token", required=True, secret=True),
        "access_token_secret": get_input("Access Token Secret", required=True, secret=True),
        "bearer_token": get_input("Bearer Token (optional)", required=False, secret=True),
    }

    print("\n--- AI Provider (Gemini / OpenAI / Anthropic) ---")
    print("Pick a default provider. Others can be added later in the dashboard.")
    print()

    provider = get_input("Default provider (gemini/openai/anthropic)", "gemini").lower()
    if provider not in ("gemini", "openai", "anthropic"):
        provider = "gemini"

    config["ai"] = {
        "provider": provider,
        "prompt_profile": get_input("Niche profile name", "default"),
        "temperature": 0.7,
    }
    config["gemini"] = {"enabled": False, "api_key": "", "model": "gemini-1.5-flash"}
    config["openai"] = {"enabled": False, "api_key": "", "model": "gpt-4o-mini"}
    config["anthropic"] = {
        "enabled": False,
        "api_key": "",
        "model": "claude-3-5-haiku-latest",
    }

    use_ai = get_bool_input(f"Enable {provider} now?", default=True)
    if use_ai:
        key = get_input(f"{provider} API Key", required=True, secret=True)
        config[provider]["api_key"] = key
        config[provider]["enabled"] = True

    config["man_in_the_loop"] = {
        "enabled": get_bool_input("Enable man-in-the-loop (approve drafts before post)?", True)
    }

    print("\n--- Schedule Settings ---")
    config["schedule"] = {
        "morning_time": get_input("Morning run time (HH:MM)", "09:00"),
        "evening_time": get_input("Evening run time (HH:MM)", "18:00"),
        "morning_start": "09:00",
        "morning_end": "11:00",
        "evening_start": "18:00",
        "evening_end": "20:00",
        "timezone": get_input("Timezone", "UTC"),
    }

    print("\n--- Reply Settings ---")
    config["reply_settings"] = {
        "max_replies_per_run": get_int_input("Max replies per run", 10, min_val=1),
        "delay_minutes_min": get_int_input("Minimum delay between replies (minutes)", 5, min_val=1),
        "delay_minutes_max": get_int_input("Maximum delay between replies (minutes)", 15, min_val=1),
        "timeline_count": get_int_input("Number of timeline tweets to fetch", 50, min_val=1),
        "keyword_search_count": get_int_input("Number of tweets per keyword to fetch", 20, min_val=1),
    }

    print("\n--- Keywords ---")
    print("Enter keywords/topics (one per line, empty line to finish):")
    keywords = []
    while True:
        keyword = input(f"Keyword {len(keywords) + 1}: ").strip()
        if not keyword:
            break
        keywords.append(keyword)
    if not keywords:
        keywords = ["web3", "blockchain"]
        print(f"No keywords entered. Using defaults: {keywords}")
    config["keywords"] = keywords

    print("\n--- Reply Templates (fallback if AI offline) ---")
    templates = []
    while True:
        template = input(f"Template {len(templates) + 1}: ").strip()
        if not template:
            break
        templates.append(template)
    if not templates:
        templates = [
            "That's an interesting perspective! {context}",
            "Thanks for sharing this. I've been thinking about {topic} recently.",
            "Great point! Have you considered {related_idea}?",
            "This resonates with me. {personal_insight}",
        ]
        print("No templates entered. Using defaults.")
    config["reply_templates"] = templates

    print("\n--- Filters ---")
    config["filters"] = {
        "exclude_retweets": get_bool_input("Exclude retweets?", True),
        "exclude_own_tweets": get_bool_input("Exclude your own tweets?", True),
        "exclude_replied_tweets": get_bool_input("Exclude already-replied tweets?", True),
        "min_followers": get_int_input("Minimum followers (0 to disable)", 0, min_val=0),
    }

    config["tweet_settings"] = {
        "enabled": True,
        "tweets_per_run": 1,
        "threads_per_run": 0,
        "thread_tweet_count": 3,
    }

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

    print(f"\nConfiguration saved to {config_path.absolute()}")
    print("\nAlso create a .env from env.example with:")
    print("  ADMIN_EMAIL / ADMIN_PASSWORD")
    print("  DB_* / DATABASE_URL PostgreSQL settings")
    print("  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (for MITL)")
    print("  AI provider API keys")
    print("\nNext steps:")
    print("1. pip install -r requirements.txt")
    print("2. python dashboard.py  (login, review drafts, AI settings)")
    print("3. Or run once: python main.py --run-once")


if __name__ == "__main__":
    print_header()
    setup_config()
