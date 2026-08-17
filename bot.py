import hashlib
import json
import logging
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

import requests
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# =============================================================================
# Configuration
# =============================================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is not set.")

BASE_URL = "https://bridgetosuccess.learncentre.tech/public/study_api_v9/"
PLAYER_URL = "https://lctplayer.learncentre.online/v/player.php?v="

STORAGE = {
    "pdf": "https://bridgetosuccess.learncentre.tech/public/storage/pdf/",
    "timetable": "https://bridgetosuccess.learncentre.tech/public/storage/timetable/",
}

REQUEST_TIMEOUT = 30
API_RETRIES = 3
CATEGORY_DELAY = 0.3

COMMON_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 13; Pixel 6) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.6099.230 Mobile Safari/537.36"
    ),
    "Accept": "application/json",
    "ktx": "co.exam.study.trend1",
    "ktxx": "18.0",
    "brand": "google",
    "model": "Pixel 6",
    "Connection": "close",
}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# =============================================================================
# In-memory user state
# =============================================================================

USER_SESSIONS = {}
USER_CREDENTIALS = {}
USER_COURSES = {}
USER_ALL_COURSES = {}
USER_FREE_CATEGORIES = {}


# =============================================================================
# Health server
# =============================================================================

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        return


def run_health_server():
    try:
        server = HTTPServer(("0.0.0.0", 8000), HealthHandler)
        logger.info("Health server running on port 8000")
        server.serve_forever()
    except Exception:
        logger.exception("Health server stopped")


def start_health_server():
    threading.Thread(target=run_health_server, daemon=True).start()


# =============================================================================
# General helpers
# =============================================================================

def get_device_id(user_id: int) -> str:
    return hashlib.md5(str(user_id).encode()).hexdigest()[:16]


def safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def unique_media(entries):
    seen = set()
    result = []

    for url, title in entries:
        key = url.strip() if isinstance(url, str) and url.strip() else f"title:{title}"
        if key in seen:
            continue
        seen.add(key)
        result.append((url, title))

    return result


def parse_selection(text, max_index):
    indices = []

    for part in text.replace(" ", "").split(","):
        if not part:
            return None

        if "-" in part:
            pieces = part.split("-")
            if len(pieces) != 2:
                return None

            try:
                start, end = map(int, pieces)
            except ValueError:
                return None

            if start < 1 or end > max_index or start > end:
                return None

            indices.extend(range(start - 1, end))
        else:
            try:
                index = int(part)
            except ValueError:
                return None

            if index < 1 or index > max_index:
                return None

            indices.append(index - 1)

    return sorted(set(indices))


def generate_media_text(media_entries, header=None):
    lines = [header] if header else []

    for index, (url, title) in enumerate(media_entries, 1):
        lines.extend(
            [
                f"Entry {index}",
                f"Title: {title}",
                f"URL: {url or '(No URL)'}",
                "",
            ]
        )

    return "\n".join(lines)


def write_temp_file(filename, content):
    path = Path(filename)
    path.write_text(content, encoding="utf-8")
    return path


def remove_file(path):
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        logger.warning("Could not remove temporary file: %s", path)


async def send_document(update, path, filename, caption):
    try:
        with open(path, "rb") as document:
            await update.message.reply_document(
                document=document,
                filename=filename,
                caption=caption,
            )
    finally:
        remove_file(path)


def json_text(data):
    return json.dumps(data, indent=2, ensure_ascii=False)


async def require_session(update):
    user_id = update.effective_user.id
    api = USER_SESSIONS.get(user_id)

    if not api:
        await update.message.reply_text(
            "❌ Not logged in. Use /login or /login_token first."
        )
        return None

    return api


# =============================================================================
# Recursive API extraction helpers
# =============================================================================

def recursive_find(obj, keys):
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in keys and value:
                return value
            found = recursive_find(value, keys)
            if found:
                return found

    elif isinstance(obj, list):
        for item in obj:
            found = recursive_find(item, keys)
            if found:
                return found

    return None


