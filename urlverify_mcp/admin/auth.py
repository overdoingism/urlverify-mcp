"""Password-only admin authentication. One JSON file (admin.auth_file) holds a random salt, a PBKDF2-HMAC-SHA256
hash (RFC 2898) and the session-signing key. Delete the file to reset the password to "admin"; the signing key is
regenerated with it, which invalidates every existing session."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

DEFAULT_PASSWORD = "admin"
ITERATIONS = 200_000
COOKIE = "urlverify_session"


class AdminAuth:
    def __init__(self, path: str | os.PathLike, session_days: int = 7):
        self.path = Path(os.path.expanduser(str(path)))
        self.session_seconds = max(1, session_days) * 86400
        self._data: dict | None = None

    # ---- file
    def _load(self) -> dict:
        if self._data is not None:
            return self._data
        if self.path.is_file():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
                if all(k in self._data for k in ("salt", "hash", "session_key", "iterations")):
                    return self._data
            except (OSError, json.JSONDecodeError):
                pass
        self._data = self._write(DEFAULT_PASSWORD, is_default=True)
        return self._data

    def _write(self, password: str, is_default: bool) -> dict:
        salt = secrets.token_bytes(16)
        data = {"salt": base64.b64encode(salt).decode(), "iterations": ITERATIONS,
                "hash": base64.b64encode(self._hash(password, salt, ITERATIONS)).decode(),
                "session_key": secrets.token_hex(32), "is_default": is_default, "updated": time.time()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=1), encoding="utf-8")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        self._data = data
        return data

    @staticmethod
    def _hash(password: str, salt: bytes, iterations: int) -> bytes:
        return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, dklen=32)

    # ---- password
    def verify_password(self, password: str) -> bool:
        d = self._load()
        h = self._hash(password, base64.b64decode(d["salt"]), int(d["iterations"]))
        return hmac.compare_digest(h, base64.b64decode(d["hash"]))

    def set_password(self, new_password: str) -> None:
        if len(new_password) < 4:
            raise ValueError("password must be at least 4 characters")
        self._write(new_password, is_default=(new_password == DEFAULT_PASSWORD))

    def is_default(self) -> bool:
        return bool(self._load().get("is_default"))

    # ---- sessions: "<expiry>.<hmac>" signed with the file's session key
    def issue_session(self) -> str:
        exp = int(time.time()) + self.session_seconds
        return f"{exp}.{self._sign(str(exp))}"

    def check_session(self, token: str | None) -> bool:
        if not token or "." not in token:
            return False
        exp_s, sig = token.split(".", 1)
        if not exp_s.isdigit() or int(exp_s) < time.time():
            return False
        return hmac.compare_digest(sig, self._sign(exp_s))

    def _sign(self, payload: str) -> str:
        key = bytes.fromhex(self._load()["session_key"])
        return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()
