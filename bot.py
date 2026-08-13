import os
import json
import uuid
import time
import hashlib
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# -------------------------------------------------------------------
# Logging & Health Server
# -------------------------------------------------------------------
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def run_health_server():
    try:
        server = HTTPServer(('0.0.0.0', 8000), HealthHandler)
        logger.info("Health check server running on port 8000")
        server.serve_forever()
    except Exception as e:
        logger.error(f"Health server error: {e}")

health_thread = threading.Thread(target=run_health_server, daemon=True)
health_thread.start()

# -------------------------------------------------------------------
# Configuration
# -------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable not set")

BASE_URL = "https://bridgetosuccess.learncentre.tech/public/study_api_v9/"
PLAYER_URL = "https://lctplayer.learncentre.online/v/player.php?v="
STORAGE = {
    "pdf": "https://bridgetosuccess.learncentre.tech/public/storage/pdf/",
    "timetable": "https://bridgetosuccess.learncentre.tech/public/storage/timetable/",
}

user_sessions = {}
user_credentials = {}
user_courses = {}
user_all_courses = {}
user_free_categories = {}

# -------------------------------------------------------------------
# Helper: stable device ID per user
# -------------------------------------------------------------------
def get_device_id(user_id: int) -> str:
    return hashlib.md5(str(user_id).encode()).hexdigest()[:16]

