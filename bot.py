import os
import json
import uuid
import time
import hashlib
from urllib.parse import urlparse
from datetime import datetime

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

# Master account credentials (enable --without-login mode)
MASTER_MOBILE = os.environ.get("MASTER_MOBILE", "")
MASTER_PASSWORD = os.environ.get("MASTER_PASSWORD", "")
MASTER_ANDROID_ID = os.environ.get("MASTER_ANDROID_ID", "0000000000000000")

# In-memory storage for user sessions and data
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
# API Client
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

    # ------------------------------------------------------------------
    # Authentication & Profile
    # ------------------------------------------------------------------
    def login_api(self, mobile, password, android_id="", fcm_id=""):
        return self.call("login", mobile=mobile, password=password,
                         androidId=android_id, fcmId=fcm_id)

    def register_otp(self, mobile, is_signup_otp_on=True):
        return self.call("registerOTP", mobile=mobile, isSignupOtpOn=str(is_signup_otp_on).lower())

    def register(self, name, mobile, email, state, city, password, fcm_id, otp, android_id, is_signup_otp_on=True):
        return self.call("register", name=name, mobile=mobile, email=email,
                         state=state, city=city, password=password, fcmId=fcm_id,
                         otp=otp, androidId=android_id, isSignupOtpOn=str(is_signup_otp_on).lower())

    def forgot_otp(self, mobile):
        return self.call("forgotOTP", mobile=mobile)

    def validate_forgot_otp(self, mobile, otp):
        return self.call("validateForgotOTP", mobile=mobile, otp=otp)

    def reset_password(self, mobile, password):
        return self.call("resetPassword", mobile=mobile, password=password)

    def get_profile(self):
        return self.call("getProfile", userId=self.user_id)

    def update_profile(self, name, mobile, email, state, city, password, base64_image=""):
        return self.call("updateProfile", userId=self.user_id, name=name, mobile=mobile,
                         email=email, state=state, city=city, password=password,
                         base64Image=base64_image)

    def update_fcm_id(self, fcm_id):
        return self.call("updateFcmId", userId=self.user_id, fcmId=fcm_id)

    # ------------------------------------------------------------------
    # Courses
    # ------------------------------------------------------------------
    def all_courses(self, is_ebook=False):
        return self.call("allCourses", userId=self.user_id, isEBook=1 if is_ebook else 0)

    def top_courses(self, is_ebook=False):
        return self.call("topCourses", userId=self.user_id, isEBook=1 if is_ebook else 0)

    def my_courses(self, is_ebook=False):
        return self.call("myCourses", userId=self.user_id, isEBook=1 if is_ebook else 0)

    def course_info(self, course_id):
        return self.call("courseInfo", courseId=course_id, userId=self.user_id)

    def get_all_category(self, course_id):
        return self.call("getAllCategory", courseId=course_id)

    def get_category(self, course_id, category_id):
        return self.call("getCategory", courseId=course_id, categoryId=category_id)

    def get_category_mixed(self, course_id, category_id):
        return self.call("getCategoryMixed", courseId=course_id, categoryId=category_id, userId=self.user_id)

    def all_course_video(self, category_id):
        return self.call("allCourseVideo", categoryId=category_id, userId=self.user_id)

    def my_course_video(self, category_id):
        return self.call("myCourseVideo", categoryId=category_id, userId=self.user_id)

    def all_course_pdf(self, category_id):
        return self.call("allCoursePdf", categoryId=category_id, userId=self.user_id)

    def my_course_pdf(self, category_id):
        return self.call("myCoursePdf", categoryId=category_id, userId=self.user_id)

    def live_class(self):
        return self.call("liveClass", userId=self.user_id)

    def books(self):
        return self.call("books")

    # ------------------------------------------------------------------
    # Free Content
    # ------------------------------------------------------------------
    def free_course_video(self):
        return self.call("freeCourseVideo", userId=self.user_id)

    def free_course_pdf(self):
        return self.call("freeCoursePdf", userId=self.user_id)

    def get_free_content_category(self, course_type):
        return self.call("getFreeContentCategory", userId=self.user_id, courseType=course_type)

    def get_free_content(self, course_type, category_id, page=1, page_size=20):
        return self.call("getFreeContent", userId=self.user_id, courseType=course_type,
                         categoryId=category_id, pageNumber=str(page), pageItemSize=str(page_size))

    # ------------------------------------------------------------------
    # Tests & Quizzes
    # ------------------------------------------------------------------
    def test_course_list(self, fetch_type, category_id="", page=1, page_size=20):
        return self.call("testCourseList", userId=self.user_id, fetchType=fetch_type,
                         categoryId=category_id, pageNumber=str(page), pageItemSize=str(page_size))

    def test_list(self, course_id, category_id, page=1, page_size=20):
        return self.call("testList", userId=self.user_id, courseId=course_id,
                         categoryId=category_id, pageNumber=str(page), pageItemSize=str(page_size))

    def free_test(self, page=1, page_size=20):
        return self.call("freeTest", userId=self.user_id, pageNumber=str(page), pageItemSize=str(page_size))

    def online_test(self, test_id):
        return self.call("onlineTest", testId=test_id, userId=self.user_id)

    def save_test_rank(self, test_id, score):
        return self.call("saveTestRank", userId=self.user_id, testId=test_id, score=str(score))

    def test_top_rank(self, test_id):
        return self.call("testTopRank", testId=test_id)

    def save_test(self, test_id):
        return self.call("saveTest", userId=self.user_id, testId=test_id)

    def delete_saved_test(self, test_id):
        return self.call("deleteSavedTest", userId=self.user_id, testId=test_id)

    def quiz_category_list(self):
        return self.call("quizCategoryList")

    def quiz_by_category(self, quiz_category_id, page=1, page_size=20):
        return self.call("quizByCategory", quizCategoryId=quiz_category_id,
                         pageNumber=str(page), pageItemSize=str(page_size))

    def online_quiz(self, test_id):
        return self.call("onlineQuiz", testId=test_id, userId=self.user_id)

    # ------------------------------------------------------------------
    # Miscellaneous
    # ------------------------------------------------------------------
    def banner(self, banner_type):
        return self.call("banner", userId=self.user_id, bannerType=banner_type)

    def configuration(self):
        return self.call("configuration")

    def social_links(self):
        return self.call("socialLinks")

    def frames(self):
        return self.call("frames")

    def enroll(self, course_id, course_name, course_price, amount, course_package_id):
        return self.call("enroll", userId=self.user_id, courseId=course_id,
                         courseName=course_name, coursePrice=course_price,
                         amount=amount, coursePackageId=course_package_id)

    def checkout_initiated(self, course_id, course_name, course_price, amount,
                           discount, promotion_id, promo_code, status):
        return self.call("checkoutInitiated", userId=self.user_id, courseId=course_id,
                         courseName=course_name, coursePrice=course_price, amount=amount,
                         discount=discount, promotionId=promotion_id, promoCode=promo_code,
                         status=status)

    def checkout_update(self, payment_ledger_id, status):
        return self.call("checkoutUpdate", paymentLedgerId=payment_ledger_id, status=status)

    def checkout_completed(self, payment_ledger_id, order_id, pay_id, status, course_package_id):
        return self.call("checkoutCompleted", paymentLedgerId=payment_ledger_id,
                         orderId=order_id, payId=pay_id, status=status,
                         coursePackageId=course_package_id)

    def device_verification(self, android_id):
        return self.call("deviceVerification", androidId=android_id, userId=self.user_id)

    def event_watching(self, name, video_id, action):
        return self.call("eventWatching", userId=self.user_id, name=name, videoId=video_id, action=action)

    def my_comments(self, video_id):
        return self.call("myComments", userId=self.user_id, videoId=video_id)

    def send_comment(self, name, video_id, message):
        return self.call("sendComment", userId=self.user_id, name=name, videoId=video_id, message=message)

    def add_course_doubt(self, course_id, category_id, category_name, problem, image_base64=""):
        return self.call("addCourseDoubt", userId=self.user_id, courseId=course_id,
                         categoryId=category_id, categoryName=category_name, problem=problem,
                         imageBase64=image_base64)

    def get_my_course_doubt(self, course_id):
        return self.call("getMyCourseDoubt", userId=self.user_id, courseId=course_id)

    def get_my_tickets(self):
        return self.call("getMyTickets", userId=self.user_id)

    def get_ticket_category(self):
        return self.call("getTicketCategory")

    def add_ticket(self, category_id, category_name, problem, image_base64=""):
        return self.call("addTicket", userId=self.user_id, categoryId=category_id,
                         categoryName=category_name, problem=problem, imageBase64=image_base64)

    def super_stream(self):
        return self.call("superStream", userId=self.user_id)

    def check_stream(self):
        return self.call("checkStream", userId=self.user_id)

    def save_stream(self, stream_id, medium):
        return self.call("saveStream", userId=self.user_id, streamId=stream_id, medium=medium)

    def extract_url(self, ytvideo_id):
        return self.call("extractURL", ytvideoId=ytvideo_id)

    def crash_log(self, app_version, phone_version, model, brand, activity, crash_log):
        return self.call("crashLog", userId=self.user_id, appVersion=app_version,
                         phoneVersion=phone_version, model=model, brand=brand,
                         activity=activity, crashLog=crash_log)

    def top_teachers(self, course_id):
        return self.call("topTeachers", courseId=course_id, userId=self.user_id)

    def top_students(self):
        return self.call("topStudents", userId=self.user_id)

    def get_notifications(self):
        return self.call("getNotifications", userId=self.user_id)

    def delivered_notifications(self):
        return self.call("deliveredNotifications", userId=self.user_id)

    def get_web_notifications(self):
        return self.call("getWebpageNotifications", userId=self.user_id)

    def get_my_blocked_course(self):
        return self.call("getMyBlockedCourse", userId=self.user_id)

    def auto_expired_files(self):
        return self.call("autoExpiredFiles", userId=self.user_id)

    def remove_expired_files(self, video_ids, pdf_ids):
        return self.call("removeExpiredFiles", userId=self.user_id,
                         videoIds=video_ids, pdfIds=pdf_ids)

    def auto_apply_promo(self, course_id, course_price):
        return self.call("autoApplyPromo", userId=self.user_id, courseId=course_id, coursePrice=course_price)

    def validate_promo(self, course_id, promo_code, course_price):
        return self.call("validatePromo", userId=self.user_id, courseId=course_id,
                         promoCode=promo_code, coursePrice=course_price)

    def can_show_test_result(self, test_id):
        return self.call("canShowTestResult", testId=test_id)

    def my_subject(self, course_id, tag_id):
        return self.call("mySubject", courseId=course_id, tagId=tag_id)

    def get_plans(self, course_id):
        return self.call("getPlans", courseId=course_id)

    def get_poll(self, event_id):
        return self.call("getPoll", userId=self.user_id, eventId=event_id)

    def poll_vote(self, poll_id, poll_option_id):
        return self.call("pollVote", userId=self.user_id, pollId=poll_id, pollOptionId=poll_option_id)

    def my_exam_list(self):
        return self.call("myExamList", userId=self.user_id)

    def my_mcq_rank(self, exam_id):
        return self.call("myMCQRank", userId=self.user_id, examId=exam_id)

    def get_exam(self, exam_key):
        return self.call("getExam", examKey=exam_key, userId=self.user_id)

    def update_exam(self, exam_data):  # exam_data is dict from Exam model
        return self.call("updateExam", userId=self.user_id, **exam_data)

    # ------------------------------------------------------------------
    # Bulk fetch for master caching
    # ------------------------------------------------------------------
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
def get_device_id(user_id: int) -> str:
    return hashlib.md5(str(user_id).encode()).hexdigest()[:16]

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
        print("Master credentials missing.")
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
        "/tests – test/quiz submenu (work in progress)\n"
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
        android_id = get_device_id(user_id)
        await update.message.reply_text("⏳ Logging in...")
        try:
            api = BridgeToSuccessAPI(mobile=mobile, password=password, android_id=android_id)
            api.login_api(mobile, password, android_id, "")
            if api.user_id and api.auth_token:
                user_sessions[user_id] = api
                user_credentials[user_id] = (mobile, password, android_id)
                context.user_data["login_step"] = None
                await update.message.reply_text(f"✅ Login successful!\nUser ID: {api.user_id}")
                # Pre-fetch courses for this user
                await update.message.reply_text("⏳ Fetching courses...")
                try:
                    user_courses[user_id] = api.fetch_all_courses_with_details()
                    await update.message.reply_text("✅ Courses cached.")
                except Exception as e:
                    await update.message.reply_text(f"⚠️ Could not fetch courses: {e}")
            else:
                await update.message.reply_text("❌ Login failed – invalid credentials or server error.")
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
                api.login_api(mobile, password, android_id, "")
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
                    api.login_api(mobile, password, android_id, "")
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

    # Deduplicate
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
        caption=f"✅ {len(unique)} media entries."
    )
    os.remove(filename)
    context.user_data["select_step"] = None

