"""
Crypto News Channel Forwarder Bot (v2 — multi-language, non-admin source)
--------------------------------------------------------------------------
Reads new posts from a source channel you are only a SUBSCRIBER of (via a
Telethon "userbot" logged into your own Telegram account), cleans them up
(strips @handles and hashtags), translates them into English/French/Spanish/
Indonesian/German/Italian, and posts to all configured destination channels
(via your bot, which must be admin there) — including multi-image albums.

How media moves: the userbot downloads each photo/video's raw bytes once,
then those bytes are re-uploaded via the bot to every destination channel.
(A Telethon-downloaded file's ID can't be reused directly through the Bot
API, since file IDs are scoped per-bot — re-uploading the bytes is the
reliable way around that.)

All secrets and channel IDs are read from environment variables — set these
in Render's dashboard under Environment:

    BOT_TOKEN                - your bot token from BotFather (used for POSTING only)
    TELEGRAM_API_ID          - your personal api_id from my.telegram.org
    TELEGRAM_API_HASH        - your personal api_hash from my.telegram.org
    TELEGRAM_SESSION         - a Telethon StringSession (see generate_session.py —
                                 run that file LOCALLY once to produce this string;
                                 it cannot be generated on Render since it needs
                                 your live login code sent to your Telegram app)
    SOURCE_CHANNEL_ID        - numeric chat ID of the source channel you read from

    ENGLISH_CHANNEL_IDS      - comma-separated numeric chat IDs, e.g. "-100111,-100222,-100333"
    FRENCH_CHANNEL_IDS       - comma-separated numeric chat IDs
    SPANISH_CHANNEL_IDS      - comma-separated numeric chat IDs
    INDONESIAN_CHANNEL_IDS   - comma-separated numeric chat IDs
    GERMAN_CHANNEL_IDS       - comma-separated numeric chat IDs
    ITALIAN_CHANNEL_IDS      - comma-separated numeric chat IDs

    FOOTER_TEXT (optional)   - text appended to every post, e.g. "— Crypto News"

Requirements:
    pip install -r requirements.txt
"""

import asyncio
import io
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

# =========================== CONFIG — READS FROM ENVIRONMENT VARIABLES ===========================


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


def _require_env_id_list(name: str) -> list:
    raw = _require_env(name)
    return [int(x.strip()) for x in raw.split(",") if x.strip()]


BOT_TOKEN = _require_env("BOT_TOKEN")
TELEGRAM_API_ID = _require_env_int("TELEGRAM_API_ID")
TELEGRAM_API_HASH = _require_env("TELEGRAM_API_HASH")
TELEGRAM_SESSION = _require_env("TELEGRAM_SESSION")
SOURCE_CHANNEL_ID = _require_env_int("SOURCE_CHANNEL_ID")

# Each language: translate_to (None = no translation) + list of destination chat IDs.
LANGUAGE_GROUPS = {
    "english": {
        "translate_to": None,
        "chat_ids": _require_env_id_list("ENGLISH_CHANNEL_IDS"),
    },
    "french": {
        "translate_to": "fr",
        "chat_ids": _require_env_id_list("FRENCH_CHANNEL_IDS"),
    },
    "spanish": {
        "translate_to": "es",
        "chat_ids": _require_env_id_list("SPANISH_CHANNEL_IDS"),
    },
    "indonesian": {
        "translate_to": "id",
        "chat_ids": _require_env_id_list("INDONESIAN_CHANNEL_IDS"),
    },
    "german": {
        "translate_to": "de",
        "chat_ids": _require_env_id_list("GERMAN_CHANNEL_IDS"),
    },
    "italian": {
        "translate_to": "it",
        "chat_ids": _require_env_id_list("ITALIAN_CHANNEL_IDS"),
    },
}

# Patterns to strip out of every post before forwarding.
REMOVE_PATTERNS = [
    r"@\w{3,32}",   # any @handle / channel mention, anywhere in the text
    r"#\w+",        # any hashtags
]

FOOTER = os.environ.get("FOOTER_TEXT", "")

MAX_CAPTION_LEN = 1024  # Telegram's cap for media captions (vs 4096 for plain text messages)
SEND_STAGGER_SECONDS = 0.3  # small delay between sends to avoid flood limits across many channels

# ============================================================================


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


def build_final_text(raw_text: str, translate_to) -> str:
    cleaned = clean_text(raw_text) if raw_text else ""
    if translate_to and cleaned:
        final_text = translate_text(cleaned, translate_to)
    else:
        final_text = cleaned
    if FOOTER and final_text:
        final_text += FOOTER
    return final_text


