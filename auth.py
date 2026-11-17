from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from storage import StateStorage

AUTH_FILE = Path("auth_users.json")


def _hash_password(password: str, salt: str) -> str:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return dk.hex()


class AuthManager:
    """Manages web/miniapp & messenger authentication, password changes, and admin user CRUD."""

    def __init__(self) -> None:
        self._storage = StateStorage()
        self._data: dict[str, Any] = self._load()
        self._failed_attempts: dict[str, list[float]] = {}

    def check_rate_limit(self, key: str, max_attempts: int = 5, window_sec: int = 60) -> None:
        """Raise ValueError if too many failed login attempts occurred recently."""
        now = time.time()
        recent = [t for t in self._failed_attempts.get(key, []) if now - t < window_sec]
        self._failed_attempts[key] = recent
        if len(recent) >= max_attempts:
            raise ValueError("تعداد تلاش‌های ناموفق زیاد است. لطفاً ۶۰ ثانیه دیگر امتحان کنید.")

    def record_failure(self, key: str) -> None:
        self._failed_attempts.setdefault(key, []).append(time.time())

    def clear_failures(self, key: str) -> None:
        self._failed_attempts.pop(key, None)

    def _load(self) -> dict[str, Any]:
        data = self._storage.get_json("auth_users", AUTH_FILE)
        if data and "users" in data and "secret" in data:
            data.setdefault("chat_sessions", {})
            return data

        default_user = os.getenv("ADMIN_USERNAME", "admin").strip() or "admin"
        default_pass = os.getenv("ADMIN_PASSWORD", "admin1234").strip() or "admin1234"
        salt = secrets.token_hex(16)
        data = {
            "secret": os.getenv("AUTH_SECRET", "").strip() or secrets.token_hex(32),
            "users": {
                default_user: {
                    "username": default_user,
                    "role": "admin",
                    "salt": salt,
                    "password_hash": _hash_password(default_pass, salt),
                    "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                }
            },
            "chat_sessions": {},
        }
        self._save(data)
        return data

    def _save(self, data: dict[str, Any] | None = None) -> None:
        target = data if data is not None else self._data
        self._storage.set_json("auth_users", target, AUTH_FILE)

    def authenticate(self, username: str, password: str) -> dict[str, Any] | None:
        user = self._data["users"].get(username.strip())
        if not user:
            return None
        expected = user["password_hash"]
        actual = _hash_password(password, user["salt"])
        if hmac.compare_digest(expected, actual):
            return {"username": user["username"], "role": user["role"]}
        return None

    def create_session_token(self, username: str, role: str, ttl_seconds: int = 86400 * 7) -> str:
        exp = int(time.time()) + ttl_seconds
        payload = f"{username}|{role}|{exp}"
        sig = hmac.new(self._data["secret"].encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"{payload}|{sig}"

    def verify_session_token(self, token: str | None) -> dict[str, str] | None:
        if not token or "|" not in token:
            return None
        parts = token.split("|")
        if len(parts) != 4:
            return None
        username, role, exp_s, sig = parts
        try:
            if int(exp_s) < int(time.time()):
                return None
        except ValueError:
            return None
        if username not in self._data["users"]:
            return None
        payload = f"{username}|{role}|{exp_s}"
        expected_sig = hmac.new(
            self._data["secret"].encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        if hmac.compare_digest(sig, expected_sig):
            return {"username": username, "role": self._data["users"][username]["role"]}
        return None

    def list_users(self) -> list[dict[str, str]]:
        return [
            {
                "username": u["username"],
                "role": u["role"],
                "created_at": u.get("created_at", "-"),
            }
            for u in self._data["users"].values()
        ]

    def create_user(self, username: str, password: str, role: str = "user") -> dict[str, str]:
        uname = username.strip()
        if not uname or len(password) < 4:
            raise ValueError("نام کاربری الزامی و رمز عبور حداقل ۴ کاراکتر باشد.")
        if uname in self._data["users"]:
            raise ValueError("این نام کاربری قبلاً ثبت شده است.")
        if role not in {"admin", "user"}:
            role = "user"
        salt = secrets.token_hex(16)
        self._data["users"][uname] = {
            "username": uname,
            "role": role,
            "salt": salt,
            "password_hash": _hash_password(password, salt),
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        self._save()
        return {"username": uname, "role": role}

    def change_password(self, username: str, new_password: str) -> None:
        uname = username.strip()
        if uname not in self._data["users"]:
            raise ValueError("کاربر یافت نشد.")
        if len(new_password) < 4:
            raise ValueError("رمز عبور جدید باید حداقل ۴ کاراکتر باشد.")
        salt = secrets.token_hex(16)
        self._data["users"][uname]["salt"] = salt
        self._data["users"][uname]["password_hash"] = _hash_password(new_password, salt)
        self._save()

    def delete_user(self, username: str) -> None:
        uname = username.strip()
        if uname not in self._data["users"]:
            raise ValueError("کاربر یافت نشد.")
        if self._data["users"][uname]["role"] == "admin":
            admins = [u for u in self._data["users"].values() if u["role"] == "admin"]
            if len(admins) <= 1:
                raise ValueError("حداقل یک کاربر ادمین باید باقی بماند.")
        del self._data["users"][uname]
        self._data["chat_sessions"] = {
            k: v for k, v in self._data["chat_sessions"].items() if v != uname
        }
        self._save()

    # Messenger chat session helpers
    def login_chat(self, chat_key: str, username: str, password: str) -> dict[str, Any] | None:
        auth = self.authenticate(username, password)
        if not auth:
            return None
        self._data["chat_sessions"][chat_key] = auth["username"]
        self._save()
        return auth

    def logout_chat(self, chat_key: str) -> None:
        if chat_key in self._data["chat_sessions"]:
            del self._data["chat_sessions"][chat_key]
            self._save()

    def get_chat_user(self, chat_key: str) -> dict[str, str] | None:
        uname = self._data["chat_sessions"].get(chat_key)
        if not uname or uname not in self._data["users"]:
            return None
        return {"username": uname, "role": self._data["users"][uname]["role"]}
