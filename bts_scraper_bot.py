"""
Bridge to Success — Dev Scraper Bot
Fixed: Login debug + better error reporting
"""

import logging
import os
import threading
import requests
import json
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters, ConversationHandler
)

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
PORT      = int(os.environ.get("PORT", 8000))

BASE_URL = "https://bridgetosuccess.learncentre.tech"
API_BASE = f"{BASE_URL}/public/study_api_sprint13_security_promo/"

STORAGE = {
    "video" : f"{BASE_URL}/public/storage/video/",
    "pdf"   : f"{BASE_URL}/public/storage/pdf/",
    "ebook" : f"{BASE_URL}/public/storage/ebook/",
}

# ─────────────────────────────────────────────────────────────────────────────
# HEALTH CHECK SERVER
# ─────────────────────────────────────────────────────────────────────────────

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        pass

def run_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    server.serve_forever()

# ─────────────────────────────────────────────────────────────────────────────
# CONVERSATION STATES
# ─────────────────────────────────────────────────────────────────────────────
MOBILE, PASSWORD = range(2)

# ─────────────────────────────────────────────────────────────────────────────
# SESSION STORE
# ─────────────────────────────────────────────────────────────────────────────
sessions: dict[int, dict] = {}

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# API HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def get_headers(uid: int = 0) -> dict:
    h = {
        "Content-Type" : "application/json",
        "Accept"       : "application/json",
        "User-Agent"   : "okhttp/4.9.3",
    }
    if uid and uid in sessions:
        token = sessions[uid].get("token", "")
        if token:
            h["Authorization"] = f"Bearer {token}"
            h["authtoken"]     = token
    return h


def api_post(endpoint: str, data: dict, uid: int = 0) -> dict:
    url = API_BASE + endpoint
    try:
        logger.info(f"POST {url} | payload: {data}")
        r = requests.post(url, json=data, headers=get_headers(uid), timeout=20)
        logger.info(f"Response [{r.status_code}]: {r.text[:300]}")
        return r.json()
    except Exception as e:
        logger.error(f"api_post error: {e}")
        return {"status": 0, "message": str(e)}


def api_get(endpoint: str, params: dict = None, uid: int = 0) -> dict:
    url = API_BASE + endpoint
    try:
        r = requests.get(url, params=params, headers=get_headers(uid), timeout=20)
        return r.json()
    except Exception as e:
        return {"status": 0, "message": str(e)}


def as_list(data) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return list(data.values())
    return []

# ─────────────────────────────────────────────────────────────────────────────
# SMART LOGIN — tries all known endpoint + field combos
# ─────────────────────────────────────────────────────────────────────────────

def try_login(mobile: str, password: str) -> dict:
    """
    Try every known login endpoint + field name combination.
    Returns the first successful response, or the last failed one.
    """
    attempts = [
        # endpoint              payload
        ("login",               {"mobile": mobile,   "password": password}),
        ("login",               {"phone": mobile,    "password": password}),
        ("login",               {"username": mobile, "password": password}),
        ("login",               {"mobile_no": mobile,"password": password}),
        ("user-login",          {"mobile": mobile,   "password": password}),
        ("student-login",       {"mobile": mobile,   "password": password}),
        ("auth/login",          {"mobile": mobile,   "password": password}),
        # OTP-less password login variant
        ("login",               {"mobile": mobile,   "pass": password}),
        ("login",               {"mobile": mobile,   "pwd": password}),
    ]

    last = {}
    for endpoint, payload in attempts:
        resp = api_post(endpoint, payload)
        logger.info(f"Login attempt [{endpoint}] -> status={resp.get('status')} msg={resp.get('message','')}")
        if resp.get("status") == 1:
            return resp
        last = resp

    return last   # return last failed response for error display


def extract_token(data: dict) -> str:
    d = data.get("data") or data
    if isinstance(d, dict):
        for key in ["token", "authtoken", "api_token", "access_token",
                    "auth_token", "user_token", "sessionToken"]:
            val = d.get(key, "")
            if val and isinstance(val, str) and len(val) > 5:
                return val
        # Nested under 'user'
        user = d.get("user", {})
        if isinstance(user, dict):
            for key in ["token", "authtoken", "api_token", "access_token"]:
                val = user.get(key, "")
                if val:
                    return val
    return ""

# ─────────────────────────────────────────────────────────────────────────────
# LINK EXTRACTORS
# ─────────────────────────────────────────────────────────────────────────────

