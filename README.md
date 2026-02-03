# Auto-Reply X Bot

An automated Twitter/X engagement bot that generates and posts thoughtful replies to tweets in your home timeline and those matching specified keywords. The bot runs on a scheduled basis (twice daily by default) with intelligent batching to mimic natural human interaction.

## Features

- **Timeline Engagement**: Automatically replies to tweets in your home timeline
- **Keyword-Based Replies**: Searches for tweets matching your keywords and engages with them
- **Smart Scheduling**: Runs twice daily (morning and evening) with configurable times
- **Natural Behavior**: Delays between replies (30-40 minutes) to avoid spam detection
- **Duplicate Prevention**: Tracks replied tweets to avoid duplicate responses
- **AI-Powered Replies** (Optional): Uses OpenAI API for more natural, contextual replies
- **Template-Based Replies**: Fallback to customizable templates if AI is not enabled
- **Comprehensive Logging**: Detailed logs for monitoring and debugging

## Prerequisites

- Python 3.8 or higher
- X (Twitter) Developer Account with API access
- (Optional) OpenAI API key for AI-generated replies

## Installation

1. **Clone or download this repository**

2. **Install dependencies**:
```bash
pip install -r requirements.txt
```

3. **Get X (Twitter) API Credentials**:
   
   You need a Twitter/X Developer Account with API access:
   
   - Sign up at [Twitter Developer Portal](https://developer.twitter.com/en/portal/dashboard)
   - Create a new App/Project
   - Generate API keys and tokens:
     - **Consumer Key** (also called API Key)
     - **Consumer Secret** (also called API Secret)
     - **Access Token**
     - **Access Token Secret**
     - **Bearer Token** (optional but recommended)
   - **Important**: Enable Read and Write permissions for your app
   - Free tier allows up to 1,500 posts per month

4. **Set up configuration** (choose one method):
   
   **Method A: Interactive Setup (Recommended)**
   ```bash
   python setup.py
   ```
   This will guide you through configuring all settings interactively.
   
   **Method B: Manual Setup**
   - Create `config.json` (e.g. run `python setup.py`)
   - Fill in your X API credentials
   - Configure keywords, schedules, and other settings
   - (Optional) Add OpenAI API key if you want AI-generated replies
   
   **Method C: Environment Variables**
   - Create `.env` file manually (see `env.example` as reference)
   - The bot will automatically load environment variables

5. **Test your configuration**:
   - Run the dashboard to test your credentials: `python dashboard.py`
   - Or run the bot once to test: `python main.py --run-once`

## Configuration

Edit `config.json` to customize:

- **X API Credentials**: Your Twitter/X API keys
- **Schedule Times**: Morning and evening run times (default: 09:00 and 18:00 UTC)
- **Reply Settings**: Max replies per run, delay between replies, etc.
- **Keywords**: List of keywords/topics to search for and engage with
- **Reply Templates**: Custom templates for replies (if not using AI)
- **Filters**: Options to exclude retweets, own tweets, etc.

### Example Configuration

```json
{
  "schedule": {
    "morning_time": "09:00",
    "evening_time": "18:00"
  },
  "reply_settings": {
    "max_replies_per_run": 10,
    "delay_minutes_min": 30,
    "delay_minutes_max": 40
  },
  "keywords": [
    "python programming",
    "AI ethics"
  ],
  "filters": {
    "min_followers": 100
  }
}
```

## Usage

### Run Once (for testing)

Run the bot once and exit:

```bash
python main.py --run-once
```

### Run with Scheduler (default)

Run the bot continuously with scheduled runs:

```bash
python main.py
```

The bot will run at the scheduled times (morning and evening) as configured.

### Command Line Options

- `--config PATH`: Specify a custom configuration file path (default: `config.json`)
- `--run-once`: Run bot once and exit (no scheduling)
- `--log-level LEVEL`: Set logging level (DEBUG, INFO, WARNING, ERROR)

## How It Works

1. **Fetching**: The bot fetches tweets from your home timeline and searches for tweets matching your keywords
2. **Filtering**: Filters out retweets, your own tweets, tweets from users with fewer than minimum followers, and already-replied tweets
3. **Generation**: Generates contextual replies using templates or AI
4. **Posting**: Posts replies with delays (30-40 minutes) between each reply to mimic natural behavior
5. **Tracking**: Records all replied tweets in a local database to prevent duplicates

## X API Requirements

You need a Twitter/X Developer Account with:

- **Read and Write permissions**
- API v2 access (recommended)
- OAuth 1.0a credentials (Consumer Key/Secret, Access Token/Secret)
- Bearer Token (optional, for read operations)

Free tier allows up to 1,500 posts per month. Consider upgrading if you need more.

## OpenAI Integration (Optional)

To enable AI-generated replies:

1. Add your OpenAI API key to `config.json` or `.env`
2. Set `"enabled": true` in the `openai` section of `config.json`
3. Customize the model and temperature as needed

Cost estimate: ~$0.02 per 1,000 tokens (very affordable for this use case).

## Safety and Best Practices

- **Start Small**: Begin with low `max_replies_per_run` (e.g., 5) to test
- **Review Replies**: Monitor your bot's replies initially to ensure quality
- **Respect Rate Limits**: The bot uses `wait_on_rate_limit=True` to handle rate limits automatically
- **Add Value**: Ensure your reply templates/prompts generate valuable, non-spammy responses
- **Monitor Logs**: Check `bot.log` regularly for errors or issues
- **Test Account**: Consider testing with a secondary account first

## Troubleshooting

### Authentication Errors

- Verify your API credentials are correct
- Ensure your X Developer account has Read and Write permissions
- Check that your API keys are not expired

### Rate Limit Errors

- The bot automatically waits on rate limits, but you may need to reduce `max_replies_per_run`
- Consider upgrading your X API tier if you hit limits frequently

### No Replies Generated

- Check that your keywords are returning results (try searching manually on X)
- Verify your filters aren't too restrictive
- Check logs for specific error messages

### Database Errors

- Ensure the bot has write permissions in the directory
- Delete `bot_state.db` to reset (warning: will lose track of replied tweets)

## Project Structure

```
tweetpy/
├── src/
│   ├── __init__.py
│   ├── config.py          # Configuration management
│   ├── database.py        # SQLite database for tracking
│   ├── x_api.py           # X/Twitter API integration
│   ├── reply_generator.py # Reply generation logic
│   ├── bot.py             # Main bot orchestrator
│   └── scheduler.py       # Scheduling system
├── main.py                # Entry point
├── setup.py               # Interactive configuration setup (RECOMMENDED)
├── env.example            # Environment variables template
├── requirements.txt       # Python dependencies
├── README.md             # This file
└── bot.log               # Log file (generated at runtime)
```

## License

This is a personal project. Use at your own risk. Ensure compliance with X/Twitter's Terms of Service and API policies.

## Disclaimer

This bot is designed for personal use to enhance engagement on X/Twitter. Use responsibly and in compliance with X's automation rules. The authors are not responsible for any account suspensions or violations of platform policies.

## Contributing

This is a personal project, but suggestions and improvements are welcome!

## Support

For issues or questions:

1. Check the logs in `bot.log`
2. Review the configuration in `config.json`
3. Verify your API credentials and permissions
4. Consult X/Twitter API documentation for API-related issues

---

**Version**: 1.0.0  
**Last Updated**: January 2026