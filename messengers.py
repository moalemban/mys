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
    from auth import AuthManager
    from fuel_service import FuelBotService

MENU_BUTTONS = [
    ["▶️ شروع پایش", "⏹ توقف پایش"],
    ["🔍 بررسی فوری", "📊 وضعیت"],
    ["🔔 فقط خبر بده", "⚡ ثبت خودکار"],
    ["👥 کاربران", "⚙️ تنظیمات"],
]


def _telegram_reply_keyboard() -> dict[str, Any]:
    return {
        "keyboard": [[{"text": b} for b in row] for row in MENU_BUTTONS],
        "resize_keyboard": True,
    }


def _to_tg_inline(rows: list[list[tuple[str, str]]], allow_webapp: bool = True) -> dict[str, Any]:
    """Convert [(text, callback_data_or_webapp)] rows to Telegram/Bale/Soroush+ inline_keyboard."""
    kb = []
    for row in rows:
        btns = []
        for label, cb in row:
            if cb.startswith("webapp:"):
                u = cb.split(":", 1)[1]
                if allow_webapp:
                    btns.append({"text": label, "web_app": {"url": u}})
                else:
                    btns.append({"text": label, "url": u})
            elif cb.startswith("url:"):
                btns.append({"text": label, "url": cb.split(":", 1)[1]})
            else:
                btns.append({"text": label, "callback_data": cb})
        kb.append(btns)
    return {"inline_keyboard": kb}


def _to_rubika_inline(rows: list[list[tuple[str, str]]]) -> dict[str, Any]:
    """Convert [(text, callback_data)] rows to Rubika inline_keypad."""
    return {
        "rows": [
            {
                "buttons": [
                    {"id": cb, "type": "Simple", "button_text": label}
                    for label, cb in row
                    if not cb.startswith(("webapp:", "url:"))
                ]
            }
            for row in rows
            if any(not cb.startswith(("webapp:", "url:")) for _, cb in row)
        ]
    }


def _http_json(url: str, payload: dict[str, Any] | None = None, timeout: int = 35) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_multipart_photo(url: str, chat_id: str, image_path: Path, field_name: str = "photo") -> None:
    boundary = f"----WebKitFormBoundary{uuid.uuid4().hex}"
    mime_type = mimetypes.guess_type(str(image_path))[0] or "image/png"
    image_bytes = image_path.read_bytes()
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="chat_id"\r\n\r\n{chat_id}\r\n'
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field_name}"; filename="{image_path.name}"\r\n'
        f"Content-Type: {mime_type}\r\n\r\n"
    ).encode("utf-8") + image_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")

    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30):
        pass


