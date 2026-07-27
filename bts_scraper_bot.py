"""
Bridge to Success — Developer Course Scraper Bot
Login via mobile + password, fetch all courses and their content links.
Reads BOT_TOKEN from environment variable (Koyeb-friendly).
"""

import logging
import os
import requests
import json
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters, ConversationHandler
)

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

BASE_URL = "https://bridgetosuccess.learncentre.tech"
API_BASE = f"{BASE_URL}/public/study_api_sprint13_security_promo/"

STORAGE = {
    "video" : f"{BASE_URL}/public/storage/video/",
    "pdf"   : f"{BASE_URL}/public/storage/pdf/",
    "ebook" : f"{BASE_URL}/public/storage/ebook/",
}

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

# ─────────────────────────────────────────────────────────────────────────────
# API HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def get_headers(uid: int) -> dict:
    h = {
        "Content-Type": "application/json",
        "Accept":       "application/json",
        "User-Agent":   "okhttp/4.9.3",
    }
    if uid in sessions:
        token = sessions[uid].get("token", "")
        if token:
            h["Authorization"] = f"Bearer {token}"
            h["authtoken"]     = token
    return h


def api_post(endpoint: str, data: dict, uid: int = 0) -> dict:
    try:
        r = requests.post(
            API_BASE + endpoint,
            json=data,
            headers=get_headers(uid),
            timeout=20
        )
        return r.json()
    except Exception as e:
        return {"status": 0, "message": str(e)}


def api_get(endpoint: str, params: dict = None, uid: int = 0) -> dict:
    try:
        r = requests.get(
            API_BASE + endpoint,
            params=params,
            headers=get_headers(uid),
            timeout=20
        )
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
# LINK EXTRACTORS
# ─────────────────────────────────────────────────────────────────────────────

def get_video_url(v: dict) -> str:
    for key in ["video_url", "url", "file_url", "hls_url", "stream_url", "link"]:
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
    for key in ["pdf_url", "url", "file_url", "file", "link", "pdf_file"]:
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

            # Videos
            vid_r  = api_post("get-video-list", payload, uid)
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

            # PDFs
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
    status = "✅ Logged in" if uid in sessions else "❌ Not logged in"
    kb = [
        [InlineKeyboardButton("🔑 Login",                callback_data="do_login")],
        [InlineKeyboardButton("📦 All Courses + Links",  callback_data="do_all")],
        [InlineKeyboardButton("🎬 Videos Only",          callback_data="do_videos")],
        [InlineKeyboardButton("📄 PDFs Only",            callback_data="do_pdfs")],
        [InlineKeyboardButton("💾 Export JSON",          callback_data="do_json")],
        [InlineKeyboardButton("🚪 Logout",               callback_data="do_logout")],
    ]
    await update.message.reply_text(
        f"🎓 *Bridge to Success — Dev Scraper*\n\nStatus: {status}\n\nChoose an action:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb)
    )


# ── LOGIN CONVERSATION ───────────────────────────────────────────────────────

async def login_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
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

    await update.message.reply_text("⏳ Logging in...")

    resp = api_post("login", {"mobile": mobile, "password": password})
    if resp.get("status") != 1:
        resp = api_post("login", {"phone": mobile, "password": password})
    if resp.get("status") != 1:
        resp = api_post("login", {"username": mobile, "password": password})

    if resp.get("status") == 1:
        d     = resp.get("data", {})
        token = (d.get("token") or d.get("authtoken") or
                 d.get("api_token") or d.get("access_token") or "")
        name  = d.get("name") or d.get("full_name") or mobile
        sessions[uid] = {"token": token, "mobile": mobile, "name": name}
        await update.message.reply_text(
            f"✅ *Logged in as {name}!*\n\nUse /start to continue.",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            f"❌ Failed: {resp.get('message', 'Check credentials.')}\nTry /login again."
        )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# ── SCRAPE ───────────────────────────────────────────────────────────────────

async def do_scrape(update: Update, context: ContextTypes.DEFAULT_TYPE,
                    filter_type: str = None, export_json: bool = False):
    uid = update.effective_user.id
    msg = update.callback_query.message if update.callback_query else update.message

    if uid not in sessions:
        await msg.reply_text("⚠️ Please /login first.")
        return

    await msg.reply_text("🔍 Fetching courses...")

    my_r    = api_get("get-my-courses", uid=uid)
    courses = as_list(my_r.get("data", []))
    if not courses:
        all_r   = api_get("get-all-courses", uid=uid)
        courses = as_list(all_r.get("data", []))

    if not courses:
        await msg.reply_text("⚠️ No courses found.")
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
        await msg.reply_text("⚠️ No links found.")
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
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id
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
        raise ValueError("Set BOT_TOKEN env variable before running!")

    app = Application.builder().token(BOT_TOKEN).build()

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
    )

    app.add_handler(login_conv)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))

    print("🤖 Bot is running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
