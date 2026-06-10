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
from google.auth.transport.requests import Request

# ============================================================
# CONFIGURATION
# ============================================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8947464641:AAH7k-rhp0hel_ysA9dACmKkZJZB9kUP2Yg")
YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

with open("channels.json", "r") as f:
    CHANNELS_CONFIG = json.load(f)["channels"]

CHANNEL_NAMES = list(CHANNELS_CONFIG.keys())
# ============================================================

# Conversation states
WAITING_CHANNEL = 0
WAITING_PLATFORMS = 1
WAITING_TITLE = 2
WAITING_DESCRIPTION = 3
WAITING_SERIES = 4
WAITING_PART = 5
WAITING_PRIVACY = 6
CONFIRM_UPLOAD = 7

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
video_store = {}

# ============================================================
# CREDENTIALS SETUP
# ============================================================
def setup_youtube_credentials(channel_name):
    config = CHANNELS_CONFIG[channel_name]
    safe_name = channel_name.replace(" ", "_")
    secrets_env = config.get("youtube_secret", "YOUTUBE_CLIENT_SECRETS")
    token_env = config["platforms"]["youtube"]["token_secret"]
    secrets_file = f"client_secrets_{safe_name}.json"
    token_file = f"youtube_token_{safe_name}.pickle"

    if os.environ.get(secrets_env):
        with open(secrets_file, "w") as f:
            f.write(os.environ[secrets_env])
    elif os.path.exists("client_secrets.json"):
        import shutil
        shutil.copy("client_secrets.json", secrets_file)

    if os.environ.get(token_env):
        token_str = os.environ[token_env].strip()
        padding = 4 - len(token_str) % 4
        if padding != 4:
            token_str += "=" * padding
        token_data = base64.b64decode(token_str)
        with open(token_file, "wb") as f:
            f.write(token_data)
    elif os.path.exists("youtube_token.pickle"):
        import shutil
        shutil.copy("youtube_token.pickle", token_file)

    return secrets_file, token_file

# ============================================================
# PLATFORM UPLOADERS
# ============================================================
async def upload_youtube(video_path, metadata, channel_name):
    secrets_file, token_file = setup_youtube_credentials(channel_name)
    creds = None
    if os.path.exists(token_file):
        with open(token_file, "rb") as token:
            creds = pickle.load(token)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
    youtube = build("youtube", "v3", credentials=creds)
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
    request = youtube.videos().insert(part=",".join(body.keys()), body=body, media_body=media)
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            logger.info(f"YouTube upload: {int(status.progress() * 100)}%")
    return f"https://www.youtube.com/watch?v={response['id']}"

async def upload_facebook(video_path, metadata, channel_name):
    config = CHANNELS_CONFIG[channel_name]["platforms"]["facebook"]
    token = os.environ.get(config["token_secret"], "")
    page_id = os.environ.get(config.get("page_id_secret", ""), "")
    if not token or not page_id:
        return "❌ Facebook token not configured"
    import httpx
    async with httpx.AsyncClient() as client:
        with open(video_path, "rb") as f:
            response = await client.post(
                f"https://graph-video.facebook.com/v18.0/{page_id}/videos",
                data={"title": metadata["title"], "description": metadata["description"], "access_token": token},
                files={"source": f}
            )
    if response.status_code == 200:
        video_id = response.json().get("id")
        return f"https://www.facebook.com/video/{video_id}"
    return f"❌ Facebook upload failed: {response.text}"

async def upload_telegram_channel(video_path, metadata, channel_name):
    config = CHANNELS_CONFIG[channel_name]["platforms"]["telegram_channel"]
    channel_id = os.environ.get(config["channel_id_secret"], "")
    if not channel_id:
        return "❌ Telegram channel ID not configured"
    from telegram import Bot
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    with open(video_path, "rb") as f:
        msg = await bot.send_video(
            chat_id=channel_id,
            video=f,
            caption=f"{metadata['title']}\n\n{metadata['description'][:800]}"
        )
    return f"✅ Posted to Telegram channel"

