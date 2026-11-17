from __future__ import annotations

import json
import mimetypes
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fuel_service import FuelBotService

MENU_KEYBOARD = {
    "keyboard": [
        [{"text": "▶️ شروع پایش"}, {"text": "⏹ توقف پایش"}],
        [{"text": "🔍 بررسی فوری"}, {"text": "📊 وضعیت"}],
        [{"text": "🔔 فقط خبر بده"}, {"text": "⚡ ثبت خودکار"}],
        [{"text": "⚙️ تنظیمات"}],
    ],
    "resize_keyboard": True,
}


class TelegramController:
    """Two-way Telegram bot controller (long-polling) + instant notifier."""

    def __init__(self, service: "FuelBotService") -> None:
        self.service = service
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._offset = 0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def notify(self, message: str, image_path: Path | None = None) -> None:
        """Send alert text (and optional screenshot) to configured Telegram chat."""
        cfg = self.service.settings
        if not cfg.telegram_bot_token or not cfg.telegram_chat_id:
            return
        self._send_message(cfg.telegram_bot_token, cfg.telegram_chat_id, message)
        if image_path and image_path.exists():
            self._send_photo(cfg.telegram_bot_token, cfg.telegram_chat_id, image_path)

    def _api_json(self, token: str, method: str, payload: dict[str, Any], timeout: int = 35) -> dict[str, Any]:
        url = f"https://api.telegram.org/bot{token}/{method}"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _send_message(self, token: str, chat_id: str, text: str, reply_markup: dict | None = None) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            self._api_json(token, "sendMessage", payload, timeout=15)
        except Exception as exc:
            self.service.log(f"خطای ارسال پیام تلگرام: {exc}")

    def _send_photo(self, token: str, chat_id: str, image_path: Path) -> None:
        boundary = f"----WebKitFormBoundary{uuid.uuid4().hex}"
        mime_type = mimetypes.guess_type(str(image_path))[0] or "image/png"
        image_bytes = image_path.read_bytes()

        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="chat_id"\r\n\r\n{chat_id}\r\n'
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="photo"; filename="{image_path.name}"\r\n'
            f"Content-Type: {mime_type}\r\n\r\n"
        ).encode("utf-8") + image_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")

        url = f"https://api.telegram.org/bot{token}/sendPhoto"
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30):
                pass
        except Exception as exc:
            self.service.log(f"خطای ارسال تصویر تلگرام: {exc}")

    def _format_status(self) -> str:
        st = self.service.get_status()
        cfg = st["settings"]
        mode_fa = "🔔 فقط اطلاع‌رسانی" if st["mode"] == "notify_only" else "⚡ ثبت خودکار"
        run_fa = "🟢 در حال اجرا" if st["running"] else "🔴 متوقف"
        dry_fa = "فعال (تست)" if cfg["dry_run"] else "غیرفعال (ثبت واقعی)"
        lines = [
            f"وضعیت ربات: {run_fa}",
            f"حالت عملکرد: {mode_fa}",
            f"حالت تست (Dry Run): {dry_fa}",
            f"تعداد بررسی‌ها: {st['attempt_count']}",
            f"آخرین بررسی: {st['last_check_at'] or 'هنوز انجام نشده'}",
            f"پیام وضعیت: {st['last_status']}",
        ]
        if st["available_slots"]:
            lines.append("\nنوبت‌های باز پیدا شده:")
            for s in st["available_slots"][:4]:
                lines.append(f"• {s['day']} | {s['station']} ({len(s['times'])} ساعت)")
        return "\n".join(lines)

    def _format_settings(self) -> str:
        cfg = self.service.settings
        return (
            "⚙️ تنظیمات فعلی:\n"
            f"• کد پرسنلی: {cfg.personnel_code or '-'}\n"
            f"• میزان سوخت: {cfg.fuel_amount} لیتر\n"
            f"• جایگاه دلخواه: {cfg.preferred_stations or 'همه جایگاه‌ها'}\n"
            f"• روز دلخواه: {cfg.preferred_days or 'همه روزها'}\n"
            f"• بازه بررسی: {cfg.poll_interval} ثانیه\n"
            f"• تست (dry_run): {cfg.dry_run}\n\n"
            "برای تغییر سریع دستور بفرستید:\n"
            "`/set fuel 60`\n"
            "`/set station الماس نودژ` (یا `all` برای همه)\n"
            "`/set interval 4`\n"
            "`/set dryrun false`"
        )

    def _handle_command(self, token: str, chat_id: str, text: str) -> None:
        allowed_id = self.service.settings.telegram_chat_id.strip()
        if allowed_id and str(chat_id) != allowed_id:
            self._send_message(token, str(chat_id), "⛔ شما دسترسی کنترل این ربات را ندارید.")
            return

        cmd = text.strip()
        if cmd in {"/start", "منو", "/menu"}:
            extra = ""
            if not allowed_id:
                extra = f"\n\n🆔 Chat ID شما: `{chat_id}`\n(برای امنیت، آن را در تنظیمات وب یا `.env` وارد کنید.)"
            self._send_message(
                token,
                str(chat_id),
                "سلام! به ربات هوشمند نوبت‌دهی سوخت خوش آمدید. از دکمه‌های زیر استفاده کنید:" + extra,
                reply_markup=MENU_KEYBOARD,
            )
            return

        if cmd in {"▶️ شروع پایش", "/start_bot"}:
            try:
                started = self.service.start()
                reply = "✅ پایش ربات شروع شد." if started else "ℹ️ ربات از قبل در حال اجراست."
            except Exception as exc:
                reply = f"❌ خطا در شروع: {exc}"
            self._send_message(token, str(chat_id), reply, MENU_KEYBOARD)
            return

        if cmd in {"⏹ توقف پایش", "/stop_bot"}:
            stopped = self.service.stop()
            reply = "⏹ ربات متوقف شد." if stopped else "ℹ️ ربات در حال اجرا نبود."
            self._send_message(token, str(chat_id), reply, MENU_KEYBOARD)
            return

        if cmd in {"🔍 بررسی فوری", "/check"}:
            msg = self.service.trigger_check_now()
            self._send_message(token, str(chat_id), f"🔍 {msg}", MENU_KEYBOARD)
            return

        if cmd in {"🔔 فقط خبر بده", "/notify_mode"}:
            self.service.update_settings({"mode": "notify_only"})
            self._send_message(token, str(chat_id), "🔔 حالت ربات روی «فقط اطلاع‌رسانی سریع» تنظیم شد.", MENU_KEYBOARD)
            return

        if cmd in {"⚡ ثبت خودکار", "/book_mode"}:
            self.service.update_settings({"mode": "auto_book"})
            self._send_message(token, str(chat_id), "⚡ حالت ربات روی «ثبت خودکار نوبت» تنظیم شد.", MENU_KEYBOARD)
            return

        if cmd in {"📊 وضعیت", "/status"}:
            self._send_message(token, str(chat_id), self._format_status(), MENU_KEYBOARD)
            return

        if cmd in {"⚙️ تنظیمات", "/settings"}:
            self._send_message(token, str(chat_id), self._format_settings(), MENU_KEYBOARD)
            return

        if cmd.startswith("/set "):
            parts = cmd.split(maxsplit=2)
            if len(parts) < 3:
                self._send_message(token, str(chat_id), "فرمت دستور: `/set key value`", MENU_KEYBOARD)
                return
            key, val = parts[1].lower(), parts[2].strip()
            mapping = {
                "fuel": ("fuel_amount", val),
                "station": ("preferred_stations", "" if val.lower() == "all" else val),
                "day": ("preferred_days", "" if val.lower() == "all" else val),
                "interval": ("poll_interval", val),
                "dryrun": ("dry_run", val),
            }
            if key not in mapping:
                self._send_message(token, str(chat_id), "کلید نامعتبر. گزینه‌ها: fuel, station, day, interval, dryrun")
                return
            field_name, mapped_val = mapping[key]
            try:
                self.service.update_settings({field_name: mapped_val})
                self._send_message(token, str(chat_id), f"✅ تنظیم `{field_name}` به‌روزرسانی شد.", MENU_KEYBOARD)
            except Exception as exc:
                self._send_message(token, str(chat_id), f"❌ خطا: {exc}", MENU_KEYBOARD)
            return

        self._send_message(token, str(chat_id), "دستور نامشخص است. از دکمه‌های منو استفاده کنید.", MENU_KEYBOARD)

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            token = self.service.settings.telegram_bot_token.strip()
            if not token:
                time.sleep(3)
                continue
            try:
                resp = self._api_json(
                    token,
                    "getUpdates",
                    {"offset": self._offset, "timeout": 25},
                    timeout=30,
                )
                for upd in resp.get("result", []):
                    self._offset = max(self._offset, upd["update_id"] + 1)
                    msg = upd.get("message") or {}
                    text = msg.get("text")
                    chat_id = msg.get("chat", {}).get("id")
                    if text and chat_id is not None:
                        self._handle_command(token, str(chat_id), text)
            except urllib.error.HTTPError as exc:
                self.service.log(f"خطای HTTP تلگرام ({exc.code}): توکن را بررسی کنید.")
                time.sleep(5)
            except Exception:
                time.sleep(3)
