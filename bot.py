import os
import json
import uuid
import time
import hashlib
from pathlib import Path
from urllib.parse import urlparse

import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# -------------------------------------------------------------------
# Configuration
# -------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable not set")

BASE_URL = "https://bridgetosuccess.learncentre.tech/public/study_api_v9/"

# Master account credentials (enable /withoutlogin)
MASTER_MOBILE = os.environ.get("MASTER_MOBILE", "")
MASTER_PASSWORD = os.environ.get("MASTER_PASSWORD", "")
MASTER_ANDROID_ID = os.environ.get("MASTER_ANDROID_ID", "0000000000000000")

# In-memory storage
user_sessions = {}          # user_id -> BridgeToSuccessAPI instance
user_credentials = {}       # user_id -> (mobile, password, android_id)
user_courses = {}           # user_id -> list of course dicts
user_free_categories = {}   # user_id -> list of free category dicts

# Master cache for without-login mode
cached_courses = []
cached_free_videos = []
cached_free_pdfs = []
cached_data_loaded = False

# -------------------------------------------------------------------
# Helper to generate stable device ID per user
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
                    if self.mobile and self.password and attempt < retries - 1:
                        if hasattr(self, 'last_login_time') and time.time() - self.last_login_time < 300:
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
        data = {"tag": "login", "mobile": self.mobile, "password": self.password,
                "androidId": self.android_id, "fcmId": ""}
        resp = self.session.post(self.base_url, data=data, timeout=30)
        resp.raise_for_status()
        result = resp.json()
        self._process_login_response(result)

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

    def call(self, tag, **params):
        return self.post({"tag": tag, **params})

    # --- Auth ---
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

    def all_course_video(self, category_id):
        return self.call("allCourseVideo", categoryId=category_id, userId=self.user_id)

    def all_course_pdf(self, category_id):
        return self.call("allCoursePdf", categoryId=category_id, userId=self.user_id)

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

    # --- Bulk fetch for master caching ---
    def fetch_all_courses_with_details(self):
        courses_data = self.all_courses()
        courses = extract_courses(courses_data)
        result = []
        for course in courses:
            course_id = course["courseId"]
            course_name = course["courseName"]
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
            if "courseId" in obj and "courseName" in obj:
                courses.append({
                    "courseId": obj["courseId"],
                    "courseName": obj["courseName"],
                    "categoryId": obj.get("categoryId") or obj["courseId"]
                })
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

def load_master_data():
    global cached_courses, cached_free_videos, cached_free_pdfs, cached_data_loaded
    if not MASTER_MOBILE or not MASTER_PASSWORD:
        print("Master credentials not set – skipping cache load.")
        return False
    try:
        api = BridgeToSuccessAPI(mobile=MASTER_MOBILE, password=MASTER_PASSWORD, android_id=MASTER_ANDROID_ID)
        api.login_api(MASTER_MOBILE, MASTER_PASSWORD, MASTER_ANDROID_ID, "")
        cached_courses = api.fetch_all_courses_with_details()
        cached_free_videos, cached_free_pdfs = api.fetch_free_content_all()
        cached_data_loaded = True
        print(f"✅ Master cache loaded: {len(cached_courses)} courses, {len(cached_free_videos)} free videos, {len(cached_free_pdfs)} free PDFs.")
        return True
    except Exception as e:
        print(f"❌ Failed to load master cache: {e}")
        return False

# -------------------------------------------------------------------
# Telegram Bot Handlers
# -------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome to BridgeToSuccess Bot!\n\n"
        "Commands:\n"
        "/login – log in with mobile & password\n"
        "/courses – list all courses (uses cache if available)\n"
        "/select – choose course(s) and export media URLs\n"
        "/free – list free content categories\n"
        "/free_select <number> – export media from a free category\n"
        "/allcourses – get a full dump of all courses with contents\n"
        "/withoutlogin – get all cached data without login (if master cache enabled)\n"
        "/profile – view your profile (login required)\n"
        "/notifications – fetch notifications\n"
        "/help – show this message"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Enter your mobile number (e.g., 9876543210):")
    context.user_data["login_step"] = "mobile"

