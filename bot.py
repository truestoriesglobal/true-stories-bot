import os
import logging
import tempfile
import pickle
import json
import base64
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, MessageHandler, CommandHandler,
    filters, ContextTypes, ConversationHandler, CallbackQueryHandler
)
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

# ============================================================
# CONFIGURATION
# ============================================================
TELEGRAM_BOT_TOKEN = "8947464641:AAH7k-rhp0hel_ysA9dACmKkZJZB9kUP2Yg"
TELEGRAM_CHANNEL_ID = -1003919101691
YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
CLIENT_SECRETS_FILE = "client_secrets.json"
TOKEN_PICKLE_FILE = "youtube_token.pickle"

# Write credentials from environment variables to files if they exist
if os.environ.get("YOUTUBE_CLIENT_SECRETS"):
    with open(CLIENT_SECRETS_FILE, "w") as f:
        f.write(os.environ["YOUTUBE_CLIENT_SECRETS"])

if os.environ.get("YOUTUBE_TOKEN"):
    token_data = base64.b64decode(os.environ["YOUTUBE_TOKEN"])
    with open(TOKEN_PICKLE_FILE, "wb") as f:
        f.write(token_data)
# ============================================================

# Conversation states
WAITING_TITLE = 1
WAITING_DESCRIPTION = 2
WAITING_SERIES = 3
WAITING_PART = 4
WAITING_PRIVACY = 5
CONFIRM_UPLOAD = 6

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

video_store = {}

def get_youtube_service():
    creds = None
    if os.path.exists(TOKEN_PICKLE_FILE):
        with open(TOKEN_PICKLE_FILE, "rb") as token:
            creds = pickle.load(token)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                CLIENT_SECRETS_FILE, YOUTUBE_SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(TOKEN_PICKLE_FILE, "wb") as token:
            pickle.dump(creds, token)
    return build("youtube", "v3", credentials=creds)

def build_full_title(title: str, series: str, part: str) -> str:
    """Build full YouTube title with series and part info."""
    if series and series != "skip" and part and part != "skip":
        return f"{series} | Part {part} - {title}"
    elif series and series != "skip":
        return f"{series} - {title}"
    elif part and part != "skip":
        return f"{title} | Part {part}"
    else:
        return title

def build_description(title: str, series: str, part: str, custom_desc: str) -> str:
    series_text = ""
    if series and series != "skip":
        part_text = f" Part {part}" if part and part != "skip" else ""
        series_text = f"📺 Series: {series}{part_text}\n\n"

    custom_text = f"{custom_desc}\n\n" if custom_desc and custom_desc != "skip" else ""

    return f"""{custom_text}{series_text}Welcome to True Stories Global — where we share real life stories that will shock, inspire and move you.

Every story featured on this channel is based on real events shared by real people from around the world.

🔔 Subscribe for daily true stories
👍 Like if this story moved you
💬 Comment your thoughts below

#TrueStories #RealLife #StoryTime #Emotional #Viral #TrueStoriesGlobal #RealStories #LifeStories"""

def build_tags(title: str, series: str) -> list:
    title_words = [w.lower() for w in title.split() if len(w) > 3][:4]
    series_words = [w.lower() for w in series.split() if len(w) > 3][:2] if series and series != "skip" else []
    return title_words + series_words + [
        "true stories", "real life stories", "story time",
        "emotional stories", "viral stories", "true stories global",
        "real events", "life stories", "shocking stories", "inspirational"
    ]

async def upload_to_youtube(video_path: str, metadata: dict) -> str:
    youtube = get_youtube_service()
    body = {
        "snippet": {
            "title": metadata["title"],
            "description": metadata["description"],
            "tags": metadata["tags"],
            "categoryId": "22"
        },
        "status": {
            "privacyStatus": metadata.get("privacy", "public"),
            "selfDeclaredMadeForKids": False
        }
    }
    media = MediaFileUpload(video_path, chunksize=-1, resumable=True, mimetype="video/*")
    request = youtube.videos().insert(
        part=",".join(body.keys()), body=body, media_body=media
    )
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            logger.info(f"Upload: {int(status.progress() * 100)}%")
    return f"https://www.youtube.com/watch?v={response['id']}"