async def upload_dailymotion(video_path, metadata, channel_name):
    config = CHANNELS_CONFIG[channel_name]["platforms"]["dailymotion"]
    token = os.environ.get(config["token_secret"], "")
    if not token:
        return "❌ Dailymotion token not configured"
    import httpx
    async with httpx.AsyncClient() as client:
        with open(video_path, "rb") as f:
            upload_resp = await client.post(
                "https://api.dailymotion.com/file/upload",
                headers={"Authorization": f"Bearer {token}"},
                files={"file": f}
            )
        if upload_resp.status_code != 200:
            return f"❌ Dailymotion upload failed"
        upload_url = upload_resp.json().get("url")
        publish_resp = await client.post(
            "https://api.dailymotion.com/me/videos",
            headers={"Authorization": f"Bearer {token}"},
            data={"url": upload_url, "title": metadata["title"], "description": metadata["description"], "published": "true", "channel": "fun"}
        )
        if publish_resp.status_code == 200:
            vid_id = publish_resp.json().get("id")
            return f"https://www.dailymotion.com/video/{vid_id}"
    return "❌ Dailymotion publish failed"

async def upload_to_platforms(video_path, metadata, channel_name, selected_platforms):
    """Upload to all selected platforms and return results."""
    results = {}
    platform_config = CHANNELS_CONFIG[channel_name]["platforms"]

    for platform in selected_platforms:
        if platform not in platform_config:
            results[platform] = "❌ Not configured in channels.json"
            continue
        if not platform_config[platform].get("enabled", False) and platform not in selected_platforms:
            continue
        try:
            if platform == "youtube":
                results[platform] = await upload_youtube(video_path, metadata, channel_name)
            elif platform == "facebook":
                results[platform] = await upload_facebook(video_path, metadata, channel_name)
            elif platform == "telegram_channel":
                results[platform] = await upload_telegram_channel(video_path, metadata, channel_name)
            elif platform == "dailymotion":
                results[platform] = await upload_dailymotion(video_path, metadata, channel_name)
            else:
                results[platform] = f"⏳ {platform.title()} - Coming soon!"
        except Exception as e:
            results[platform] = f"❌ {platform.title()} error: {str(e)[:100]}"

    return results

# ============================================================
# METADATA HELPERS
# ============================================================
def build_full_title(title, series, part):
    if series and series != "skip" and part and part != "skip":
        return f"{series} | Part {part} - {title}"
    elif series and series != "skip":
        return f"{series} - {title}"
    elif part and part != "skip":
        return f"{title} | Part {part}"
    return title

def build_description(title, series, part, custom_desc):
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

#TrueStories #RealLife #StoryTime #Emotional #Viral #TrueStoriesGlobal"""

def build_tags(title, series):
    title_words = [w.lower() for w in title.split() if len(w) > 3][:4]
    series_words = [w.lower() for w in series.split() if len(w) > 3][:2] if series and series != "skip" else []
    return title_words + series_words + ["true stories", "real life stories", "story time", "emotional stories", "viral stories", "true stories global"]

# ============================================================
# PLATFORM EMOJI MAP
# ============================================================
PLATFORM_EMOJI = {
    "youtube": "▶️ YouTube",
    "facebook": "👥 Facebook",
    "tiktok": "🎵 TikTok",
    "instagram": "📸 Instagram",
    "rumble": "📹 Rumble",
    "dailymotion": "🎬 Dailymotion",
    "telegram_channel": "✈️ Telegram",
    "twitter": "🐦 Twitter/X",
    "linkedin": "💼 LinkedIn",
    "pinterest": "📌 Pinterest",
    "snapchat": "👻 Snapchat",
    "threads": "🧵 Threads",
    "bluesky": "🦋 Bluesky",
    "odysee": "🌊 Odysee",
    "vk": "💬 VK",
    "triller": "🎸 Triller",
    "clapper": "🎭 Clapper",
    "kwai": "🎯 Kwai",
    "likee": "❤️ Likee",
    "lemon8": "🍋 Lemon8"
}