# -------------------------------------------------------------------
# API Client (with auto-re-login)
# -------------------------------------------------------------------
class BridgeToSuccessAPI:
    def __init__(self, mobile=None, password=None, android_id=None):
        self.base_url = BASE_URL.rstrip("/") + "/"
        self.session = requests.Session()
        self.user_id = None
        self.auth_token = None
        self.mobile = mobile
        self.password = password
        self.android_id = android_id or uuid.uuid4().hex[:16]

        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Linux; Android 13; Pixel 6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.6099.230 Mobile Safari/537.36",
            "Accept": "application/json",
            "ktx": "co.exam.study.trend1",
            "ktxx": "18.0",
            "brand": "google",
            "model": "Pixel 6",
            "Connection": "close",
        })
        self.last_login_time = 0

    def _default_headers(self):
        headers = {
            "User-Agent": "Mozilla/5.0 (Linux; Android 13; Pixel 6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.6099.230 Mobile Safari/537.36",
            "Accept": "application/json",
            "ktx": "co.exam.study.trend1",
            "ktxx": "18.0",
            "brand": "google",
            "model": "Pixel 6",
            "Connection": "close",
        }
        if self.auth_token:
            headers["Authtoken"] = self.auth_token
            headers["Authorization"] = f"Bearer {self.auth_token}"
        return headers

    def set_token(self, user_id, auth_token):
        self.user_id = user_id
        self.auth_token = auth_token
        self.session.headers.update(self._default_headers())
        logger.info(f"Token manually set for user {user_id}")

    def post(self, data, retries=3):
        for attempt in range(retries):
            try:
                resp = self.session.post(self.base_url, data=data, timeout=30)
                resp.raise_for_status()
                if "application/json" in resp.headers.get("Content-Type", ""):
                    return resp.json()
                raise ValueError(f"Non-JSON response: {resp.text[:200]}")
            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 401:
                    logger.warning(f"401 Unauthorized. Attempt {attempt+1}/{retries}")
                    if self.mobile and self.password and attempt < retries - 1:
                        if time.time() - self.last_login_time < 300:
                            time.sleep(30)
                        self._re_login()
                        self.last_login_time = time.time()
                        self.session.headers.update(self._default_headers())
                        continue
                    else:
                        raise PermissionError("Token expired and re‑login failed")
                raise
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                if attempt == retries - 1:
                    raise
                wait = 2 ** attempt
                time.sleep(wait)
                self.session = requests.Session()
                self.session.headers.update(self._default_headers())

    def _re_login(self):
        logger.info("Attempting auto-re-login...")
        data = {"tag": "login", "mobile": self.mobile, "password": self.password,
                "androidId": self.android_id, "fcmId": ""}
        resp = self.session.post(self.base_url, data=data, timeout=30)
        resp.raise_for_status()
        result = resp.json()
        self._process_login_response(result)
        logger.info("Auto-re-login successful.")

    def _process_login_response(self, obj):
        def find(ob, keys):
            if isinstance(ob, dict):
                for k, v in ob.items():
                    if k in keys and v:
                        return v
                    res = find(v, keys)
                    if res:
                        return res
            elif isinstance(ob, list):
                for item in ob:
                    res = find(item, keys)
                    if res:
                        return res
            return None
        self.user_id = find(obj, ("user_id", "userId", "id"))
        self.auth_token = find(obj, ("authToken", "auth_token", "token"))
        if self.auth_token:
            self.session.headers.update({
                "Authtoken": self.auth_token,
                "Authorization": f"Bearer {self.auth_token}"
            })
            logger.info(f"Token updated for user {self.user_id}")

    def call(self, tag, **params):
        return self.post({"tag": tag, **params})

    # --- Authentication ---
    def login_api(self, mobile, password, android_id="", fcm_id=""):
        return self.call("login", mobile=mobile, password=password,
                         androidId=android_id, fcmId=fcm_id)

    # --- Profile ---
    def get_profile(self):
        return self.call("getProfile", userId=self.user_id)

    def get_notifications(self):
        return self.call("getNotifications", userId=self.user_id)

    # --- Courses ---
    def all_courses(self, is_ebook=False):
        return self.call("allCourses", userId=self.user_id, isEBook=1 if is_ebook else 0)

    def get_all_category(self, course_id):
        return self.call("getAllCategory", courseId=course_id)

    def get_category_mixed(self, course_id, category_id=""):
        return self.call("getCategoryMixed", courseId=course_id, categoryId=category_id, userId=self.user_id)

    def course_info(self, course_id):
        return self.call("courseInfo", courseId=course_id, userId=self.user_id)

    def all_course_video(self, category_id):
        return self.call("allCourseVideo", categoryId=category_id, userId=self.user_id)

    def all_course_pdf(self, category_id):
        return self.call("allCoursePdf", categoryId=category_id, userId=self.user_id)

    def my_courses(self, is_ebook=False):
        return self.call("myCourses", userId=self.user_id, isEBook=1 if is_ebook else 0)

    def my_course_video(self, category_id):
        return self.call("myCourseVideo", categoryId=category_id, userId=self.user_id)

    def my_course_pdf(self, category_id):
        return self.call("myCoursePdf", categoryId=category_id, userId=self.user_id)

    # --- Free content ---
    def free_course_video(self):
        return self.call("freeCourseVideo", userId=self.user_id)

    def free_course_pdf(self):
        return self.call("freeCoursePdf", userId=self.user_id)

    def get_free_content_category(self, course_type):
        return self.call("getFreeContentCategory", userId=self.user_id, courseType=course_type)

    def get_free_content(self, course_type, category_id, page=1, page_size=20):
        return self.call("getFreeContent", userId=self.user_id, courseType=course_type,
                         categoryId=category_id, pageNumber=str(page), pageItemSize=str(page_size))

    # --- Bulk fetch for all courses (merges eBook and non-eBook) ---
    def fetch_all_courses_with_details(self, fetch_my=False):
        if fetch_my:
            courses_data = self.my_courses()
            courses = extract_courses(courses_data)
        else:
            normal_data = self.all_courses(is_ebook=False)
            ebook_data = self.all_courses(is_ebook=True)
            courses_normal = extract_courses(normal_data)
            courses_ebook = extract_courses(ebook_data)
            combined = {}
            for c in courses_normal + courses_ebook:
                combined[c['courseId']] = c
            courses = list(combined.values())

        result = []
        for course in courses:
            course_id = course["courseId"]
            course_name = course["courseName"]
            course_price = course.get("coursePrice", "N/A")
            strikeout_price = course.get("strikeoutPrice", None)
            categories_data = self.get_all_category(course_id)
            categories = extract_categories(categories_data)
            cat_list = []
            for cat in categories:
                cat_id = cat["categoryId"]
                cat_name = cat["categoryName"]
                try:
                    videos_data = self.all_course_video(cat_id)
                    videos = extract_media_entries(videos_data)
                except:
                    videos = []
                try:
                    pdfs_data = self.all_course_pdf(cat_id)
                    pdfs = extract_media_entries(pdfs_data)
                except:
                    pdfs = []
                cat_list.append({
                    "categoryId": cat_id,
                    "categoryName": cat_name,
                    "videos": videos,
                    "pdfs": pdfs,
                })
                time.sleep(0.3)
            result.append({
                "courseId": course_id,
                "courseName": course_name,
                "coursePrice": course_price,
                "strikeoutPrice": strikeout_price,
                "categories": cat_list,
            })
        return result

    def fetch_free_content_all(self):
        free_videos = []
        free_pdfs = []
        try:
            videos_data = self.free_course_video()
            free_videos = extract_media_entries(videos_data)
        except:
            pass
        try:
            pdfs_data = self.free_course_pdf()
            free_pdfs = extract_media_entries(pdfs_data)
        except:
            pass
        return free_videos, free_pdfs