# ============================================================
# CONVERSATION FLOW
# ============================================================

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 1 — Video received, ask for title."""
    message = update.channel_post or update.message
    if not message or (not message.video and not message.document):
        return

    user_id = message.chat_id

    if message.video:
        file = await message.video.get_file()
    else:
        file = await message.document.get_file()

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmp_path = tmp.name
    await file.download_to_drive(tmp_path)

    video_store[user_id] = {
        "video_path": tmp_path,
        "title": "",
        "description": "",
        "series": "",
        "part": "",
        "privacy": "public"
    }

    await context.bot.send_message(
        chat_id=user_id,
        text="📹 *Video received!*\n\n"
             "I'll ask you a few quick questions to set it up perfectly on YouTube.\n\n"
             "━━━━━━━━━━━━━━━━\n"
             "📝 *Step 1 of 5 — Title*\n"
             "━━━━━━━━━━━━━━━━\n\n"
             "What is the *title* of this video?\n\n"
             "Example:\n"
             "_'I found out my husband had a secret family'_",
        parse_mode="Markdown"
    )
    return WAITING_TITLE

async def handle_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 2 — Title received, ask for description."""
    message = update.channel_post or update.message
    if not message or not message.text:
        return WAITING_TITLE

    user_id = message.chat_id
    title = message.text.strip()

    if len(title) > 70:
        await context.bot.send_message(
            chat_id=user_id,
            text=f"⚠️ Title too long ({len(title)} chars). Max is 70.\n\nPlease shorten it:"
        )
        return WAITING_TITLE

    video_store[user_id]["title"] = title

    await context.bot.send_message(
        chat_id=user_id,
        text=f"✅ *Title saved!*\n\n"
             "━━━━━━━━━━━━━━━━\n"
             "📝 *Step 2 of 5 — Description*\n"
             "━━━━━━━━━━━━━━━━\n\n"
             "Do you want to add any *extra text* at the top of the description?\n\n"
             "Example:\n"
             "_'This story was submitted by a viewer from Pakistan...'_\n\n"
             "Or type *skip* to use the default description only.",
        parse_mode="Markdown"
    )
    return WAITING_DESCRIPTION

