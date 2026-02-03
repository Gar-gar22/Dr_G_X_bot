#!/usr/bin/env python3
"""Main entry point for Auto-Reply X Bot."""
import logging
import sys
import argparse
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent))

from src.config import Config
from src.bot import AutoReplyBot
from src.scheduler import BotScheduler


def setup_logging(log_level: str = "INFO") -> None:
    """Configure logging."""
    logging.basicConfig(
        level=getattr(logging, log_level.upper()),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler('bot.log'),
            logging.StreamHandler(sys.stdout)
        ]
    )


def main():
    """Main function."""
    parser = argparse.ArgumentParser(description="Auto-Reply X Bot - Automated Twitter/X engagement bot")
    parser.add_argument(
        '--config',
        type=str,
        default='config.json',
        help='Path to configuration file (default: config.json)'
    )
    parser.add_argument(
        '--run-once',
        action='store_true',
        help='Run bot once and exit (no scheduling)'
    )
    parser.add_argument(
        '--log-level',
        type=str,
        default='INFO',
        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
        help='Logging level (default: INFO)'
    )
    
    args = parser.parse_args()
    
    # Setup logging
    setup_logging(args.log_level)
    logger = logging.getLogger(__name__)
    
    try:
        # Load configuration
        logger.info("Loading configuration...")
        config = Config(args.config)
        
        # Initialize bot
        logger.info("Initializing bot...")
        bot = AutoReplyBot(config)
        
        if args.run_once:
            # Run once and exit
            logger.info("Running bot once (--run-once mode)...")
            stats = bot.run()
            logger.info(f"Run completed. Statistics: {stats}")
            bot.close()
        else:
            # Run with scheduler
            logger.info("Starting bot with scheduler...")
            scheduler = BotScheduler(
                bot.run,
                bot.post_tweet,
                bot.post_thread_tweet,
                config.get_schedule_config(),
                config.get_tweet_settings()
            )
            scheduler.setup_schedule()
            
            try:
                scheduler.run_continuously()
            except KeyboardInterrupt:
                logger.info("Received interrupt signal. Shutting down...")
                scheduler.stop()
                bot.close()
                sys.exit(0)
    
    except FileNotFoundError as e:
        logger.error(f"Configuration error: {e}")
        logger.error("Please create config.json or run python setup.py to configure credentials.")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()

