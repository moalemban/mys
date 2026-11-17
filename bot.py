from __future__ import annotations

import json
import os
import random
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

BASE_URL = "https://m7n.ir/fuel"
BOOK_URL = "https://m7n.ir/fuel/book"

DIGIT_MAP = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def normalize_text(value: str) -> str:
    """Convert Persian/Arabic digits to ASCII and collapse whitespace."""
    return " ".join(value.translate(DIGIT_MAP).split())


def parse_csv(value: str) -> list[str]:
    """Split comma-separated config values into a clean list."""
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_bool(value: str, default: bool = False) -> bool:
    """Parse boolean environment variable."""
    if not value:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    """Runtime configuration loaded from environment variables."""

    personnel_code: str
    national_code: str
    recipient_first_name: str
    recipient_last_name: str
    recipient_national_code: str
    fuel_amount: str
    preferred_days: list[str]
    preferred_stations: list[str]
    poll_interval: float
    headless: bool
    dry_run: bool
    telegram_bot_token: str
    telegram_chat_id: str

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()
        personnel_code = os.getenv("PERSONNEL_CODE", "").strip()
        national_code = os.getenv("NATIONAL_CODE", "").strip()
        if not personnel_code or not national_code:
            raise ValueError("PERSONNEL_CODE و NATIONAL_CODE باید در فایل .env تنظیم شوند.")

        return cls(
            personnel_code=personnel_code,
            national_code=national_code,
            recipient_first_name=os.getenv("RECIPIENT_FIRST_NAME", "").strip(),
            recipient_last_name=os.getenv("RECIPIENT_LAST_NAME", "").strip(),
            recipient_national_code=os.getenv("RECIPIENT_NATIONAL_CODE", "").strip(),
            fuel_amount=normalize_text(os.getenv("FUEL_AMOUNT", "60")),
            preferred_days=parse_csv(os.getenv("PREFERRED_DAYS", "")),
            preferred_stations=[normalize_text(s) for s in parse_csv(os.getenv("PREFERRED_STATIONS", ""))],
            poll_interval=max(1.0, float(os.getenv("POLL_INTERVAL_SECONDS", "3"))),
            headless=parse_bool(os.getenv("HEADLESS", "false"), default=False),
            dry_run=parse_bool(os.getenv("DRY_RUN", "true"), default=True),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        )


def log(msg: str) -> None:
    """Print timestamped log message."""
    now = datetime.now().strftime("%H:%M:%S")
    print(f"[{now}] {msg}", flush=True)


def play_alert() -> None:
    """Play an audible alert on Windows (or terminal bell fallback)."""
    try:
        import winsound

        for freq, dur in [(880, 200), (1175, 200), (1568, 400)]:
            winsound.Beep(freq, dur)
    except Exception:
        print("\a", end="", flush=True)