async def handle_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 3 — Description received, ask if part of a series."""
    message = update.channel_post or update.message
    if not message or not message.text:
        return WAITING_DESCRIPTION

    user_id = message.chat_id
    desc = message.text.strip()
    video_store[user_id]["description"] = "" if desc.lower() == "skip" else desc

    # Ask series with buttons
    keyboard = [
        [
            InlineKeyboardButton("✅ Yes, it's part of a series", callback_data="series_yes"),
            InlineKeyboardButton("❌ No, standalone video", callback_data="series_no")
        ]
    ]

    await context.bot.send_message(
        chat_id=user_id,
        text="━━━━━━━━━━━━━━━━\n"
             "📝 *Step 3 of 5 — Series*\n"
             "━━━━━━━━━━━━━━━━\n\n"
             "Is this video part of a *series*?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return WAITING_SERIES

async def handle_series_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle yes/no series choice."""
    query = update.callback_query
    await query.answer()
    user_id = query.message.chat_id

    if query.data == "series_no":
        video_store[user_id]["series"] = ""
        video_store[user_id]["part"] = ""
        await query.edit_message_text("✅ Standalone video noted!")
        # Skip to privacy
        keyboard = [[
            InlineKeyboardButton("🌍 Public", callback_data="privacy_public"),
            InlineKeyboardButton("🔒 Private", callback_data="privacy_private"),
            InlineKeyboardButton("🔗 Unlisted", callback_data="privacy_unlisted")
        ]]
        await context.bot.send_message(
            chat_id=user_id,
            text="━━━━━━━━━━━━━━━━\n"
                 "📝 *Step 4 of 5 — Privacy*\n"
                 "━━━━━━━━━━━━━━━━\n\n"
                 "What should the *privacy setting* be?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return WAITING_PRIVACY
    else:
        await query.edit_message_text(
            "━━━━━━━━━━━━━━━━\n"
            "📝 *Step 3 of 5 — Series Name*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "What is the *name of the series*?\n\n"
            "Example:\n"
            "_'Secret Lives'_ or _'Betrayal Stories'_",
            parse_mode="Markdown"
        )
        return WAITING_SERIES

async def handle_series_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Series name received, ask for part number."""
    message = update.channel_post or update.message
    if not message or not message.text:
        return WAITING_SERIES

    user_id = message.chat_id
    series = message.text.strip()
    video_store[user_id]["series"] = series

    await context.bot.send_message(
        chat_id=user_id,
        text=f"✅ Series: *{series}*\n\n"
             "What *part number* is this?\n\n"
             "Example: _1_, _2_, _3_",
        parse_mode="Markdown"
    )
    return WAITING_PART

async def handle_part(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Part number received, ask for privacy."""
    message = update.channel_post or update.message
    if not message or not message.text:
        return WAITING_PART

    user_id = message.chat_id
    part = message.text.strip()
    video_store[user_id]["part"] = part

    keyboard = [[
        InlineKeyboardButton("🌍 Public", callback_data="privacy_public"),
        InlineKeyboardButton("🔒 Private", callback_data="privacy_private"),
        InlineKeyboardButton("🔗 Unlisted", callback_data="privacy_unlisted")
    ]]

    await context.bot.send_message(
        chat_id=user_id,
        text=f"✅ Part *{part}* saved!\n\n"
             "━━━━━━━━━━━━━━━━\n"
             "📝 *Step 4 of 5 — Privacy*\n"
             "━━━━━━━━━━━━━━━━\n\n"
             "What should the *privacy setting* be?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return WAITING_PRIVACY

async def handle_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Privacy selected — show full summary."""
    query = update.callback_query
    await query.answer()
    user_id = query.message.chat_id
    privacy = query.data.replace("privacy_", "")
    video_store[user_id]["privacy"] = privacy

    data = video_store[user_id]
    full_title = build_full_title(data["title"], data["series"], data["part"])

    # Truncate title if too long
    if len(full_title) > 70:
        full_title = full_title[:67] + "..."

    privacy_emoji = {"public": "🌍", "private": "🔒", "unlisted": "🔗"}
    series_text = f"📺 Series: {data['series']} | Part {data['part']}" if data["series"] else "📺 Standalone video"

    keyboard = [[
        InlineKeyboardButton("✅ Upload Now!", callback_data="confirm_upload"),
        InlineKeyboardButton("❌ Cancel", callback_data="cancel_upload")
    ]]

    await query.edit_message_text(
        text="━━━━━━━━━━━━━━━━\n"
             "📋 *Step 5 of 5 — Confirm Upload*\n"
             "━━━━━━━━━━━━━━━━\n\n"
             f"🎬 *Title:* {full_title}\n"
             f"📄 *Description:* {'Custom + Default' if data['description'] else 'Default only'}\n"
             f"{series_text}\n"
             f"{privacy_emoji.get(privacy, '🌍')} *Privacy:* {privacy.capitalize()}\n\n"
             "Ready to upload to YouTube?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return CONFIRM_UPLOAD

async def confirm_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Upload to YouTube."""
    query = update.callback_query
    await query.answer()
    user_id = query.message.chat_id

    if query.data == "cancel_upload":
        if user_id in video_store:
            try:
                os.unlink(video_store[user_id]["video_path"])
            except:
                pass
            del video_store[user_id]
        await query.edit_message_text("❌ Upload cancelled. Send a new video whenever you're ready!")
        return ConversationHandler.END

    await query.edit_message_text("⏳ *Uploading to YouTube...*\n\nThis may take a few minutes depending on video size. Please wait!", parse_mode="Markdown")

    data = video_store[user_id]
    full_title = build_full_title(data["title"], data["series"], data["part"])
    if len(full_title) > 70:
        full_title = full_title[:67] + "..."

    metadata = {
        "title": full_title,
        "description": build_description(data["title"], data["series"], data["part"], data["description"]),
        "tags": build_tags(data["title"], data["series"]),
        "privacy": data["privacy"]
    }

    try:
        youtube_url = await upload_to_youtube(data["video_path"], metadata)
        await context.bot.send_message(
            chat_id=user_id,
            text=f"🎉 *Successfully uploaded!*\n\n"
                 f"📺 *Title:* {full_title}\n"
                 f"🔗 *URL:* {youtube_url}\n\n"
                 f"Send your next video whenever you're ready! 🚀",
            parse_mode="Markdown"
        )
        os.unlink(data["video_path"])
        del video_store[user_id]
    except Exception as e:
        await context.bot.send_message(
            chat_id=user_id,
            text=f"❌ Upload failed: {str(e)}\nPlease try again."
        )
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = (update.message or update.channel_post).chat_id
    if user_id in video_store:
        try:
            os.unlink(video_store[user_id]["video_path"])
        except:
            pass
        del video_store[user_id]
    await context.bot.send_message(chat_id=user_id, text="❌ Cancelled. Send a new video whenever you're ready!")
    return ConversationHandler.END

def main():
    logger.info("Starting True Stories Bot...")
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video)
        ],
        states={
            WAITING_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_title)],
            WAITING_DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_description)],
            WAITING_SERIES: [
                CallbackQueryHandler(handle_series_choice, pattern="^series_(yes|no)$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_series_name)
            ],
            WAITING_PART: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_part)],
            WAITING_PRIVACY: [CallbackQueryHandler(handle_privacy, pattern="^privacy_")],
            CONFIRM_UPLOAD: [CallbackQueryHandler(confirm_upload, pattern="^(confirm|cancel)_upload$")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_chat=True
    )

    app.add_handler(conv_handler)
    logger.info("Bot running! Send a video to start.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

