"""
Crypto News Channel Forwarder Bot (multi-language, multi-channel edition)
--------------------------------------------------------------------------
Reads new posts from a source channel using YOUR Telegram account (via
Telethon), since you're not an admin there — cleans them up, translates
them into English/French/Spanish/Indonesian/German/Italian, and posts the
results to all your destination channels using the bot.

All secrets and channel IDs are read from environment variables — set them
in Render's dashboard under Environment:

    BOT_TOKEN                - bot token from BotFather (posts to destination channels)
    TELEGRAM_API_ID          - your personal api_id from my.telegram.org
    TELEGRAM_API_HASH        - your personal api_hash from my.telegram.org
    TELEGRAM_SESSION         - the session string from generate_session.py

    SOURCE_CHANNEL_ID        - numeric chat ID of the source channel (you're a member, not admin)

    ENGLISH_CHANNEL_IDS      - comma-separated chat IDs, e.g. "-100111,-100222,-100333"
    FRENCH_CHANNEL_IDS       - comma-separated chat IDs
    SPANISH_CHANNEL_IDS      - comma-separated chat IDs
    INDONESIAN_CHANNEL_IDS   - comma-separated chat IDs
    GERMAN_CHANNEL_IDS       - comma-separated chat IDs
    ITALIAN_CHANNEL_IDS      - comma-separated chat IDs

    FOOTER_TEXT (optional)   - text appended to every post

Requirements:
    pip install -r requirements.txt

The bot must be admin (with "Post Messages") in every DESTINATION channel.
Your personal account just needs to be a regular member of the SOURCE channel.
"""

import asyncio
import logging
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from deep_translator import GoogleTranslator
from telegram import Bot, InputMediaPhoto, InputMediaVideo, InputMediaDocument
from telethon import TelegramClient, events
from telethon.sessions import StringSession

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# =========================== CONFIG (from env vars) ===========================


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            "Set it in Render's dashboard (Environment tab) before running."
        )
    return value


def _require_env_int(name: str) -> int:
    return int(_require_env(name))


def _require_env_id_list(name: str) -> list[int]:
    raw = _require_env(name)
    try:
        return [int(part.strip()) for part in raw.split(",") if part.strip()]
    except ValueError:
        raise RuntimeError(
            f"Environment variable {name} must be a comma-separated list of numeric "
            f"channel IDs, e.g. '-1001111111111,-1002222222222'. Got: {raw!r}"
        )


BOT_TOKEN = _require_env("BOT_TOKEN")

TELEGRAM_API_ID = _require_env_int("TELEGRAM_API_ID")
TELEGRAM_API_HASH = _require_env("TELEGRAM_API_HASH")
TELEGRAM_SESSION = _require_env("TELEGRAM_SESSION")

SOURCE_CHANNEL_ID = _require_env_int("SOURCE_CHANNEL_ID")

# Each language: list of destination chat_ids + the translation target code
# (translate_to = None means "post as-is, no translation")
DESTINATIONS = {
    "english": {
        "chat_ids": _require_env_id_list("ENGLISH_CHANNEL_IDS"),
        "translate_to": None,
    },
    "french": {
        "chat_ids": _require_env_id_list("FRENCH_CHANNEL_IDS"),
        "translate_to": "fr",
    },
    "spanish": {
        "chat_ids": _require_env_id_list("SPANISH_CHANNEL_IDS"),
        "translate_to": "es",
    },
    "indonesian": {
        "chat_ids": _require_env_id_list("INDONESIAN_CHANNEL_IDS"),
        "translate_to": "id",
    },
    "german": {
        "chat_ids": _require_env_id_list("GERMAN_CHANNEL_IDS"),
        "translate_to": "de",
    },
    "italian": {
        "chat_ids": _require_env_id_list("ITALIAN_CHANNEL_IDS"),
        "translate_to": "it",
    },
}

# Patterns to strip out of every post before forwarding.
# @\w{3,32} matches ANY Telegram-style handle (e.g. @News_Crypto, @bittick,
# or any future source tag) so you don't need to keep editing this by hand.
REMOVE_PATTERNS = [
    r"@\w{3,32}",   # any @handle / channel mention, anywhere in the text
    r"#\w+",        # any hashtags
]