def send_telegram(cfg: Config, message: str) -> None:
    """Send an optional Telegram notification if token and chat_id are set."""
    if not cfg.telegram_bot_token or not cfg.telegram_chat_id:
        return
    url = f"https://api.telegram.org/bot{cfg.telegram_bot_token}/sendMessage"
    payload = json.dumps({"chat_id": cfg.telegram_chat_id, "text": message}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception as exc:
        log(f"خطا در ارسال پیام تلگرام: {exc}")


def login(page: Page, cfg: Config) -> None:
    """Log into https://m7n.ir/fuel using personnel_code and national_code."""
    log("در حال ورود به سامانه...")
    page.goto(BASE_URL, wait_until="domcontentloaded")
    if "/fuel/book" in page.url:
        log("نشست قبلی هنوز فعال است.")
        return

    page.locator("#personnel_code").fill(cfg.personnel_code)
    page.locator("#national_code").fill(cfg.national_code)
    page.locator('button[type="submit"]').click()
    page.wait_for_url("**/fuel/book**", timeout=15000)
    log("ورود با موفقیت انجام شد.")


def ensure_logged_in(page: Page, cfg: Config) -> None:
    """Re-login automatically if the session expired."""
    if "/fuel/book" not in page.url:
        login(page, cfg)


def find_available_days(page: Page, cfg: Config) -> list[tuple[str, str]]:
    """Return list of (href, label) for days with available appointments."""
    links = page.locator('a[href*="day="]')
    count = links.count()
    results: list[tuple[str, str]] = []
    for i in range(count):
        link = links.nth(i)
        href = link.get_attribute("href") or ""
        label = normalize_text(link.inner_text())
        if cfg.preferred_days and not any(day in href for day in cfg.preferred_days):
            continue
        results.append((href, label))
    return results


def find_available_stations(page: Page, cfg: Config) -> list[tuple[str, str]]:
    """Return list of (href, label) for stations with available appointments."""
    links = page.locator('a[href*="station="]')
    count = links.count()
    results: list[tuple[str, str]] = []
    for i in range(count):
        link = links.nth(i)
        href = link.get_attribute("href") or ""
        label = normalize_text(link.inner_text())
        if cfg.preferred_stations and not any(st in label for st in cfg.preferred_stations):
            continue
        results.append((href, label))
    return results


def pick_time_slot(page: Page) -> str | None:
    """Select the first enabled time-slot radio button and return its label."""
    time_group = page.get_by_role("radiogroup", name="ساعت نوبت")
    radios = time_group.get_by_role("radio")
    count = radios.count()
    for i in range(count):
        radio = radios.nth(i)
        if radio.is_disabled():
            continue
        slot_label = radio.get_attribute("aria-label") or normalize_text(radio.inner_text()) or f"slot #{i + 1}"
        radio.click(force=True)
        return slot_label
    return None


def pick_fuel_amount(page: Page, cfg: Config) -> None:
    """Select the requested fuel amount radio button."""
    fuel_group = page.get_by_role("radiogroup", name="میزان سوخت درخواستی")
    radios = fuel_group.get_by_role("radio")
    target = "".join(ch for ch in cfg.fuel_amount if ch.isdigit()) or "60"
    count = radios.count()
    for i in range(count):
        radio = radios.nth(i)
        text = normalize_text(radio.get_attribute("aria-label") or radio.inner_text() or "")
        if target in text:
            radio.click(force=True)
            return
    # Fallback: check parent labels if radio itself has empty text
    labels = fuel_group.locator("label")
    for i in range(labels.count()):
        lbl = labels.nth(i)
        if target in normalize_text(lbl.inner_text()):
            lbl.click()
            return
    raise RuntimeError(f"گزینه میزان سوخت «{cfg.fuel_amount}» پیدا نشد.")


def fill_recipient_and_submit(page: Page, cfg: Config, station_label: str, slot_label: str) -> bool:
    """Fill recipient fields and submit (or stop before submit in DRY_RUN mode)."""
    if not (cfg.recipient_first_name and cfg.recipient_last_name and cfg.recipient_national_code):
        raise ValueError("مشخصات تحویل‌گیرنده (نام، نام خانوادگی، کد ملی) در .env کامل نیست.")

    page.get_by_role("textbox", name="نام", exact=True).fill(cfg.recipient_first_name)
    page.get_by_role("textbox", name="نام خانوادگی", exact=True).fill(cfg.recipient_last_name)
    page.get_by_role("textbox", name="کد ملی", exact=True).fill(cfg.recipient_national_code)

    if cfg.dry_run:
        screenshot_path = Path("dry_run_ready.png")
        page.screenshot(path=str(screenshot_path), full_page=True)
        msg = f"[DRY_RUN] نوبت پیدا شد و فرم پر شد (ثبت نهایی انجام نشد): {station_label} | ساعت {slot_label}"
        log(msg)
        log(f"اسکرین‌شات ذخیره شد: {screenshot_path.resolve()}")
        play_alert()
        send_telegram(cfg, msg)
        return True

    submit_btn = page.get_by_role("button", name="ثبت نوبت")
    submit_btn.click()
    page.wait_for_load_state("networkidle", timeout=15000)

    screenshot_path = Path("booked_result.png")
    page.screenshot(path=str(screenshot_path), full_page=True)
    msg = f"نوبت ثبت شد! جایگاه: {station_label} | ساعت: {slot_label} | لینک: {page.url}"
    log(msg)
    log(f"اسکرین‌شات نتیجه ذخیره شد: {screenshot_path.resolve()}")
    play_alert()
    send_telegram(cfg, msg)
    return True


def try_book_once(page: Page, cfg: Config) -> bool:
    """Perform one full scan of /fuel/book -> day -> station -> slot."""
    page.goto(BOOK_URL, wait_until="domcontentloaded")
    ensure_logged_in(page, cfg)

    days = find_available_days(page, cfg)
    if not days:
        return False

    for day_href, day_label in days:
        log(f"روز دارای نوبت پیدا شد: {day_label}")
        page.goto(day_href if day_href.startswith("http") else f"https://m7n.ir{day_href}", wait_until="domcontentloaded")
        ensure_logged_in(page, cfg)

        stations = find_available_stations(page, cfg)
        if not stations:
            continue

        for station_href, station_label in stations:
            log(f"جایگاه دارای نوبت پیدا شد: {station_label}")
            page.goto(
                station_href if station_href.startswith("http") else f"https://m7n.ir{station_href}",
                wait_until="domcontentloaded",
            )
            ensure_logged_in(page, cfg)

            slot_label = pick_time_slot(page)
            if not slot_label:
                log(f"در جایگاه «{station_label}» ساعت خالی باقی نمانده بود.")
                continue

            pick_fuel_amount(page, cfg)
            return fill_recipient_and_submit(page, cfg, station_label, slot_label)

    return False


def launch_browser(p, headless: bool):
    """Launch Chromium, falling back to installed Chrome or Edge if needed."""
    for channel in (None, "chrome", "msedge"):
        try:
            kwargs = {"headless": headless}
            if channel:
                kwargs["channel"] = channel
            return p.chromium.launch(**kwargs)
        except Exception:
            continue
    raise RuntimeError(
        "مرورگر یافت نشد. لطفاً دستور `playwright install chromium` را اجرا کنید یا Chrome/Edge نصب داشته باشید."
    )


def main() -> int:
    try:
        cfg = Config.from_env()
    except ValueError as exc:
        log(f"خطای تنظیمات: {exc}")
        return 1

    log(
        f"شروع ربات نوبت‌دهی سوخت | حالت تست (DRY_RUN)={'فعال' if cfg.dry_run else 'غیرفعال'} "
        f"| بازه بررسی: {cfg.poll_interval} ثانیه"
    )

    with sync_playwright() as p:
        browser = launch_browser(p, cfg.headless)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()

        try:
            login(page, cfg)
            attempt = 0
            while True:
                attempt += 1
                try:
                    if try_book_once(page, cfg):
                        log("عملیات با موفقیت به پایان رسید.")
                        if not cfg.headless:
                            log("مرورگر تا ۳۰ ثانیه باز می‌ماند تا نتیجه را ببینید...")
                            time.sleep(30)
                        return 0
                    log(f"تلاش #{attempt}: نوبت خالی مطابق فیلترها پیدا نشد.")
                except PlaywrightTimeoutError:
                    log(f"تلاش #{attempt}: خطای تایم‌اوت در بارگذاری صفحه، تلاش مجدد...")
                except Exception as exc:
                    log(f"تلاش #{attempt}: خطای غیرمنتظره: {exc}")

                jitter = random.uniform(0.0, 0.8)
                time.sleep(cfg.poll_interval + jitter)
        except KeyboardInterrupt:
            log("ربات توسط کاربر متوقف شد.")
            return 0
        finally:
            browser.close()


if __name__ == "__main__":
    sys.exit(main())