def get_video_url(v: dict) -> str:
    for key in ["video_url", "url", "file_url", "hls_url", "stream_url", "link", "video_link"]:
        val = v.get(key, "")
        if val and isinstance(val, str):
            if val.startswith("http"):
                return val
            if len(val) > 3:
                return STORAGE["video"] + val
    vid_id = v.get("id") or v.get("video_id") or ""
    if vid_id:
        return f"https://lctplayer.learncentre.online/v/player.php?v={vid_id}"
    return "N/A"


def get_pdf_url(p: dict) -> str:
    for key in ["pdf_url", "url", "file_url", "file", "link", "pdf_file", "pdf_link"]:
        val = p.get(key, "")
        if val and isinstance(val, str):
            if val.startswith("http"):
                return val
            if len(val) > 3:
                return STORAGE["pdf"] + val
    return "N/A"

# ─────────────────────────────────────────────────────────────────────────────
# SCRAPER
# ─────────────────────────────────────────────────────────────────────────────

def scrape_course(course_id, course_name: str, uid: int) -> list:
    results = []

    subj_r   = api_post("get-subject-list", {"course_id": course_id}, uid)
    subjects = as_list(subj_r.get("data", []))
    if not subjects:
        subjects = [{"id": None, "name": "General"}]

    for subj in subjects:
        subj_id   = subj.get("id") or subj.get("subject_id")
        subj_name = subj.get("name") or subj.get("subject_name") or "General"

        chap_r   = api_post("get-chapter-list",
                            {"course_id": course_id, "subject_id": subj_id}, uid)
        chapters = as_list(chap_r.get("data", []))
        if not chapters:
            chapters = [{"id": None, "name": "General"}]

        for chap in chapters:
            chap_id   = chap.get("id") or chap.get("chapter_id")
            chap_name = chap.get("name") or chap.get("chapter_name") or "General"

            payload = {
                "course_id"  : course_id,
                "subject_id" : subj_id,
                "chapter_id" : chap_id,
            }

            vid_r = api_post("get-video-list", payload, uid)
            for v in as_list(vid_r.get("data", [])):
                results.append({
                    "type"    : "VIDEO",
                    "course"  : course_name,
                    "subject" : subj_name,
                    "chapter" : chap_name,
                    "title"   : v.get("title") or v.get("name") or "Untitled",
                    "url"     : get_video_url(v),
                    "extra"   : v.get("video_type") or v.get("type") or "",
                })

            pdf_r = api_post("get-pdf-list", payload, uid)
            for p in as_list(pdf_r.get("data", [])):
                results.append({
                    "type"    : "PDF",
                    "course"  : course_name,
                    "subject" : subj_name,
                    "chapter" : chap_name,
                    "title"   : p.get("title") or p.get("name") or "Untitled",
                    "url"     : get_pdf_url(p),
                    "extra"   : "",
                })

    return results


def make_chunks(items: list, filter_type: str = None) -> list[str]:
    filtered = [i for i in items if not filter_type or i["type"] == filter_type]
    parts, cur = [], ""
    for idx, item in enumerate(filtered, 1):
        icon = "🎬" if item["type"] == "VIDEO" else "📄"
        line = (
            f"{icon} *{idx}. {item['title']}*\n"
            f"   📂 {item['course']} › {item['subject']} › {item['chapter']}\n"
            f"   🔗 `{item['url']}`\n\n"
        )
        if len(cur) + len(line) > 4000:
            parts.append(cur)
            cur = line
        else:
            cur += line
    if cur:
        parts.append(cur)
    return parts or ["Nothing found."]

# ─────────────────────────────────────────────────────────────────────────────
# BOT HANDLERS
# ─────────────────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid    = update.effective_user.id
    status = f"✅ Logged in as *{sessions[uid]['name']}*" if uid in sessions else "❌ Not logged in"
    kb = [
        [InlineKeyboardButton("🔑 Login",               callback_data="do_login")],
        [InlineKeyboardButton("📦 All Courses + Links", callback_data="do_all")],
        [InlineKeyboardButton("🎬 Videos Only",         callback_data="do_videos")],
        [InlineKeyboardButton("📄 PDFs Only",           callback_data="do_pdfs")],
        [InlineKeyboardButton("💾 Export JSON",         callback_data="do_json")],
        [InlineKeyboardButton("🚪 Logout",              callback_data="do_logout")],
    ]
    await update.message.reply_text(
        f"🎓 *Bridge to Success — Dev Scraper*\n\nStatus: {status}\n\nChoose an action:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb)
    )

