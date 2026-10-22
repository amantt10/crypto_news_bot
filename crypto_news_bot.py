"""
Crypto News Bot — ORIGINAL version (translate + post, no button)
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

Captions over the bot's 1024 limit are never cut: the media is sent first, then the full
text as a normal message.

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

import requests
from deep_translator import GoogleTranslator
from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    InputMediaDocument,
)
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)  # keep the bot token out of the logs
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

# Button attached under every post. The label is hardcoded per language (rather
# than run through the translator) so it's always correct and never affected by
# translation rate limits.
BUTTON_TEXTS = {
    "english": "CONTACT ME ✅",
    "french": "CONTACTEZ-MOI ✅",
    "spanish": "CONTÁCTAME ✅",
    "indonesian": "HUBUNGI SAYA ✅",
    "german": "KONTAKTIERE MICH ✅",
    "italian": "CONTATTAMI ✅",
}

# English channels link to one account, every other language links to another.
ENGLISH_BUTTON_URL = "https://t.me/Dubois_Lucas"
OTHER_LANGUAGES_BUTTON_URL = "https://t.me/miguel_p2p"

# Albums can't carry a button, so it goes in a tiny follow-up message instead.
ALBUM_BUTTON_MESSAGE_TEXT = "👇"

BUTTON_MARKUPS = {label: None for label in DESTINATIONS}  # original version: no button

MAX_CAPTION_LEN = 1024  # Telegram's cap for media captions sent by a BOT (vs 4096 for plain text)

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


def _looks_like_translation_error(text: str) -> bool:
    """Detect Google's generic error page slipping through as a 'successful' translation."""
    if not text:
        return False
    lowered = text.lower()
    # Only phrases that appear on Google's actual error page. (Do NOT add bare
    # things like "500." here — real news text such as "$60,500." would match.)
    error_markers = [
        "<html",
        "that's an error", "that’s an error",
        "that's all we know", "that’s all we know",
        "error 500 (server error)",
        "there was an error. please try again later",
    ]
    return any(marker in lowered for marker in error_markers)


TRANSLATION_CHUNK_LEN = 900   # long single requests to Google's free endpoint fail far more often
TELEGRAM_MESSAGE_LEN = 4096   # Telegram's cap for a plain text message


def _pack_atoms(atoms: list, limit: int) -> list:
    """Greedily pack (text, separator_after) atoms into chunks of at most `limit`
    characters. Returns (chunk, separator_after_its_last_atom) pairs."""
    chunks = []
    cur = None
    cur_sep = ""
    for atom_text, atom_sep in atoms:
        if cur is None:
            cur, cur_sep = atom_text, atom_sep
            continue
        candidate = cur + cur_sep + atom_text
        if len(candidate) <= limit:
            cur, cur_sep = candidate, atom_sep
        else:
            chunks.append((cur, cur_sep))
            cur, cur_sep = atom_text, atom_sep
    if cur is not None:
        chunks.append((cur, cur_sep))
    return chunks


def _split_paragraph(para: str, limit: int) -> list:
    """Split one over-long paragraph at sentence ends, keeping the ORIGINAL whitespace
    (spaces/newlines) as the separators. A single unbroken run longer than `limit`
    is cut hard with an empty separator, so nothing is inserted or lost."""
    atoms = []
    parts = re.split(r"(?<=[.!?…])(\s+)", para)  # [sentence, whitespace, sentence, ...]
    for i in range(0, len(parts), 2):
        sentence = parts[i]
        ws = parts[i + 1] if i + 1 < len(parts) else ""
        while len(sentence) > limit:
            atoms.append((sentence[:limit], ""))
            sentence = sentence[limit:]
        atoms.append((sentence, ws))
    return atoms