async def post_single(bot: Bot, raw_text: str, media_type: str, media_bytes: bytes) -> None:
    """media_type is one of: None, 'photo', 'video', 'animation', 'document'."""
    for lang_label, group in LANGUAGE_GROUPS.items():
        final_text = build_final_text(raw_text, group["translate_to"])
        caption = final_text[:MAX_CAPTION_LEN] if final_text else None

        for chat_id in group["chat_ids"]:
            try:
                if media_type == "photo":
                    await bot.send_photo(chat_id=chat_id, photo=io.BytesIO(media_bytes), caption=caption)
                elif media_type == "video":
                    await bot.send_video(chat_id=chat_id, video=io.BytesIO(media_bytes), caption=caption)
                elif media_type == "animation":
                    await bot.send_animation(chat_id=chat_id, animation=io.BytesIO(media_bytes), caption=caption)
                elif media_type == "document":
                    await bot.send_document(chat_id=chat_id, document=io.BytesIO(media_bytes), caption=caption)
                else:
                    if not final_text:
                        continue
                    await bot.send_message(chat_id=chat_id, text=final_text)

                logger.info(f"Posted to {lang_label} channel {chat_id} successfully.")
            except Exception as e:
                logger.error(f"Failed to post to {lang_label} channel {chat_id}: {e}")

            await asyncio.sleep(SEND_STAGGER_SECONDS)


async def post_album(bot: Bot, raw_text: str, media_items: list) -> None:
    """media_items: list of dicts {"type": "photo"/"video", "bytes": bytes}."""
    for lang_label, group in LANGUAGE_GROUPS.items():
        final_text = build_final_text(raw_text, group["translate_to"])
        caption = final_text[:MAX_CAPTION_LEN] if final_text else None

        for chat_id in group["chat_ids"]:
            try:
                media_list = []
                for i, item in enumerate(media_items):
                    item_caption = caption if i == 0 else None
                    file_obj = io.BytesIO(item["bytes"])
                    if item["type"] == "photo":
                        media_list.append(InputMediaPhoto(media=file_obj, caption=item_caption))
                    elif item["type"] == "video":
                        media_list.append(InputMediaVideo(media=file_obj, caption=item_caption))

                if not media_list:
                    continue

                await bot.send_media_group(chat_id=chat_id, media=media_list)
                logger.info(f"Posted album ({len(media_list)} items) to {lang_label} channel {chat_id} successfully.")
            except Exception as e:
                logger.error(f"Failed to post album to {lang_label} channel {chat_id}: {e}")

            await asyncio.sleep(SEND_STAGGER_SECONDS)


async def run_userbot() -> None:
    bot = Bot(token=BOT_TOKEN)
    client = TelegramClient(StringSession(TELEGRAM_SESSION), TELEGRAM_API_ID, TELEGRAM_API_HASH)

    await client.start()
    logger.info("Telethon userbot logged in and listening for new posts...")

    # Pre-populate the entity cache so SOURCE_CHANNEL_ID resolves correctly
    await client.get_dialogs()

    @client.on(events.NewMessage(chats=SOURCE_CHANNEL_ID))
    async def on_new_message(event):
        if event.grouped_id:
            return  # albums are handled by on_album below instead

        message = event.message
        raw_text = message.text or ""

        media_type = None
        media_bytes = None
        try:
            if message.photo:
                media_type = "photo"
                media_bytes = await client.download_media(message.photo, file=bytes)
            elif message.video:
                media_type = "video"
                media_bytes = await client.download_media(message.video, file=bytes)
            elif message.gif:
                media_type = "animation"
                media_bytes = await client.download_media(message.gif, file=bytes)
            elif message.document:
                media_type = "document"
                media_bytes = await client.download_media(message.document, file=bytes)
        except Exception as e:
            logger.error(f"Failed to download media from source post: {e}")
            return

        if not raw_text and media_type is None:
            logger.info("Post has no text or supported media — skipping.")
            return

        await post_single(bot, raw_text, media_type, media_bytes)

    @client.on(events.Album(chats=SOURCE_CHANNEL_ID))
    async def on_album(event):
        raw_text = event.text or ""
        media_items = []
        for m in event.messages:
            try:
                if m.photo:
                    data = await client.download_media(m.photo, file=bytes)
                    media_items.append({"type": "photo", "bytes": data})
                elif m.video:
                    data = await client.download_media(m.video, file=bytes)
                    media_items.append({"type": "video", "bytes": data})
            except Exception as e:
                logger.error(f"Failed to download an item from album: {e}")

        if not media_items:
            logger.info("Album had no downloadable photo/video items — skipping.")
            return

        await post_album(bot, raw_text, media_items)

    await client.run_until_disconnected()


class HealthCheckHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler so Render treats this as a 'web service' and an
    external pinger (UptimeRobot / cron-job.org) can keep it awake for free."""

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive")

    def log_message(self, format, *args):
        pass


def start_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    logger.info(f"Health check server listening on port {port}")
    server.serve_forever()


def main() -> None:
    threading.Thread(target=start_health_server, daemon=True).start()
    asyncio.run(run_userbot())


if __name__ == "__main__":
    main()