# -------------------------------------------------------------------
# Helper functions
# -------------------------------------------------------------------
def extract_courses(json_data):
    courses = []
    def _extract(obj):
        if isinstance(obj, dict):
            course_id = obj.get("courseId") or obj.get("id")
            if course_id and "courseName" in obj:
                course = {
                    "courseId": course_id,
                    "courseName": obj["courseName"],
                    "categoryId": obj.get("categoryId") or course_id,
                    "coursePrice": obj.get("coursePrice", "N/A"),
                    "strikeoutPrice": obj.get("strikeoutPrice", None)
                }
                courses.append(course)
            else:
                for v in obj.values():
                    _extract(v)
        elif isinstance(obj, list):
            for item in obj:
                _extract(item)
    _extract(json_data)
    return courses

def extract_categories(json_data):
    cats = []
    def _extract(obj):
        if isinstance(obj, dict):
            if "categoryId" in obj and "categoryName" in obj:
                cats.append({
                    "categoryId": obj["categoryId"],
                    "categoryName": obj["categoryName"],
                })
            else:
                for v in obj.values():
                    _extract(v)
        elif isinstance(obj, list):
            for item in obj:
                _extract(item)
    _extract(json_data)
    return cats

def extract_media_entries(json_data):
    entries = []
    schema = {
        "videoStreamURL": ["videoTitle", "title", "name"],
        "pdfUrl": ["pdfTitle", "title", "name"],
        "url": ["title", "name", "videoTitle", "pdfTitle"],
        "streamURL": ["title", "videoTitle"],
        "audioUrl": ["title", "name"],
    }
    def _extract(obj):
        if isinstance(obj, dict):
            for url_key, title_keys in schema.items():
                if url_key in obj and obj[url_key]:
                    url = str(obj[url_key]).strip()
                    if not url.startswith(("http", "https")):
                        continue
                    title = None
                    for tk in title_keys:
                        if tk in obj and obj[tk]:
                            title = str(obj[tk]).strip()
                            break
                    if not title:
                        title = os.path.basename(urlparse(url).path) or "file"
                    entries.append((url, title))
            for v in obj.values():
                _extract(v)
        elif isinstance(obj, list):
            for item in obj:
                _extract(item)
    _extract(json_data)
    return entries

def generate_media_text(media_entries):
    lines = []
    for i, (url, title) in enumerate(media_entries, 1):
        lines.append(f"Entry {i}")
        lines.append(f"Title: {title}")
        lines.append(f"URL: {url}")
        lines.append("")
    return "\n".join(lines)

def parse_selection(text, max_idx):
    indices = []
    for part in text.replace(" ", "").split(","):
        if "-" in part:
            s, e = part.split("-")
            try:
                s, e = int(s), int(e)
                if s < 1 or e > max_idx or s > e:
                    return None
                indices.extend(range(s-1, e))
            except ValueError:
                return None
        else:
            try:
                idx = int(part)
                if idx < 1 or idx > max_idx:
                    return None
                indices.append(idx-1)
            except ValueError:
                return None
    return sorted(set(indices))

# -------------------------------------------------------------------
# Helper to get leaf category IDs recursively from getAllCategory response
# -------------------------------------------------------------------
def get_leaf_category_ids(category_list):
    leaves = []
    def traverse(cat, path):
        current_path = path + [cat.get("categoryName", "Unknown")]
        if cat.get("hasChild") == "0" or not cat.get("children"):
            leaves.append({
                "id": cat["id"],
                "name": cat.get("categoryName", "Unknown"),
                "path": " → ".join(current_path)
            })
        else:
            for child in cat.get("children", []):
                traverse(child, current_path)
    if isinstance(category_list, list):
        for cat in category_list:
            traverse(cat, [])
    elif isinstance(category_list, dict):
        traverse(category_list, [])
    return leaves

# -------------------------------------------------------------------
# Telegram Bot Handlers
# -------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome!\n\n"
        "/login – log in with your mobile & password\n"
        "/login_token <user_id> <auth_token> – log in using an existing token\n"
        "/logout – clear your session\n"
        "/courses – list your purchased courses\n"
        "/allcourses – list **all** available courses with price and full content\n"
        "/select – choose courses and export media URLs\n"
        "/free – free content\n"
        "/free_select <number> – export media from a free category\n"
        "/profile – your profile info\n"
        "/notifications – your notifications\n"
        "/debug – dump raw API response\n"
        "/debug_categories <course_id> – inspect categories API response (sends a file)\n"
        "/session – check session status\n"
        "/getcourse <id> – fetch all media (videos/PDFs) for a specific course ID\n"
        "/help – this message"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Enter your mobile number (e.g., 9876543210):")
    context.user_data["login_step"] = "mobile"