FOOTER = os.environ.get("FOOTER_TEXT", "")

MAX_CAPTION_LEN = 1024  # Telegram's cap for media captions (vs 4096 for plain text messages)

# ================================================================================


def clean_text(text: str) -> str:
    """Remove unwanted tags/patterns from the raw post text."""
    if not text:
        return text
    cleaned = text
    for pattern in REMOVE_PATTERNS:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE | re.MULTILINE)
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


def build_final_text(raw_text: str, translate_to: str | None) -> str:
    cleaned = clean_text(raw_text) if raw_text else ""
    if translate_to and cleaned:
        final_text = translate_text(cleaned, translate_to)
    else:
        final_text = cleaned
    if FOOTER and final_text:
        final_text += FOOTER
    return final_text


async def send_to_all_destinations(bot: Bot, raw_text: str, media_items: list) -> None:
    """media_items: list of Telethon Message objects sharing one album (or a
    single-element list for a normal post). Each has .photo / .video / .document."""

    for label, dest in DESTINATIONS.items():
        try:
            final_text = build_final_text(raw_text, dest["translate_to"])
            caption = final_text[:MAX_CAPTION_LEN] if final_text else None

            for chat_id in dest["chat_ids"]:
                try:
                    if len(media_items) > 1:
                        media_list = []
                        for i, m in enumerate(media_items):
                            item_caption = caption if i == 0 else None
                            file_bytes = await m.download_media(bytes)
                            if m.photo:
                                media_list.append(InputMediaPhoto(media=file_bytes, caption=item_caption))
                            elif m.video:
                                media_list.append(InputMediaVideo(media=file_bytes, caption=item_caption))
                            elif m.document:
                                media_list.append(InputMediaDocument(media=file_bytes, caption=item_caption))
                        if media_list:
                            await bot.send_media_group(chat_id=chat_id, media=media_list)
                        else:
                            continue
                    else:
                        m = media_items[0]
                        if m.photo:
                            file_bytes = await m.download_media(bytes)
                            await bot.send_photo(chat_id=chat_id, photo=file_bytes, caption=caption)
                        elif m.video:
                            file_bytes = await m.download_media(bytes)
                            await bot.send_video(chat_id=chat_id, video=file_bytes, caption=caption)
                        elif m.document:
                            file_bytes = await m.download_media(bytes)
                            await bot.send_document(chat_id=chat_id, document=file_bytes, caption=caption)
                        elif m.gif:
                            file_bytes = await m.download_media(bytes)
                            await bot.send_animation(chat_id=chat_id, animation=file_bytes, caption=caption)
                        else:
                            if not final_text:
                                continue
                            await bot.send_message(chat_id=chat_id, text=final_text)

                    logger.info(f"Posted to {label} channel {chat_id} successfully.")
                except Exception as e:
                    # One channel failing shouldn't stop the others
                    logger.error(f"Failed to post to {label} channel ({chat_id}): {e}")
        except Exception as e:
            logger.error(f"Failed processing language '{label}': {e}")


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


async def main() -> None:
    threading.Thread(target=start_health_server, daemon=True).start()

    bot = Bot(token=BOT_TOKEN)
    client = TelegramClient(StringSession(TELEGRAM_SESSION), TELEGRAM_API_ID, TELEGRAM_API_HASH)

    @client.on(events.Album(chats=SOURCE_CHANNEL_ID))
    async def album_handler(event):
        raw_text = ""
        for m in event.messages:
            if m.text:
                raw_text = m.text
                break
        logger.info(f"Received album with {len(event.messages)} items from source channel.")
        await send_to_all_destinations(bot, raw_text, event.messages)

    @client.on(events.NewMessage(chats=SOURCE_CHANNEL_ID))
    async def single_handler(event):
        if event.message.grouped_id:
            return  # handled by album_handler instead
        raw_text = event.message.text or ""
        if not raw_text and not (event.message.photo or event.message.video or event.message.document or event.message.gif):
            logger.info("Post has no text or supported media — skipping.")
            return
        logger.info("Received single post from source channel.")
        await send_to_all_destinations(bot, raw_text, [event.message])

    await client.start()
    logger.info("Bot started. Listening for new posts via your account...")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
