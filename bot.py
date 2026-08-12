import asyncio
import logging
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Dict

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from bridge_to_success_sdk import BridgeToSuccess, BTSAuthError, BTSAPIError

# ─── Logging ──────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── In-memory sessions ─────────────────────────────────────────
user_sessions: Dict[int, BridgeToSuccess] = {}

# ─── SDK executor ──────────────────────────────────────────────
async def run_sdk_method(method, *args, **kwargs):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: method(*args, **kwargs))

# ─── Bot command handlers ──────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Plain text – no Markdown to avoid parse errors
    await update.message.reply_text(
        "🎓 Bridge to Success Bot\n\n"
        "I can help you access your courses, videos, and PDFs.\n"
        "Use /login mobile password to authenticate.\n"
        "Then try /courses, /videos, /pdfs, /scrape, /download_pdf.\n"
        "Use /logout to clear your session."
    )

async def login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /login mobile password")
        return
    mobile, password = args[0], args[1]
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
        await update.message.reply_text("Usage: /videos course_id subject_id chapter_id")
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
        msg = "🎬 *Videos:*\n\n"
        for v in vlist:
            title = v.get("title") or v.get("name") or "Untitled"
            url = v.get("play_url") or "No URL"
            # Use inline links – Markdown is safe here
            msg += f"• [{title}]({url})\n"
        await update.message.reply_text(msg, parse_mode="Markdown", disable_web_page_preview=True)
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def pdfs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 3:
        await update.message.reply_text("Usage: /pdfs course_id subject_id chapter_id")
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
        await update.message.reply_text("Usage: /download_pdf pdf_url_or_filename")
        return
    url_or_name = args[0]
    app = user_sessions.get(update.effective_user.id)
    if not app or not app.is_logged_in():
        await update.message.reply_text("Please login first")
        return
    await update.message.reply_text("⏳ Downloading PDF...")
    try:
        save_path = await run_sdk_method(app.download_pdf, url_or_name, show_progress=False)
        with open(save_path, "rb") as f:
            await update.message.reply_document(document=f, filename=os.path.basename(save_path))
        os.remove(save_path)
    except Exception as e:
        await update.message.reply_text(f"Download failed: {e}")

async def scrape(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 1:
        await update.message.reply_text("Usage: /scrape course_id")
        return
    course_id = args[0]
    app = user_sessions.get(update.effective_user.id)
    if not app or not app.is_logged_in():
        await update.message.reply_text("Please login first")
        return
    await update.message.reply_text("⏳ Scraping course content (this may take a while)...")
    try:
        result = await run_sdk_method(app.scrape_course, course_id)
        msg = f"✅ Scraped *{result.get('course', '')}*\n"
        msg += f"Videos: {len(result.get('videos', []))}\n"
        msg += f"PDFs: {len(result.get('pdfs', []))}\n"
        if result.get('videos'):
            msg += "\n*Sample video:*\n" + result['videos'][0].get('play_url', 'N/A')
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

# ─── Simple HTTP server for Koyeb health checks ────────────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def run_http_server():
    server = HTTPServer(("0.0.0.0", 8000), HealthHandler)
    logger.info("Health check server running on port 8000")
    server.serve_forever()

# ─── Main ──────────────────────────────────────────────────────
def main():
    # Start HTTP server in a background thread
    thread = threading.Thread(target=run_http_server, daemon=True)
    thread.start()

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN environment variable not set")

    application = Application.builder().token(token).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("login", login))
    application.add_handler(CommandHandler("logout", logout))
    application.add_handler(CommandHandler("courses", courses))
    application.add_handler(CommandHandler("videos", videos))
    application.add_handler(CommandHandler("pdfs", pdfs))
    application.add_handler(CommandHandler("download_pdf", download_pdf))
    application.add_handler(CommandHandler("scrape", scrape))

    # Start polling (this will block)
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