async def login_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("❌ Usage: /login_token <user_id> <auth_token>")
        return
    uid = args[0].strip()
    token = args[1].strip()
    if not uid or not token:
        await update.message.reply_text("❌ User ID and token cannot be empty.")
        return
    try:
        api = BridgeToSuccessAPI()
        api.set_token(uid, token)
        try:
            profile = api.get_profile()
            user_sessions[user_id] = api
            user_credentials[user_id] = (None, None, None)
            await update.message.reply_text(f"✅ Login successful via token!\nUser ID: {uid}")
            await update.message.reply_text("⏳ Loading courses...")
            try:
                user_all_courses[user_id] = api.fetch_all_courses_with_details(fetch_my=False)
                user_courses[user_id] = api.fetch_all_courses_with_details(fetch_my=True)
                await update.message.reply_text("✅ Courses loaded.")
            except Exception as e:
                await update.message.reply_text(f"⚠️ Could not load courses: {e}")
        except Exception as e:
            await update.message.reply_text(f"❌ Token validation failed: {str(e)}")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def logout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_sessions.pop(user_id, None)
    user_credentials.pop(user_id, None)
    user_courses.pop(user_id, None)
    user_all_courses.pop(user_id, None)
    user_free_categories.pop(user_id, None)
    context.user_data.clear()
    logger.info(f"User {user_id} logged out manually.")
    await update.message.reply_text("✅ Logged out.")

async def handle_login_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    step = context.user_data.get("login_step")
    if step == "mobile":
        mobile = update.message.text.strip()
        if not mobile.isdigit() or len(mobile) < 10:
            await update.message.reply_text("❌ Invalid mobile.")
            return
        context.user_data["mobile"] = mobile
        context.user_data["login_step"] = "password"
        await update.message.reply_text("Now enter your password:")
    elif step == "password":
        password = update.message.text.strip()
        mobile = context.user_data.get("mobile")
        if not mobile:
            await update.message.reply_text("❌ Session expired.")
            return
        android_id = get_device_id(user_id)
        await update.message.reply_text("⏳ Logging in...")
        try:
            api = BridgeToSuccessAPI(mobile=mobile, password=password, android_id=android_id)
            result = api.login_api(mobile, password, android_id, "")
            api._process_login_response(result)
            if api.user_id and api.auth_token:
                user_sessions[user_id] = api
                user_credentials[user_id] = (mobile, password, android_id)
                context.user_data["login_step"] = None
                await update.message.reply_text(f"✅ Login successful! User ID: {api.user_id}")
                await update.message.reply_text("⏳ Loading courses...")
                try:
                    user_all_courses[user_id] = api.fetch_all_courses_with_details(fetch_my=False)
                    user_courses[user_id] = api.fetch_all_courses_with_details(fetch_my=True)
                    await update.message.reply_text("✅ Courses loaded.")
                except Exception as e:
                    await update.message.reply_text(f"⚠️ Could not load courses: {e}")
            else:
                error_msg = result.get("message") or result.get("error") or "Invalid credentials"
                await update.message.reply_text(f"❌ Login failed: {error_msg}")
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {str(e)}")
        context.user_data["login_step"] = None
    else:
        await update.message.reply_text("Please start with /login first.")

async def session_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    api = user_sessions.get(user_id)
    if api and api.auth_token:
        await update.message.reply_text(
            f"✅ Session active.\nUser ID: {api.user_id}\nToken: {'Present' if api.auth_token else 'Missing'}\nMobile: {api.mobile if api.mobile else 'Token login'}"
        )
    else:
        await update.message.reply_text("❌ No active session. Use /login or /login_token.")

async def courses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    api = user_sessions.get(user_id)
    if not api:
        await update.message.reply_text("❌ Not logged in. Use /login or /login_token first.")
        return
    courses_list = user_courses.get(user_id)
    if not courses_list:
        await update.message.reply_text("📚 Fetching your purchased courses...")
        try:
            data = api.my_courses()
            courses_list = extract_courses(data)
            if not courses_list:
                await update.message.reply_text("No purchased courses found.")
                return
            user_courses[user_id] = courses_list
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {str(e)}")
            return
    msg = "📖 Your Purchased Courses:\n\n"
    for i, c in enumerate(courses_list, 1):
        msg += f"{i}. {c['courseName']} (ID: {c['courseId']})\n"
        if c.get('coursePrice') and c.get('strikeoutPrice'):
            msg += f"   Price: ₹{c['coursePrice']} (Strikeout: ₹{c['strikeoutPrice']})\n"
        elif c.get('coursePrice'):
            msg += f"   Price: ₹{c['coursePrice']}\n"
    msg += "\nUse /allcourses to see all available courses with price."
    await update.message.reply_text(msg)