def _split_into_segments(text: str, limit: int) -> list:
    """Break text into (piece, separator_that_followed_it) pairs, each piece at most
    `limit` characters. Prefers paragraph breaks, then sentence ends, then a hard cut."""
    segments = []
    paragraphs = text.split("\n\n")
    for pi, para in enumerate(paragraphs):
        para_sep = "\n\n" if pi < len(paragraphs) - 1 else ""
        if len(para) <= limit:
            segments.append((para, para_sep))
            continue
        pieces = _pack_atoms(_split_paragraph(para, limit), limit)
        for k, (piece, sep) in enumerate(pieces):
            segments.append((piece, sep + para_sep if k == len(pieces) - 1 else sep))
    return segments


def _chunk_text(text: str, limit: int) -> list:
    """Group text into chunks of at most `limit` characters. Returns (chunk, separator)
    pairs; joining chunk+separator for every pair rebuilds the original text exactly."""
    return _pack_atoms(_split_into_segments(text, limit), limit)


async def translate_text(text: str, target_lang: str):
    """Translate text in small chunks so long posts don't fail as one big request.
    Returns the full translation, or None if ANY chunk couldn't be translated after
    retries (caller then skips that language rather than posting a half-translation)."""
    if not text:
        return text

    chunks = _chunk_text(text, TRANSLATION_CHUNK_LEN)
    out = []
    for i, (chunk, sep) in enumerate(chunks):
        if not chunk.strip():
            out.append(chunk + sep)
            continue
        translated = await _translate_chunk(chunk, target_lang)
        if translated is None:
            logger.error(
                f"Chunk {i + 1}/{len(chunks)} could not be translated to '{target_lang}'."
            )
            return None
        out.append(translated + sep)
        if i < len(chunks) - 1:
            await asyncio.sleep(0.3)
    return "".join(out)


def _gtx_translate_sync(text: str, target_lang: str) -> str:
    """Backup route: Google's other free translate endpoint, called directly. It's used
    only when the main (deep-translator) route has failed every retry."""
    resp = requests.post(
        "https://translate.googleapis.com/translate_a/single",
        params={"client": "gtx", "sl": "auto", "tl": target_lang, "dt": "t"},
        data={"q": text},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=12,
    )
    resp.raise_for_status()
    # Response: [[[translated, original, ...], [translated, original, ...], ...], ...]
    return "".join(part[0] for part in resp.json()[0] if part and part[0])


async def _translate_chunk(text: str, target_lang: str, max_retries: int = 2):
    """Translate ONE chunk using Google Translate (free, via deep-translator).
    Returns the translated string on success, or None if translation could
    not be completed after all retries (caller should skip posting rather
    than fall back to the untranslated English text).

    IMPORTANT: deep-translator makes a blocking, synchronous network call.
    Running it directly on the asyncio event loop would freeze the ENTIRE
    bot (including the Telethon connection reading the source channel) if
    the call ever hangs. asyncio.to_thread + a timeout prevents that.

    The free endpoint also occasionally rate-limits and returns an HTML
    error page instead of raising — we detect that and retry with backoff
    rather than posting the error page as if it were the translation.
    """
    if not text:
        return text

    for attempt in range(max_retries + 1):
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    lambda: GoogleTranslator(source="auto", target=target_lang).translate(text)
                ),
                timeout=15,
            )
            if result and not _looks_like_translation_error(result):
                return result
            logger.warning(
                f"Translation attempt {attempt + 1}/{max_retries + 1} for '{target_lang}' "
                f"returned an empty/error response ({(result or '')[:80]!r}) — retrying..."
            )
        except asyncio.TimeoutError:
            logger.error(f"Translation attempt {attempt + 1}/{max_retries + 1} timed out for '{target_lang}'.")
        except Exception as e:
            logger.error(f"Translation attempt {attempt + 1}/{max_retries + 1} failed for '{target_lang}': {type(e).__name__}: {e}")

        if attempt < max_retries:
            await asyncio.sleep(2 * (attempt + 1))  # 2s, then 4s backoff

    logger.warning(f"Main translation route failed for '{target_lang}' — trying the backup route...")
    for attempt in range(2):
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(_gtx_translate_sync, text, target_lang), timeout=15
            )
            if result and not _looks_like_translation_error(result):
                logger.info(f"Backup translation route worked for '{target_lang}'.")
                return result
            logger.warning(f"Backup route returned an empty/error response for '{target_lang}'.")
        except asyncio.TimeoutError:
            logger.error(f"Backup translation route timed out for '{target_lang}'.")
        except Exception as e:
            logger.error(f"Backup translation route failed for '{target_lang}': {type(e).__name__}: {e}")
        if attempt == 0:
            await asyncio.sleep(2)

    logger.error(f"All translation attempts failed for '{target_lang}' — skipping this language's channels for this post.")
    return None