async def handle_login_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    step = context.user_data.get("login_step")
    if step == "mobile":
        mobile = update.message.text.strip()
        if not mobile.isdigit() or len(mobile) < 10:
            await update.message.reply_text("❌ Invalid mobile. Please enter a 10-digit number.")
            return
        context.user_data["mobile"] = mobile
        context.user_data["login_step"] = "password"
        await update.message.reply_text("Now enter your password:")
    elif step == "password":
        password = update.message.text.strip()
        mobile = context.user_data.get("mobile")
        if not mobile:
            await update.message.reply_text("❌ Session expired. Please start over with /login")
            return
        android_id = get_device_id(user_id)   # stable per user
        await update.message.reply_text("⏳ Logging in...")
        try:
            api = BridgeToSuccessAPI(mobile=mobile, password=password, android_id=android_id)
            result = api.login_api(mobile, password, android_id, "")
            # CRITICAL: process the response to set user_id and auth_token
            api._process_login_response(result)
            if api.user_id and api.auth_token:
                user_sessions[user_id] = api
                user_credentials[user_id] = (mobile, password, android_id)
                context.user_data["login_step"] = None
                await update.message.reply_text(f"✅ Login successful!\nUser ID: {api.user_id}")
                # Pre-fetch courses
                await update.message.reply_text("⏳ Fetching courses...")
                try:
                    user_courses[user_id] = api.fetch_all_courses_with_details()
                    await update.message.reply_text("✅ Courses cached.")
                except Exception as e:
                    await update.message.reply_text(f"⚠️ Could not fetch courses: {e}")
            else:
                # Extract error message from server response
                error_msg = result.get("message") or result.get("error") or result.get("msg") or "Invalid credentials"
                await update.message.reply_text(f"❌ Login failed: {error_msg}")
        except requests.exceptions.HTTPError as e:
            try:
                error_data = e.response.json()
                error_msg = error_data.get("message") or error_data.get("error") or str(e)
            except:
                error_msg = str(e)
            await update.message.reply_text(f"❌ HTTP error: {error_msg}")
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {str(e)}")
        context.user_data["login_step"] = None
    else:
        await update.message.reply_text("Please start with /login first.")

async def courses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    api = user_sessions.get(user_id)
    if not api:
        # Try using cached data if available
        if cached_data_loaded:
            courses_list = cached_courses
            if not courses_list:
                await update.message.reply_text("No cached courses available.")
                return
            user_courses[user_id] = courses_list
            msg = "📖 Available Courses (cached):\n\n"
            for i, c in enumerate(courses_list, 1):
                msg += f"{i}. {c['courseName']} (ID: {c['courseId']})\n"
            msg += "\nUse /select to choose course(s) (e.g., 1,3,5 or 1-5)"
            await update.message.reply_text(msg)
            return
        else:
            await update.message.reply_text("❌ Not logged in. Use /login first.")
        return

    await update.message.reply_text("📚 Fetching courses...")
    try:
        data = api.all_courses()
    except PermissionError:
        if user_id in user_credentials:
            await update.message.reply_text("🔄 Auto‑re‑logging in...")
            mobile, password, android_id = user_credentials[user_id]
            api = BridgeToSuccessAPI(mobile=mobile, password=password, android_id=android_id)
            try:
                result = api.login_api(mobile, password, android_id, "")
                api._process_login_response(result)
                if api.user_id and api.auth_token:
                    user_sessions[user_id] = api
                    data = api.all_courses()
                else:
                    await update.message.reply_text("❌ Auto‑re‑login failed. Please /login again.")
                    return
            except Exception as e:
                await update.message.reply_text(f"❌ Auto‑re‑login error: {str(e)}")
                return
        else:
            await update.message.reply_text("❌ Session expired. Please /login again.")
            return
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to fetch courses: {str(e)}")
        return
    courses_list = extract_courses(data)
    if not courses_list:
        await update.message.reply_text("❌ No courses found.")
        return
    user_courses[user_id] = courses_list
    msg = "📖 Available Courses:\n\n"
    for i, c in enumerate(courses_list, 1):
        msg += f"{i}. {c['courseName']} (ID: {c['courseId']})\n"
    msg += "\nUse /select to choose course(s) (e.g., 1,3,5 or 1-5)"
    await update.message.reply_text(msg)

