import asyncio
import logging
import os
from typing import Dict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

# Import the SDK
from bridge_to_success_sdk import BridgeToSuccess, BTSAuthError, BTSAPIError

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Global store for user sessions (key: telegram user_id, value: BridgeToSuccess instance)
user_sessions: Dict[int, BridgeToSuccess] = {}

# Helper to run SDK methods in a thread
async def run_sdk_method(method, *args, **kwargs):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: method(*args, **kwargs))

# --- Bot command handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎓 *Bridge to Success Bot*\n\n"
        "I can help you access your courses, videos, and PDFs.\n"
        "Use /login <mobile> <password> to authenticate.\n"
        "Then try /courses, /videos, /pdfs, /scrape, /download_pdf.\n"
        "Use /logout to clear your session.",
        parse_mode="Markdown"
    )

async def login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /login <mobile> <password>")
        return
    mobile, password = args[0], args[1]

    # Create a session store for this user (in-memory dict)
    store = {}
    app = BridgeToSuccess(session_store=store, verbose=False)
    try:
        await run_sdk_method(app.login, mobile, password)
        user_sessions[user_id] = app
        await update.message.reply_text(f"✅ Logged in as {app.name} ({app.mobile})")
    except BTSAuthError as e:
        await update.message.reply_text(f"❌ Login failed: {e}")
    except Exception as e:
        logger.exception("Login error")
        await update.message.reply_text(f"❌ Unexpected error: {e}")

async def logout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in user_sessions:
        app = user_sessions.pop(user_id)
        await run_sdk_method(app.logout)
        await update.message.reply_text("👋 Logged out.")
    else:
        await update.message.reply_text("You are not logged in.")

async def courses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    app = user_sessions.get(update.effective_user.id)
    if not app or not app.is_logged_in():
        await update.message.reply_text("Please login first using /login")
        return
    try:
        courses = await run_sdk_method(app.get_my_courses)
        if not courses:
            await update.message.reply_text("You are not enrolled in any courses.")
            return
        msg = "📚 *Your Courses:*\n\n"
        for idx, c in enumerate(courses, 1):
            name = c.get("name") or c.get("course_name") or "Unnamed"
            cid = c.get("id") or c.get("course_id")
            msg += f"{idx}. [{cid}] {name}\n"
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def videos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 3:
        await update.message.reply_text("Usage: /videos <course_id> <subject_id> <chapter_id>")
        return
    course_id, subject_id, chapter_id = args[0], args[1], args[2]
    app = user_sessions.get(update.effective_user.id)
    if not app or not app.is_logged_in():
        await update.message.reply_text("Please login first")
        return
    try:
        vlist = await run_sdk_method(app.get_video_list, course_id, subject_id, chapter_id)
        if not vlist:
            await update.message.reply_text("No videos found.")
            return
        # Build a message with clickable links (Markdown)
        msg = "🎬 *Videos:*\n\n"
        for v in vlist:
            title = v.get("title") or v.get("name") or "Untitled"
            url = v.get("play_url") or "No URL"
            msg += f"• [{title}]({url})\n"
        await update.message.reply_text(msg, parse_mode="Markdown", disable_web_page_preview=True)
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def pdfs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 3:
        await update.message.reply_text("Usage: /pdfs <course_id> <subject_id> <chapter_id>")
        return
    course_id, subject_id, chapter_id = args[0], args[1], args[2]
    app = user_sessions.get(update.effective_user.id)
    if not app or not app.is_logged_in():
        await update.message.reply_text("Please login first")
        return
    try:
        plist = await run_sdk_method(app.get_pdf_list, course_id, subject_id, chapter_id)
        if not plist:
            await update.message.reply_text("No PDFs found.")
            return
        msg = "📄 *PDFs:*\n\n"
        for p in plist:
            title = p.get("title") or p.get("name") or "Untitled"
            url = p.get("pdf_full_url") or "No URL"
            msg += f"• [{title}]({url})\n"
        await update.message.reply_text(msg, parse_mode="Markdown", disable_web_page_preview=True)
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def download_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 1:
        await update.message.reply_text("Usage: /download_pdf <pdf_url_or_filename>")
        return
    url_or_name = args[0]
    app = user_sessions.get(update.effective_user.id)
    if not app or not app.is_logged_in():
        await update.message.reply_text("Please login first")
        return
    # We'll download and send the file
    await update.message.reply_text("⏳ Downloading PDF...")
    try:
        # Run download in thread
        save_path = await run_sdk_method(app.download_pdf, url_or_name, show_progress=False)
        with open(save_path, "rb") as f:
            await update.message.reply_document(document=f, filename=os.path.basename(save_path))
        os.remove(save_path)  # cleanup
    except Exception as e:
        await update.message.reply_text(f"Download failed: {e}")

async def scrape(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 1:
        await update.message.reply_text("Usage: /scrape <course_id>")
        return
    course_id = args[0]
    app = user_sessions.get(update.effective_user.id)
    if not app or not app.is_logged_in():
        await update.message.reply_text("Please login first")
        return
    await update.message.reply_text("⏳ Scraping course content (this may take a while)...")
    try:
        # Run scrape in thread
        result = await run_sdk_method(app.scrape_course, course_id)
        msg = f"✅ Scraped *{result.get('course', '')}*\n"
        msg += f"Videos: {len(result.get('videos', []))}\n"
        msg += f"PDFs: {len(result.get('pdfs', []))}\n"
        # Provide first few links as sample
        if result.get('videos'):
            msg += "\n*Sample video:*\n" + result['videos'][0].get('play_url', 'N/A')
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

# --- Main ---

def main():
    # Get token from environment variable
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN environment variable not set")

    # Create the Application
    application = Application.builder().token(token).build()

    # Command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("login", login))
    application.add_handler(CommandHandler("logout", logout))
    application.add_handler(CommandHandler("courses", courses))
    application.add_handler(CommandHandler("videos", videos))
    application.add_handler(CommandHandler("pdfs", pdfs))
    application.add_handler(CommandHandler("download_pdf", download_pdf))
    application.add_handler(CommandHandler("scrape", scrape))

    # Start the bot
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