async def build_final_text(raw_text: str, translate_to: str | None):
    """Returns the final text, or None to signal 'skip this language entirely'
    (translation failed after retries — never fall back to posting English)."""
    cleaned = clean_text(raw_text) if raw_text else ""
    if translate_to and cleaned:
        translated = await translate_text(cleaned, translate_to)
        await asyncio.sleep(0.5)  # small gap between languages to avoid bursting the translate endpoint
        if translated is None:
            return None
        final_text = translated
    else:
        final_text = cleaned
    if FOOTER and final_text:
        final_text += FOOTER
    return final_text


def _media_kind(m, album: bool):
    """Which kind of media a Telethon message holds (None if unsupported)."""
    # isinstance check: m.photo is also set for a plain text post that only shows a
    # link-preview picture, which must stay a text post.
    if isinstance(m.media, MessageMediaPhoto):
        return "photo"
    if isinstance(m.media, MessageMediaDocument):
        if m.gif:
            return None if album else "animation"  # GIFs can't go inside an album
        if m.video:
            return "video"
        return "document"
    return None


async def _load_media(media_items: list, album: bool) -> list:
    """Download each media item ONCE per post (not once per channel).
    Returns (kind, bytes, original_telethon_message) triples."""
    loaded = []
    for m in media_items:
        kind = _media_kind(m, album)
        if kind is None:
            continue
        loaded.append((kind, await m.download_media(bytes), m))
    return loaded


async def _send_text(bot: Bot, chat_id: int, text: str, button_markup) -> None:
    """Send the full text as a normal message (splitting only if it exceeds
    Telegram's 4096 limit), with the button on the last part."""
    parts = [chunk for chunk, _ in _chunk_text(text, TELEGRAM_MESSAGE_LEN)]
    for i, part in enumerate(parts):
        is_last = i == len(parts) - 1
        await bot.send_message(
            chat_id=chat_id, text=part, reply_markup=button_markup if is_last else None
        )


async def _send_to_channel(bot: Bot, chat_id: int, final_text: str, loaded: list,
                           is_album: bool, button_markup) -> None:
    # Captions sent by the bot are capped at 1024 chars.
    text_fits = len(final_text) <= MAX_CAPTION_LEN

    # Bot path. If the text is still too long for a caption, NEVER cut it: send the
    # media on its own, then the full text as a normal message with the button on it.
    caption = final_text if (final_text and text_fits) else None

    if is_album:
        media_list = []
        for i, (kind, data, _msg) in enumerate(loaded):
            item_caption = caption if i == 0 else None
            if kind == "photo":
                media_list.append(InputMediaPhoto(media=data, caption=item_caption))
            elif kind == "video":
                media_list.append(InputMediaVideo(media=data, caption=item_caption))
            elif kind == "document":
                media_list.append(InputMediaDocument(media=data, caption=item_caption))
        await bot.send_media_group(chat_id=chat_id, media=media_list)
        # Telegram doesn't allow buttons on albums, so the button goes in a follow-up message.
        if final_text and not text_fits:
            await _send_text(bot, chat_id, final_text, button_markup)
        elif button_markup is not None:
            await bot.send_message(
                chat_id=chat_id, text=ALBUM_BUTTON_MESSAGE_TEXT, reply_markup=button_markup
            )
        return

    if loaded:
        kind, data, _msg = loaded[0]
        media_markup = button_markup if text_fits else None
        if kind == "photo":
            await bot.send_photo(chat_id=chat_id, photo=data, caption=caption, reply_markup=media_markup)
        elif kind == "video":
            await bot.send_video(chat_id=chat_id, video=data, caption=caption, reply_markup=media_markup)
        elif kind == "document":
            await bot.send_document(chat_id=chat_id, document=data, caption=caption, reply_markup=media_markup)
        elif kind == "animation":
            await bot.send_animation(chat_id=chat_id, animation=data, caption=caption, reply_markup=media_markup)
        if final_text and not text_fits:
            await _send_text(bot, chat_id, final_text, button_markup)
        return

    if final_text:
        await _send_text(bot, chat_id, final_text, button_markup)


