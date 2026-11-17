from __future__ import annotations

import json
import os
import random
import re
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from dotenv import load_dotenv
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

from storage import StateStorage

BASE_URL = "https://m7n.ir/fuel"
BOOK_URL = "https://m7n.ir/fuel/book"
CONFIG_FILE = Path("config.json")
SCREENSHOT_PATH = Path("latest_status.png")

DIGIT_MAP = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def normalize_text(value: str) -> str:
    """Convert Persian/Arabic digits to ASCII and collapse whitespace."""
    return " ".join(value.translate(DIGIT_MAP).split())


def parse_csv(value: str) -> list[str]:
    """Split comma-separated config values into a clean list."""
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_bool(value: Any, default: bool = False) -> bool:
    """Parse boolean values from strings or native bools."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def parse_hhmm(value: str) -> int | None:
    """Parse HH:MM (Persian or ASCII digits) into minutes since midnight."""
    norm = normalize_text(value)
    m = re.search(r"(\d{1,2})\s*:\s*(\d{2})", norm)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour * 60 + minute
    return None


def jalali_to_gregorian(jy: int, jm: int, jd: int) -> date:
    """Convert Jalali (Solar Hijri) date to Gregorian date."""
    jy += 1595
    days = -355668 + (365 * jy) + ((jy // 33) * 8) + (((jy % 33) + 3) // 4) + jd
    days += ((jm - 1) * 31) if jm < 7 else (((jm - 7) * 30) + 186)
    gy = 400 * (days // 146097)
    days %= 146097
    if days > 36524:
        days -= 1
        gy += 100 * (days // 36524)
        days %= 36524
        if days >= 365:
            days += 1
    gy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        gy += (days - 1) // 365
        days = (days - 1) % 365
    gd = days + 1
    sal_a = [0, 31, 29 if (gy % 4 == 0 and gy % 100 != 0) or (gy % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    gm = 0
    while gm < 13 and gd > sal_a[gm]:
        gd -= sal_a[gm]
        gm += 1
    return date(gy, gm, gd)


def extract_next_eligible_date(note_text: str) -> tuple[str, str] | None:
    """Extract Jalali date from 'نوبت بعدی را از ... ۱۴۰۵/۰۹/۰۲ می‌توانید بگیرید' -> (jalali_str, iso_str)."""
    norm = normalize_text(note_text)
    m = re.search(r"(\d{4})/(\d{1,2})/(\d{1,2})", norm)
    if not m:
        return None
    jy, jm, jd = int(m.group(1)), int(m.group(2)), int(m.group(3))
    g_date = jalali_to_gregorian(jy, jm, jd)
    return f"{jy:04d}/{jm:02d}/{jd:02d}", g_date.isoformat()


def validate_station_url(url: str) -> str:
    """Strictly allow only https://m7n.ir/fuel... URLs to prevent SSRF."""
    parsed = urlparse(url.strip())
    if parsed.scheme != "https" or parsed.netloc.lower() != "m7n.ir" or not parsed.path.startswith("/fuel"):
        raise ValueError("آدرس جایگاه نامعتبر است.")
    return url.strip()


def mask_secret(value: str) -> str:
    """Mask bot tokens for safe display in API responses."""
    v = value.strip()
    if not v:
        return ""
    if len(v) <= 8:
        return "****"
    return f"****{v[-4:]}"


@dataclass
class BotSettings:
    """Persistent settings editable from .env, Web/PWA Dashboard, or Messengers."""

    personnel_code: str = ""
    national_code: str = ""
    recipient_first_name: str = ""
    recipient_last_name: str = ""
    recipient_national_code: str = ""
    fuel_amount: str = "40"
    min_time: str = "14:00"
    max_time: str = "16:30"
    fallback_max_time: str = "17:00"
    preferred_days: str = ""
    preferred_stations: str = ""
    poll_interval: float = 5.0
    headless: bool = True
    dry_run: bool = True
    auto_next_booking: bool = True
    next_eligible_jalali: str = ""
    next_eligible_iso: str = ""
    mode: str = "notify_only"  # "notify_only" or "auto_book"
    active_profile_id: str = ""
    scanner_profile_id: str = ""
    group_profile_ids: list[str] = field(default_factory=list)
    profiles: list[dict[str, str]] = field(default_factory=list)
    mini_app_url: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    bale_bot_token: str = ""
    bale_chat_id: str = ""
    rubika_bot_token: str = ""
    rubika_chat_id: str = ""
    soroush_bot_token: str = ""
    soroush_chat_id: str = ""

    @classmethod
    def load(cls) -> "BotSettings":
        load_dotenv()
        p_code = os.getenv("PERSONNEL_CODE", "").strip()
        n_code = os.getenv("NATIONAL_CODE", "").strip()
        f_name = os.getenv("RECIPIENT_FIRST_NAME", "").strip()
        l_name = os.getenv("RECIPIENT_LAST_NAME", "").strip()

        initial_profiles: list[dict[str, str]] = []
        active_id = ""
        if p_code and n_code:
            active_id = "default"
            initial_profiles.append(
                {
                    "id": active_id,
                    "label": f"{f_name} {l_name}".strip() or p_code,
                    "personnel_code": p_code,
                    "national_code": n_code,
                    "first_name": f_name,
                    "last_name": l_name,
                }
            )

        defaults = cls(
            personnel_code=p_code,
            national_code=n_code,
            recipient_first_name=f_name,
            recipient_last_name=l_name,
            recipient_national_code=os.getenv("RECIPIENT_NATIONAL_CODE", "").strip() or n_code,
            fuel_amount=normalize_text(os.getenv("FUEL_AMOUNT", "40")) or "40",
            min_time=normalize_text(os.getenv("MIN_TIME", "14:00")) or "14:00",
            max_time=normalize_text(os.getenv("MAX_TIME", "16:30")) or "16:30",
            fallback_max_time=normalize_text(os.getenv("FALLBACK_MAX_TIME", "17:00")) or "17:00",
            preferred_days=os.getenv("PREFERRED_DAYS", "").strip(),
            preferred_stations=os.getenv("PREFERRED_STATIONS", "").strip(),
            poll_interval=max(2.0, float(os.getenv("POLL_INTERVAL_SECONDS", "5"))),
            headless=parse_bool(os.getenv("HEADLESS", "true"), default=True),
            dry_run=parse_bool(os.getenv("DRY_RUN", "true"), default=True),
            mode=os.getenv("BOT_MODE", "notify_only").strip() or "notify_only",
            active_profile_id=active_id,
            group_profile_ids=[active_id] if active_id else [],
            profiles=initial_profiles,
            mini_app_url=os.getenv("MINI_APP_URL", "").strip(),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
            bale_bot_token=os.getenv("BALE_BOT_TOKEN", "").strip(),
            bale_chat_id=os.getenv("BALE_CHAT_ID", "").strip(),
            rubika_bot_token=os.getenv("RUBIKA_BOT_TOKEN", "").strip(),
            rubika_chat_id=os.getenv("RUBIKA_CHAT_ID", "").strip(),
            soroush_bot_token=os.getenv("SOROUSH_BOT_TOKEN", "").strip(),
            soroush_chat_id=os.getenv("SOROUSH_CHAT_ID", "").strip(),
        )
        raw = StateStorage().get_json("bot_config", CONFIG_FILE)
        if raw:
            for key, val in raw.items():
                if hasattr(defaults, key):
                    setattr(defaults, key, val)
        defaults.poll_interval = max(2.0, float(defaults.poll_interval))
        if defaults.mode not in {"notify_only", "auto_book"}:
            defaults.mode = "notify_only"
        if not defaults.recipient_national_code and defaults.national_code:
            defaults.recipient_national_code = defaults.national_code
        return defaults

    def save(self) -> None:
        StateStorage().set_json("bot_config", asdict(self), CONFIG_FILE)