# ── LOGIN CONVERSATION ───────────────────────────────────────────────────────

async def login_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q      = update.callback_query
    target = q.message if q else update.message
    if q:
        await q.answer()
    await target.reply_text("📱 Enter your *mobile number*:", parse_mode="Markdown")
    return MOBILE


async def got_mobile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mobile = update.message.text.strip()
    if not mobile.isdigit():
        await update.message.reply_text("❌ Digits only. Try again:")
        return MOBILE
    context.user_data["mobile"] = mobile
    await update.message.reply_text("🔒 Enter your *password*:", parse_mode="Markdown")
    return PASSWORD


async def got_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid      = update.effective_user.id
    mobile   = context.user_data.get("mobile", "")
    password = update.message.text.strip()

    await update.message.reply_text("⏳ Trying to login, please wait...")

    resp = try_login(mobile, password)

    if resp.get("status") == 1:
        token = extract_token(resp)
        d     = resp.get("data") or {}
        if isinstance(d, dict):
            name = (d.get("name") or d.get("full_name") or
                    d.get("student_name") or d.get("user_name") or mobile)
            uid_api = d.get("id") or d.get("user_id") or ""
        else:
            name    = mobile
            uid_api = ""

        sessions[uid] = {
            "token"   : token,
            "mobile"  : mobile,
            "name"    : name,
            "user_id" : str(uid_api),
        }
        await update.message.reply_text(
            f"✅ *Logged in as {name}!*\n\n"
            f"Token: `{token[:30]}...`\n\n"
            "Use /start to scrape courses.",
            parse_mode="Markdown"
        )
    else:
        # Show full raw response for debugging
        raw = json.dumps(resp, indent=2)[:800]
        await update.message.reply_text(
            f"❌ *Login failed.*\n\n"
            f"*Raw API response:*\n```{raw}```\n\n"
            "Check the mobile number and password and try /login again.\n"
            "If the app uses OTP login, use /otp instead.",
            parse_mode="Markdown"
        )

    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END

# ── OTP LOGIN (separate flow if app doesn't use password) ───────────────────

OTP_MOBILE, OTP_CODE = range(2, 4)

async def otp_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📱 Enter your *mobile number* for OTP login:",
                                    parse_mode="Markdown")
    return OTP_MOBILE


async def otp_got_mobile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mobile = update.message.text.strip()
    if not mobile.isdigit():
        await update.message.reply_text("❌ Digits only. Try again:")
        return OTP_MOBILE
    context.user_data["otp_mobile"] = mobile

    # Send OTP
    resp = api_post("send-otp", {"mobile": mobile, "type": "login"})
    if resp.get("status") != 1:
        resp = api_post("send-otp", {"mobile": mobile})
    if resp.get("status") != 1:
        resp = api_post("get-otp",  {"mobile": mobile})

    if resp.get("status") == 1:
        await update.message.reply_text(
            f"✅ OTP sent to {mobile}.\nEnter the *OTP* below:", parse_mode="Markdown"
        )
    else:
        raw = json.dumps(resp)[:300]
        await update.message.reply_text(
            f"⚠️ OTP send response: `{raw}`\n\nEnter OTP if you received one anyway:",
            parse_mode="Markdown"
        )
    return OTP_CODE


async def otp_got_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid    = update.effective_user.id
    mobile = context.user_data.get("otp_mobile", "")
    otp    = update.message.text.strip()

    await update.message.reply_text("⏳ Verifying OTP...")

    attempts = [
        ("verify-otp", {"mobile": mobile, "otp": otp}),
        ("login",      {"mobile": mobile, "otp": otp}),
        ("login",      {"mobile": mobile, "otp": otp, "type": "login"}),
        ("otp-login",  {"mobile": mobile, "otp": otp}),
    ]

    resp = {}
    for endpoint, payload in attempts:
        resp = api_post(endpoint, payload)
        if resp.get("status") == 1:
            break

    if resp.get("status") == 1:
        token = extract_token(resp)
        d     = resp.get("data") or {}
        name  = (d.get("name") or d.get("full_name") or
                 d.get("student_name") or mobile) if isinstance(d, dict) else mobile
        sessions[uid] = {"token": token, "mobile": mobile, "name": name}
        await update.message.reply_text(
            f"✅ *Logged in as {name}!*\nUse /start to continue.",
            parse_mode="Markdown"
        )
    else:
        raw = json.dumps(resp, indent=2)[:600]
        await update.message.reply_text(
            f"❌ OTP verification failed.\n\n*Raw response:*\n```{raw}```",
            parse_mode="Markdown"
        )
    return ConversationHandler.END