async def send_to_all_destinations(bot: Bot, raw_text: str, media_items: list) -> None:
    """media_items: list of Telethon Message objects sharing one album (or a
    single-element list for a normal post)."""
    is_album = len(media_items) > 1

    try:
        loaded = await _load_media(media_items, album=is_album)
    except Exception as e:
        logger.error(f"Failed to download media from source post: {e}")
        return

    if is_album and not loaded:
        logger.info("Album had no supported photo/video/document items — skipping.")
        return

    for label, dest in DESTINATIONS.items():
        try:
            final_text = await build_final_text(raw_text, dest["translate_to"])

            if final_text is None:
                # Translation failed after all retries — skip this language entirely
                # rather than posting untranslated English into a non-English channel.
                logger.warning(f"Skipping {label} channels for this post — translation unavailable.")
                continue

            if not final_text and not loaded:
                continue

            button_markup = BUTTON_MARKUPS[label]

            for chat_id in dest["chat_ids"]:
                try:
                    await _send_to_channel(
                        bot, chat_id, final_text, loaded, is_album, button_markup,
                    )
                    logger.info(f"Posted to {label} channel {chat_id} successfully.")
                except Exception as e:
                    # One channel failing shouldn't stop the others
                    logger.error(f"Failed to post to {label} channel ({chat_id}): {type(e).__name__}: {e}")
        except Exception as e:
            logger.error(f"Failed processing language '{label}': {type(e).__name__}: {e}")


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
    if os.environ.get("HEALTH_SERVER", "1") != "0":
        threading.Thread(target=start_health_server, daemon=True).start()

    bot = Bot(token=BOT_TOKEN)
    client = TelegramClient(StringSession(TELEGRAM_SESSION), TELEGRAM_API_ID, TELEGRAM_API_HASH)

    @client.on(events.Album(chats=SOURCE_CHANNEL_ID))
    async def album_handler(event):
        raw_text = ""
        for m in event.messages:
            if m.raw_text:
                raw_text = m.raw_text
                break
        logger.info(f"Received album with {len(event.messages)} items from source channel.")
        await send_to_all_destinations(bot, raw_text, event.messages)

    @client.on(events.NewMessage(chats=SOURCE_CHANNEL_ID))
    async def single_handler(event):
        if event.message.grouped_id:
            return  # handled by album_handler instead
        raw_text = event.message.raw_text or ""
        if not raw_text and not isinstance(event.message.media, (MessageMediaPhoto, MessageMediaDocument)):
            logger.info("Post has no text or supported media — skipping.")
            return
        logger.info("Received single post from source channel.")
        await send_to_all_destinations(bot, raw_text, [event.message])

    await client.start()
    logger.info("Telethon client logged in. Syncing dialogs...")
    await client.get_dialogs()  # populates entity cache so SOURCE_CHANNEL_ID resolves correctly

    logger.info("Bot started. Listening for new posts via your account...")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