def play_alert() -> None:
    """Play an audible alert on Windows (or terminal bell fallback)."""
    try:
        import winsound

        for freq, dur in [(880, 180), (1175, 180), (1568, 350)]:
            winsound.Beep(freq, dur)
    except Exception:
        print("\a", end="", flush=True)


def launch_browser(p, headless: bool):
    """Launch Chromium, falling back to installed Chrome or Edge if needed."""
    for channel in (None, "chrome", "msedge"):
        try:
            kwargs: dict[str, Any] = {"headless": headless}
            if channel:
                kwargs["channel"] = channel
            return p.chromium.launch(**kwargs)
        except Exception:
            continue
    raise RuntimeError(
        "مرورگر یافت نشد. لطفاً Chrome/Edge نصب داشته باشید یا `playwright install chromium` را اجرا کنید."
    )


def enable_fast_mode(page: Page) -> None:
    """Abort heavy static assets (fonts, images, stylesheets, media) to speed up page loads."""
    def _route_handler(route):
        if route.request.resource_type in {"image", "font", "media", "stylesheet"}:
            route.abort()
        else:
            route.continue_()

    page.route("**/*", _route_handler)


class FuelBotService:
    """Thread-safe controller for multi-user monitoring, time-gated auto-booking, and confirmations."""

    def __init__(self, on_notify: Callable[[str, Path | None], None] | None = None) -> None:
        self._lock = threading.RLock()
        self.settings = BotSettings.load()
        self.settings.save()
        self._on_notify = on_notify

        self._running = False
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._worker_thread: threading.Thread | None = None

        self._logs: deque[str] = deque(maxlen=150)
        self._attempt_count = 0
        self._last_check_at: str | None = None
        self._last_status = "آماده به کار"
        self._available_slots: list[dict[str, Any]] = []
        self._active_ticket: dict[str, str] | None = None
        self._pending_confirmation: dict[str, Any] | None = None
        self._approved_late_slot: dict[str, str] | None = None
        self._booked_info: str | None = None
        self._booking_history: deque[dict[str, str]] = deque(maxlen=50)
        self._last_notified_sig: str | None = None
        self._logged_in_credentials: tuple[str, str] | None = None
        self._warm_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pw-warm")
        self._warm_pw = None
        self._warm_browser = None
        self._warm_page: Page | None = None
        self._warm_headless: bool | None = None
        self._scheduler_thread = threading.Thread(target=self._next_booking_scheduler, daemon=True)
        self._scheduler_thread.start()

    def _run_on_warm_page(self, headless: bool, fn: Callable[[Page], Any]) -> Any:
        """Execute a Playwright task on a persistent warm browser page (1-2s instead of 10s)."""
        def _task():
            if self._warm_page is None or self._warm_headless != headless:
                try:
                    if self._warm_browser:
                        self._warm_browser.close()
                    if self._warm_pw:
                        self._warm_pw.stop()
                except Exception:
                    pass
                self._warm_pw = sync_playwright().start()
                self._warm_browser = launch_browser(self._warm_pw, headless)
                ctx = self._warm_browser.new_context(viewport={"width": 1280, "height": 900})
                self._warm_page = ctx.new_page()
                enable_fast_mode(self._warm_page)
                self._warm_headless = headless
                self._logged_in_credentials = None
            try:
                return fn(self._warm_page)
            except Exception:
                # Reset warm page if browser crashed or disconnected
                if self._warm_page and self._warm_page.is_closed():
                    self._warm_page = None
                    self._logged_in_credentials = None
                raise

        return self._warm_executor.submit(_task).result(timeout=60)

    def set_notifier(self, fn: Callable[[str, Path | None], None]) -> None:
        self._on_notify = fn

    def log(self, message: str) -> None:
        now = datetime.now().strftime("%H:%M:%S")
        entry = f"[{now}] {message}"
        with self._lock:
            self._logs.append(entry)
        print(entry, flush=True)

    def _notify(self, message: str, image_path: Path | None = None) -> None:
        play_alert()
        if self._on_notify:
            try:
                self._on_notify(message, image_path)
            except Exception as exc:
                self.log(f"خطا در ارسال اعلان: {exc}")

    def upsert_profile(
        self,
        personnel_code: str,
        national_code: str,
        first_name: str = "",
        last_name: str = "",
        label: str = "",
        activate: bool = True,
    ) -> dict[str, str]:
        """Save or update a user profile and optionally make it active."""
        p_code = normalize_text(personnel_code)
        n_code = normalize_text(national_code)
        if not p_code or not n_code:
            raise ValueError("کد پرسنلی و کد ملی الزامی است.")

        with self._lock:
            existing = next(
                (p for p in self.settings.profiles if p.get("personnel_code") == p_code),
                None,
            )
            if existing:
                existing["national_code"] = n_code
                if first_name:
                    existing["first_name"] = first_name.strip()
                if last_name:
                    existing["last_name"] = last_name.strip()
                if label:
                    existing["label"] = label.strip()
                elif first_name or last_name:
                    existing["label"] = f"{existing.get('first_name', '')} {existing.get('last_name', '')}".strip()
                profile = existing
            else:
                profile = {
                    "id": uuid.uuid4().hex[:8],
                    "label": label.strip() or f"{first_name} {last_name}".strip() or p_code,
                    "personnel_code": p_code,
                    "national_code": n_code,
                    "first_name": first_name.strip(),
                    "last_name": last_name.strip(),
                }
                self.settings.profiles.append(profile)

            if activate:
                self.select_profile(profile["id"])
            else:
                self.settings.save()
            return profile

    def select_profile(self, profile_id: str) -> dict[str, str]:
        """Switch active login & recipient credentials to a saved profile."""
        with self._lock:
            profile = next((p for p in self.settings.profiles if p.get("id") == profile_id), None)
            if not profile:
                raise ValueError("پروفایل مورد نظر پیدا نشد.")
            self.settings.active_profile_id = profile["id"]
            self.settings.personnel_code = profile["personnel_code"]
            self.settings.national_code = profile["national_code"]
            self.settings.recipient_first_name = profile.get("first_name", "")
            self.settings.recipient_last_name = profile.get("last_name", "")
            self.settings.recipient_national_code = profile["national_code"]
            self._logged_in_credentials = None
            self._active_ticket = None
            self._last_notified_sig = None
            self.settings.save()
            self._wake_event.set()
            self.log(f"کاربر فعال تغییر کرد به: {profile.get('label', profile['personnel_code'])}")
            return profile

    def delete_profile(self, profile_id: str) -> None:
        with self._lock:
            self.settings.profiles = [p for p in self.settings.profiles if p.get("id") != profile_id]
            if self.settings.active_profile_id == profile_id:
                self.settings.active_profile_id = ""
            if self.settings.scanner_profile_id == profile_id:
                self.settings.scanner_profile_id = ""
            self.settings.save()

    def toggle_group_profile(self, profile_id: str) -> list[str]:
        with self._lock:
            if profile_id in self.settings.group_profile_ids:
                self.settings.group_profile_ids = [x for x in self.settings.group_profile_ids if x != profile_id]
            else:
                self.settings.group_profile_ids.append(profile_id)
            self.settings.save()
            return list(self.settings.group_profile_ids)

    def update_settings(self, updates: dict[str, Any]) -> BotSettings:
        with self._lock:
            for key, val in updates.items():
                if not hasattr(self.settings, key) or key == "profiles":
                    continue
                if key in {"headless", "dry_run", "auto_next_booking"}:
                    val = parse_bool(val)
                elif key == "poll_interval":
                    val = max(2.0, float(val))
                elif key == "mode" and val not in {"notify_only", "auto_book"}:
                    continue
                elif key in {"min_time", "max_time", "fallback_max_time"}:
                    val = normalize_text(str(val))
                elif key in {"telegram_bot_token", "bale_bot_token", "rubika_bot_token", "soroush_bot_token"}:
                    s_val = str(val).strip()
                    if s_val.startswith("****"):
                        continue
                    val = s_val
                elif key == "group_profile_ids" and isinstance(val, list):
                    val = [str(x) for x in val]
                elif isinstance(val, str):
                    val = val.strip()
                setattr(self.settings, key, val)

            # Sync recipient national code to login national code if recipient national code left blank
            if not self.settings.recipient_national_code and self.settings.national_code:
                self.settings.recipient_national_code = self.settings.national_code

            self.settings.save()
            self._wake_event.set()
            return self.settings

    def confirm_late_booking(self, chosen_time: str | None = None, station_url: str | None = None) -> str:
        """Approve booking a slot later than max_time (from Web/PWA or Messengers)."""
        with self._lock:
            pending = self._pending_confirmation
            if not pending and not (chosen_time and station_url):
                return "درخواست منتظر تأییدی وجود ندارد."
            target_url = station_url or pending["url"]
            target_time = normalize_text(chosen_time) if chosen_time else pending["times"][0]
            self._approved_late_slot = {"url": target_url, "time": target_time}
            self._pending_confirmation = None

        if self._running:
            self._wake_event.set()
            return f"تأیید ثبت ساعت {target_time} دریافت شد؛ ربات الان ثبت را انجام می‌دهد."
        threading.Thread(target=self._book_approved_once, daemon=True).start()
        return f"در حال ثبت ساعت {target_time}..."

    def decline_late_booking(self) -> str:
        with self._lock:
            self._pending_confirmation = None
            self._approved_late_slot = None
        return "تأیید لغو شد؛ ربات همچنان منتظر ساعت‌های زودتر می‌ماند."

    def start(self) -> bool:
        with self._lock:
            if self._running:
                return False



            if not self.settings.personnel_code or not self.settings.national_code:
                raise ValueError("کد پرسنلی و کد ملی نباید خالی باشد.")
            self._stop_event.clear()
            self._wake_event.clear()
            self._running = True
            self._booked_info = None
            self._last_notified_sig = None
            self._pending_confirmation = None
            self._last_status = "در حال شروع پایش..."
            self._worker_thread = threading.Thread(target=self._run_loop, daemon=True)
            self._worker_thread.start()
            return True

    def stop(self) -> bool:
        with self._lock:
            if not self._running:
                return False



            self._stop_event.set()
            self._wake_event.set()
            self._running = False
            self._last_status = "متوقف شده توسط کاربر"
            self.log("درخواست توقف ربات ثبت شد.")
            return True

    def trigger_check_now(self) -> str:
        with self._lock:
            if self._running:
                self._wake_event.set()
                return "بررسی فوری در صف قرار گرفت."
        threading.Thread(target=self._single_check_task, daemon=True).start()
        return "بررسی تکی فوری شروع شد."

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            safe_settings = asdict(self.settings)
            for secret_key in ("telegram_bot_token", "bale_bot_token", "rubika_bot_token", "soroush_bot_token"):
                safe_settings[secret_key] = mask_secret(safe_settings.get(secret_key, ""))
            return {
                "running": self._running,
                "mode": self.settings.mode,
                "dry_run": self.settings.dry_run,
                "attempt_count": self._attempt_count,
                "last_check_at": self._last_check_at,
                "last_status": self._last_status,
                "available_slots": list(self._available_slots),
                "active_ticket": dict(self._active_ticket) if self._active_ticket else None,
                "pending_confirmation": dict(self._pending_confirmation) if self._pending_confirmation else None,
                "booked_info": self._booked_info,
                "booking_history": list(self._booking_history),
                "has_screenshot": SCREENSHOT_PATH.exists(),
                "logs": list(self._logs),
                "settings": safe_settings,
            }

    def _login_with_credentials(self, page: Page, personnel_code: str, national_code: str) -> None:
        if self._logged_in_credentials == (personnel_code, national_code) and "/fuel/book" in page.url:
            return
        self.log(f"در حال ورود با کد پرسنلی {personnel_code}...")
        # Logout first if logged in as a different user
        exit_btn = page.locator('form[action*="/fuel/exit"] button[type="submit"]')
        if "/fuel/book" in page.url and exit_btn.count() > 0:
            exit_btn.first.click()
            page.wait_for_load_state("domcontentloaded")

        page.goto(BASE_URL, wait_until="domcontentloaded")
        if "/fuel/book" in page.url and self._logged_in_credentials == (personnel_code, national_code):
            return

        page.locator("#personnel_code").fill(personnel_code)
        page.locator("#national_code").fill(national_code)
        page.locator('button[type="submit"]').click()
        page.wait_for_url("**/fuel/book**", timeout=15000)
        self._logged_in_credentials = (personnel_code, national_code)
        self.log("ورود با موفقیت انجام شد.")

    def _sync_name_from_page(self, page: Page, cfg: BotSettings) -> None:
        """Auto-detect user's full name from <h1> on /fuel/book if recipient name is empty."""
        if cfg.recipient_first_name and cfg.recipient_last_name:
            return
        h1 = page.locator("header.ui-page-head h1")
        if h1.count() == 0:
            return
        full_name = normalize_text(h1.first.inner_text())
        parts = full_name.split(" ", 1)
        if not parts:
            return
        first = parts[0]
        last = parts[1] if len(parts) > 1 else parts[0]
        with self._lock:
            if not self.settings.recipient_first_name:
                self.settings.recipient_first_name = first
            if not self.settings.recipient_last_name:
                self.settings.recipient_last_name = last
            for p in self.settings.profiles:
                if p.get("personnel_code") == cfg.personnel_code:
                    p.setdefault("first_name", first)
                    p.setdefault("last_name", last)
                    if not p.get("first_name"):
                        p["first_name"] = first
                    if not p.get("last_name"):
                        p["last_name"] = last
                    p["label"] = full_name
            self.settings.save()

    def _extract_active_ticket(self, page: Page) -> dict[str, str] | None:
        ticket = page.locator("section.ui-ticket")
        if ticket.count() == 0:
            return None
        info: dict[str, str] = {}
        dts = ticket.locator("dl.ui-kv dt")
        dds = ticket.locator("dl.ui-kv dd")
        for i in range(min(dts.count(), dds.count())):
            key = normalize_text(dts.nth(i).inner_text())
            val = normalize_text(dds.nth(i).inner_text())
            if key:
                info[key] = val
        note = ticket.locator("p.ui-mut")
        if note.count() > 0:
            note_txt = normalize_text(note.first.inner_text())
            info["یادداشت"] = note_txt
            parsed = extract_next_eligible_date(note_txt)
            if parsed:
                j_str, iso_str = parsed
                with self._lock:
                    if self.settings.next_eligible_iso != iso_str:
                        self.settings.next_eligible_jalali = j_str
                        self.settings.next_eligible_iso = iso_str
                        self.settings.save()
        return info or None

    def _find_days(self, page: Page, cfg: BotSettings) -> list[tuple[str, str]]:
        pref_days = parse_csv(cfg.preferred_days)
        links = page.locator('a[href*="day="]')
        results: list[tuple[str, str]] = []
        for i in range(links.count()):
            link = links.nth(i)
            href = link.get_attribute("href") or ""
            label = normalize_text(link.inner_text())
            if pref_days and not any(d in href for d in pref_days):
                continue
            results.append((href, label))
        return results

    def _find_stations(self, page: Page, cfg: BotSettings) -> list[tuple[str, str]]:
        pref_stations = [normalize_text(s) for s in parse_csv(cfg.preferred_stations)]
        links = page.locator('a[href*="station="]')
        results: list[tuple[str, str]] = []
        for i in range(links.count()):
            link = links.nth(i)
            href = link.get_attribute("href") or ""
            label = normalize_text(link.inner_text())
            if pref_stations and not any(s in label for s in pref_stations):
                continue
            results.append((href, label))
        return results

    def _radio_label_text(self, page: Page, radio) -> str:
        """Extract visible label text for an <input type='radio'>."""
        aria = radio.get_attribute("aria-label")
        if aria:
            return normalize_text(aria)
        anc = radio.locator("xpath=ancestor::label")
        if anc.count() > 0:
            txt = normalize_text(anc.first.inner_text())
            if txt:
                return txt
        rid = radio.get_attribute("id")
        if rid:
            lbl = page.locator(f'label[for="{rid}"]')
            if lbl.count() > 0:
                txt = normalize_text(lbl.first.inner_text())
                if txt:
                    return txt
        val = radio.get_attribute("value") or ""
        return normalize_text(val)

    def _list_enabled_times(self, page: Page) -> list[str]:
        time_group = page.get_by_role("radiogroup", name="ساعت نوبت")
        radios = time_group.get_by_role("radio")
        times: list[str] = []
        for i in range(radios.count()):
            r = radios.nth(i)
            if not r.is_disabled():
                lbl = self._radio_label_text(page, r) or f"#{i + 1}"
                times.append(lbl)
        return times

    def _check_radio_safely(self, page: Page, radio) -> None:
        """Click the visible label for a styled/hidden radio input, with JS fallback."""
        rid = radio.get_attribute("id")
        if rid:
            lbl = page.locator(f'label[for="{rid}"]')
            if lbl.count() > 0:
                lbl.first.click()
                if radio.is_checked():
                    return
        anc = radio.locator("xpath=ancestor::label")
        if anc.count() > 0:
            anc.first.click()
            if radio.is_checked():
                return
        radio.evaluate("el => { el.checked = true; el.dispatchEvent(new Event('change', {bubbles: true})); }")

    def _select_specific_time(self, page: Page, target_time: str) -> str | None:
        time_group = page.get_by_role("radiogroup", name="ساعت نوبت")
        radios = time_group.get_by_role("radio")
        target_norm = normalize_text(target_time)
        for i in range(radios.count()):
            r = radios.nth(i)
            if r.is_disabled():
                continue
            lbl = self._radio_label_text(page, r)
            if lbl == target_norm or target_norm in lbl:
                self._check_radio_safely(page, r)
                return lbl
        return None

    def _pick_fuel_amount(self, page: Page, cfg: BotSettings) -> None:
        fuel_group = page.get_by_role("radiogroup", name="میزان سوخت درخواستی")
        target = "".join(ch for ch in normalize_text(cfg.fuel_amount) if ch.isdigit()) or "40"
        radios = fuel_group.get_by_role("radio")
        for i in range(radios.count()):
            r = radios.nth(i)
            txt = self._radio_label_text(page, r)
            if target in txt:
                self._check_radio_safely(page, r)
                return
        labels = fuel_group.locator("label")
        for i in range(labels.count()):
            lbl = labels.nth(i)
            if target in normalize_text(lbl.inner_text()):
                lbl.click()
                return
        raise RuntimeError(f"گزینه میزان سوخت «{cfg.fuel_amount}» پیدا نشد.")

    def _submit_slot(self, page: Page, cfg: BotSettings, day_label: str, station_label: str, chosen_time: str) -> bool:
        self._pick_fuel_amount(page, cfg)
        first_name = cfg.recipient_first_name.strip()
        last_name = cfg.recipient_last_name.strip()
        rec_national = cfg.recipient_national_code.strip() or cfg.national_code.strip()
        if not (first_name and last_name and rec_national):
            raise ValueError("نام و نام خانوادگی تحویل‌گیرنده کامل نیست.")

        page.get_by_role("textbox", name="نام", exact=True).fill(first_name)
        page.get_by_role("textbox", name="نام خانوادگی", exact=True).fill(last_name)
        page.get_by_role("textbox", name="کد ملی", exact=True).fill(rec_national)

        if cfg.dry_run:
            page.screenshot(path=str(SCREENSHOT_PATH), full_page=True)
            msg = (
                f"🧪 [حالت تست] نوبت پیدا شد و فرم پر شد (ثبت نهایی انجام نشد):\n"
                f"👤 {first_name} {last_name}\n"
                f"📅 {day_label}\n⛽ {station_label}\n⏰ ساعت: {chosen_time}\n🛢 {cfg.fuel_amount} لیتر"
            )
            with self._lock:
                self._booked_info = msg
                self._last_status = f"تست موفق برای ساعت {chosen_time}"
            self.log(msg.replace("\n", " | "))
            self._notify(msg, SCREENSHOT_PATH)
            return True

        try:
            with page.expect_navigation(timeout=15000):
                page.get_by_role("button", name="ثبت نوبت").click()
        except PlaywrightTimeoutError:
            page.wait_for_load_state("networkidle", timeout=10000)

        page.screenshot(path=str(SCREENSHOT_PATH), full_page=True)
        ticket_after = self._extract_active_ticket(page)
        if not ticket_after:
            alerts = [
                normalize_text(a.inner_text())
                for a in page.locator(".ui-alert:not(.ui-tone-warn), [role='alert']").all()
            ]
            err_msg = " | ".join(a for a in alerts if a and "تعهدی" not in a) or "کد پیگیری دریافت نشد"
            raise RuntimeError(f"ثبت نوبت انجام نشد: {err_msg}")

        tracking = ticket_after.get("کد پیگیری", "-")
        msg = (
            f"✅ نوبت سوخت با موفقیت ثبت شد!\n"
            f"🎫 کد پیگیری: {tracking}\n"
            f"👤 {first_name} {last_name}\n"
            f"📅 {day_label}\n⛽ {station_label}\n⏰ ساعت: {chosen_time}\n🛢 {cfg.fuel_amount} لیتر\n🔗 {page.url}"
        )
        with self._lock:
            self._active_ticket = ticket_after
            self._booked_info = msg
            self._last_status = f"نوبت ثبت شد! (کد پیگیری: {tracking})"
            hist_entry = {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "user": f"{first_name} {last_name}",
                "station": station_label,
                "slot": chosen_time,
                "fuel": f"{cfg.fuel_amount} لیتر",
                "tracking": tracking,
            }
            self._booking_history.appendleft(hist_entry)
            StateStorage().add_booking_history(hist_entry)
        self.log(msg.replace("\n", " | "))
        self._notify(msg, SCREENSHOT_PATH)
        return True

    def _extract_clean_day_label(self, page: Page, fallback: str) -> str:
        """Read clean Jalali day title from section 2 heading (e.g. 'شنبه ۱۴۰۵/۰۸/۳۰')."""
        h2 = page.locator("h2", has_text="۲. جایگاه")
        if h2.count() > 0:
            txt = normalize_text(h2.first.inner_text())
            if "—" in txt:
                return txt.split("—", 1)[1].strip()
        return fallback

    def _pick_tiered_time(self, enabled_times: list[str], cfg: BotSettings) -> str | None:
        """Pick earliest time in [min_time, max_time], else fallback up to fallback_max_time."""
        min_m = parse_hhmm(cfg.min_time)
        max_m = parse_hhmm(cfg.max_time)
        fb_m = parse_hhmm(cfg.fallback_max_time)

        # Tier 1: [min_time, max_time]
        for t in enabled_times:
            m = parse_hhmm(t)
            if m is None:
                continue
            if (min_m is None or m >= min_m) and (max_m is None or m <= max_m):
                return t

        # Tier 2: (max_time, fallback_max_time]
        if fb_m is not None:
            for t in enabled_times:
                m = parse_hhmm(t)
                if m is None:
                    continue
                if (min_m is None or m >= min_m) and m <= fb_m:
                    return t
        return None

    def _scan_and_act(self, page: Page, cfg: BotSettings) -> bool:
        """Scan /fuel/book. Returns True if booking completed and loop should stop."""
        # Handle user-confirmed late booking first
        with self._lock:
            approved = self._approved_late_slot
            self._approved_late_slot = None
        if approved:
            self._login_with_credentials(page, cfg.personnel_code, cfg.national_code)
            self._sync_name_from_page(page, cfg)
            page.goto(approved["url"], wait_until="domcontentloaded")
            selected = self._select_specific_time(page, approved["time"])
            if selected:
                return self._submit_slot(page, cfg, "روز انتخاب‌شده", "جایگاه انتخاب‌شده", selected)
            self.log(f"متأسفانه ساعت {approved['time']} پر شده بود.")

        self._login_with_credentials(page, cfg.personnel_code, cfg.national_code)
        page.goto(BOOK_URL, wait_until="domcontentloaded")
        self._sync_name_from_page(page, cfg)

        active_ticket = self._extract_active_ticket(page)
        with self._lock:
            self._active_ticket = active_ticket

        scan_only_from_scanner = False
        if active_ticket:
            page.screenshot(path=str(SCREENSHOT_PATH), full_page=True)
            tracking = active_ticket.get("کد پیگیری", "-")
            ticket_msg = (
                f"⚠️ کاربر فعلی ({cfg.personnel_code}) هم‌اکنون نوبت فعال دارد "
                f"(کد پیگیری: {tracking}) و سایت ساعت‌های خالی را به این کاربر نشان نمی‌دهد.\n"
                f"برای دیدن ساعت‌های خالی، یک کد ملی و کد پرسنلی دیگر انتخاب یا اضافه کنید."
            )
            with self._lock:
                self._last_status = f"دارای نوبت فعال (کد پیگیری: {tracking})"

            # If a scanner profile is configured, switch to it just to inspect free hours
            scanner = next(
                (p for p in cfg.profiles if p.get("id") == cfg.scanner_profile_id and p.get("personnel_code") != cfg.personnel_code),
                None,
            )
            if scanner:
                self.log("کاربر اصلی نوبت فعال دارد؛ بررسی ساعت‌های خالی با حساب اسکنر جایگزین...")
                self._login_with_credentials(page, scanner["personnel_code"], scanner["national_code"])
                page.goto(BOOK_URL, wait_until="domcontentloaded")
                if self._extract_active_ticket(page):
                    self._notify(ticket_msg, SCREENSHOT_PATH)
                    return cfg.mode == "auto_book"
                scan_only_from_scanner = True
            else:
                if self._last_notified_sig != f"ticket:{tracking}":
                    self._last_notified_sig = f"ticket:{tracking}"
                    self._notify(ticket_msg, SCREENSHOT_PATH)
                return cfg.mode == "auto_book"

        days = self._find_days(page, cfg)
        found_summary: list[dict[str, Any]] = []
        max_minutes = parse_hhmm(cfg.max_time)

        if not days:
            with self._lock:
                self._available_slots = []
                self._last_status = "نوبت خالی یافت نشد (در حال پایش)"
            self._last_notified_sig = None
            return False

        late_candidate: dict[str, Any] | None = None

        for day_href, day_label in days:
            full_day_url = day_href if day_href.startswith("http") else f"https://m7n.ir{day_href}"
            page.goto(full_day_url, wait_until="domcontentloaded")
            clean_day = self._extract_clean_day_label(page, day_label)
            stations = self._find_stations(page, cfg)
            for station_href, station_label in stations:
                full_st_url = station_href if station_href.startswith("http") else f"https://m7n.ir{station_href}"
                page.goto(full_st_url, wait_until="domcontentloaded")
                enabled_times = self._list_enabled_times(page)
                if not enabled_times:
                    continue

                slot_info = {
                    "day": clean_day,
                    "station": station_label,
                    "times": enabled_times,
                    "url": full_st_url,
                }
                found_summary.append(slot_info)

                if cfg.mode == "auto_book" and not scan_only_from_scanner:
                    tiered = self._pick_tiered_time(enabled_times, cfg)
                    if tiered:
                        chosen = self._select_specific_time(page, tiered)
                        if chosen:
                            return self._submit_slot(page, cfg, clean_day, station_label, chosen)
                    elif late_candidate is None:
                        late_candidate = slot_info

        with self._lock:
            self._available_slots = found_summary

        if found_summary:
            sig = json.dumps(found_summary, ensure_ascii=False, sort_keys=True)
            if late_candidate and cfg.mode == "auto_book" and not scan_only_from_scanner:
                with self._lock:
                    self._pending_confirmation = late_candidate
                    self._last_status = f"ساعت بعد از {cfg.max_time} باز است؛ منتظر تأیید شما"
                if sig != self._last_notified_sig:
                    self._last_notified_sig = sig
                    page.screenshot(path=str(SCREENSHOT_PATH), full_page=True)
                    times_str = "، ".join(late_candidate["times"])
                    msg = (
                        f"⏰ تا ساعت {cfg.max_time} نوبتی خالی نبود، اما ساعت‌های زیر باز هستند:\n"
                        f"📅 {late_candidate['day']} | ⛽ {late_candidate['station']}\n"
                        f"🕒 ساعت‌های خالی: {times_str}\n\n"
                        f"آیا می‌خواهید یکی از این ساعت‌ها را با {cfg.fuel_amount} لیتر برایتان ثبت کنم؟\n"
                        f"(دکمه «✅ ثبت اولین ساعت» را بزنید یا بنویسید: `/book {late_candidate['times'][0]}`)"
                    )
                    self.log(f"ساعت دیرتر از {cfg.max_time} پیدا شد؛ منتظر تأیید کاربر.")
                    self._notify(msg, SCREENSHOT_PATH)
                return False

            with self._lock:
                self._last_status = f"نوبت خالی پیدا شد! ({len(found_summary)} مورد)"
            if sig != self._last_notified_sig:
                self._last_notified_sig = sig
                page.screenshot(path=str(SCREENSHOT_PATH), full_page=True)
                lines = ["🚨 ساعت‌های نوبت سوخت خالی باز شد!"]
                for item in found_summary[:5]:
                    lines.append(
                        f"📅 {item['day']} | ⛽ {item['station']}\n"
                        f"🕒 {'، '.join(item['times'])}\n🔗 {item['url']}"
                    )
                self.log(f"ساعت‌های خالی اطلاع‌رسانی شد ({len(found_summary)} جایگاه/روز).")
                self._notify("\n\n".join(lines), SCREENSHOT_PATH)
        else:
            with self._lock:
                self._last_status = "نوبت خالی یافت نشد (در حال پایش)"
            self._last_notified_sig = None

        return False




    def direct_fetch(self, personnel_code: str, national_code: str) -> dict[str, Any]:
        """Synchronously fetch user name, active ticket, or all open slots using the warm browser."""
        p_code = normalize_text(personnel_code)
        n_code = normalize_text(national_code)
        if not p_code or not n_code:
            raise ValueError("کد پرسنلی و کد ملی الزامی است.")

        with self._lock:
            headless = self.settings.headless

        def _work(page: Page) -> dict[str, Any]:
            self._login_with_credentials(page, p_code, n_code)
            page.goto(BOOK_URL, wait_until="domcontentloaded")

            h1 = page.locator("header.ui-page-head h1")
            user_name = normalize_text(h1.first.inner_text()) if h1.count() > 0 else p_code
            parts = user_name.split(" ", 1)
            f_name = parts[0] if parts else ""
            l_name = parts[1] if len(parts) > 1 else f_name

            self.upsert_profile(p_code, n_code, f_name, l_name, label=user_name, activate=False)

            active_ticket = self._extract_active_ticket(page)
            if active_ticket:
                page.screenshot(path=str(SCREENSHOT_PATH), full_page=True)
                with self._lock:
                    self._active_ticket = active_ticket
                    self._available_slots = []
                    self._last_check_at = datetime.now().strftime("%H:%M:%S")
                    self._last_status = f"دارای نوبت فعال (کد پیگیری: {active_ticket.get('کد پیگیری', '-')})"
                return {
                    "ok": True,
                    "user_name": user_name,
                    "first_name": f_name,
                    "last_name": l_name,
                    "active_ticket": active_ticket,
                    "slots": [],
                }

            days = self._find_days(page, BotSettings())
            slots: list[dict[str, Any]] = []
            for day_href, day_label in days:
                full_day_url = day_href if day_href.startswith("http") else f"https://m7n.ir{day_href}"
                page.goto(full_day_url, wait_until="domcontentloaded")
                clean_day = self._extract_clean_day_label(page, day_label)
                stations = self._find_stations(page, BotSettings())
                for st_href, st_label in stations:
                    full_st_url = st_href if st_href.startswith("http") else f"https://m7n.ir{st_href}"
                    page.goto(full_st_url, wait_until="domcontentloaded")
                    times = self._list_enabled_times(page)
                    if times:
                        slots.append({"day": clean_day, "station": st_label, "url": full_st_url, "times": times})

            page.screenshot(path=str(SCREENSHOT_PATH), full_page=True)
            with self._lock:
                self._active_ticket = None
                self._available_slots = slots
                self._last_check_at = datetime.now().strftime("%H:%M:%S")
                self._last_status = f"نوبت خالی پیدا شد! ({len(slots)} مورد)" if slots else "نوبت خالی یافت نشد"
            return {
                "ok": True,
                "user_name": user_name,
                "first_name": f_name,
                "last_name": l_name,
                "active_ticket": None,
                "slots": slots,
            }

        return self._run_on_warm_page(headless, _work)

    def check_now_sync(self) -> dict[str, Any]:
        """Run an immediate check for the active user and return the structured result."""
        with self._lock:
            p_code = self.settings.personnel_code
            n_code = self.settings.national_code
            self._attempt_count += 1
        return self.direct_fetch(p_code, n_code)

    def direct_book(
        self,
        personnel_code: str,
        national_code: str,
        station_url: str,
        chosen_time: str,
        fuel_amount: str = "40",
        first_name: str = "",
        last_name: str = "",
        rec_national_code: str = "",
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Synchronously book a specific slot via the Web Intermediary."""
        p_code = normalize_text(personnel_code)
        n_code = normalize_text(national_code)
        if not (p_code and n_code and station_url and chosen_time):
            raise ValueError("اطلاعات نوبت (کد پرسنلی، کد ملی، جایگاه و ساعت) کامل نیست.")
        station_url = validate_station_url(station_url)

        with self._lock:
            headless = self.settings.headless

        with sync_playwright() as p:
            browser = launch_browser(p, headless)
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                self._login_with_credentials(page, p_code, n_code)
                page.goto(station_url, wait_until="domcontentloaded")

                h1 = page.locator("header.ui-page-head h1")
                user_name = normalize_text(h1.first.inner_text()) if h1.count() > 0 else ""
                parts = user_name.split(" ", 1)
                f_name = first_name.strip() or (parts[0] if parts else "")
                l_name = last_name.strip() or (parts[1] if len(parts) > 1 else f_name)
                r_nat = normalize_text(rec_national_code) or n_code

                selected = self._select_specific_time(page, chosen_time)
                if not selected:
                    raise RuntimeError(f"ساعت {chosen_time} دیگر خالی نیست.")

                temp_cfg = BotSettings(
                    personnel_code=p_code,
                    national_code=n_code,
                    recipient_first_name=f_name,
                    recipient_last_name=l_name,
                    recipient_national_code=r_nat,
                    fuel_amount=normalize_text(fuel_amount) or "40",
                    dry_run=dry_run,
                )
                self._submit_slot(page, temp_cfg, "رزرو وب واسطه", station_url, selected)
                ticket = self._extract_active_ticket(page)
                return {
                    "ok": True,
                    "dry_run": dry_run,
                    "time": selected,
                    "ticket": ticket,
                    "message": self._booked_info or "انجام شد",
                }
            finally:
                browser.close()

    def direct_cancel(self, personnel_code: str = "", national_code: str = "") -> dict[str, Any]:
        """Cancel the active fuel appointment for the given or active user."""
        with self._lock:
            p_code = normalize_text(personnel_code or self.settings.personnel_code)
            n_code = normalize_text(national_code or self.settings.national_code)
            headless = self.settings.headless

        if not p_code or not n_code:
            raise ValueError("کد پرسنلی و کد ملی برای لغو نوبت الزامی است.")

        with sync_playwright() as p:
            browser = launch_browser(p, headless)
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                self._login_with_credentials(page, p_code, n_code)
                page.goto(BOOK_URL, wait_until="domcontentloaded")

                cancel_btn = page.locator('form[action*="/fuel/cancel"] button[type="submit"]')
                if cancel_btn.count() == 0:
                    raise RuntimeError("نوبت فعالی برای لغو وجود ندارد.")

                ticket_before = self._extract_active_ticket(page)
                tracking = ticket_before.get("کد پیگیری", "-") if ticket_before else "-"

                page.once("dialog", lambda d: d.accept())
                cancel_btn.first.click()
                page.wait_for_load_state("networkidle", timeout=15000)
                page.screenshot(path=str(SCREENSHOT_PATH), full_page=True)

                msg = f"🗑 نوبت سوخت (کد پیگیری: {tracking}) برای کاربر {p_code} با موفقیت لغو شد."
                with self._lock:
                    if p_code == self.settings.personnel_code:
                        self._active_ticket = None
                        self._last_status = "نوبت لغو شد"
                self.log(msg)
                self._notify(msg, SCREENSHOT_PATH)
                return {"ok": True, "message": msg, "tracking_code": tracking}
            finally:
                browser.close()

    def batch_book(self, profile_ids: list[str] | None = None) -> list[dict[str, Any]]:
        """Book fuel appointments sequentially for a group of saved user profiles."""
        with self._lock:
            ids = profile_ids or self.settings.group_profile_ids or [p["id"] for p in self.settings.profiles]
            targets = [p for p in self.settings.profiles if p["id"] in ids]
            cfg = BotSettings(**self.settings.__dict__)

        if not targets:
            raise ValueError("هیچ کاربری برای نوبت‌گیری گروهی انتخاب نشده است.")

        results: list[dict[str, Any]] = []
        with sync_playwright() as p:
            browser = launch_browser(p, cfg.headless)
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                for prof in targets:
                    p_code = prof["personnel_code"]
                    n_code = prof["national_code"]
                    label = prof.get("label") or p_code
                    try:
                        self._login_with_credentials(page, p_code, n_code)
                        page.goto(BOOK_URL, wait_until="domcontentloaded")
                        h1 = page.locator("header.ui-page-head h1")
                        full_name = normalize_text(h1.first.inner_text()) if h1.count() > 0 else label
                        parts = full_name.split(" ", 1)
                        f_name = prof.get("first_name") or (parts[0] if parts else "")
                        l_name = prof.get("last_name") or (parts[1] if len(parts) > 1 else f_name)

                        existing = self._extract_active_ticket(page)
                        if existing:
                            results.append({
                                "user": full_name,
                                "personnel_code": p_code,
                                "status": "skipped",
                                "message": f"دارای نوبت فعال (کد پیگیری: {existing.get('کد پیگیری', '-')})",
                            })
                            continue

                        booked = False
                        for day_href, day_label in self._find_days(page, cfg):
                            page.goto(day_href if day_href.startswith("http") else f"https://m7n.ir{day_href}", wait_until="domcontentloaded")
                            clean_day = self._extract_clean_day_label(page, day_label)
                            for st_href, st_label in self._find_stations(page, cfg):
                                page.goto(st_href if st_href.startswith("http") else f"https://m7n.ir{st_href}", wait_until="domcontentloaded")
                                times = self._list_enabled_times(page)
                                tiered = self._pick_tiered_time(times, cfg)
                                if not tiered:
                                    continue
                                chosen = self._select_specific_time(page, tiered)
                                if not chosen:
                                    continue
                                temp_cfg = BotSettings(
                                    personnel_code=p_code,
                                    national_code=n_code,
                                    recipient_first_name=f_name,
                                    recipient_last_name=l_name,
                                    recipient_national_code=n_code,
                                    fuel_amount=cfg.fuel_amount,
                                    dry_run=cfg.dry_run,
                                )
                                self._submit_slot(page, temp_cfg, clean_day, st_label, chosen)
                                ticket = self._extract_active_ticket(page)
                                tracking = ticket.get("کد پیگیری", "-") if ticket else "DRY_RUN"
                                results.append({
                                    "user": full_name,
                                    "personnel_code": p_code,
                                    "status": "booked",
                                    "message": f"ثبت شد ({chosen} | {st_label} | کد پیگیری: {tracking})",
                                })
                                booked = True
                                break
                            if booked:
                                break
                        if not booked:
                            results.append({
                                "user": full_name,
                                "personnel_code": p_code,
                                "status": "no_slot",
                                "message": f"ساعتی در بازه {cfg.min_time} تا {cfg.fallback_max_time or cfg.max_time} خالی نبود",
                            })
                    except Exception as exc:
                        results.append({
                            "user": label,
                            "personnel_code": p_code,
                            "status": "error",
                            "message": str(exc),
                        })
            finally:
                browser.close()

        summary_lines = ["👨‍👩‍👧‍👦 گزارش نوبت‌گیری گروهی:"]
        for r in results:
            icon = "✅" if r["status"] == "booked" else ("⏭" if r["status"] == "skipped" else "⚠️")
            summary_lines.append(f"{icon} {r['user']} ({r['personnel_code']}): {r['message']}")
        self._notify("\n".join(summary_lines), SCREENSHOT_PATH)
        return results

    def _book_approved_once(self) -> None:
        with self._lock:
            cfg = BotSettings(**asdict(self.settings))
        try:
            with sync_playwright() as p:
                browser = launch_browser(p, cfg.headless)
                try:
                    page = browser.new_page(viewport={"width": 1280, "height": 900})
                    self._scan_and_act(page, cfg)
                finally:
                    browser.close()
        except Exception as exc:
            self.log(f"خطا در ثبت ساعت تأییدشده: {exc}")

    def _single_check_task(self) -> None:
        with self._lock:
            cfg = BotSettings(**asdict(self.settings))
        try:
            with sync_playwright() as p:
                browser = launch_browser(p, cfg.headless)
                try:
                    page = browser.new_page(viewport={"width": 1280, "height": 900})
                    with self._lock:
                        self._attempt_count += 1
                        self._last_check_at = datetime.now().strftime("%H:%M:%S")
                    self._scan_and_act(page, cfg)
                finally:
                    browser.close()
        except Exception as exc:
            self.log(f"خطا در بررسی تکی: {exc}")

    def _run_loop(self) -> None:
        self.log(
            f"شروع پایش | حالت: {'فقط اطلاع‌رسانی' if self.settings.mode == 'notify_only' else 'ثبت خودکار'} "
            f"| حداکثر ساعت خودکار: {self.settings.max_time} | سوخت پیش‌فرض: {self.settings.fuel_amount} لیتر"
        )
        try:
            with sync_playwright() as p:
                browser = launch_browser(p, self.settings.headless)
                context = browser.new_context(viewport={"width": 1280, "height": 900})
                page = context.new_page()
                enable_fast_mode(page)
                try:
                    while not self._stop_event.is_set():
                        with self._lock:
                            cfg = BotSettings(**asdict(self.settings))
                            self._attempt_count += 1
                            attempt = self._attempt_count
                            self._last_check_at = datetime.now().strftime("%H:%M:%S")
                        try:
                            done = self._scan_and_act(page, cfg)
                            if done:
                                with self._lock:
                                    self._running = False
                                break
                            self.log(f"بررسی #{attempt}: {self._last_status}")
                        except PlaywrightTimeoutError:
                            self.log(f"بررسی #{attempt}: تایم‌اوت در بارگذاری صفحه.")
                        except Exception as exc:
                            self.log(f"بررسی #{attempt}: خطا -> {exc}")

                        wait_sec = cfg.poll_interval + random.uniform(0.0, 0.8)
                        self._wake_event.wait(timeout=wait_sec)
                        self._wake_event.clear()
                finally:
                    browser.close()
        except Exception as exc:
            with self._lock:
                self._last_status = f"خطا: {exc}"
            self.log(f"توقف ربات به دلیل خطا: {exc}")
        finally:
            with self._lock:
                self._running = False

    def _next_booking_scheduler(self) -> None:
        """Automatically start auto-booking when next_eligible_iso date arrives."""
        while True:
            time.sleep(30)
            try:
                with self._lock:
                    enabled = self.settings.auto_next_booking
                    target_iso = self.settings.next_eligible_iso
                    target_jalali = self.settings.next_eligible_jalali
                    already_running = self._running
                if not enabled or not target_iso or already_running:
                    continue
                if date.today().isoformat() >= target_iso:
                    with self._lock:
                        self.settings.next_eligible_iso = ""
                        self.settings.mode = "auto_book"
                        self.settings.save()
                    msg = f"📅 تاریخ مجاز نوبت بعدی ({target_jalali}) فرا رسید! پایش و رزرو خودکار به صورت اتوماتیک شروع شد."
                    self.log(msg)
                    self._notify(msg, None)
                    self.start()
            except Exception as exc:
                self.log(f"خطا در زمان‌بند نوبت بعدی: {exc}")
