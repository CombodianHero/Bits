"""
╔══════════════════════════════════════════════════════════════════╗
║     BRIDGE TO SUCCESS — Complete Python SDK                     ║
║     Mirrors the full app flow: Login → Courses → Video/PDF     ║
║     API Base: study_api_v9                                      ║
╚══════════════════════════════════════════════════════════════════╝

Usage:
    from bridge_to_success_sdk import BridgeToSuccess
    
    app = BridgeToSuccess()
    app.login("9999999999", "password")
    courses = app.get_my_courses()
    videos  = app.get_videos(course_id=1, subject_id=2, chapter_id=3)
    app.download_pdf("chapter1.pdf", "output.pdf")
"""

import os
import json
import time
import requests
import hashlib
from pathlib import Path
from typing import Optional


# ─────────────────────────────────────────────────────────────────
# CONSTANTS (from APK DEX analysis)
# ─────────────────────────────────────────────────────────────────

BASE_URL  = "https://bridgetosuccess.learncentre.tech"
API_BASE  = f"{BASE_URL}/public/study_api_v9/"

STORAGE = {
    "video"       : f"{BASE_URL}/public/storage/video/",
    "pdf"         : f"{BASE_URL}/public/storage/pdf/",
    "banner"      : f"{BASE_URL}/public/storage/banner/",
    "category"    : f"{BASE_URL}/public/storage/category/",
    "course"      : f"{BASE_URL}/public/storage/course/",
    "profile"     : f"{BASE_URL}/public/storage/profile_image/",
    "question"    : f"{BASE_URL}/public/storage/question/",
    "timetable"   : f"{BASE_URL}/public/storage/timetable/",
    "top_student" : f"{BASE_URL}/public/storage/top_student/",
    "top_teacher" : f"{BASE_URL}/public/storage/top_teacher/",
    "stream"      : f"{BASE_URL}/public/storage/stream/",
    "ticket"      : f"{BASE_URL}/public/storage/ticket/",
    "frame"       : f"{BASE_URL}/public/storage/frame/",
    "social"      : f"{BASE_URL}/public/storage/social/",
    "event"       : f"{BASE_URL}/public/storage/event/",
}

PLAYER_URL      = "https://lctplayer.learncentre.online/v/player.php?v="
LIVE_PLAYER_URL = "https://lctplayer.learncentre.online/live/live_player.php?v="
TEST_URL        = f"{BASE_URL}/attempt-test/%s?user_id=%s"
TEST_ANSWER_URL = f"{BASE_URL}/view-test-answers/%s?user_id=%s"
PDF_WEB_URL     = f"{BASE_URL}/pdf-page?name="

DEVICE_INFO = {
    "device_id"      : "python-sdk-001",
    "device_token"   : "fcm_placeholder",
    "device_type"    : "android",
    "device_model"   : "Pixel 6",
    "platform"       : "android",
    "source"         : "app",
    "app_version"    : "1.0",
    "version_code"   : "1",
    "os_version"     : "13",
    "android_version": "13",
    "registration_id": "fcm_placeholder",
}

SESSION_FILE = "bts_session.json"


# ─────────────────────────────────────────────────────────────────
# EXCEPTIONS
# ─────────────────────────────────────────────────────────────────

class BTSError(Exception):          pass
class BTSAuthError(BTSError):       pass
class BTSNotFoundError(BTSError):   pass
class BTSAPIError(BTSError):        pass


# ─────────────────────────────────────────────────────────────────
# MAIN SDK CLASS
# ─────────────────────────────────────────────────────────────────

