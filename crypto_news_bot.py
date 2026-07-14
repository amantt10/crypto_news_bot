"""
Crypto News Channel Forwarder Bot
----------------------------------
Listens for new posts in your source channel, cleans them up (removes a
configurable "tag" pattern), translates them for your Spanish and Indonesian
channels, and posts the results to all three destination channels.

All secrets and channel IDs are read from environment variables (never
hardcoded here) — set them in Render's dashboard under Environment:

    BOT_TOKEN               - your bot token from BotFather
    SOURCE_CHANNEL_ID        - numeric chat ID of the source channel
    ENGLISH_CHANNEL_ID       - numeric chat ID of the English destination
    SPANISH_CHANNEL_ID       - numeric chat ID of the Spanish destination
    INDONESIAN_CHANNEL_ID    - numeric chat ID of the Indonesian destination
    FOOTER_TEXT (optional)   - text appended to every post, e.g. "— Crypto News"

Requirements:
    pip install -r requirements.txt

Run locally (for testing only):
    BOT_TOKEN=xxx SOURCE_CHANNEL_ID=-100... ENGLISH_CHANNEL_ID=-100... \\
    SPANISH_CHANNEL_ID=-100... INDONESIAN_CHANNEL_ID=-100... python3 crypto_news_bot.py
"""

import logging
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from deep_translator import GoogleTranslator
from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# =========================== CONFIG — READS FROM ENVIRONMENT VARIABLES ===========================
# Set these in Render's dashboard under Environment (or locally via a .env file / export).
# Nothing sensitive is hardcoded in this file.

def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Set it in Render's dashboard (Environment tab) before running."
        )
    return value


def _require_env_int(name: str) -> int:
    return int(_require_env(name))


BOT_TOKEN = _require_env("BOT_TOKEN")

# The source channel the bot reads NEW posts from (numeric chat ID, e.g. -1001234567890)
SOURCE_CHANNEL_ID = _require_env_int("SOURCE_CHANNEL_ID")

# Destination channels. Keys are just labels for your own reference.
DESTINATIONS = {
    "english": {
        "chat_id": _require_env_int("ENGLISH_CHANNEL_ID"),
        "translate_to": None,       # None = no translation, just cleaned text
    },
    "spanish": {
        "chat_id": _require_env_int("SPANISH_CHANNEL_ID"),
        "translate_to": "es",
    },
    "indonesian": {
        "chat_id": _require_env_int("INDONESIAN_CHANNEL_ID"),
        "translate_to": "id",
    },
}

# Patterns to strip out of every post before forwarding.
# Add/edit these regex patterns to match your channel's actual tag/signature.
# Examples included below — delete or adjust as needed.
REMOVE_PATTERNS = [
    r"@CryptoNewsChannel",          # a channel mention/username tag
    r"#\w+",                        # any hashtags
    r"Follow us.*$",                # a promo/signature line (case-insensitive, see flag below)
]

# Optional: text added to the end of every translated post (e.g. credit line).
# Set FOOTER_TEXT env var if you want one; defaults to empty (no footer).
FOOTER = os.environ.get("FOOTER_TEXT", "")

# ============================================================================


def clean_text(text: str) -> str:
    """Remove unwanted tags/patterns from the raw post text."""
    if not text:
        return text
    cleaned = text
    for pattern in REMOVE_PATTERNS:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE | re.MULTILINE)
    # Collapse leftover blank lines / extra whitespace
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


def translate_text(text: str, target_lang: str) -> str:
    """Translate text using Google Translate (free, via deep-translator)."""
    if not text:
        return text
    try:
        return GoogleTranslator(source="auto", target=target_lang).translate(text)
    except Exception as e:
        logger.error(f"Translation failed for target '{target_lang}': {e}")
        return text  # fall back to original text rather than dropping the post


async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.channel_post
    if message is None or message.chat_id != SOURCE_CHANNEL_ID:
        return

    raw_text = message.text or message.caption or ""
    if not raw_text:
        logger.info("Post has no text/caption (media-only?) — skipping text pipeline.")
        return

    cleaned = clean_text(raw_text)

    for label, dest in DESTINATIONS.items():
        try:
            if dest["translate_to"]:
                final_text = translate_text(cleaned, dest["translate_to"])
            else:
                final_text = cleaned

            if FOOTER:
                final_text += FOOTER

            await context.bot.send_message(chat_id=dest["chat_id"], text=final_text)
            logger.info(f"Posted to {label} channel successfully.")
        except Exception as e:
            # One channel failing shouldn't stop the others
            logger.error(f"Failed to post to {label} channel ({dest['chat_id']}): {e}")


class HealthCheckHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler so Render treats this as a 'web service' and an
    external pinger (UptimeRobot / cron-job.org) can keep it awake for free."""

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive")

    def log_message(self, format, *args):
        pass  # silence default request logging


def start_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    logger.info(f"Health check server listening on port {port}")
    server.serve_forever()


def main() -> None:
    # Run the tiny HTTP server in a background thread (for Render free tier)
    threading.Thread(target=start_health_server, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, handle_channel_post))
    logger.info("Bot started. Listening for new posts...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