async def allcourses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    api = user_sessions.get(user_id)
    if not api:
        await update.message.reply_text("❌ Not logged in. Use /login or /login_token first.")
        return
    all_list = user_all_courses.get(user_id)
    if not all_list:
        await update.message.reply_text("📚 Fetching all available courses... This may take a moment.")
        try:
            all_list = api.fetch_all_courses_with_details(fetch_my=False)
            user_all_courses[user_id] = all_list
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {str(e)}")
            return
    if not all_list:
        await update.message.reply_text("No courses available.")
        return

    lines = []
    lines.append("=== ALL AVAILABLE COURSES ===")
    for course in all_list:
        lines.append(f"\n📚 {course['courseName']} (ID: {course['courseId']})")
        if course.get('strikeoutPrice') and course.get('coursePrice') and float(course['strikeoutPrice']) > float(course['coursePrice']):
            discount = int((float(course['strikeoutPrice'])-float(course['coursePrice']))/float(course['strikeoutPrice'])*100)
            lines.append(f"   💰 Price: ₹{course['coursePrice']} (~~₹{course['strikeoutPrice']}~~) - {discount}% OFF")
        elif course.get('coursePrice'):
            lines.append(f"   💰 Price: ₹{course['coursePrice']}")
        else:
            lines.append("   💰 Price: N/A")
        for cat in course.get("categories", []):
            lines.append(f"\n  🗂️ {cat['categoryName']} (ID: {cat['categoryId']})")
            if cat.get("videos"):
                lines.append(f"    🎬 Videos ({len(cat['videos'])}):")
                for vurl, vtitle in cat['videos']:
                    lines.append(f"      - {vtitle} -> {vurl}")
            else:
                lines.append("    Videos: None")
            if cat.get("pdfs"):
                lines.append(f"    📄 PDFs ({len(cat['pdfs'])}):")
                for purl, ptitle in cat['pdfs']:
                    lines.append(f"      - {ptitle} -> {purl}")
            else:
                lines.append("    PDFs: None")
    content = "\n".join(lines)
    filename = f"all_courses_{user_id}.txt"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(content)
    await update.message.reply_document(
        document=open(filename, "rb"),
        filename="all_courses_details.txt",
        caption="✅ All courses with price, categories, videos, and PDFs."
    )
    os.remove(filename)

async def select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    api = user_sessions.get(user_id)
    if not api:
        await update.message.reply_text("❌ Not logged in.")
        return
    courses_list = user_courses.get(user_id) or user_all_courses.get(user_id)
    if not courses_list:
        await update.message.reply_text("❌ No courses loaded. Use /courses or /allcourses first.")
        return
    await update.message.reply_text("📝 Enter course numbers (e.g., 1,3,5 or 1-5):")
    context.user_data["select_step"] = "waiting_selection"

async def handle_select_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if context.user_data.get("select_step") != "waiting_selection":
        return
    api = user_sessions.get(user_id)
    if not api:
        await update.message.reply_text("❌ Not logged in.")
        context.user_data["select_step"] = None
        return
    courses_list = user_courses.get(user_id) or user_all_courses.get(user_id)
    if not courses_list:
        await update.message.reply_text("❌ No courses.")
        context.user_data["select_step"] = None
        return
    selection_text = update.message.text.strip()
    indices = parse_selection(selection_text, len(courses_list))
    if indices is None:
        await update.message.reply_text("❌ Invalid selection.")
        return
    selected = [courses_list[i] for i in indices]
    await update.message.reply_text(f"✅ Selected {len(selected)} courses. Fetching media...")

    all_media = []
    for course in selected:
        cat_id = course["categoryId"]
        try:
            videos = api.all_course_video(cat_id)
            all_media.extend(extract_media_entries(videos))
        except Exception:
            pass
        try:
            pdfs = api.all_course_pdf(cat_id)
            all_media.extend(extract_media_entries(pdfs))
        except Exception:
            pass
        time.sleep(0.5)

    try:
        free_v = api.free_course_video()
        all_media.extend(extract_media_entries(free_v))
    except:
        pass
    try:
        free_p = api.free_course_pdf()
        all_media.extend(extract_media_entries(free_p))
    except:
        pass

    seen = set()
    unique = []
    for url, title in all_media:
        if url not in seen:
            seen.add(url)
            unique.append((url, title))

    if not unique:
        await update.message.reply_text("No media found for selected courses.")
        context.user_data["select_step"] = None
        return

    content = generate_media_text(unique)
    filename = f"course_media_{user_id}.txt"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(content)
    await update.message.reply_document(
        document=open(filename, "rb"),
        filename="selected_courses_media.txt",
        caption=f"✅ {len(unique)} media entries."
    )
    os.remove(filename)
    context.user_data["select_step"] = None