class BridgeToSuccess:
    """
    Complete Python SDK for Bridge to Success app.
    Mirrors the full app flow from SplashActivity to video/PDF play.
    """

    def __init__(self, session_file: str = SESSION_FILE, verbose: bool = True):
        self.session_file = session_file
        self.verbose      = verbose
        self.token        = None
        self.user_id      = None
        self.name         = None
        self.mobile       = None
        self.session      = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept"      : "application/json",
            "User-Agent"  : "okhttp/4.9.3",
        })
        # Try to restore saved session (like app reading SharedPreferences)
        self._load_session()

    # ── LOGGING ──────────────────────────────────────────────────
    def _log(self, msg: str):
        if self.verbose:
            print(f"[BTS] {msg}")

    # ── SESSION (mirrors SharedPreferences) ──────────────────────
    def _save_session(self):
        """Save session to file (mirrors SharedPreferences on Android)."""
        data = {
            "token"  : self.token,
            "user_id": self.user_id,
            "name"   : self.name,
            "mobile" : self.mobile,
        }
        with open(self.session_file, "w") as f:
            json.dump(data, f, indent=2)
        self._log(f"Session saved to {self.session_file}")

    def _load_session(self):
        """Load saved session (mirrors app reading SharedPreferences at startup)."""
        if os.path.exists(self.session_file):
            try:
                with open(self.session_file) as f:
                    data = json.load(f)
                self.token   = data.get("token")
                self.user_id = data.get("user_id")
                self.name    = data.get("name")
                self.mobile  = data.get("mobile")
                if self.token:
                    self._set_auth_header()
                    self._log(f"Session restored for {self.name}")
            except:
                pass

    def _clear_session(self):
        """Clear session (mirrors logout)."""
        self.token = self.user_id = self.name = self.mobile = None
        self.session.headers.pop("Authorization", None)
        self.session.headers.pop("authtoken", None)
        if os.path.exists(self.session_file):
            os.remove(self.session_file)

    def _set_auth_header(self):
        """Set auth headers for all subsequent requests."""
        self.session.headers["Authorization"] = f"Bearer {self.token}"
        self.session.headers["authtoken"]     = self.token

    # ── RAW API CALLS ─────────────────────────────────────────────
    def _post(self, endpoint: str, data: dict = None) -> dict:
        url  = API_BASE + endpoint
        data = data or {}
        try:
            r = self.session.post(url, json=data, timeout=20)
            self._log(f"POST {endpoint} → {r.status_code}")
            resp = r.json()
            if resp.get("status") == 0:
                raise BTSAPIError(f"{endpoint}: {resp.get('message','API error')}")
            return resp
        except BTSAPIError:
            raise
        except Exception as e:
            raise BTSAPIError(f"Request failed [{endpoint}]: {e}")

    def _get(self, endpoint: str, params: dict = None) -> dict:
        url = API_BASE + endpoint
        try:
            r = self.session.get(url, params=params, timeout=20)
            self._log(f"GET {endpoint} → {r.status_code}")
            resp = r.json()
            if resp.get("status") == 0:
                raise BTSAPIError(f"{endpoint}: {resp.get('message','API error')}")
            return resp
        except BTSAPIError:
            raise
        except Exception as e:
            raise BTSAPIError(f"Request failed [{endpoint}]: {e}")

    def _as_list(self, data) -> list:
        if isinstance(data, list): return data
        if isinstance(data, dict): return list(data.values())
        return []

    def _extract(self, resp: dict, *keys):
        """Extract first matching key from nested response data."""
        d = resp.get("data") or resp
        if isinstance(d, dict):
            for k in keys:
                v = d.get(k)
                if v: return v
            # Check nested 'user' key
            u = d.get("user", {})
            if isinstance(u, dict):
                for k in keys:
                    v = u.get(k)
                    if v: return v
        return None

    # ─────────────────────────────────────────────────────────────
    # STEP 1 — SPLASH / STARTUP
    # Mirrors: SplashActivity → check token → LoginActivity or Home
    # ─────────────────────────────────────────────────────────────
    def is_logged_in(self) -> bool:
        """Check if session exists (mirrors SplashActivity token check)."""
        return bool(self.token)

    # ─────────────────────────────────────────────────────────────
    # STEP 2 — LOGIN
    # Mirrors: LoginActivity → callLoginAPI → MenuActivity
    # ─────────────────────────────────────────────────────────────
    def login(self, mobile: str, password: str) -> dict:
        """
        Login with mobile + password.
        Mirrors LoginActivity.callLoginAPI()
        """
        self._log(f"Logging in as {mobile}...")

        combos = [
            {"mobile": mobile, "password": password},
            {"phone" : mobile, "password": password},
            {"mobile": mobile, "pass"    : password},
        ]

        last_error = None
        for combo in combos:
            try:
                payload = {**combo, "type": "login", **DEVICE_INFO}
                resp    = self._post("login", payload)

                self.token   = self._extract(resp, "token","authtoken","api_token","access_token") or ""
                self.user_id = str(self._extract(resp, "id","user_id","userId") or "")
                self.name    = self._extract(resp, "name","full_name","student_name") or mobile
                self.mobile  = mobile

                self._set_auth_header()
                self._save_session()
                self._log(f"✅ Logged in as {self.name} (user_id={self.user_id})")
                return {
                    "success" : True,
                    "name"    : self.name,
                    "user_id" : self.user_id,
                    "token"   : self.token,
                }
            except BTSAPIError as e:
                last_error = e
                continue

        raise BTSAuthError(f"Login failed: {last_error}")

    # ─────────────────────────────────────────────────────────────
    # STEP 2B — LOGOUT
    # Mirrors: ProfileActivity → logout button
    # ─────────────────────────────────────────────────────────────
    def logout(self):
        """Logout and clear session. Mirrors logout button in app."""
        try:
            self._post("logout", {"user_id": self.user_id})
        except:
            pass
        self._clear_session()
        self._log("Logged out.")

    # ─────────────────────────────────────────────────────────────
    # STEP 3 — HOME / DASHBOARD
    # Mirrors: MenuActivity → DashboardActivity
    # ─────────────────────────────────────────────────────────────
    def get_home_data(self) -> dict:
        """
        Get home screen data.
        Mirrors DashboardActivity loading banners, top courses, notifications.
        """
        resp = self._get("get-home-data")
        data = resp.get("data", {})
        return {
            "banners"     : self._as_list(data.get("slider") or data.get("banner", [])),
            "top_courses" : self._as_list(data.get("top_course") or data.get("top_courses", [])),
            "notices"     : self._as_list(data.get("notice") or data.get("notices", [])),
            "raw"         : data,
        }

    def get_profile(self) -> dict:
        """Get student profile. Mirrors ProfileActivity."""
        resp = self._get("get-profile")
        return resp.get("data", {})

    def get_notifications(self) -> list:
        """Get notifications. Mirrors NotificationListActivity."""
        resp = self._get("get-notifications")
        return self._as_list(resp.get("data", []))

    # ─────────────────────────────────────────────────────────────
    # STEP 4 — COURSES
    # Mirrors: AllCoursesActivity / MyCoursesActivity
    # ─────────────────────────────────────────────────────────────
    def get_all_courses(self) -> list:
        """Get all available courses. Mirrors AllCoursesActivity."""
        resp = self._get("get-all-courses")
        return self._as_list(resp.get("data", []))

    def get_my_courses(self) -> list:
        """Get enrolled courses. Mirrors MyCoursesActivity."""
        resp = self._get("get-my-courses")
        return self._as_list(resp.get("data", []))

    def get_top_courses(self) -> list:
        """Get top/featured courses. Mirrors TopCoursesActivity."""
        resp = self._get("get-top-courses")
        return self._as_list(resp.get("data", []))

    def get_categories(self) -> list:
        """Get course categories. Mirrors CategoryActivity."""
        resp = self._get("get-categories")
        return self._as_list(resp.get("data", []))

    def get_category_courses(self, category_id) -> list:
        """Get courses by category."""
        resp = self._post("get-category-courses", {"category_id": category_id})
        return self._as_list(resp.get("data", []))

    def get_course_detail(self, course_id) -> dict:
        """Get full course details. Mirrors CourseDetailActivity."""
        resp = self._post("get-course-detail", {"course_id": course_id})
        return resp.get("data", {})

    # ─────────────────────────────────────────────────────────────
    # STEP 5 — ENROLL
    # Mirrors: ShoppingCartActivity → RazorPayActivity
    # ─────────────────────────────────────────────────────────────
    def enroll_free(self, course_id) -> dict:
        """Enroll in a free course. Mirrors enroll-free-course flow."""
        resp = self._post("enroll-free-course", {"course_id": course_id})
        self._log(f"Enrolled in course {course_id}")
        return resp.get("data", {})

    def get_cart(self) -> list:
        """Get shopping cart. Mirrors ShoppingCartActivity."""
        resp = self._get("get-cart")
        return self._as_list(resp.get("data", []))

    # ─────────────────────────────────────────────────────────────
    # STEP 6 — CONTENT TREE
    # Mirrors: VideoPdfTabViewActivity content hierarchy
    # ─────────────────────────────────────────────────────────────
    def get_batch_list(self, course_id) -> list:
        """Get batches for a course."""
        resp = self._post("get-batch-list", {"course_id": course_id})
        return self._as_list(resp.get("data", []))

    def get_subject_list(self, course_id, batch_id=None) -> list:
        """Get subjects. Mirrors subject list screen."""
        payload = {"course_id": course_id}
        if batch_id: payload["batch_id"] = batch_id
        resp = self._post("get-subject-list", payload)
        return self._as_list(resp.get("data", []))

    def get_chapter_list(self, course_id, subject_id, batch_id=None) -> list:
        """Get chapters for a subject."""
        payload = {"course_id": course_id, "subject_id": subject_id}
        if batch_id: payload["batch_id"] = batch_id
        resp = self._post("get-chapter-list", payload)
        return self._as_list(resp.get("data", []))

    def get_topic_list(self, course_id, subject_id, chapter_id) -> list:
        """Get topics for a chapter."""
        resp = self._post("get-topic-list", {
            "course_id" : course_id,
            "subject_id": subject_id,
            "chapter_id": chapter_id,
        })
        return self._as_list(resp.get("data", []))

    # ─────────────────────────────────────────────────────────────
    # STEP 7A — VIDEOS
    # Mirrors: PlayVideoActivity → ExoPlayer / WebView
    # ─────────────────────────────────────────────────────────────
    def get_video_list(self, course_id, subject_id, chapter_id,
                       batch_id=None) -> list:
        """
        Get video list for a chapter.
        Mirrors get-video-list API call in VideoPdfTabViewActivity.
        """
        payload = {
            "course_id" : course_id,
            "subject_id": subject_id,
            "chapter_id": chapter_id,
        }
        if batch_id: payload["batch_id"] = batch_id
        resp = self._post("get-video-list", payload)
        videos = self._as_list(resp.get("data", []))

        # Resolve play URLs exactly like the app does
        for v in videos:
            v["play_url"] = self._resolve_video_url(v)

        return videos

    def _resolve_video_url(self, v: dict) -> str:
        """
        Resolve the actual playable URL from video data.
        Mirrors the switch-case in PlayVideoActivity / ExoPlayerMedia3Activity.
        
        Video types found in APK:
          lct      → LCT custom player (HLS/MP4)
          youtube  → YouTube embed / ytdl proxy
          vimeo    → Vimeo player
          live     → LCT live stream player
        """
        vtype = str(v.get("video_type") or v.get("type") or "").lower()

        # Get raw URL or ID
        raw = None
        for k in ["video_url","url","file_url","hls_url","stream_url","link","video_link"]:
            val = v.get(k, "")
            if val and isinstance(val, str) and len(val) > 2:
                raw = val
                break

        if not raw:
            raw = str(v.get("id") or v.get("video_id") or "")

        if not raw:
            return "URL_NOT_FOUND"

        # Resolve by type (mirrors app's video type switch)
        if "live" in vtype:
            return LIVE_PLAYER_URL + raw if not raw.startswith("http") else raw

        if "youtube" in vtype or "yt" in vtype:
            if "youtube.com" in raw or "youtu.be" in raw:
                return raw
            return f"https://www.youtube.com/watch?v={raw}"

        if "vimeo" in vtype:
            if "vimeo.com" in raw:
                return raw
            return f"https://player.vimeo.com/video/{raw}"

        # Default: LCT player (self-hosted)
        if raw.startswith("http"):
            return raw
        return PLAYER_URL + raw

    def get_video_detail(self, video_id) -> dict:
        """Get single video detail."""
        resp = self._post("get-video-detail", {"video_id": video_id})
        data = resp.get("data", {})
        if isinstance(data, dict):
            data["play_url"] = self._resolve_video_url(data)
        return data

    def get_free_videos(self) -> list:
        """Get free videos (no auth needed). Mirrors FreeVideoActivity."""
        resp = self._get("get-free-video")
        videos = self._as_list(resp.get("data", []))
        for v in videos:
            v["play_url"] = self._resolve_video_url(v)
        return videos

    def get_live_classes(self) -> list:
        """Get live classes. Mirrors LiveClassActivity."""
        resp = self._get("get-live-class")
        classes = self._as_list(resp.get("data", []))
        for c in classes:
            c["play_url"] = LIVE_PLAYER_URL + str(c.get("id") or c.get("stream_id") or "")
        return classes

    # ─────────────────────────────────────────────────────────────
    # STEP 7B — PDFs / NOTES
    # Mirrors: PDF_View → barteksc/AndroidPdfViewer
    # ─────────────────────────────────────────────────────────────
    def get_pdf_list(self, course_id, subject_id, chapter_id,
                     batch_id=None) -> list:
        """
        Get PDF list for a chapter.
        Mirrors get-pdf-list API call.
        """
        payload = {
            "course_id" : course_id,
            "subject_id": subject_id,
            "chapter_id": chapter_id,
        }
        if batch_id: payload["batch_id"] = batch_id
        resp = self._post("get-pdf-list", payload)
        pdfs = self._as_list(resp.get("data", []))

        for p in pdfs:
            p["pdf_full_url"] = self._resolve_pdf_url(p)

        return pdfs

    def _resolve_pdf_url(self, p: dict) -> str:
        """
        Resolve full PDF URL.
        Mirrors PDF_View / ViewPdfWebViewActivity URL building.
        """
        for k in ["pdf_url","url","file_url","file","link","pdf_file","pdf_link"]:
            val = p.get(k, "")
            if val and isinstance(val, str) and len(val) > 2:
                if val.startswith("http"):
                    return val
                return STORAGE["pdf"] + val
        return "URL_NOT_FOUND"

    def get_free_pdfs(self) -> list:
        """Get free PDFs (no auth). Mirrors FreePdfActivity."""
        resp = self._get("get-free-pdf")
        pdfs = self._as_list(resp.get("data", []))
        for p in pdfs:
            p["pdf_full_url"] = self._resolve_pdf_url(p)
        return pdfs

    def download_pdf(self, pdf_url_or_filename: str,
                     save_path: str = None,
                     show_progress: bool = True) -> str:
        """
        Download a PDF file.
        Mirrors how barteksc/AndroidPdfViewer downloads PDF before rendering.
        
        Args:
            pdf_url_or_filename: Full URL or just filename
            save_path: Where to save. Defaults to filename in current dir.
            show_progress: Print download progress.
        
        Returns:
            Path to downloaded file.
        """
        # Build full URL if only filename given
        if not pdf_url_or_filename.startswith("http"):
            url = STORAGE["pdf"] + pdf_url_or_filename
        else:
            url = pdf_url_or_filename

        filename = url.split("/")[-1].split("?")[0] or "download.pdf"
        save_path = save_path or filename

        self._log(f"Downloading PDF: {url}")
        try:
            r = self.session.get(url, stream=True, timeout=30)
            r.raise_for_status()

            total = int(r.headers.get("content-length", 0))
            downloaded = 0

            with open(save_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if show_progress and total:
                            pct = downloaded * 100 // total
                            print(f"\r  Downloading... {pct}% ({downloaded//1024}KB)", end="")

            if show_progress:
                print(f"\r✅ Downloaded: {save_path} ({downloaded//1024} KB)    ")

            return save_path
        except Exception as e:
            raise BTSError(f"PDF download failed: {e}")

    # ─────────────────────────────────────────────────────────────
    # STEP 8 — TESTS / QUIZZES
    # Mirrors: TestSeriesActivity → WebView
    # ─────────────────────────────────────────────────────────────
    def get_test_series(self) -> list:
        """Get test series list. Mirrors TestSeriesActivity."""
        resp = self._get("get-test-series")
        return self._as_list(resp.get("data", []))

    def get_test_list(self, series_id) -> list:
        """Get tests in a series. Mirrors TestSeriesListActivity."""
        resp = self._post("get-test-list", {"series_id": series_id})
        return self._as_list(resp.get("data", []))

    def get_test_detail(self, test_id) -> dict:
        """Get test detail. Mirrors TestDetailListActivity."""
        resp = self._post("get-test-detail", {"test_id": test_id})
        return resp.get("data", {})

    def get_test_url(self, test_id) -> str:
        """
        Get the WebView URL to attempt a test.
        Mirrors the URL opened in WebView by TestActivity.
        """
        return TEST_URL % (test_id, self.user_id)

    def get_test_answers_url(self, test_id) -> str:
        """
        Get the WebView URL to view test answers.
        Mirrors ViewTestAnswersActivity WebView URL.
        """
        return TEST_ANSWER_URL % (test_id, self.user_id)

    # ─────────────────────────────────────────────────────────────
    # EBOOKS
    # Mirrors: EBookListActivity / EbookActivity
    # ─────────────────────────────────────────────────────────────
    def get_ebook_list(self) -> list:
        """Get ebooks. Mirrors EBookListActivity."""
        resp = self._get("get-ebook-list")
        return self._as_list(resp.get("data", []))

    def get_ebook_series(self) -> list:
        """Get ebook series. Mirrors EBookSeriesActivity."""
        resp = self._get("get-ebook-series")
        return self._as_list(resp.get("data", []))

    # ─────────────────────────────────────────────────────────────
    # DOUBTS & TICKETS
    # Mirrors: CourseDoubtListActivity / TicketListActivity
    # ─────────────────────────────────────────────────────────────
    def get_doubts(self, course_id) -> list:
        """Get doubts for a course. Mirrors CourseDoubtListActivity."""
        resp = self._post("get-doubt-list", {"course_id": course_id})
        return self._as_list(resp.get("data", []))

    def add_doubt(self, course_id, question: str) -> dict:
        """Post a doubt. Mirrors CourseDoubtAddActivity."""
        resp = self._post("add-doubt", {
            "course_id": course_id,
            "question" : question,
        })
        return resp.get("data", {})

    def get_tickets(self) -> list:
        """Get support tickets. Mirrors TicketListActivity."""
        resp = self._get("get-ticket-list")
        return self._as_list(resp.get("data", []))

    # ─────────────────────────────────────────────────────────────
    # TIMETABLE / EVENTS
    # ─────────────────────────────────────────────────────────────
    def get_timetable(self, course_id) -> list:
        """Get timetable for a course."""
        resp = self._post("get-timetable", {"course_id": course_id})
        return self._as_list(resp.get("data", []))

    def get_events(self) -> list:
        """Get events. Mirrors PlayEventVideoActivity."""
        resp = self._get("get-events")
        return self._as_list(resp.get("data", []))

    # ─────────────────────────────────────────────────────────────
    # FULL COURSE SCRAPER
    # Walks the entire content tree of a course
    # ─────────────────────────────────────────────────────────────
    def scrape_course(self, course_id, course_name: str = "") -> dict:
        """
        Scrape ALL content from a course.
        Walks: subjects → chapters → videos + PDFs
        
        Returns dict with all videos and PDFs found.
        """
        self._log(f"Scraping course: {course_name or course_id}")
        results = {"videos": [], "pdfs": [], "course": course_name}

        subjects = self.get_subject_list(course_id)
        if not subjects:
            subjects = [{"id": None, "name": "General"}]

        for s in subjects:
            sid   = s.get("id") or s.get("subject_id")
            sname = s.get("name") or s.get("subject_name") or "General"
            self._log(f"  Subject: {sname}")

            chapters = self.get_chapter_list(course_id, sid)
            if not chapters:
                chapters = [{"id": None, "name": "General"}]

            for c in chapters:
                cid   = c.get("id") or c.get("chapter_id")
                cname = c.get("name") or c.get("chapter_name") or "General"

                # Videos
                try:
                    videos = self.get_video_list(course_id, sid, cid)
                    for v in videos:
                        results["videos"].append({
                            "title"      : v.get("title") or v.get("name") or "Untitled",
                            "play_url"   : v.get("play_url", ""),
                            "video_type" : v.get("video_type") or "lct",
                            "course"     : course_name,
                            "subject"    : sname,
                            "chapter"    : cname,
                            "duration"   : v.get("duration") or "",
                        })
                    self._log(f"    Chapter: {cname} → {len(videos)} videos")
                except:
                    pass

                # PDFs
                try:
                    pdfs = self.get_pdf_list(course_id, sid, cid)
                    for p in pdfs:
                        results["pdfs"].append({
                            "title"    : p.get("title") or p.get("name") or "Untitled",
                            "pdf_url"  : p.get("pdf_full_url", ""),
                            "course"   : course_name,
                            "subject"  : sname,
                            "chapter"  : cname,
                        })
                    self._log(f"    Chapter: {cname} → {len(pdfs)} PDFs")
                except:
                    pass

                time.sleep(0.2)   # be polite

        self._log(f"✅ Done: {len(results['videos'])} videos, {len(results['pdfs'])} PDFs")
        return results

    def scrape_all_courses(self) -> dict:
        """
        Scrape ALL enrolled courses.
        Full content dump — mirrors doing everything in the app manually.
        """
        courses = self.get_my_courses()
        if not courses:
            self._log("No enrolled courses. Trying all courses...")
            courses = self.get_all_courses()

        all_results = {"courses": [], "total_videos": 0, "total_pdfs": 0}

        for c in courses:
            cid   = c.get("id") or c.get("course_id")
            cname = c.get("name") or c.get("course_name") or f"Course-{cid}"
            self._log(f"\n📚 Course: {cname}")

            result = self.scrape_course(cid, cname)
            all_results["courses"].append(result)
            all_results["total_videos"] += len(result["videos"])
            all_results["total_pdfs"]   += len(result["pdfs"])

        self._log(f"\n📦 Total: {all_results['total_videos']} videos, {all_results['total_pdfs']} PDFs")
        return all_results

    def export_links(self, results: dict, output_file: str = "all_links.json"):
        """Export all scraped links to JSON file."""
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        self._log(f"✅ Exported to {output_file}")
        return output_file

    def download_all_pdfs(self, results: dict, folder: str = "pdfs"):
        """Download all PDFs from scrape results."""
        Path(folder).mkdir(exist_ok=True)
        pdfs = []
        for course in results.get("courses", []):
            pdfs.extend(course.get("pdfs", []))

        self._log(f"Downloading {len(pdfs)} PDFs to ./{folder}/")
        for i, p in enumerate(pdfs, 1):
            url  = p.get("pdf_url", "")
            name = url.split("/")[-1] or f"pdf_{i}.pdf"
            # Sanitize filename
            name = "".join(c for c in name if c.isalnum() or c in "._- ").strip()
            path = os.path.join(folder, name)
            try:
                self.download_pdf(url, path, show_progress=False)
                self._log(f"  [{i}/{len(pdfs)}] ✅ {name}")
            except Exception as e:
                self._log(f"  [{i}/{len(pdfs)}] ❌ {name}: {e}")


# ─────────────────────────────────────────────────────────────────
# CLI — Run directly as script
# ─────────────────────────────────────────────────────────────────

def main():
    import sys

    print("=" * 60)
    print("  Bridge to Success — Python SDK")
    print("=" * 60)

    app = BridgeToSuccess()

    # Check saved session
    if app.is_logged_in():
        print(f"\n✅ Session found: {app.name} ({app.mobile})")
        print("   (Delete bts_session.json to login again)\n")
    else:
        print("\n🔑 Login Required")
        mobile   = input("Mobile number : ").strip()
        password = input("Password      : ").strip()
        try:
            app.login(mobile, password)
        except BTSAuthError as e:
            print(f"❌ {e}")
            sys.exit(1)

    while True:
        print("\n" + "─" * 40)
        print("1. Home Data")
        print("2. My Courses")
        print("3. All Courses")
        print("4. Scrape ALL course content (videos + PDFs)")
        print("5. Export links to JSON")
        print("6. Download all PDFs")
        print("7. Free Videos (no login)")
        print("8. Free PDFs (no login)")
        print("9. Notifications")
        print("10. Test Series")
        print("0. Logout & Exit")
        print("─" * 40)

        choice = input("Choice: ").strip()

        if choice == "1":
            data = app.get_home_data()
            print(f"\n📢 Banners    : {len(data['banners'])}")
            print(f"🏆 Top Courses: {len(data['top_courses'])}")
            print(f"📌 Notices    : {len(data['notices'])}")

        elif choice == "2":
            courses = app.get_my_courses()
            print(f"\n📚 My Courses ({len(courses)}):")
            for i, c in enumerate(courses, 1):
                print(f"  {i}. [{c.get('id')}] {c.get('name') or c.get('course_name')}")

        elif choice == "3":
            courses = app.get_all_courses()
            print(f"\n📚 All Courses ({len(courses)}):")
            for i, c in enumerate(courses, 1):
                print(f"  {i}. [{c.get('id')}] {c.get('name') or c.get('course_name')}")

        elif choice == "4":
            print("\n⏳ Scraping all courses...")
            results = app.scrape_all_courses()
            print(f"\n✅ Found:")
            print(f"   🎬 Videos : {results['total_videos']}")
            print(f"   📄 PDFs   : {results['total_pdfs']}")
            # Store for export/download
            app._last_results = results

        elif choice == "5":
            if not hasattr(app, "_last_results"):
                print("❌ Run option 4 first.")
            else:
                f = app.export_links(app._last_results)
                print(f"✅ Saved to {f}")

        elif choice == "6":
            if not hasattr(app, "_last_results"):
                print("❌ Run option 4 first.")
            else:
                app.download_all_pdfs(app._last_results)

        elif choice == "7":
            videos = app.get_free_videos()
            print(f"\n🎬 Free Videos ({len(videos)}):")
            for v in videos:
                print(f"  • {v.get('title')} → {v.get('play_url')}")

        elif choice == "8":
            pdfs = app.get_free_pdfs()
            print(f"\n📄 Free PDFs ({len(pdfs)}):")
            for p in pdfs:
                print(f"  • {p.get('title')} → {p.get('pdf_full_url')}")

        elif choice == "9":
            notifs = app.get_notifications()
            print(f"\n🔔 Notifications ({len(notifs)}):")
            for n in notifs[:10]:
                print(f"  • {n.get('title') or n.get('message','')}")

        elif choice == "10":
            series = app.get_test_series()
            print(f"\n🧪 Test Series ({len(series)}):")
            for s in series:
                sid = s.get("id")
                print(f"  [{sid}] {s.get('title') or s.get('name')}")
                print(f"        URL: {app.get_test_url(sid)}")

        elif choice == "0":
            app.logout()
            print("👋 Goodbye!")
            break

        else:
            print("Invalid choice.")


if __name__ == "__main__":
    main()