# ── SCRAPE HANDLERS ──────────────────────────────────────────────────────────

async def do_scrape(update: Update, context: ContextTypes.DEFAULT_TYPE,
                    filter_type: str = None, export_json: bool = False):
    uid = update.effective_user.id
    msg = update.callback_query.message if update.callback_query else update.message

    if uid not in sessions:
        await msg.reply_text("⚠️ Please /login or /otp first.")
        return

    await msg.reply_text("🔍 Fetching courses...")

    my_r    = api_get("get-my-courses", uid=uid)
    courses = as_list(my_r.get("data", []))
    if not courses:
        all_r   = api_get("get-all-courses", uid=uid)
        courses = as_list(all_r.get("data", []))

    if not courses:
        # Show raw response for debug
        raw = json.dumps(my_r)[:400]
        await msg.reply_text(
            f"⚠️ No courses found.\n\n*API response:*\n```{raw}```",
            parse_mode="Markdown"
        )
        return

    await msg.reply_text(f"📚 Found *{len(courses)}* course(s). Scraping...",
                         parse_mode="Markdown")

    all_items = []
    for c in courses:
        cid   = c.get("id") or c.get("course_id")
        cname = c.get("name") or c.get("course_name") or f"Course-{cid}"
        await msg.reply_text(f"⚙️ *{cname}*", parse_mode="Markdown")
        all_items.extend(scrape_course(cid, cname, uid))

    if not all_items:
        await msg.reply_text("⚠️ No links found in any course.")
        return

    vids = [i for i in all_items if i["type"] == "VIDEO"]
    pdfs = [i for i in all_items if i["type"] == "PDF"]

    await msg.reply_text(
        f"✅ *Done!*\n🎬 Videos: {len(vids)}\n📄 PDFs: {len(pdfs)}\n📦 Total: {len(all_items)}",
        parse_mode="Markdown"
    )

    if export_json:
        data = json.dumps(all_items, indent=2, ensure_ascii=False).encode()
        await msg.reply_document(document=data, filename="course_links.json",
                                 caption="📦 All links exported.")
        return

    for t in (["VIDEO", "PDF"] if not filter_type else [filter_type]):
        filtered = [i for i in all_items if i["type"] == t]
        if not filtered:
            continue
        icon = "🎬" if t == "VIDEO" else "📄"
        await msg.reply_text(f"*{icon} {t} LINKS ({len(filtered)}):*",
                             parse_mode="Markdown")
        for chunk in make_chunks(all_items, filter_type=t):
            await msg.reply_text(chunk, parse_mode="Markdown",
                                 disable_web_page_preview=True)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q      = update.callback_query
    await q.answer()
    uid    = update.effective_user.id
    action = q.data

    if   action == "do_login"  : await login_entry(update, context)
    elif action == "do_all"    : await do_scrape(update, context)
    elif action == "do_videos" : await do_scrape(update, context, filter_type="VIDEO")
    elif action == "do_pdfs"   : await do_scrape(update, context, filter_type="PDF")
    elif action == "do_json"   : await do_scrape(update, context, export_json=True)
    elif action == "do_logout" :
        sessions.pop(uid, None)
        await q.message.reply_text("🚪 Logged out.")

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        raise ValueError("Set BOT_TOKEN environment variable!")

    # Start health check HTTP server (Koyeb TCP check)
    t = threading.Thread(target=run_health_server, daemon=True)
    t.start()
    logger.info(f"Health check server on port {PORT}")

    app = Application.builder().token(BOT_TOKEN).build()

    # Password login conversation
    login_conv = ConversationHandler(
        entry_points=[
            CommandHandler("login", login_entry),
            CallbackQueryHandler(login_entry, pattern="^do_login$"),
        ],
        states={
            MOBILE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, got_mobile)],
            PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_password)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_chat=True,
        per_message=False,
    )

    # OTP login conversation
    otp_conv = ConversationHandler(
        entry_points=[CommandHandler("otp", otp_start)],
        states={
            OTP_MOBILE: [MessageHandler(filters.TEXT & ~filters.COMMAND, otp_got_mobile)],
            OTP_CODE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, otp_got_code)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_chat=True,
        per_message=False,
    )

    app.add_handler(login_conv)
    app.add_handler(otp_conv)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))

    print("🤖 Bot is running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