class BotCommandRouter:
    """Shared Persian command & inline glass-button router for all messengers."""

    def __init__(self, service: "FuelBotService", auth: "AuthManager | None" = None) -> None:
        self.service = service
        self.auth = auth
        self._chat_states: dict[str, dict[str, str]] = {}

    def main_inline_rows(self) -> list[list[tuple[str, str]]]:
        st = self.service.get_status()
        cfg = self.service.settings
        mode_icon = "🔔" if st["mode"] == "notify_only" else "⚡"
        rows: list[list[tuple[str, str]]] = [
            [("▶️ شروع پایش", "act:start"), ("⏹ توقف پایش", "act:stop")],
            [("🔍 بررسی فوری", "act:check"), ("⛽ جایگاه‌ها و ساعت‌ها", "menu:stations")],
            [
                ("🔔 فقط خبر بده" + (" ✓" if st["mode"] == "notify_only" else ""), "mode:notify_only"),
                ("⚡ ثبت خودکار" + (" ✓" if st["mode"] == "auto_book" else ""), "mode:auto_book"),
            ],
            [("👥 کاربران", "menu:users"), ("👨‍👩‍👧‍👦 رزرو گروهی", "menu:group")],
            [("📊 وضعیت", "menu:status"), (f"{mode_icon} تنظیمات", "menu:settings")],
            [("📊 اکسل نوبت‌ها", "export:excel"), ("📄 PDF نوبت‌ها", "export:pdf")],
            [("📜 تاریخچه", "menu:history"), ("🗑 لغو نوبت", "menu:cancel_ask")],
            [("📱 مینی‌اپ (به‌زودی...)", "act:miniapp_soon"), ("🚪 خروج از حساب", "act:logout")],
        ]
        return rows

    def stations_inline_rows(self) -> list[list[tuple[str, str]]]:
        slots = self.service.get_status().get("available_slots") or []
        rows: list[list[tuple[str, str]]] = []
        for idx, s in enumerate(slots):
            rows.append([(f"⛽ {s['station']} | {s['day']} ({len(s['times'])} ساعت)", f"st:{idx}")])
        rows.append([("📊 خروجی اکسل", "export:excel"), ("📄 خروجی PDF", "export:pdf")])
        rows.append([("🔄 استعلام مجدد", "act:check"), ("🔙 منوی اصلی", "menu:main")])
        return rows

    def station_detail_rows(self, idx: int) -> tuple[str, list[list[tuple[str, str]]]]:
        slots = self.service.get_status().get("available_slots") or []
        if idx < 0 or idx >= len(slots):
            return "اطلاعات این جایگاه منقضی شده است؛ دوباره بررسی کنید.", self.stations_inline_rows()
        s = slots[idx]
        rows: list[list[tuple[str, str]]] = []
        times = s.get("times", [])
        for i in range(0, len(times), 3):
            rows.append([(f"⚡ {t}", f"stbook:{idx}:{t}") for t in times[i : i + 3]])
        rows.append([("🔙 لیست جایگاه‌ها", "menu:stations"), ("🏠 منوی اصلی", "menu:main")])
        txt = (
            f"⛽ جایگاه: {s['station']}\n"
            f"📅 روز: {s['day']}\n"
            f"🕒 تعداد ساعت خالی: {len(times)}\n\n"
            f"روی هر ساعت کلیک کنید تا با سهمیه پیش‌فرض ({self.service.settings.fuel_amount} لیتر) ثبت شود:"
        )
        return txt, rows

    def group_inline_rows(self) -> list[list[tuple[str, str]]]:
        cfg = self.service.settings
        rows: list[list[tuple[str, str]]] = []
        for p in cfg.profiles:
            checked = "☑️ " if p["id"] in cfg.group_profile_ids else "⬜ "
            rows.append([(f"{checked}{p.get('label') or p['personnel_code']} ({p['personnel_code']})", f"grp_tgl:{p['id']}")])
        rows.append([("🚀 اجرای نوبت‌گیری گروهی الان", "act:group_book")])
        rows.append([("🔙 منوی اصلی", "menu:main")])
        return rows

    def users_inline_rows(self) -> list[list[tuple[str, str]]]:
        cfg = self.service.settings
        rows: list[list[tuple[str, str]]] = []
        for p in cfg.profiles:
            mark = "✅ " if p.get("id") == cfg.active_profile_id else "👤 "
            rows.append([(f"{mark}{p.get('label') or p['personnel_code']} ({p['personnel_code']})", f"use:{p['id']}")])
        rows.append([("➕ افزودن کاربر جدید", "act:adduser"), ("🔙 منوی اصلی", "menu:main")])
        return rows

    def settings_inline_rows(self) -> list[list[tuple[str, str]]]:
        cfg = self.service.settings
        dry_lbl = "🧪 تست: روشن ✓" if cfg.dry_run else "🚀 تست: خاموش (ثبت واقعی)"
        return [
            [
                ("۲۰ لیتر" + (" ✓" if cfg.fuel_amount == "20" else ""), "set:fuel:20"),
                ("۴۰ لیتر" + (" ✓" if cfg.fuel_amount == "40" else ""), "set:fuel:40"),
                ("۶۰ لیتر" + (" ✓" if cfg.fuel_amount == "60" else ""), "set:fuel:60"),
            ],
            [
                ("⏰ ۱۴ تا ۱۶:۳۰ (نهایتاً ۱۷)", "set:range:14:00:16:30:17:00"),
                ("⏰ بدون محدودیت ساعت", "set:range:00:00:23:59:23:59"),
            ],
            [(dry_lbl, "toggle:dryrun")],
            [("🔙 بازگشت به منوی اصلی", "menu:main")],
        ]

    def cancel_inline_rows(self) -> list[list[tuple[str, str]]]:
        return [
            [("⚠️ بله، نوبت لغو شود", "act:cancel_confirm"), ("🔙 خیر، انصراف", "menu:main")],
        ]

    def late_slots_inline_rows(self) -> list[list[tuple[str, str]]]:
        pending = self.service.get_status().get("pending_confirmation")
        if not pending:
            return self.main_inline_rows()
        rows: list[list[tuple[str, str]]] = []
        times = pending.get("times", [])[:6]
        for i in range(0, len(times), 2):
            pair = [(f"⚡ ثبت {t}", f"book:{t}") for t in times[i : i + 2]]
            rows.append(pair)
        rows.append([("❌ ثبت نکن، منتظر بمان", "act:decline"), ("🔙 منوی اصلی", "menu:main")])
        return rows

    def format_status(self) -> str:
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
        if st.get("active_ticket"):
            t = st["active_ticket"]
            lines.append(
                f"\n🎫 نوبت فعال شما:\n"
                f"کد پیگیری: {t.get('کد پیگیری', '-')}\n"
                f"روز: {t.get('روز', '-')} | ساعت: {t.get('ساعت', '-')}\n"
                f"جایگاه: {t.get('جایگاه', '-')} | میزان: {t.get('میزان سوخت', '-')}"
            )
        if st["available_slots"]:
            lines.append("\nنوبت‌های باز پیدا شده:")
            for s in st["available_slots"][:4]:
                lines.append(f"• {s['day']} | {s['station']} ({len(s['times'])} ساعت)")
        return "\n".join(lines)

    def format_settings(self) -> str:
        cfg = self.service.settings
        return (
            "⚙️ تنظیمات سریع:\n"
            f"• کاربر فعال: {cfg.recipient_first_name} {cfg.recipient_last_name} ({cfg.personnel_code or '-'})\n"
            f"• میزان سوخت پیش‌فرض: {cfg.fuel_amount} لیتر\n"
            f"• حداکثر ساعت ثبت خودکار: {cfg.max_time}\n"
            f"• جایگاه دلخواه: {cfg.preferred_stations or 'همه جایگاه‌ها'}\n"
            f"• بازه بررسی: {cfg.poll_interval} ثانیه\n"
            f"• حالت تست: {'روشن' if cfg.dry_run else 'خاموش (ثبت واقعی)'}"
        )

    def handle_action(self, action: str, chat_id: str, allowed_chat_id: str) -> tuple[str, list[list[tuple[str, str]]] | None]:
        """Handle both text commands and inline glass-button callbacks."""
        if allowed_chat_id and str(chat_id) != allowed_chat_id:
            return "⛔ شما دسترسی کنترل این ربات را ندارید.", None

        cmd = action.strip()

        if self.auth is not None:
            chat_key = f"chat:{chat_id}"
            state = self._chat_states.get(chat_key)

            if cmd in {"/logout", "act:logout"}:
                self.auth.logout_chat(chat_key)
                self._chat_states[chat_key] = {"step": "awaiting_username"}
                return "🚪 از حساب خارج شدید.\n\n🔐 برای ورود مجدد، **نام کاربری** خود را ارسال کنید:", None

            if cmd.startswith("/login "):
                parts = cmd.split(maxsplit=2)
                if len(parts) < 3:
                    return "فرمت: `/login نام_کاربری رمز_عبور`", None
                user = self.auth.login_chat(chat_key, parts[1], parts[2])
                if not user:
                    return "❌ نام کاربری یا رمز عبور اشتباه است.", None
                self._chat_states.pop(chat_key, None)
                if not self.service.settings.profiles:
                    self._chat_states[chat_key] = {"step": "awaiting_profile_personnel"}
                    return (
                        f"✅ خوش آمدید {user['username']}!\n\n"
                        "👤 هنوز هیچ کاربر سوختی تعریف نشده است. برای شروع باید حداقل یک کاربر تعریف کنید.\n"
                        "لطفاً **کد پرسنلی** کاربر سوخت را ارسال کنید:",
                        None,
                    )
                return f"✅ خوش آمدید {user['username']}!\nاز دکمه‌های شیشه‌ای زیر استفاده کنید:", self.main_inline_rows()

            if not self.auth.get_chat_user(chat_key):
                if state and state.get("step") == "awaiting_password":
                    username = state.get("username", "")
                    try:
                        self.auth.check_rate_limit(chat_key)
                    except ValueError as exc:
                        return f"⏳ {exc}", None
                    user = self.auth.login_chat(chat_key, username, cmd)
                    if not user:
                        self.auth.record_failure(chat_key)
                        self._chat_states[chat_key] = {"step": "awaiting_username"}
                        return "❌ نام کاربری یا رمز عبور اشتباه است.\n\n🔐 لطفاً دوباره **نام کاربری** خود را ارسال کنید:", None
                    self.auth.clear_failures(chat_key)
                    self._chat_states.pop(chat_key, None)
                    if not self.service.settings.profiles:
                        self._chat_states[chat_key] = {"step": "awaiting_profile_personnel"}
                        return (
                            f"✅ خوش آمدید {user['username']}!\n\n"
                            "👤 هنوز هیچ کاربر سوختی تعریف نشده است. باید حداقل یک کاربر تعریف کنید.\n"
                            "لطفاً **کد پرسنلی** را ارسال کنید:",
                            None,
                        )
                    return f"✅ خوش آمدید {user['username']}!\nاز دکمه‌های شیشه‌ای زیر استفاده کنید:", self.main_inline_rows()

                if state and state.get("step") == "awaiting_username" and not cmd.startswith("/"):
                    self._chat_states[chat_key] = {"step": "awaiting_password", "username": cmd}
                    return f"👤 نام کاربری: `{cmd}`\n🔑 حالا **رمز عبور** را ارسال کنید:", None

                self._chat_states[chat_key] = {"step": "awaiting_username"}
                return "🔐 برای ورود به ربات، لطفاً **نام کاربری** خود را ارسال کنید:", None

            # Logged-in interactive onboarding / add-user wizard
            if state and state.get("step") == "awaiting_profile_personnel" and not cmd.startswith("/"):
                self._chat_states[chat_key] = {"step": "awaiting_profile_national", "personnel_code": cmd}
                return f"کد پرسنلی `{cmd}` ثبت شد.\n🆔 حالا **کد ملی ۱۰ رقمی** را ارسال کنید:", None

            if state and state.get("step") == "awaiting_profile_national" and not cmd.startswith("/"):
                p_code = state.get("personnel_code", "")
                self._chat_states.pop(chat_key, None)
                try:
                    prof = self.service.upsert_profile(p_code, cmd, activate=True)
                    return (
                        f"✅ کاربر سوخت ({prof['label']}) با موفقیت ذخیره و فعال شد!\n"
                        "حالا می‌توانید از تمام امکانات ربات استفاده کنید:",
                        self.main_inline_rows(),
                    )
                except Exception as exc:
                    return f"❌ خطا در ثبت کاربر: {exc}", self.main_inline_rows()

            if not self.service.settings.profiles and cmd not in {"act:adduser", "act:logout", "/logout"}:
                self._chat_states[chat_key] = {"step": "awaiting_profile_personnel"}
                return (
                    "👤 هنوز هیچ کاربر سوختی تعریف نشده است! لطفاً ابتدا یک کاربر تعریف کنید.\n"
                    "لطفاً **کد پرسنلی** را ارسال کنید:",
                    None,
                )

        if cmd == "act:miniapp_soon":
            return (
                "🚀 مینی‌اپ اختصاصی به‌زودی اضافه می‌شود!\n"
                "در حال حاضر تمام امکانات (پایش لحظه‌ای، جایگاه‌به‌جایگاه، رزرو خودکار، گروهی و خروجی اکسل/PDF) مستقیماً داخل همین ربات فعال است.",
                self.main_inline_rows(),
            )

        if cmd == "act:adduser":
            self._chat_states[f"chat:{chat_id}"] = {"step": "awaiting_profile_personnel"}
            return "➕ افزودن کاربر جدید\nلطفاً **کد پرسنلی** را ارسال کنید:", None

        # Inline menu navigation
        if cmd in {"/start", "منو", "/menu", "menu:main"}:
            extra = f"\n\n🆔 شناسه چت شما: {chat_id}" if not allowed_chat_id else ""
            return (
                "⛽ سامانه هوشمند نوبت‌دهی سوخت\nاز دکمه‌های شیشه‌ای زیر برای کنترل کامل ربات استفاده کنید:" + extra,
                self.main_inline_rows(),
            )

        if cmd in {"📊 وضعیت", "/status", "menu:status"}:
            return self.format_status(), self.main_inline_rows()

        if cmd in {"👥 کاربران", "/users", "menu:users"}:
            return "👥 یک کاربر را برای فعال‌سازی انتخاب کنید:", self.users_inline_rows()

        if cmd in {"menu:stations", "/stations"}:
            slots = self.service.get_status().get("available_slots") or []
            if not slots:
                return "در حال حاضر نوبت خالی کشف‌شده‌ای در حافظه نیست. «🔍 بررسی فوری» را بزنید.", self.stations_inline_rows()
            return f"⛽ {len(slots)} جایگاه دارای نوبت خالی — هر جایگاه را جداگانه انتخاب کنید:", self.stations_inline_rows()

        if cmd.startswith("st:"):
            idx = int(cmd.split(":", 1)[1])
            return self.station_detail_rows(idx)

        if cmd.startswith("stbook:"):
            _, idx_s, chosen_t = cmd.split(":", 2)
            slots = self.service.get_status().get("available_slots") or []
            idx = int(idx_s)
            if idx < 0 or idx >= len(slots):
                return "❌ جایگاه منقضی شد.", self.stations_inline_rows()
            return self.service.confirm_late_booking(chosen_time=chosen_t, station_url=slots[idx]["url"]), self.main_inline_rows()

        if cmd in {"menu:group", "/group"}:
            return "👨‍👩‍👧‍👦 کاربران مورد نظر برای نوبت‌گیری گروهی را تیک بزنید:", self.group_inline_rows()

        if cmd.startswith("grp_tgl:"):
            pid = cmd.split(":", 1)[1]
            self.service.toggle_group_profile(pid)
            return "👨‍👩‍👧‍👦 لیست نوبت‌گیری گروهی به‌روزرسانی شد:", self.group_inline_rows()

        if cmd in {"act:group_book", "/groupbook"}:
            threading.Thread(target=self.service.batch_book, daemon=True).start()
            return "🚀 نوبت‌گیری گروهی برای کاربران انتخاب‌شده شروع شد؛ گزارش نهایی ارسال می‌شود.", self.main_inline_rows()

        if cmd in {"menu:history", "/history"}:
            hist = self.service.get_status().get("booking_history") or []
            if not hist:
                return "📜 هنوز نوبتی در این نشست ثبت نشده است.", self.main_inline_rows()
            lines = ["📜 تاریخچه نوبت‌های ثبت‌شده:"]
            for h in hist[:10]:
                lines.append(f"• {h['user']} | {h['station']} ({h['slot']}) | {h['fuel']} | کد: {h['tracking']}")
            return "\n".join(lines), self.main_inline_rows()

        if cmd in {"⚙️ تنظیمات", "/settings", "menu:settings"}:
            return self.format_settings(), self.settings_inline_rows()

        if cmd in {"/cancel", "menu:cancel_ask"}:
            return "⚠️ آیا مطمئن هستید که می‌خواهید نوبت فعال کاربر فعلی لغو شود؟", self.cancel_inline_rows()

        if cmd == "help:adduser":
            return (
                "برای افزودن کاربر جدید، پیام زیر را بفرستید:\n"
                "/adduser کدپرسنلی کدملی\n"
                "مثال: `/adduser 47625126 3160682145`",
                self.users_inline_rows(),
            )

        # Inline actions
        if cmd in {"▶️ شروع پایش", "/start_bot", "act:start"}:
            try:
                started = self.service.start()
                msg = "✅ پایش ربات شروع شد." if started else "ℹ️ ربات از قبل در حال اجراست."
            except Exception as exc:
                msg = f"❌ خطا در شروع: {exc}"
            return msg, self.main_inline_rows()

        if cmd in {"⏹ توقف پایش", "/stop_bot", "act:stop"}:
            stopped = self.service.stop()
            msg = "⏹ ربات متوقف شد." if stopped else "ℹ️ ربات در حال اجرا نبود."
            return msg, self.main_inline_rows()

        if cmd in {"🔍 بررسی فوری", "/check", "act:check"}:
            try:
                res = self.service.check_now_sync()
                if res.get("active_ticket"):
                    t = res["active_ticket"]
                    msg = (
                        f"🎫 کاربر فعلی ({res['user_name']}) دارای نوبت فعال است:\n"
                        f"کد پیگیری: {t.get('کد پیگیری', '-')}\n"
                        f"📅 {t.get('روز', '-')} | ⏰ {t.get('ساعت', '-')}\n"
                        f"⛽ {t.get('جایگاه', '-')} | 🛢 {t.get('میزان سوخت', '-')}"
                    )
                    return msg, self.main_inline_rows()
                slots = res.get("slots") or []
                if slots:
                    return (
                        f"✅ بررسی انجام شد ({res['user_name']}) — {len(slots)} جایگاه دارای ظرفیت پیدا شد:",
                        self.stations_inline_rows(),
                    )
                return f"ℹ️ بررسی انجام شد ({res['user_name']}) — در حال حاضر نوبت خالی باز نیست.", self.main_inline_rows()
            except Exception as exc:
                return f"❌ خطا در بررسی: {exc}", self.main_inline_rows()

        if cmd in {"🔔 فقط خبر بده", "/notify_mode", "mode:notify_only"}:
            self.service.update_settings({"mode": "notify_only"})
            return "🔔 حالت ربات روی «فقط اطلاع‌رسانی سریع» تنظیم شد.", self.main_inline_rows()

        if cmd in {"⚡ ثبت خودکار", "/book_mode", "mode:auto_book"}:
            self.service.update_settings({"mode": "auto_book"})
            return "⚡ حالت ربات روی «ثبت خودکار نوبت» تنظیم شد.", self.main_inline_rows()

        if cmd in {"✅ ثبت اولین ساعت", "/confirm"}:
            return self.service.confirm_late_booking(), self.main_inline_rows()

        if cmd in {"❌ ثبت نکن", "/decline", "act:decline"}:
            return self.service.decline_late_booking(), self.main_inline_rows()

        if cmd in {"/cancel_confirm", "act:cancel_confirm"}:
            try:
                res = self.service.direct_cancel()
                return res["message"], self.main_inline_rows()
            except Exception as exc:
                return f"❌ خطا در لغو نوبت: {exc}", self.main_inline_rows()

        if cmd.startswith("book:"):
            return self.service.confirm_late_booking(chosen_time=cmd.split(":", 1)[1]), self.main_inline_rows()

        if cmd.startswith("/book "):
            return self.service.confirm_late_booking(chosen_time=cmd.split(maxsplit=1)[1].strip()), self.main_inline_rows()

        if cmd.startswith("use:"):
            prof_id = cmd.split(":", 1)[1]
            try:
                prof = self.service.select_profile(prof_id)
                return f"✅ کاربر فعال تغییر کرد به: {prof['label']}", self.users_inline_rows()
            except Exception as exc:
                return f"❌ {exc}", self.users_inline_rows()

        if cmd == "toggle:dryrun":
            self.service.update_settings({"dry_run": not self.service.settings.dry_run})
            return self.format_settings(), self.settings_inline_rows()

        if cmd.startswith("set:fuel:"):
            val = cmd.split(":", 2)[2]
            self.service.update_settings({"fuel_amount": val})
            return self.format_settings(), self.settings_inline_rows()

        if cmd.startswith("set:maxtime:"):
            val = cmd.split(":", 2)[2]
            self.service.update_settings({"max_time": val})
            return self.format_settings(), self.settings_inline_rows()

        if cmd.startswith("set:range:"):
            parts = cmd.split(":")
            # set:range:14:00:16:30:17:00
            if len(parts) == 8:
                self.service.update_settings({
                    "min_time": f"{parts[2]}:{parts[3]}",
                    "max_time": f"{parts[4]}:{parts[5]}",
                    "fallback_max_time": f"{parts[6]}:{parts[7]}",
                })
            return self.format_settings(), self.settings_inline_rows()

        if cmd.startswith("/adduser "):
            parts = cmd.split()
            if len(parts) < 3:
                return "فرمت: /adduser کدپرسنلی کدملی [نام] [نام_خانوادگی]", self.users_inline_rows()
            p_code, n_code = parts[1], parts[2]
            f_name = parts[3] if len(parts) > 3 else ""
            l_name = " ".join(parts[4:]) if len(parts) > 4 else ""
            prof = self.service.upsert_profile(p_code, n_code, f_name, l_name, activate=True)
            return f"✅ کاربر {prof['label']} ذخیره و فعال شد.", self.users_inline_rows()

        if cmd.startswith("/use "):
            target = cmd.split(maxsplit=1)[1].strip()
            prof = next(
                (p for p in self.service.settings.profiles if p["personnel_code"] == target or p["id"] == target),
                None,
            )
            if not prof:
                return "❌ کاربری با این کد پیدا نشد.", self.users_inline_rows()
            self.service.select_profile(prof["id"])
            return f"✅ کاربر فعال به {prof['label']} تغییر یافت.", self.users_inline_rows()

        if cmd.startswith("/set "):
            parts = cmd.split(maxsplit=2)
            if len(parts) < 3:
                return "فرمت دستور: /set key value", self.settings_inline_rows()
            key, val = parts[1].lower(), parts[2].strip()
            mapping = {
                "fuel": ("fuel_amount", val),
                "maxtime": ("max_time", val),
                "station": ("preferred_stations", "" if val.lower() == "all" else val),
                "day": ("preferred_days", "" if val.lower() == "all" else val),
                "interval": ("poll_interval", val),
                "dryrun": ("dry_run", val),
            }
            if key not in mapping:
                return "کلید نامعتبر. گزینه‌ها: fuel, maxtime, station, day, interval, dryrun", self.settings_inline_rows()
            field_name, mapped_val = mapping[key]
            try:
                self.service.update_settings({field_name: mapped_val})
                return f"✅ تنظیم {field_name} به‌روزرسانی شد.", self.settings_inline_rows()
            except Exception as exc:
                return f"❌ خطا: {exc}", self.settings_inline_rows()

        return "از دکمه‌های شیشه‌ای زیر استفاده کنید:", self.main_inline_rows()