async def select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    api = user_sessions.get(user_id)
    if not api:
        await update.message.reply_text("❌ Not logged in. Use /login first.")
        return
    courses_list = user_courses.get(user_id)
    if not courses_list:
        await update.message.reply_text("❌ No course list. Use /courses first.")
        return
    await update.message.reply_text("📝 Enter the numbers of the courses you want (e.g., 1,3,5 or 1-5):")
    context.user_data["select_step"] = "waiting_selection"

async def handle_select_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if context.user_data.get("select_step") != "waiting_selection":
        return
    api = user_sessions.get(user_id)
    if not api:
        await update.message.reply_text("❌ Not logged in. Use /login first.")
        context.user_data["select_step"] = None
        return
    courses_list = user_courses.get(user_id)
    if not courses_list:
        await update.message.reply_text("❌ No courses. Use /courses first.")
        context.user_data["select_step"] = None
        return
    selection_text = update.message.text.strip()
    indices = parse_selection(selection_text, len(courses_list))
    if indices is None:
        await update.message.reply_text("❌ Invalid selection. Please use numbers like 1,3,5 or 1-5.")
        return
    selected = [courses_list[i] for i in indices]
    await update.message.reply_text(f"✅ Selected {len(selected)} course(s). Fetching media...")

    all_media = []
    for course in selected:
        cat_id = course["categoryId"]
        try:
            videos = api.all_course_video(cat_id)
            all_media.extend(extract_media_entries(videos))
        except PermissionError:
            if user_id in user_credentials:
                mobile, password, android_id = user_credentials[user_id]
                api = BridgeToSuccessAPI(mobile=mobile, password=password, android_id=android_id)
                try:
                    result = api.login_api(mobile, password, android_id, "")
                    api._process_login_response(result)
                    if api.user_id and api.auth_token:
                        user_sessions[user_id] = api
                        videos = api.all_course_video(cat_id)
                        all_media.extend(extract_media_entries(videos))
                    else:
                        await update.message.reply_text("❌ Auto‑re‑login failed.")
                        context.user_data["select_step"] = None
                        return
                except Exception as e:
                    await update.message.reply_text(f"❌ Auto‑re‑login error: {str(e)}")
                    context.user_data["select_step"] = None
                    return
            else:
                await update.message.reply_text("❌ Session expired. Please /login again.")
                context.user_data["select_step"] = None
                return
        except Exception as e:
            await update.message.reply_text(f"⚠️ Video error: {str(e)}")
        try:
            pdfs = api.all_course_pdf(cat_id)
            all_media.extend(extract_media_entries(pdfs))
        except Exception as e:
            await update.message.reply_text(f"⚠️ PDF error: {str(e)}")
        time.sleep(0.5)

    # Free content
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
        await update.message.reply_text("No media found for the selected courses.")
        context.user_data["select_step"] = None
        return

    content = generate_media_text(unique)
    filename = f"course_media_{user_id}.txt"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(content)

    await update.message.reply_document(
        document=open(filename, "rb"),
        filename="selected_courses_media.txt",
        caption=f"✅ Media URLs for {len(unique)} items."
    )
    os.remove(filename)
    context.user_data["select_step"] = None

