# bridge_to_success_sdk.py
# (Full content is same as original, with these changes)

class BridgeToSuccess:
    def __init__(self, session_file: str = SESSION_FILE, verbose: bool = True,
                 session_store: Optional[dict] = None):
        self.session_file = session_file
        self.verbose = verbose
        self.session_store = session_store  # <-- new
        self.token = None
        self.user_id = None
        self.name = None
        self.mobile = None
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "okhttp/4.9.3",
        })
        self._load_session()

    def _save_session(self):
        data = {
            "token": self.token,
            "user_id": self.user_id,
            "name": self.name,
            "mobile": self.mobile,
        }
        if self.session_store is not None:
            self.session_store.update(data)
        else:
            with open(self.session_file, "w") as f:
                json.dump(data, f, indent=2)
        self._log(f"Session saved")

    def _load_session(self):
        data = None
        if self.session_store is not None:
            data = self.session_store
        elif os.path.exists(self.session_file):
            try:
                with open(self.session_file) as f:
                    data = json.load(f)
            except:
                pass
        if data:
            self.token = data.get("token")
            self.user_id = data.get("user_id")
            self.name = data.get("name")
            self.mobile = data.get("mobile")
            if self.token:
                self._set_auth_header()
                self._log(f"Session restored for {self.name}")

    def _clear_session(self):
        self.token = self.user_id = self.name = self.mobile = None
        self.session.headers.pop("Authorization", None)
        self.session.headers.pop("authtoken", None)
        if self.session_store is not None:
            self.session_store.clear()
        elif os.path.exists(self.session_file):
            os.remove(self.session_file)