class MultiMessengerHub:
    """Manages Telegram, Bale, Rubika, and Soroush+ bots with inline glass buttons."""

    def __init__(self, service: "FuelBotService", auth: "AuthManager | None" = None) -> None:
        self.service = service
        self.router = BotCommandRouter(service, auth=auth)
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []
        self._tg_offset = 0
        self._bale_offset = 0
        self._soroush_offset = 0
        self._rubika_next_id: str | None = None

    def start(self) -> None:
        if self._threads:
            return
        self._stop_event.clear()
        targets = [
            ("telegram", self._poll_telegram),
            ("bale", self._poll_bale),
            ("rubika", self._poll_rubika),
            ("soroush", self._poll_soroush),
        ]
        for name, fn in targets:
            t = threading.Thread(target=fn, name=f"messenger-{name}", daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop_event.set()

    def sync_bot_profile(self) -> dict[str, str]:
        """Apply bot Name, Description, Short Description, and Slash Commands to Telegram & Bale."""
        cfg = self.service.settings
        name = "⛽ نوبت‌یار هوشمند سوخت"
        short_desc = "پایش لحظه‌ای، اطلاع‌رسانی سریع و ثبت خودکار نوبت سوخت با رزرو گروهی و خروجی اکسل/PDF"
        desc = (
            "⛽ ربات هوشمند نوبت‌دهی سوخت\n\n"
            "✨ امکانات:\n"
            "• پایش لحظه‌ای و اطلاع‌رسانی جایگاه‌به‌جایگاه\n"
            "• ثبت خودکار در بازه زمانی دلخواه (۱۴:۰۰ تا ۱۶:۳۰ / ۱۷:۰۰)\n"
            "• رزرو گروهی برای چند کاربر مختلف\n"
            "• زمان‌بندی خودکار نوبت بعدی\n"
            "• خروجی اکسل (.xlsx) و PDF + مینی‌اپ اختصاصی"
        )
        commands = [
            {"command": "start", "description": "منوی اصلی و دکمه‌های شیشه‌ای"},
            {"command": "status", "description": "وضعیت لحظه‌ای ربات و نوبت فعال"},
            {"command": "check", "description": "بررسی فوری نوبت‌های خالی"},
            {"command": "stations", "description": "نمایش جایگاه‌به‌جایگاه"},
            {"command": "group", "description": "نوبت‌گیری گروهی"},
            {"command": "excel", "description": "دریافت خروجی اکسل"},
            {"command": "pdf", "description": "دریافت خروجی PDF"},
            {"command": "settings", "description": "تنظیمات سریع"},
            {"command": "login", "description": "ورود به حساب ربات"},
            {"command": "logout", "description": "خروج از حساب ربات"},
        ]

        results: dict[str, str] = {}
        for label, host, token in [
            ("telegram", "https://api.telegram.org", cfg.telegram_bot_token.strip()),
            ("bale", "https://tapi.bale.ai", cfg.bale_bot_token.strip()),
            ("soroush", "https://api.splus.ir", cfg.soroush_bot_token.strip()),
        ]:
            if not token:
                results[label] = "توکن تنظیم نشده"
                continue
            try:
                _http_json(f"{host}/bot{token}/setMyCommands", {"commands": commands}, timeout=10)
                if label in {"telegram", "bale"}:
                    _http_json(f"{host}/bot{token}/setMyName", {"name": name}, timeout=10)
                    _http_json(f"{host}/bot{token}/setMyShortDescription", {"short_description": short_desc}, timeout=10)
                    _http_json(f"{host}/bot{token}/setMyDescription", {"description": desc}, timeout=10)
                results[label] = "پروفایل با موفقیت اعمال شد ✅"
            except Exception as exc:
                results[label] = f"خطا: {exc}"
        return results

    def notify(self, message: str, image_path: Path | None = None) -> None:
        cfg = self.service.settings
        inline_rows = (
            self.router.late_slots_inline_rows()
            if self.service.get_status().get("pending_confirmation")
            else self.router.main_inline_rows()
        )
        if cfg.telegram_bot_token and cfg.telegram_chat_id:
            self._send_tg_like("https://api.telegram.org", cfg.telegram_bot_token, cfg.telegram_chat_id, message, image_path, inline_rows=inline_rows)
        if cfg.bale_bot_token and cfg.bale_chat_id:
            self._send_tg_like("https://tapi.bale.ai", cfg.bale_bot_token, cfg.bale_chat_id, message, image_path, inline_rows=inline_rows)
        if cfg.soroush_bot_token and cfg.soroush_chat_id:
            self._send_tg_like("https://api.splus.ir", cfg.soroush_bot_token, cfg.soroush_chat_id, message, image_path, inline_rows=inline_rows)
        if cfg.rubika_bot_token and cfg.rubika_chat_id:
            self._send_rubika(cfg.rubika_bot_token, cfg.rubika_chat_id, message, inline_rows=inline_rows)

    def _send_tg_document(self, base_host: str, token: str, chat_id: str, file_path: Path, caption: str = "") -> None:
        try:
            _http_multipart_photo(f"{base_host}/bot{token}/sendDocument", chat_id, file_path, field_name="document")
        except Exception as exc:
            self.service.log(f"خطای ارسال فایل ({base_host}): {exc}")

    def _send_tg_like(
        self,
        base_host: str,
        token: str,
        chat_id: str,
        text: str,
        image_path: Path | None = None,
        inline_rows: list[list[tuple[str, str]]] | None = None,
        with_reply_kb: bool = False,
    ) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        allow_webapp = "api.telegram.org" in base_host
        if inline_rows:
            payload["reply_markup"] = _to_tg_inline(inline_rows, allow_webapp=allow_webapp)
        elif with_reply_kb:
            payload["reply_markup"] = _telegram_reply_keyboard()
        try:
            _http_json(f"{base_host}/bot{token}/sendMessage", payload, timeout=15)
            if image_path and image_path.exists():
                _http_multipart_photo(f"{base_host}/bot{token}/sendPhoto", chat_id, image_path)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            self.service.log(f"خطای ارسال پیام ({base_host}): {exc} -> {detail}")
        except Exception as exc:
            self.service.log(f"خطای ارسال پیام ({base_host}): {exc}")

    def _edit_tg_like(
        self,
        base_host: str,
        token: str,
        chat_id: str,
        message_id: int,
        text: str,
        inline_rows: list[list[tuple[str, str]]] | None = None,
    ) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if inline_rows:
            payload["reply_markup"] = _to_tg_inline(inline_rows, allow_webapp="api.telegram.org" in base_host)
        try:
            _http_json(f"{base_host}/bot{token}/editMessageText", payload, timeout=15)
        except Exception:
            self._send_tg_like(base_host, token, chat_id, text, inline_rows=inline_rows)

    def _answer_callback(self, base_host: str, token: str, callback_id: str, text: str = "") -> None:
        try:
            payload: dict[str, Any] = {"callback_query_id": callback_id}
            if text:
                payload["text"] = text
            _http_json(f"{base_host}/bot{token}/answerCallbackQuery", payload, timeout=10)
        except Exception:
            pass

    def _send_rubika(
        self,
        token: str,
        chat_id: str,
        text: str,
        inline_rows: list[list[tuple[str, str]]] | None = None,
    ) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if inline_rows:
            payload["inline_keypad"] = _to_rubika_inline(inline_rows)
        try:
            _http_json(f"https://botapi.rubika.ir/v3/{token}/sendMessage", payload, timeout=15)
        except Exception as exc:
            self.service.log(f"خطای ارسال پیام روبیکا: {exc}")

    def _send_soroush(self, token: str, chat_id: str, text: str) -> None:
        payload = {"type": "TEXT", "to": chat_id, "body": text}
        try:
            _http_json(f"https://api.splus.ir/bot{token}/sendMessage", payload, timeout=15)
        except Exception as exc:
            self.service.log(f"خطای ارسال پیام سروش‌پلاس: {exc}")

    def _process_tg_like_updates(self, base_host: str, token: str, allowed_chat_id: str, updates: list[dict[str, Any]]) -> int:
        max_uid = 0
        for upd in updates:
            max_uid = max(max_uid, upd["update_id"] + 1)
            cb = upd.get("callback_query")
            if cb:
                cb_id = cb.get("id", "")
                data = cb.get("data", "")
                msg = cb.get("message") or {}
                chat_id = str(msg.get("chat", {}).get("id", ""))
                message_id = msg.get("message_id")
                toast = "⏳ در حال بررسی..." if data in {"act:check", "export:excel", "export:pdf"} else ""
                self._answer_callback(base_host, token, cb_id, text=toast)
                if chat_id and data:
                    if data in {"export:excel", "export:pdf"}:
                        self._handle_export_callback(base_host, token, chat_id, data)
                        continue
                    reply, rows = self.router.handle_action(data, chat_id, allowed_chat_id)
                    if message_id:
                        self._edit_tg_like(base_host, token, chat_id, message_id, reply, inline_rows=rows)
                    else:
                        self._send_tg_like(base_host, token, chat_id, reply, inline_rows=rows)
                continue

            msg = upd.get("message") or {}
            text = msg.get("text")
            chat_id = msg.get("chat", {}).get("id")
            if text and chat_id is not None:
                if text.strip() in {"/excel", "/pdf"}:
                    kind = "export:excel" if text.strip() == "/excel" else "export:pdf"
                    self._handle_export_callback(base_host, token, str(chat_id), kind)
                    continue
                reply, rows = self.router.handle_action(text, str(chat_id), allowed_chat_id)
                self._send_tg_like(base_host, token, str(chat_id), reply, inline_rows=rows)
        return max_uid

    def _handle_export_callback(self, base_host: str, token: str, chat_id: str, kind: str) -> None:
        from exports import export_slots_excel, export_slots_pdf

        slots = self.service.get_status().get("available_slots") or []
        if not slots:
            try:
                res = self.service.check_now_sync()
                slots = res.get("slots") or []
            except Exception:
                slots = []
        if not slots:
            self._send_tg_like(
                base_host,
                token,
                chat_id,
                "⚠️ در حال حاضر نوبت خالی روی سایت باز نیست که خروجی گرفته شود.",
                inline_rows=self.router.main_inline_rows(),
            )
            return
        try:
            out_path = export_slots_excel(slots) if kind == "export:excel" else export_slots_pdf(slots)
            self._send_tg_document(base_host, token, chat_id, out_path)
        except Exception as exc:
            self._send_tg_like(base_host, token, chat_id, f"❌ خطا در ساخت فایل: {exc}")

    def _poll_telegram(self) -> None:
        while not self._stop_event.is_set():
            cfg = self.service.settings
            token = cfg.telegram_bot_token.strip()
            if not token:
                time.sleep(3)
                continue
            try:
                resp = _http_json(
                    f"https://api.telegram.org/bot{token}/getUpdates",
                    {"offset": self._tg_offset, "timeout": 25},
                    timeout=30,
                )
                nxt = self._process_tg_like_updates("https://api.telegram.org", token, cfg.telegram_chat_id.strip(), resp.get("result", []))
                if nxt:
                    self._tg_offset = max(self._tg_offset, nxt)
            except Exception:
                time.sleep(4)

    def _poll_bale(self) -> None:
        while not self._stop_event.is_set():
            cfg = self.service.settings
            token = cfg.bale_bot_token.strip()
            if not token:
                time.sleep(3)
                continue
            try:
                resp = _http_json(
                    f"https://tapi.bale.ai/bot{token}/getUpdates",
                    {"offset": self._bale_offset, "timeout": 25},
                    timeout=30,
                )
                nxt = self._process_tg_like_updates("https://tapi.bale.ai", token, cfg.bale_chat_id.strip(), resp.get("result", []))
                if nxt:
                    self._bale_offset = max(self._bale_offset, nxt)
            except Exception:
                time.sleep(4)

    def _poll_rubika(self) -> None:
        while not self._stop_event.is_set():
            cfg = self.service.settings
            token = cfg.rubika_bot_token.strip()
            if not token:
                time.sleep(3)
                continue
            try:
                payload: dict[str, Any] = {"limit": 20}
                if self._rubika_next_id:
                    payload["start_id"] = self._rubika_next_id
                resp = _http_json(f"https://botapi.rubika.ir/v3/{token}/getUpdates", payload, timeout=30)
                data = resp.get("data") or resp
                if data.get("next_start_id"):
                    self._rubika_next_id = str(data["next_start_id"])
                for upd in data.get("updates", []):
                    msg = upd.get("new_message") or upd.get("message") or {}
                    aux = msg.get("aux_data") or {}
                    action = aux.get("button_id") or msg.get("text")
                    chat_id = upd.get("chat_id") or msg.get("chat_id")
                    if action and chat_id:
                        reply, rows = self.router.handle_action(str(action), str(chat_id), cfg.rubika_chat_id.strip())
                        self._send_rubika(token, str(chat_id), reply, inline_rows=rows)
            except Exception:
                time.sleep(4)

    def _poll_soroush(self) -> None:
        while not self._stop_event.is_set():
            cfg = self.service.settings
            token = cfg.soroush_bot_token.strip()
            if not token:
                time.sleep(3)
                continue
            try:
                resp = _http_json(
                    f"https://api.splus.ir/bot{token}/getUpdates",
                    {"offset": self._soroush_offset, "timeout": 25},
                    timeout=30,
                )
                nxt = self._process_tg_like_updates(
                    "https://api.splus.ir", token, cfg.soroush_chat_id.strip(), resp.get("result", [])
                )
                if nxt:
                    self._soroush_offset = max(self._soroush_offset, nxt)
            except Exception:
                time.sleep(4)
