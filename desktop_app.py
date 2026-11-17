from __future__ import annotations

import socket
import threading
import tkinter as tk
import webbrowser
from tkinter import scrolledtext, ttk

import uvicorn

from app import app, hub, service


def get_local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


class FuelDesktopApp:
    """Native Windows GUI launcher for the Fuel Bot, Web/PWA Dashboard, and Messengers."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("نوبت‌دهی هوشمند سوخت (ویندوز + موبایل + پیام‌رسان‌ها)")
        self.root.geometry("760x540")
        self.root.configure(bg="#0f172a")

        self.local_ip = get_local_ip()
        self._build_ui()
        self._start_server()
        self._refresh_loop()

    def _build_ui(self) -> None:
        top = tk.Frame(self.root, bg="#1e293b", padx=14, pady=12)
        top.pack(fill="x", padx=12, pady=(12, 6))

        self.status_lbl = tk.Label(
            top, text="وضعیت: آماده", font=("Tahoma", 11, "bold"), fg="#f8fafc", bg="#1e293b"
        )
        self.status_lbl.pack(side="right")

        url_lbl = tk.Label(
            top,
            text=f"آدرس موبایل (PWA): http://{self.local_ip}:8000",
            font=("Tahoma", 9),
            fg="#60a5fa",
            bg="#1e293b",
        )
        url_lbl.pack(side="left")

        btns = tk.Frame(self.root, bg="#0f172a", pady=6)
        btns.pack(fill="x", padx=12)

        tk.Button(
            btns, text="▶ شروع پایش", bg="#10b981", fg="white", font=("Tahoma", 10, "bold"),
            padx=14, pady=6, relief="flat", command=self._on_start
        ).pack(side="right", padx=4)

        tk.Button(
            btns, text="⏹ توقف", bg="#ef4444", fg="white", font=("Tahoma", 10, "bold"),
            padx=14, pady=6, relief="flat", command=self._on_stop
        ).pack(side="right", padx=4)

        tk.Button(
            btns, text="🔍 بررسی فوری", bg="#3b82f6", fg="white", font=("Tahoma", 10, "bold"),
            padx=14, pady=6, relief="flat", command=self._on_check
        ).pack(side="right", padx=4)

        tk.Button(
            btns, text="🌐 باز کردن داشبورد کامل / تنظیمات", bg="#6366f1", fg="white",
            font=("Tahoma", 10, "bold"), padx=14, pady=6, relief="flat",
            command=lambda: webbrowser.open("http://127.0.0.1:8000")
        ).pack(side="left", padx=4)

        mode_frame = tk.Frame(self.root, bg="#0f172a", pady=4)
        mode_frame.pack(fill="x", padx=12)

        self.mode_var = tk.StringVar(value=service.settings.mode)
        ttk.Radiobutton(
            mode_frame, text="فقط اطلاع‌رسانی سریع", value="notify_only",
            variable=self.mode_var, command=self._on_mode_change
        ).pack(side="right", padx=8)
        ttk.Radiobutton(
            mode_frame, text="ثبت خودکار نوبت", value="auto_book",
            variable=self.mode_var, command=self._on_mode_change
        ).pack(side="right", padx=8)

        self.logs_box = scrolledtext.ScrolledText(
            self.root, bg="#020617", fg="#cbd5e1", font=("Consolas", 9), height=22
        )
        self.logs_box.pack(fill="both", expand=True, padx=12, pady=(6, 12))

    def _start_server(self) -> None:
        hub.start()
        t = threading.Thread(
            target=lambda: uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning"),
            daemon=True,
        )
        t.start()

    def _on_start(self) -> None:
        try:
            service.start()
        except Exception as exc:
            service.log(f"خطا: {exc}")

    def _on_stop(self) -> None:
        service.stop()

    def _on_check(self) -> None:
        service.trigger_check_now()

    def _on_mode_change(self) -> None:
        service.update_settings({"mode": self.mode_var.get()})

    def _refresh_loop(self) -> None:
        st = service.get_status()
        run_txt = "در حال پایش" if st["running"] else "متوقف"
        self.status_lbl.config(text=f"وضعیت: {run_txt} | بررسی #{st['attempt_count']} | {st['last_status']}")

        self.logs_box.delete("1.0", tk.END)
        self.logs_box.insert(tk.END, "\n".join(st["logs"]))
        self.logs_box.see(tk.END)

        self.root.after(2000, self._refresh_loop)


def main() -> None:
    root = tk.Tk()
    FuelDesktopApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