async def free(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    api = user_sessions.get(user_id)

    # If not logged in, try cached free content
    if not api:
        if cached_data_loaded and (cached_free_videos or cached_free_pdfs):
            # Show cached free content
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
        # Extract video categories
        video_cats = extract_categories(video_cats_data) if video_cats_data else []
        for cat in video_cats:
            cat["type"] = "video"
        categories.extend(video_cats)
        # Extract pdf categories
        pdf_cats = extract_categories(pdf_cats_data) if pdf_cats_data else []
        for cat in pdf_cats:
            cat["type"] = "pdf"
        categories.extend(pdf_cats)

        if not categories:
            # Fallback: fetch all free content directly
            await update.message.reply_text("No categories found. Fetching all free content...")
            videos = api.free_course_video()
            pdfs = api.free_course_pdf()
            all_media = []
            all_media.extend(extract_media_entries(videos))
            all_media.extend(extract_media_entries(pdfs))
            if not all_media:
                await update.message.reply_text("No free content found.")
                return
            unique = list(dict.fromkeys(all_media))  # dedup preserves order
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

async def allcourses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    api = user_sessions.get(user_id)
    if not api:
        # Try cached data
        if cached_data_loaded:
            courses = cached_courses
            # Build dump from cached
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
        else:
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

async def withoutlogin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not cached_data_loaded:
        await update.message.reply_text("❌ No cached data available. Master credentials may be missing or cache not loaded.")
        return
    user_id = update.effective_user.id
    # Build a summary text file from cache
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
    lines.append(f"\n\nLast updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
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

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    api = user_sessions.get(user_id)
    if not api:
        await update.message.reply_text("❌ Not logged in. Use /login first.")
        return
    try:
        data = api.get_profile()
        # Pretty print as JSON (or format nicely)
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

async def tests(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Placeholder: show test submenu
    await update.message.reply_text(
        "🧪 Tests / Quizzes submenu (coming soon).\n"
        "Available commands (use /tests_<command>):\n"
        "- test_course_list\n"
        "- test_list\n"
        "- free_test\n"
        "- online_test\n"
        "- quiz_category_list\n"
        "- quiz_by_category"
    )

async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❓ Unknown command. Use /start to see available commands.")

# -------------------------------------------------------------------
# Main
# -------------------------------------------------------------------
def main():
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
    app.add_handler(CommandHandler("tests", tests))

    # Message handlers for login and select steps
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_login_input))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_select_input))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown))

    print("🤖 Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