# ============================================================
# CONVERSATION HANDLERS
# ============================================================
async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
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
        "channel": "",
        "selected_platforms": [],
        "title": "",
        "description": "",
        "series": "",
        "part": "",
        "privacy": "public"
    }

    # Build channel buttons
    keyboard = []
    row = []
    for i, name in enumerate(CHANNEL_NAMES):
        row.append(InlineKeyboardButton(name, callback_data=f"ch_{i}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    await message.reply_text(
        "📹 *Video received!*\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "📝 *Step 1 of 7 — Select Channel*\n"
        "━━━━━━━━━━━━━━━━\n\n"
        "Which channel should this go to?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return WAITING_CHANNEL

async def handle_channel_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.message.chat_id

    channel_index = int(query.data.replace("ch_", ""))
    channel_name = CHANNEL_NAMES[channel_index]
    video_store[user_id]["channel"] = channel_name
    video_store[user_id]["selected_platforms"] = []

    # Build platform selection buttons
    channel_platforms = CHANNELS_CONFIG[channel_name]["platforms"]
    keyboard = []
    for platform, config in channel_platforms.items():
        label = PLATFORM_EMOJI.get(platform, platform.title())
        if not config.get("enabled", False):
            label = f"🔒 {label} (not set up)"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"plt_{platform}")])

    keyboard.append([InlineKeyboardButton("✅ Done selecting platforms", callback_data="plt_done")])

    await query.edit_message_text(
        f"✅ Channel: *{channel_name}*\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "📝 *Step 2 of 7 — Select Platforms*\n"
        "━━━━━━━━━━━━━━━━\n\n"
        "Which platforms should this be posted to?\n"
        "Tap to select/deselect. Then tap Done.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return WAITING_PLATFORMS

async def handle_platform_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.message.chat_id

    if query.data == "plt_done":
        selected = video_store[user_id]["selected_platforms"]
        if not selected:
            await query.answer("Please select at least one platform!", show_alert=True)
            return WAITING_PLATFORMS

        await query.edit_message_text(
            f"✅ Platforms selected: {', '.join([PLATFORM_EMOJI.get(p, p) for p in selected])}\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "📝 *Step 3 of 7 — Title*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "What is the *title* of this video?\n\n"
            "Example:\n_'I found out my husband had a secret family'_",
            parse_mode="Markdown"
        )
        return WAITING_TITLE

    platform = query.data.replace("plt_", "")
    selected = video_store[user_id]["selected_platforms"]
    channel_name = video_store[user_id]["channel"]
    channel_platforms = CHANNELS_CONFIG[channel_name]["platforms"]

    if platform in selected:
        selected.remove(platform)
    else:
        selected.append(platform)

    video_store[user_id]["selected_platforms"] = selected

    # Rebuild buttons with checkmarks
    keyboard = []
    for plt, config in channel_platforms.items():
        label = PLATFORM_EMOJI.get(plt, plt.title())
        if plt in selected:
            label = f"✅ {label}"
        elif not config.get("enabled", False):
            label = f"🔒 {label} (not set up)"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"plt_{plt}")])

    keyboard.append([InlineKeyboardButton("✅ Done selecting platforms", callback_data="plt_done")])

    await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))
    return WAITING_PLATFORMS

async def handle_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return WAITING_TITLE

    user_id = message.chat_id
    title = message.text.strip()

    if len(title) > 70:
        await message.reply_text(f"⚠️ Title too long ({len(title)} chars). Max is 70. Please shorten:")
        return WAITING_TITLE

    video_store[user_id]["title"] = title

    await message.reply_text(
        f"✅ *Title saved!*\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "📝 *Step 4 of 7 — Description*\n"
        "━━━━━━━━━━━━━━━━\n\n"
        "Add *extra text* at the top of the description?\n\n"
        "Or type *skip* to use the default.",
        parse_mode="Markdown"
    )
    return WAITING_DESCRIPTION