def extract_courses(data):
    courses = []

    def visit(obj):
        if isinstance(obj, dict):
            course_id = obj.get("courseId") or obj.get("id")

            if course_id and "courseName" in obj:
                courses.append(
                    {
                        "courseId": course_id,
                        "courseName": obj["courseName"],
                        "categoryId": obj.get("categoryId") or course_id,
                        "coursePrice": obj.get("coursePrice", "N/A"),
                        "strikeoutPrice": obj.get("strikeoutPrice"),
                    }
                )
                return

            for value in obj.values():
                visit(value)

        elif isinstance(obj, list):
            for item in obj:
                visit(item)

    visit(data)
    return courses


def extract_categories(data):
    categories = []

    def visit(obj):
        if isinstance(obj, dict):
            if "categoryId" in obj and "categoryName" in obj:
                categories.append(
                    {
                        "categoryId": obj["categoryId"],
                        "categoryName": obj["categoryName"],
                    }
                )
                return

            for value in obj.values():
                visit(value)

        elif isinstance(obj, list):
            for item in obj:
                visit(item)

    visit(data)
    return categories


MEDIA_SCHEMAS = {
    "videoStreamURL": ("videoTitle", "title", "name"),
    "pdfUrl": ("pdfTitle", "title", "name"),
    "url": ("title", "name", "videoTitle", "pdfTitle"),
    "streamURL": ("title", "videoTitle"),
    "audioUrl": ("title", "name"),
}


def extract_media_entries(data):
    entries = []

    def visit(obj):
        if isinstance(obj, dict):
            for url_key, title_keys in MEDIA_SCHEMAS.items():
                value = obj.get(url_key)

                if not value:
                    continue

                url = str(value).strip()
                if not url.startswith(("http://", "https://")):
                    continue

                title = next(
                    (
                        str(obj[key]).strip()
                        for key in title_keys
                        if obj.get(key)
                    ),
                    None,
                )

                if not title:
                    title = Path(urlparse(url).path).name or "file"

                entries.append((url, title))

            for value in obj.values():
                visit(value)

        elif isinstance(obj, list):
            for item in obj:
                visit(item)

    visit(data)
    return entries


def get_leaf_category_ids(category_data):
    leaves = []

    def visit(category, path):
        if not isinstance(category, dict):
            return

        current_path = path + [category.get("categoryName", "Unknown")]
        children = category.get("children") or []
        is_leaf = str(category.get("hasChild", "")) == "0" or not children

        if is_leaf and category.get("id") is not None:
            leaves.append(
                {
                    "id": category["id"],
                    "name": category.get("categoryName", "Unknown"),
                    "path": " → ".join(current_path),
                }
            )
            return

        for child in children:
            visit(child, current_path)

    if isinstance(category_data, list):
        for category in category_data:
            visit(category, [])
    elif isinstance(category_data, dict):
        visit(category_data, [])

    return leaves


# =============================================================================
# API client
# =============================================================================