# --- Free content commands ---
async def free(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    api = user_sessions.get(user_id)
    if not api:
        if cached_data_loaded and (cached_free_videos or cached_free_pdfs):
            msg = "📂 Free Content (cached):\n\n"
            if cached_free_videos:
                msg += "🎬 Videos:\n"
                for url, title in cached_free_videos[:10]:
                    msg += f"  - {title} -> {url}\n"
                if len(cached_free_videos) > 10:
                    msg += f"  ... and {len(cached_free_videos)-10} more.\n"
            if cached_free_pdfs:
                msg += "📄 PDFs:\n"
                for url, title in cached_free_pdfs[:10]:
                    msg += f"  - {title} -> {url}\n"
                if len(cached_free_pdfs) > 10:
                    msg += f"  ... and {len(cached_free_pdfs)-10} more.\n"
            await update.message.reply_text(msg)
        else:
            await update.message.reply_text("❌ Not logged in and no cached free content. Use /login first.")
        return

    await update.message.reply_text("📂 Fetching free content categories...")
    try:
        video_cats_data = api.get_free_content_category("video")
        pdf_cats_data = api.get_free_content_category("pdf")
        categories = []
        video_cats = extract_categories(video_cats_data) if video_cats_data else []
        for cat in video_cats:
            cat["type"] = "video"
        categories.extend(video_cats)
        pdf_cats = extract_categories(pdf_cats_data) if pdf_cats_data else []
        for cat in pdf_cats:
            cat["type"] = "pdf"
        categories.extend(pdf_cats)
        if not categories:
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
            return
        user_free_categories[user_id] = categories
        msg = "📖 Free Content Categories:\n\n"
        for i, cat in enumerate(categories, 1):
            msg += f"{i}. {cat['categoryName']} ({cat['type']})\n"
        msg += "\nUse /free_select <number> to export media from a category."
        await update.message.reply_text(msg)
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

# --- All courses dump ---
async def allcourses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    api = user_sessions.get(user_id)
    if not api and cached_data_loaded:
        # Use cached data
        courses = cached_courses
        lines = []
        lines.append("=== ALL COURSES (cached) ===")
        for course in courses:
            lines.append(f"\n📚 {course['courseName']} (ID: {course['courseId']})")
            for cat in course.get("categories", []):
                lines.append(f"\n  🗂️ {cat['categoryName']} (ID: {cat['categoryId']})")
                videos = cat.get("videos", [])
                if videos:
                    lines.append(f"    🎬 Videos ({len(videos)}):")
                    for vurl, vtitle in videos:
                        lines.append(f"      - {vtitle} -> {vurl}")
                else:
                    lines.append("    Videos: None")
                pdfs = cat.get("pdfs", [])
                if pdfs:
                    lines.append(f"    📄 PDFs ({len(pdfs)}):")
                    for purl, ptitle in pdfs:
                        lines.append(f"      - {ptitle} -> {purl}")
                else:
                    lines.append("    PDFs: None")
        if cached_free_videos:
            lines.append("\n\n=== FREE VIDEOS ===")
            for vurl, vtitle in cached_free_videos:
                lines.append(f"- {vtitle} -> {vurl}")
        if cached_free_pdfs:
            lines.append("\n\n=== FREE PDFs ===")
            for purl, ptitle in cached_free_pdfs:
                lines.append(f"- {ptitle} -> {purl}")
        content = "\n".join(lines)
        filename = f"all_courses_cache_{user_id}.txt"
        with open(filename, "w", encoding="utf-8") as f:
            f.write(content)
        await update.message.reply_document(
            document=open(filename, "rb"),
            filename="all_courses_cached.txt",
            caption="✅ Full course dump (cached)."
        )
        os.remove(filename)
        return
    if not api:
        await update.message.reply_text("❌ Not logged in and no cached data. Use /login first.")
        return
    await update.message.reply_text("📚 Fetching all courses and their contents... This may take a while.")
    try:
        courses_data = api.all_courses()
        courses_list = extract_courses(courses_data)
        if not courses_list:
            await update.message.reply_text("No courses found.")
            return
        total = len(courses_list)
        output_lines = [f"Total Courses: {total}\n"]
        for idx, course in enumerate(courses_list, 1):
            course_id = course['courseId']
            course_name = course['courseName']
            output_lines.append(f"\n{idx}. {course_name} (ID: {course_id})")
            output_lines.append("-" * 40)
            try:
                categories_data = api.get_all_category(course_id)
                categories = extract_categories(categories_data)
                if not categories:
                    output_lines.append("  No categories found.")
                    continue
            except Exception as e:
                output_lines.append(f"  Error fetching categories: {str(e)}")
                continue
            for cat in categories:
                cat_id = cat['categoryId']
                cat_name = cat['categoryName']
                output_lines.append(f"\n  Category: {cat_name} (ID: {cat_id})")
                try:
                    videos_data = api.all_course_video(cat_id)
                    video_entries = extract_media_entries(videos_data)
                    if video_entries:
                        output_lines.append(f"    Videos ({len(video_entries)}):")
                        for vurl, vtitle in video_entries:
                            output_lines.append(f"      - {vtitle} -> {vurl}")
                    else:
                        output_lines.append("    Videos: None")
                except Exception as e:
                    output_lines.append(f"    Videos: Error - {str(e)}")
                try:
                    pdfs_data = api.all_course_pdf(cat_id)
                    pdf_entries = extract_media_entries(pdfs_data)
                    if pdf_entries:
                        output_lines.append(f"    PDFs ({len(pdf_entries)}):")
                        for purl, ptitle in pdf_entries:
                            output_lines.append(f"      - {ptitle} -> {purl}")
                    else:
                        output_lines.append("    PDFs: None")
                except Exception as e:
                    output_lines.append(f"    PDFs: Error - {str(e)}")
                time.sleep(0.3)
            if idx % 5 == 0:
                await update.message.reply_text(f"⏳ Processed {idx}/{total} courses...")
        content = "\n".join(output_lines)
        filename = f"all_courses_detail_{user_id}.txt"
        with open(filename, "w", encoding="utf-8") as f:
            f.write(content)
        await update.message.reply_document(
            document=open(filename, "rb"),
            filename="all_courses_details.txt",
            caption="✅ Full dump of all courses with their contents."
        )
        os.remove(filename)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

# --- Without login (cached) ---
async def withoutlogin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not cached_data_loaded:
        await update.message.reply_text("❌ No cached data available. Master credentials may be missing or cache not loaded.")
        return
    user_id = update.effective_user.id
    lines = []
    lines.append("=== ALL COURSES (cached) ===")
    for course in cached_courses:
        lines.append(f"\n📚 {course['courseName']} (ID: {course['courseId']})")
        for cat in course.get("categories", []):
            lines.append(f"\n  🗂️ {cat['categoryName']} (ID: {cat['categoryId']})")
            videos = cat.get("videos", [])
            if videos:
                lines.append(f"    🎬 Videos ({len(videos)}):")
                for vurl, vtitle in videos:
                    lines.append(f"      - {vtitle} -> {vurl}")
            else:
                lines.append("    Videos: None")
            pdfs = cat.get("pdfs", [])
            if pdfs:
                lines.append(f"    📄 PDFs ({len(pdfs)}):")
                for purl, ptitle in pdfs:
                    lines.append(f"      - {ptitle} -> {purl}")
            else:
                lines.append("    PDFs: None")
    if cached_free_videos:
        lines.append("\n\n=== FREE VIDEOS ===")
        for vurl, vtitle in cached_free_videos:
            lines.append(f"- {vtitle} -> {vurl}")
    if cached_free_pdfs:
        lines.append("\n\n=== FREE PDFs ===")
        for purl, ptitle in cached_free_pdfs:
            lines.append(f"- {ptitle} -> {purl}")
    lines.append(f"\n\nLast updated: {time.ctime()}")
    content = "\n".join(lines)
    filename = f"without_login_{user_id}.txt"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(content)
    await update.message.reply_document(
        document=open(filename, "rb"),
        filename="without_login_data.txt",
        caption="✅ All cached data (no login required)."
    )
    os.remove(filename)

# --- Profile & Notifications ---
async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    api = user_sessions.get(user_id)
    if not api:
        await update.message.reply_text("❌ Not logged in. Use /login first.")
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
        await update.message.reply_text("❌ Not logged in. Use /login first.")
        return
    try:
        data = api.get_notifications()
        await update.message.reply_text(f"Notifications:\n```\n{json.dumps(data, indent=2)}\n```", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❓ Unknown command. Use /start to see available commands.")

# -------------------------------------------------------------------
# Main
# -------------------------------------------------------------------
def main():
    try:
        # Load master cache on startup if credentials exist
        if MASTER_MOBILE and MASTER_PASSWORD:
            load_master_data()

        app = Application.builder().token(BOT_TOKEN).build()
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("help", help_command))
        app.add_handler(CommandHandler("login", login))
        app.add_handler(CommandHandler("courses", courses))
        app.add_handler(CommandHandler("select", select))
        app.add_handler(CommandHandler("free", free))
        app.add_handler(CommandHandler("free_select", free_select))
        app.add_handler(CommandHandler("allcourses", allcourses))
        app.add_handler(CommandHandler("withoutlogin", withoutlogin))
        app.add_handler(CommandHandler("profile", profile))
        app.add_handler(CommandHandler("notifications", notifications))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_login_input))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_select_input))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown))
        print("🤖 Bot is running...")
        app.run_polling()
    except Exception as e:
        print(f"❌ Bot failed to start: {e}")
        raise

if __name__ == "__main__":
    main()