async def handle_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return WAITING_DESCRIPTION

    user_id = message.chat_id
    desc = message.text.strip()
    video_store[user_id]["description"] = "" if desc.lower() == "skip" else desc

    keyboard = [[
        InlineKeyboardButton("✅ Yes, part of a series", callback_data="series_yes"),
        InlineKeyboardButton("❌ No, standalone", callback_data="series_no")
    ]]

    await message.reply_text(
        "━━━━━━━━━━━━━━━━\n"
        "📝 *Step 5 of 7 — Series*\n"
        "━━━━━━━━━━━━━━━━\n\n"
        "Is this video part of a *series*?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return WAITING_SERIES

async def handle_series_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.message.chat_id

    if query.data == "series_no":
        video_store[user_id]["series"] = ""
        video_store[user_id]["part"] = ""
        await query.edit_message_text("✅ Standalone video noted!")
        keyboard = [[
            InlineKeyboardButton("🌍 Public", callback_data="privacy_public"),
            InlineKeyboardButton("🔒 Private", callback_data="privacy_private"),
            InlineKeyboardButton("🔗 Unlisted", callback_data="privacy_unlisted")
        ]]
        await query.message.reply_text(
            "━━━━━━━━━━━━━━━━\n"
            "📝 *Step 6 of 7 — Privacy*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "What should the *privacy setting* be?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return WAITING_PRIVACY
    else:
        await query.edit_message_text(
            "What is the *name of the series*?\n\n"
            "Example: _'Secret Lives'_",
            parse_mode="Markdown"
        )
        return WAITING_SERIES

async def handle_series_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return WAITING_SERIES

    user_id = message.chat_id
    video_store[user_id]["series"] = message.text.strip()

    await message.reply_text(
        f"✅ Series saved!\n\nWhat *part number* is this?\nExample: _1_, _2_, _3_",
        parse_mode="Markdown"
    )
    return WAITING_PART

async def handle_part(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return WAITING_PART

    user_id = message.chat_id
    video_store[user_id]["part"] = message.text.strip()

    keyboard = [[
        InlineKeyboardButton("🌍 Public", callback_data="privacy_public"),
        InlineKeyboardButton("🔒 Private", callback_data="privacy_private"),
        InlineKeyboardButton("🔗 Unlisted", callback_data="privacy_unlisted")
    ]]

    await message.reply_text(
        "━━━━━━━━━━━━━━━━\n"
        "📝 *Step 6 of 7 — Privacy*\n"
        "━━━━━━━━━━━━━━━━\n\n"
        "What should the *privacy setting* be?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return WAITING_PRIVACY

async def handle_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.message.chat_id
    privacy = query.data.replace("privacy_", "")
    video_store[user_id]["privacy"] = privacy

    data = video_store[user_id]
    full_title = build_full_title(data["title"], data["series"], data["part"])
    if len(full_title) > 70:
        full_title = full_title[:67] + "..."

    privacy_emoji = {"public": "🌍", "private": "🔒", "unlisted": "🔗"}
    platforms_text = "\n".join([f"  • {PLATFORM_EMOJI.get(p, p)}" for p in data["selected_platforms"]])
    series_text = f"📺 {data['series']} | Part {data['part']}" if data["series"] else "📺 Standalone"

    keyboard = [[
        InlineKeyboardButton("✅ Upload Now!", callback_data="confirm_upload"),
        InlineKeyboardButton("❌ Cancel", callback_data="cancel_upload")
    ]]

    await query.edit_message_text(
        "━━━━━━━━━━━━━━━━\n"
        "📋 *Step 7 of 7 — Confirm*\n"
        "━━━━━━━━━━━━━━━━\n\n"
        f"📡 *Channel:* {data['channel']}\n"
        f"🎬 *Title:* {full_title}\n"
        f"{series_text}\n"
        f"{privacy_emoji.get(privacy, '🌍')} *Privacy:* {privacy.capitalize()}\n\n"
        f"📤 *Posting to:*\n{platforms_text}\n\n"
        "Ready to upload?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return CONFIRM_UPLOAD

async def confirm_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    await query.edit_message_text(
        "⏳ *Uploading to all platforms...*\n\nThis may take a few minutes. Please wait!",
        parse_mode="Markdown"
    )

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

    results = await upload_to_platforms(
        data["video_path"],
        metadata,
        data["channel"],
        data["selected_platforms"]
    )

    # Build results message
    results_text = "\n".join([f"  • {PLATFORM_EMOJI.get(p, p)}: {url}" for p, url in results.items()])

    await context.bot.send_message(
        chat_id=user_id,
        text=f"🎉 *Upload Complete!*\n\n"
             f"📡 *Channel:* {data['channel']}\n"
             f"📺 *Title:* {full_title}\n\n"
             f"📤 *Results:*\n{results_text}\n\n"
             f"Send your next video whenever you're ready! 🚀",
        parse_mode="Markdown"
    )

    try:
        os.unlink(data["video_path"])
    except:
        pass
    del video_store[user_id]
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.chat_id
    if user_id in video_store:
        try:
            os.unlink(video_store[user_id]["video_path"])
        except:
            pass
        del video_store[user_id]
    await update.message.reply_text("❌ Cancelled. Send a new video whenever you're ready!")
    return ConversationHandler.END

def main():
    logger.info("Starting True Stories Bot...")
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video)],
        states={
            WAITING_CHANNEL: [CallbackQueryHandler(handle_channel_selection, pattern="^ch_")],
            WAITING_PLATFORMS: [CallbackQueryHandler(handle_platform_selection, pattern="^plt_")],
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

    # Auto-stop after 30 minutes of inactivity
    # This prevents GitHub Actions from running forever
    import asyncio
    import signal

    async def run_with_timeout():
        await app.initialize()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
        await app.start()
        logger.info("Bot running! Will auto-stop after 30 minutes of inactivity.")

        # Run for max 25 minutes (GitHub Actions timeout is 6hrs but we want to stop early)
        await asyncio.sleep(25 * 60)

        logger.info("25 minute limit reached — stopping bot.")
        await app.updater.stop()
        await app.stop()
        await app.shutdown()

    asyncio.run(run_with_timeout())

if __name__ == "__main__":
    main()