class BridgeToSuccessAPI:
    def __init__(self, mobile=None, password=None, android_id=None):
        self.base_url = BASE_URL.rstrip("/") + "/"
        self.session = requests.Session()
        self.user_id = None
        self.auth_token = None
        self.mobile = mobile
        self.password = password
        self.android_id = android_id or uuid.uuid4().hex[:16]
        self.last_login_time = 0

        self._refresh_headers()

    def _refresh_headers(self):
        headers = COMMON_HEADERS.copy()

        if self.auth_token:
            headers["Authtoken"] = self.auth_token
            headers["Authorization"] = f"Bearer {self.auth_token}"

        self.session.headers.clear()
        self.session.headers.update(headers)

    def set_token(self, user_id, auth_token):
        self.user_id = user_id
        self.auth_token = auth_token
        self._refresh_headers()

    def _process_login_response(self, data):
        self.user_id = recursive_find(data, {"user_id", "userId", "id"})
        self.auth_token = recursive_find(
            data, {"authToken", "auth_token", "token"}
        )
        self._refresh_headers()

    def _re_login(self):
        logger.info("Attempting auto re-login")

        response = self.session.post(
            self.base_url,
            data={
                "tag": "login",
                "mobile": self.mobile,
                "password": self.password,
                "androidId": self.android_id,
                "fcmId": "",
            },
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()

        data = response.json()
        self._process_login_response(data)

        if not self.auth_token:
            raise PermissionError("Auto re-login did not return an auth token.")

        self.last_login_time = time.time()
        logger.info("Auto re-login successful")

    def post(self, data, retries=API_RETRIES):
        for attempt in range(retries):
            try:
                response = self.session.post(
                    self.base_url,
                    data=data,
                    timeout=REQUEST_TIMEOUT,
                )

                if response.status_code == 401:
                    if not (self.mobile and self.password) or attempt == retries - 1:
                        raise PermissionError("Authentication token expired.")

                    if time.time() - self.last_login_time < 300:
                        time.sleep(2)

                    self._re_login()
                    continue

                response.raise_for_status()

                content_type = response.headers.get("Content-Type", "")
                if "application/json" not in content_type.lower():
                    raise ValueError(
                        f"API returned non-JSON response: {response.text[:200]}"
                    )

                return response.json()

            except PermissionError:
                raise

            except (requests.ConnectionError, requests.Timeout):
                if attempt == retries - 1:
                    raise
                time.sleep(2 ** attempt)

        raise RuntimeError("API request failed after retries.")

    def call(self, tag, **params):
        return self.post({"tag": tag, **params})

    # Authentication
    def login_api(self, mobile, password, android_id="", fcm_id=""):
        return self.call(
            "login",
            mobile=mobile,
            password=password,
            androidId=android_id,
            fcmId=fcm_id,
        )

    # Profile
    def get_profile(self):
        return self.call("getProfile", userId=self.user_id)

    def get_notifications(self):
        return self.call("getNotifications", userId=self.user_id)

    # Courses
    def all_courses(self, is_ebook=False):
        return self.call(
            "allCourses",
            userId=self.user_id,
            isEBook=1 if is_ebook else 0,
        )

    def get_all_category(self, course_id):
        return self.call("getAllCategory", courseId=course_id)

    def get_category_mixed(self, course_id, category_id=""):
        return self.call(
            "getCategoryMixed",
            courseId=course_id,
            categoryId=category_id,
            userId=self.user_id,
        )

    def course_info(self, course_id):
        return self.call(
            "courseInfo",
            courseId=course_id,
            userId=self.user_id,
        )

    def all_course_video(self, category_id):
        return self.call(
            "allCourseVideo",
            categoryId=category_id,
            userId=self.user_id,
        )

    def all_course_pdf(self, category_id):
        return self.call(
            "allCoursePdf",
            categoryId=category_id,
            userId=self.user_id,
        )

    def my_courses(self, is_ebook=False):
        return self.call(
            "myCourses",
            userId=self.user_id,
            isEBook=1 if is_ebook else 0,
        )

    # Free content
    def free_course_video(self):
        return self.call("freeCourseVideo", userId=self.user_id)

    def free_course_pdf(self):
        return self.call("freeCoursePdf", userId=self.user_id)

    def get_free_content_category(self, course_type):
        return self.call(
            "getFreeContentCategory",
            userId=self.user_id,
            courseType=course_type,
        )

    def get_free_content(self, course_type, category_id, page=1, page_size=20):
        return self.call(
            "getFreeContent",
            userId=self.user_id,
            courseType=course_type,
            categoryId=category_id,
            pageNumber=str(page),
            pageItemSize=str(page_size),
        )

    def fetch_all_courses_with_details(self, fetch_my=False):
        if fetch_my:
            courses = extract_courses(self.my_courses())
        else:
            normal = extract_courses(self.all_courses(False))
            ebooks = extract_courses(self.all_courses(True))

            by_id = {}
            for course in normal + ebooks:
                by_id[str(course["courseId"])] = course

            courses = list(by_id.values())

        result = []

        for course in courses:
            categories = extract_categories(
                self.get_all_category(course["courseId"])
            )

            category_results = []

            for category in categories:
                category_id = category["categoryId"]

                try:
                    videos = extract_media_entries(
                        self.all_course_video(category_id)
                    )
                except Exception as exc:
                    logger.warning(
                        "Video fetch failed for category %s: %s",
                        category_id,
                        exc,
                    )
                    videos = []

                try:
                    pdfs = extract_media_entries(
                        self.all_course_pdf(category_id)
                    )
                except Exception as exc:
                    logger.warning(
                        "PDF fetch failed for category %s: %s",
                        category_id,
                        exc,
                    )
                    pdfs = []

                category_results.append(
                    {
                        "categoryId": category_id,
                        "categoryName": category["categoryName"],
                        "videos": videos,
                        "pdfs": pdfs,
                    }
                )
                time.sleep(CATEGORY_DELAY)

            result.append(
                {
                    "courseId": course["courseId"],
                    "courseName": course["courseName"],
                    "coursePrice": course.get("coursePrice", "N/A"),
                    "strikeoutPrice": course.get("strikeoutPrice"),
                    "categories": category_results,
                }
            )

        return result


# =============================================================================
# Media / mixed-content helpers
# =============================================================================

def normalize_url(url):
    if not url:
        return ""

    url = str(url).strip()

    if url.startswith(("http://", "https://")):
        return url

    return "https://bridgetosuccess.learncentre.tech/" + url.lstrip("/")


def extract_mixed_items(response):
    if isinstance(response, list):
        return response

    if not isinstance(response, dict):
        return []

    for key in ("mixedContentItems", "mixedContent", "items"):
        value = response.get(key)
        if isinstance(value, list):
            return value

    data = response.get("data")

    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in ("mixedContentItems", "mixedContent", "items"):
            value = data.get(key)
            if isinstance(value, list):
                return value

    return []


def extract_mixed_media(response):
    media = []

    for item in extract_mixed_items(response):
        if not isinstance(item, dict):
            continue

        item_type = item.get("type")
        data = item.get("data") or {}

        if not isinstance(data, dict):
            continue

        if item_type == "video":
            title = data.get("videoName") or "Video"
            fields = ("videoLink", "videoStreamURL", "streamURL", "url")
        elif item_type == "pdf":
            title = data.get("pdfTitle") or "PDF"
            fields = ("pdfFile", "pdfUrl", "url")
        else:
            continue

        urls = []
        for field in fields:
            value = data.get(field)
            if value:
                value = str(value).strip()
                if value:
                    urls.append((field, value))

        if not urls:
            media.append(("", title))
            continue

        if len(urls) == 1:
            media.append((normalize_url(urls[0][1]), title))
        else:
            for index, (_, url) in enumerate(sorted(urls), 1):
                media.append(
                    (normalize_url(url), f"{title} (Player {index})")
                )

    return media


# =============================================================================
# Course loading helpers
# =============================================================================

def get_cached_courses(user_id):
    return USER_COURSES.get(user_id) or USER_ALL_COURSES.get(user_id)


async def load_courses_for_user(update, api, user_id):
    await update.message.reply_text("⏳ Loading courses...")

    try:
        USER_ALL_COURSES[user_id] = api.fetch_all_courses_with_details(False)
        USER_COURSES[user_id] = api.fetch_all_courses_with_details(True)
        await update.message.reply_text("✅ Courses loaded.")
        return True
    except Exception as exc:
        logger.exception("Course loading failed")
        await update.message.reply_text(
            f"⚠️ Could not load courses: {exc}"
        )
        return False


# =============================================================================
# Telegram commands
# =============================================================================

HELP_TEXT = """👋 Welcome!

/login – log in with mobile & password
/login_token <user_id> <auth_token> – log in using an existing token
/logout – clear your session
/session – check session status
/courses – list purchased courses
/allcourses – export all available courses with price and content
/select – choose courses and export media URLs
/free – list free-content categories
/free_select <number> – export a free category
/profile – show profile information
/notifications – show notifications
/debug – export raw course API responses
/debug_categories <course_id> – export raw category API responses
/getcourse <id> – fetch all media for a course
/help – show this message
"""


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


async def login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["login_step"] = "mobile"
    await update.message.reply_text(
        "Enter your mobile number (e.g., 9876543210):"
    )


async def login_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args

    if len(args) < 2:
        await update.message.reply_text(
            "❌ Usage: /login_token <user_id> <auth_token>"
        )
        return

    uid, token = args[0].strip(), args[1].strip()

    if not uid or not token:
        await update.message.reply_text(
            "❌ User ID and token cannot be empty."
        )
        return

    try:
        api = BridgeToSuccessAPI()
        api.set_token(uid, token)

        api.get_profile()

        USER_SESSIONS[user_id] = api
        USER_CREDENTIALS[user_id] = (None, None, None)

        await update.message.reply_text(
            f"✅ Login successful via token!\nUser ID: {uid}"
        )
        await load_courses_for_user(update, api, user_id)

    except Exception as exc:
        await update.message.reply_text(
            f"❌ Token validation failed: {exc}"
        )


async def logout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    USER_SESSIONS.pop(user_id, None)
    USER_CREDENTIALS.pop(user_id, None)
    USER_COURSES.pop(user_id, None)
    USER_ALL_COURSES.pop(user_id, None)
    USER_FREE_CATEGORIES.pop(user_id, None)

    context.user_data.clear()

    await update.message.reply_text("✅ Logged out.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    step = context.user_data.get("step")

    if step == "login_mobile":
        mobile = update.message.text.strip()

        if not mobile.isdigit() or len(mobile) < 10:
            await update.message.reply_text("❌ Invalid mobile number.")
            return

        context.user_data["mobile"] = mobile
        context.user_data["step"] = "login_password"
        await update.message.reply_text("Now enter your password.")
        return

    if step == "login_password":
        await finish_password_login(update, context)
        return

    if step == "course_selection":
        await finish_course_selection(update, context)
        return

    await update.message.reply_text("❓ Unknown input. Use /start for help.")


async def finish_password_login(update, context):
    user_id = update.effective_user.id
    mobile = context.user_data.get("mobile")
    password = update.message.text.strip()

    if not mobile:
        context.user_data.clear()
        await update.message.reply_text("❌ Login session expired.")
        return

    android_id = get_device_id(user_id)

    await update.message.reply_text("⏳ Logging in...")

    try:
        api = BridgeToSuccessAPI(
            mobile=mobile,
            password=password,
            android_id=android_id,
        )

        result = api.login_api(
            mobile,
            password,
            android_id,
            "",
        )
        api._process_login_response(result)

        if not api.user_id or not api.auth_token:
            error = (
                result.get("message")
                if isinstance(result, dict)
                else None
            ) or (
                result.get("error")
                if isinstance(result, dict)
                else None
            ) or "Invalid credentials"

            await update.message.reply_text(f"❌ Login failed: {error}")
            context.user_data.clear()
            return

        USER_SESSIONS[user_id] = api
        USER_CREDENTIALS[user_id] = (
            mobile,
            password,
            android_id,
        )

        context.user_data.clear()

        await update.message.reply_text(
            f"✅ Login successful!\nUser ID: {api.user_id}"
        )
        await load_courses_for_user(update, api, user_id)

    except Exception as exc:
        logger.exception("Login failed")
        await update.message.reply_text(f"❌ Login error: {exc}")
        context.user_data.clear()


async def session_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    api = await require_session(update)
    if not api:
        return

    login_type = "Password login" if api.mobile else "Token login"

    await update.message.reply_text(
        f"✅ Session active.\n"
        f"User ID: {api.user_id}\n"
        f"Token: {'Present' if api.auth_token else 'Missing'}\n"
        f"Type: {login_type}"
    )


async def courses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    api = await require_session(update)

    if not api:
        return

    course_list = USER_COURSES.get(user_id)

    if not course_list:
        try:
            course_list = extract_courses(api.my_courses())
            USER_COURSES[user_id] = course_list
        except Exception as exc:
            await update.message.reply_text(f"❌ Error: {exc}")
            return

    if not course_list:
        await update.message.reply_text("No purchased courses found.")
        return

    lines = ["📖 Your Purchased Courses:", ""]

    for index, course in enumerate(course_list, 1):
        lines.append(
            f"{index}. {course['courseName']} "
            f"(ID: {course['courseId']})"
        )

        price = course.get("coursePrice")
        strike = course.get("strikeoutPrice")

        if price and strike:
            lines.append(f"   Price: ₹{price} (Strikeout: ₹{strike})")
        elif price:
            lines.append(f"   Price: ₹{price}")

    lines.append("")
    lines.append("Use /allcourses for all available courses.")

    await update.message.reply_text("\n".join(lines))


async def allcourses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    api = await require_session(update)

    if not api:
        return

    course_list = USER_ALL_COURSES.get(user_id)

    if not course_list:
        await update.message.reply_text(
            "📚 Fetching all available courses. This may take a moment..."
        )
        try:
            course_list = api.fetch_all_courses_with_details(False)
            USER_ALL_COURSES[user_id] = course_list
        except Exception as exc:
            await update.message.reply_text(f"❌ Error: {exc}")
            return

    if not course_list:
        await update.message.reply_text("No courses available.")
        return

    lines = ["=== ALL AVAILABLE COURSES ==="]

    for course in course_list:
        lines.append(
            f"\n📚 {course['courseName']} "
            f"(ID: {course['courseId']})"
        )

        price = course.get("coursePrice")
        strike = course.get("strikeoutPrice")
        price_num = safe_float(price)
        strike_num = safe_float(strike)

        if (
            price_num is not None
            and strike_num is not None
            and strike_num > price_num
        ):
            discount = int(
                (strike_num - price_num) / strike_num * 100
            )
            lines.append(
                f"   💰 Price: ₹{price} "
                f"(~~₹{strike}~~) - {discount}% OFF"
            )
        elif price:
            lines.append(f"   💰 Price: ₹{price}")
        else:
            lines.append("   💰 Price: N/A")

        for category in course.get("categories", []):
            lines.append(
                f"\n  🗂️ {category['categoryName']} "
                f"(ID: {category['categoryId']})"
            )

            videos = category.get("videos", [])
            pdfs = category.get("pdfs", [])

            lines.append(f"    🎬 Videos ({len(videos)}):")
            if videos:
                lines.extend(
                    f"      - {title} -> {url}"
                    for url, title in videos
                )
            else:
                lines.append("      None")

            lines.append(f"    📄 PDFs ({len(pdfs)}):")
            if pdfs:
                lines.extend(
                    f"      - {title} -> {url}"
                    for url, title in pdfs
                )
            else:
                lines.append("      None")

    path = write_temp_file(
        f"all_courses_{user_id}.txt",
        "\n".join(lines),
    )

    await send_document(
        update,
        path,
        "all_courses_details.txt",
        "✅ All courses with price, categories, videos, and PDFs.",
    )


async def select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    api = await require_session(update)

    if not api:
        return

    courses_list = get_cached_courses(user_id)

    if not courses_list:
        await update.message.reply_text(
            "❌ No courses loaded. Use /courses or /allcourses first."
        )
        return

    context.user_data["step"] = "course_selection"

    await update.message.reply_text(
        "📝 Enter course numbers (e.g., 1,3,5 or 1-5):"
    )


async def finish_course_selection(update, context):
    user_id = update.effective_user.id
    api = USER_SESSIONS.get(user_id)

    if not api:
        context.user_data.pop("step", None)
        await update.message.reply_text("❌ Not logged in.")
        return

    courses_list = get_cached_courses(user_id)

    if not courses_list:
        context.user_data.pop("step", None)
        await update.message.reply_text("❌ No courses loaded.")
        return

    indices = parse_selection(
        update.message.text.strip(),
        len(courses_list),
    )

    if indices is None:
        await update.message.reply_text("❌ Invalid selection.")
        return

    selected = [courses_list[index] for index in indices]

    await update.message.reply_text(
        f"✅ Selected {len(selected)} courses. Fetching media..."
    )

    media = []

    for course in selected:
        category_id = course["categoryId"]

        for fetcher in (
            api.all_course_video,
            api.all_course_pdf,
        ):
            try:
                media.extend(
                    extract_media_entries(fetcher(category_id))
                )
            except Exception as exc:
                logger.warning(
                    "Media fetch failed for category %s: %s",
                    category_id,
                    exc,
                )

        time.sleep(0.5)

    for fetcher in (
        api.free_course_video,
        api.free_course_pdf,
    ):
        try:
            media.extend(extract_media_entries(fetcher()))
        except Exception as exc:
            logger.warning("Free media fetch failed: %s", exc)

    media = unique_media(media)

    if not media:
        await update.message.reply_text(
            "No media found for selected courses."
        )
        context.user_data.pop("step", None)
        return

    path = write_temp_file(
        f"course_media_{user_id}.txt",
        generate_media_text(media),
    )

    await send_document(
        update,
        path,
        "selected_courses_media.txt",
        f"✅ {len(media)} media entries.",
    )

    context.user_data.pop("step", None)


async def free(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    api = await require_session(update)

    if not api:
        return

    await update.message.reply_text("📂 Fetching free content...")

    categories = []

    for content_type in ("video", "pdf"):
        try:
            data = api.get_free_content_category(content_type)
            for category in extract_categories(data):
                category["type"] = content_type
                categories.append(category)
        except Exception as exc:
            logger.warning(
                "Free %s category fetch failed: %s",
                content_type,
                exc,
            )

    if categories:
        USER_FREE_CATEGORIES[user_id] = categories

        lines = ["📖 Free Content Categories:", ""]
        lines.extend(
            f"{index}. {category['categoryName']} ({category['type']})"
            for index, category in enumerate(categories, 1)
        )
        lines.append("")
        lines.append(
            "Use /free_select <number> to export a category."
        )

        await update.message.reply_text("\n".join(lines))
        return

    try:
        media = []
        media.extend(
            extract_media_entries(api.free_course_video())
        )
        media.extend(
            extract_media_entries(api.free_course_pdf())
        )
        media = unique_media(media)

        if not media:
            await update.message.reply_text("No free content found.")
            return

        path = write_temp_file(
            f"free_media_{user_id}.txt",
            generate_media_text(media),
        )

        await send_document(
            update,
            path,
            "free_content.txt",
            f"✅ Free content: {len(media)} items.",
        )

    except Exception as exc:
        await update.message.reply_text(f"❌ Error: {exc}")


async def free_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    api = await require_session(update)

    if not api:
        return

    categories = USER_FREE_CATEGORIES.get(user_id)

    if not categories:
        await update.message.reply_text(
            "❌ No categories found. Run /free first."
        )
        return

    if not context.args:
        await update.message.reply_text(
            "❌ Usage: /free_select <number>"
        )
        return

    try:
        index = int(context.args[0]) - 1
    except ValueError:
        await update.message.reply_text("❌ Please enter a number.")
        return

    if not 0 <= index < len(categories):
        await update.message.reply_text("❌ Invalid category number.")
        return

    category = categories[index]
    category_type = category["type"]

    await update.message.reply_text(
        f"📥 Fetching {category_type} content for "
        f"'{category['categoryName']}'..."
    )

    try:
        data = api.get_free_content(
            category_type,
            category["categoryId"],
            page=1,
            page_size=100,
        )

        media = unique_media(
            extract_media_entries(data)
        )

        if not media:
            await update.message.reply_text(
                "No media found in this category."
            )
            return

        path = write_temp_file(
            f"free_category_{user_id}.txt",
            generate_media_text(media),
        )

        safe_name = (
            "".join(
                char if char.isalnum() or char in "._-" else "_"
                for char in category["categoryName"]
            )
            or "category"
        )

        await send_document(
            update,
            path,
            f"{safe_name}_{category_type}.txt",
            f"✅ {len(media)} media items in "
            f"{category['categoryName']}",
        )

    except Exception as exc:
        await update.message.reply_text(f"❌ Error: {exc}")


async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    api = await require_session(update)
    if not api:
        return

    try:
        data = api.get_profile()
        await update.message.reply_text(
            f"Profile info:\n```json\n{json_text(data)}\n```",
            parse_mode="Markdown",
        )
    except Exception as exc:
        await update.message.reply_text(f"❌ Error: {exc}")


async def notifications(update: Update, context: ContextTypes.DEFAULT_TYPE):
    api = await require_session(update)
    if not api:
        return

    try:
        data = api.get_notifications()
        await update.message.reply_text(
            f"Notifications:\n```json\n{json_text(data)}\n```",
            parse_mode="Markdown",
        )
    except Exception as exc:
        await update.message.reply_text(f"❌ Error: {exc}")


async def debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    api = await require_session(update)

    if not api:
        return

    try:
        data = {
            "normal": api.all_courses(False),
            "ebook": api.all_courses(True),
        }

        path = write_temp_file(
            f"debug_{user_id}.json",
            json_text(data),
        )

        await send_document(
            update,
            path,
            "debug_courses.json",
            "Raw API responses for allCourses (isEBook=0 and isEBook=1).",
        )
    except Exception as exc:
        await update.message.reply_text(f"❌ Error: {exc}")


async def debug_categories(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user_id = update.effective_user.id
    api = await require_session(update)

    if not api:
        return

    if not context.args:
        await update.message.reply_text(
            "❌ Usage: /debug_categories <course_id>"
        )
        return

    course_id = context.args[0].strip()

    try:
        data = {
            "course_id": course_id,
            "getAllCategory": api.get_all_category(course_id),
            "getCategoryMixed": api.get_category_mixed(course_id, ""),
        }

        path = write_temp_file(
            f"debug_categories_{course_id}_{user_id}.json",
            json_text(data),
        )

        await send_document(
            update,
            path,
            f"debug_categories_{course_id}.json",
            f"🔍 Raw category responses for course {course_id}.",
        )
    except Exception as exc:
        await update.message.reply_text(f"❌ Error: {exc}")


async def getcourse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    api = await require_session(update)

    if not api:
        return

    if not context.args:
        await update.message.reply_text(
            "❌ Usage: /getcourse <course_id>"
        )
        return

    course_id = context.args[0].strip()
    media = []

    try:
        category_response = api.get_all_category(course_id)
        category_data = (
            category_response.get("category")
            if isinstance(category_response, dict)
            else category_response
        )

        leaves = get_leaf_category_ids(category_data)

        for leaf in leaves:
            try:
                media.extend(
                    extract_mixed_media(
                        api.get_category_mixed(
                            course_id,
                            leaf["id"],
                        )
                    )
                )
            except Exception as exc:
                logger.warning(
                    "Category %s failed: %s",
                    leaf["id"],
                    exc,
                )

            time.sleep(CATEGORY_DELAY)

        if not media:
            media.extend(
                extract_mixed_media(
                    api.get_category_mixed(course_id, "")
                )
            )

        # Add course-level demo assets.
        info = api.course_info(course_id)

        if isinstance(info, dict):
            courses = info.get("courses") or []

            if courses:
                course = courses[0]

                intro_video_id = course.get("introVideoId")
                batch_pdf = course.get("batchInfoPdf")
                timetable = course.get("timeTableImg")

                if intro_video_id and str(intro_video_id).strip() != "null":
                    media.append(
                        (
                            PLAYER_URL + str(intro_video_id).strip(),
                            "Intro Video (Demo)",
                        )
                    )

                if batch_pdf:
                    media.append(
                        (
                            STORAGE["pdf"] + str(batch_pdf).strip(),
                            "Batch Info PDF (Demo)",
                        )
                    )

                if timetable:
                    media.append(
                        (
                            STORAGE["timetable"] + str(timetable).strip(),
                            "Timetable Image (Demo)",
                        )
                    )

        media = unique_media(media)

        if not media:
            await update.message.reply_text(
                f"No media found for course {course_id}."
            )
            return

        path = write_temp_file(
            f"course_{course_id}_{user_id}.txt",
            generate_media_text(
                media,
                header=f"Course ID: {course_id}",
            ),
        )

        await send_document(
            update,
            path,
            f"course_{course_id}_media.txt",
            f"✅ {len(media)} media items for course {course_id}.",
        )

    except Exception as exc:
        logger.exception("getcourse failed")
        await update.message.reply_text(f"❌ Error: {exc}")


# =============================================================================
# Bot setup
# =============================================================================

def build_application():
    app = Application.builder().token(BOT_TOKEN).build()

    commands = {
        "start": start,
        "help": help_command,
        "login": login,
        "login_token": login_token,
        "logout": logout,
        "session": session_command,
        "courses": courses,
        "allcourses": allcourses,
        "select": select,
        "free": free,
        "free_select": free_select,
        "profile": profile,
        "notifications": notifications,
        "debug": debug_command,
        "debug_categories": debug_categories,
        "getcourse": getcourse,
    }

    for command, handler in commands.items():
        app.add_handler(CommandHandler(command, handler))

    # One text handler handles login, course selection, and unknown input.
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_text,
        )
    )

    return app


def main():
    start_health_server()

    app = build_application()

    logger.info("Bot is starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