async def free(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    api = user_sessions.get(user_id)
    if not api:
        await update.message.reply_text("❌ Not logged in. Use /login or /login_token first.")
        return
    await update.message.reply_text("📂 Fetching free content...")
    try:
        video_cats_data = None
        pdf_cats_data = None
        try:
            video_cats_data = api.get_free_content_category("video")
        except:
            pass
        try:
            pdf_cats_data = api.get_free_content_category("pdf")
        except:
            pass

        categories = []
        if video_cats_data:
            video_cats = extract_categories(video_cats_data)
            for cat in video_cats:
                cat["type"] = "video"
            categories.extend(video_cats)
        if pdf_cats_data:
            pdf_cats = extract_categories(pdf_cats_data)
            for cat in pdf_cats:
                cat["type"] = "pdf"
            categories.extend(pdf_cats)

        if categories:
            user_free_categories[user_id] = categories
            msg = "📖 Free Content Categories:\n\n"
            for i, cat in enumerate(categories, 1):
                msg += f"{i}. {cat['categoryName']} ({cat['type']})\n"
            msg += "\nUse /free_select <number> to export media from a category."
            await update.message.reply_text(msg)
        else:
            await update.message.reply_text("No categories found. Fetching all free content directly...")
            videos = api.free_course_video()
            pdfs = api.free_course_pdf()
            all_media = []
            all_media.extend(extract_media_entries(videos))
            all_media.extend(extract_media_entries(pdfs))
            if not all_media:
                await update.message.reply_text("No free content found.")
                return
            unique = list(dict.fromkeys(all_media))
            content = generate_media_text(unique)
            filename = f"free_media_{user_id}.txt"
            with open(filename, "w", encoding="utf-8") as f:
                f.write(content)
            await update.message.reply_document(
                document=open(filename, "rb"),
                filename="free_content.txt",
                caption=f"✅ Free content: {len(unique)} items."
            )
            os.remove(filename)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def free_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    api = user_sessions.get(user_id)
    if not api:
        await update.message.reply_text("❌ Not logged in.")
        return
    categories = user_free_categories.get(user_id)
    if not categories:
        await update.message.reply_text("❌ No categories found. Run /free first.")
        return
    args = context.args
    if not args:
        await update.message.reply_text("❌ Please provide a category number: /free_select 1")
        return
    try:
        idx = int(args[0]) - 1
        if idx < 0 or idx >= len(categories):
            await update.message.reply_text("❌ Invalid number.")
            return
    except ValueError:
        await update.message.reply_text("❌ Please enter a number.")
        return
    cat = categories[idx]
    cat_id = cat["categoryId"]
    cat_type = cat["type"]
    await update.message.reply_text(f"📥 Fetching {cat_type} content for '{cat['categoryName']}'...")
    try:
        content_data = api.get_free_content(cat_type, cat_id, page=1, page_size=100)
        media_entries = extract_media_entries(content_data)
        if not media_entries:
            await update.message.reply_text("No media found in this category.")
            return
        seen = set()
        unique = []
        for url, title in media_entries:
            if url not in seen:
                seen.add(url)
                unique.append((url, title))
        content_text = generate_media_text(unique)
        filename = f"free_category_{user_id}.txt"
        with open(filename, "w", encoding="utf-8") as f:
            f.write(content_text)
        await update.message.reply_document(
            document=open(filename, "rb"),
            filename=f"{cat['categoryName']}_{cat_type}.txt",
            caption=f"✅ {len(unique)} media items in {cat['categoryName']}"
        )
        os.remove(filename)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    api = user_sessions.get(user_id)
    if not api:
        await update.message.reply_text("❌ Not logged in. Use /login or /login_token first.")
        return
    try:
        data = api.get_profile()
        await update.message.reply_text(f"Profile info:\n```\n{json.dumps(data, indent=2)}\n```", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def notifications(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    api = user_sessions.get(user_id)
    if not api:
        await update.message.reply_text("❌ Not logged in. Use /login or /login_token first.")
        return
    try:
        data = api.get_notifications()
        await update.message.reply_text(f"Notifications:\n```\n{json.dumps(data, indent=2)}\n```", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    api = user_sessions.get(user_id)
    if not api:
        await update.message.reply_text("❌ Not logged in.")
        return
    try:
        normal = api.all_courses(is_ebook=False)
        ebook = api.all_courses(is_ebook=True)
        data = {"normal": normal, "ebook": ebook}
        filename = f"debug_{user_id}.json"
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        await update.message.reply_document(
            document=open(filename, "rb"),
            filename="debug_courses.json",
            caption="Raw API responses for allCourses (isEBook=0 and isEBook=1)."
        )
        os.remove(filename)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def debug_categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    api = user_sessions.get(user_id)
    if not api:
        await update.message.reply_text("❌ Not logged in.")
        return
    args = context.args
    if not args:
        await update.message.reply_text("❌ Please provide a course ID: /debug_categories 91")
        return
    course_id = args[0].strip()
    try:
        result = api.get_all_category(course_id)
        mixed_result = api.get_category_mixed(course_id, "")
        data = {
            "course_id": course_id,
            "getAllCategory": result,
            "getCategoryMixed": mixed_result
        }
        filename = f"debug_categories_{course_id}_{user_id}.json"
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        await update.message.reply_document(
            document=open(filename, "rb"),
            filename=f"debug_categories_{course_id}.json",
            caption=f"🔍 Raw category responses for course {course_id}."
        )
        os.remove(filename)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def getcourse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    api = user_sessions.get(user_id)
    if not api:
        await update.message.reply_text("❌ Not logged in. Use /login or /login_token first.")
        return

    args = context.args
    if not args:
        await update.message.reply_text("❌ Please provide a course ID: /getcourse 91")
        return

    course_id = args[0].strip()
    all_media = []  # each entry: (url, title)

    def add_media(url, title):
        if url and not url.startswith("http"):
            url = "https://bridgetosuccess.learncentre.tech/" + url.lstrip("/")
        all_media.append((url, title))

    try:
        # 1. Fetch category tree to get leaf categories
        category_response = api.get_all_category(course_id)
        category_list = None
        if isinstance(category_response, dict) and "category" in category_response:
            category_list = category_response["category"]

        if category_list:
            leaves = get_leaf_category_ids(category_list)
            if leaves:
                for leaf in leaves:
                    cat_id = leaf["id"]
                    # Fetch media for this leaf category
                    try:
                        media_response = api.get_category_mixed(course_id, cat_id)
                        mixed_items = None
                        # Extract list from various possible structures
                        if isinstance(media_response, list):
                            mixed_items = media_response
                        elif isinstance(media_response, dict):
                            for key in ["mixedContentItems", "mixedContent", "data", "items"]:
                                if key in media_response and isinstance(media_response[key], list):
                                    mixed_items = media_response[key]
                                    break
                            if mixed_items is None and "data" in media_response:
                                data_obj = media_response["data"]
                                if isinstance(data_obj, dict):
                                    for key in ["mixedContentItems", "mixedContent", "items"]:
                                        if key in data_obj and isinstance(data_obj[key], list):
                                            mixed_items = data_obj[key]
                                            break
                                elif isinstance(data_obj, list):
                                    mixed_items = data_obj

                        if mixed_items:
                            for item in mixed_items:
                                item_type = item.get("type")
                                data_obj = item.get("data", {})
                                # We only care about videos and PDFs
                                if item_type == "video":
                                    title = data_obj.get("videoName", "Video")
                                    # Collect all possible URL fields
                                    urls = {}
                                    for key in ["videoLink", "videoStreamURL", "streamURL", "url"]:
                                        val = data_obj.get(key)
                                        if val and val.strip():
                                            urls[key] = val
                                    if urls:
                                        # If multiple URLs, add each with a suffix
                                        if len(urls) == 1:
                                            url = list(urls.values())[0]
                                            add_media(url, title)
                                        else:
                                            # Sort keys for consistent order
                                            sorted_keys = sorted(urls.keys())
                                            for idx, key in enumerate(sorted_keys, 1):
                                                url = urls[key]
                                                add_media(url, f"{title} (Player {idx})")
                                    else:
                                        # No URL found
                                        add_media("", title)
                                elif item_type == "pdf":
                                    title = data_obj.get("pdfTitle", "PDF")
                                    urls = {}
                                    for key in ["pdfFile", "pdfUrl", "url"]:
                                        val = data_obj.get(key)
                                        if val and val.strip():
                                            urls[key] = val
                                    if urls:
                                        url = list(urls.values())[0]  # usually only one, but take first
                                        add_media(url, title)
                                    else:
                                        add_media("", title)
                    except Exception as e:
                        logger.warning(f"Failed to fetch media for category {cat_id}: {e}")
                    time.sleep(0.3)

        # 2. Fallback: if no categories, try getCategoryMixed with empty categoryId
        if not all_media:
            mixed_response = api.get_category_mixed(course_id, "")
            mixed_list = None
            if isinstance(mixed_response, list):
                mixed_list = mixed_response
            elif isinstance(mixed_response, dict):
                for key in ["mixedContentItems", "mixedContent", "data", "items"]:
                    if key in mixed_response and isinstance(mixed_response[key], list):
                        mixed_list = mixed_response[key]
                        break
                if mixed_list is None and "data" in mixed_response:
                    data_obj = mixed_response["data"]
                    if isinstance(data_obj, dict):
                        for key in ["mixedContentItems", "mixedContent", "items"]:
                            if key in data_obj and isinstance(data_obj[key], list):
                                mixed_list = data_obj[key]
                                break
                    elif isinstance(data_obj, list):
                        mixed_list = data_obj

            if mixed_list:
                for item in mixed_list:
                    item_type = item.get("type")
                    data_obj = item.get("data", {})
                    if item_type == "video":
                        title = data_obj.get("videoName", "Video")
                        urls = {}
                        for key in ["videoLink", "videoStreamURL", "streamURL", "url"]:
                            val = data_obj.get(key)
                            if val and val.strip():
                                urls[key] = val
                        if urls:
                            if len(urls) == 1:
                                url = list(urls.values())[0]
                                add_media(url, title)
                            else:
                                sorted_keys = sorted(urls.keys())
                                for idx, key in enumerate(sorted_keys, 1):
                                    url = urls[key]
                                    add_media(url, f"{title} (Player {idx})")
                        else:
                            add_media("", title)
                    elif item_type == "pdf":
                        title = data_obj.get("pdfTitle", "PDF")
                        urls = {}
                        for key in ["pdfFile", "pdfUrl", "url"]:
                            val = data_obj.get(key)
                            if val and val.strip():
                                urls[key] = val
                        if urls:
                            url = list(urls.values())[0]
                            add_media(url, title)
                        else:
                            add_media("", title)

        # 3. Add demo content from courseInfo
        info = api.course_info(course_id)
        if info and "courses" in info and len(info["courses"]) > 0:
            course = info["courses"][0]
            intro_video_id = course.get("introVideoId")
            batch_pdf = course.get("batchInfoPdf")
            timetable_img = course.get("timeTableImg")
            if intro_video_id and intro_video_id != "null" and intro_video_id.strip():
                video_url = PLAYER_URL + intro_video_id
                add_media(video_url, "Intro Video (Demo)")
            if batch_pdf and batch_pdf.strip():
                pdf_url = STORAGE["pdf"] + batch_pdf
                add_media(pdf_url, "Batch Info PDF (Demo)")
            if timetable_img and timetable_img.strip():
                img_url = STORAGE["timetable"] + timetable_img
                add_media(img_url, "Timetable Image (Demo)")

        if not all_media:
            await update.message.reply_text(f"No media found for course {course_id}.")
            return

        # Deduplicate – use URL if non‑empty, otherwise use title
        seen = set()
        unique = []
        for url, title in all_media:
            key = url if url else title
            if key in seen:
                continue
            seen.add(key)
            unique.append((url, title))

        # Build and send file
        lines = [f"Course ID: {course_id}"]
        for i, (url, title) in enumerate(unique, 1):
            lines.append(f"Entry {i}")
            lines.append(f"Title: {title}")
            lines.append(f"URL: {url if url else '(No URL)'}")
            lines.append("")
        content = "\n".join(lines)
        filename = f"course_{course_id}_{user_id}.txt"
        with open(filename, "w", encoding="utf-8") as f:
            f.write(content)

        await update.message.reply_document(
            document=open(filename, "rb"),
            filename=f"course_{course_id}_media.txt",
            caption=f"✅ {len(unique)} media items for course {course_id}."
        )
        os.remove(filename)

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❓ Unknown command. Use /start for help.")

# -------------------------------------------------------------------
# Main
# -------------------------------------------------------------------
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("login", login))
    app.add_handler(CommandHandler("login_token", login_token))
    app.add_handler(CommandHandler("logout", logout))
    app.add_handler(CommandHandler("session", session_command))
    app.add_handler(CommandHandler("courses", courses))
    app.add_handler(CommandHandler("allcourses", allcourses))
    app.add_handler(CommandHandler("select", select))
    app.add_handler(CommandHandler("free", free))
    app.add_handler(CommandHandler("free_select", free_select))
    app.add_handler(CommandHandler("profile", profile))
    app.add_handler(CommandHandler("notifications", notifications))
    app.add_handler(CommandHandler("debug", debug_command))
    app.add_handler(CommandHandler("debug_categories", debug_categories))
    app.add_handler(CommandHandler("getcourse", getcourse))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_login_input))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_select_input))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown))
    print("🤖 Bot is running...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
