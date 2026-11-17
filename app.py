from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

from auth import AuthManager
from fuel_service import FuelBotService, SCREENSHOT_PATH
from messengers import MultiMessengerHub

TEMPLATES_DIR = Path(__file__).parent / "templates"
SESSION_COOKIE = "fuel_bot_session"

auth = AuthManager()
service = FuelBotService()
hub = MultiMessengerHub(service, auth=auth)
service.set_notifier(hub.notify)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    hub.start()
    yield
    service.stop()
    hub.stop()


app = FastAPI(title="پنل هوشمند نوبت‌دهی سوخت", lifespan=lifespan)

allowed_origins = [
    o.strip()
    for o in os.getenv("ALLOWED_ORIGINS", "https://mys-sigma.vercel.app").split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

PUBLIC_PATHS = {"/login", "/miniapp", "/api/auth/login", "/manifest.json", "/sw.js", "/api/bot/avatar"}


@app.middleware("http")
async def require_auth(request: Request, call_next):
    path = request.url.path
    if path in PUBLIC_PATHS:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    token = request.cookies.get(SESSION_COOKIE)
    user = auth.verify_session_token(token)
    if not user:
        if path.startswith("/api/"):
            return JSONResponse({"detail": "لطفاً ابتدا وارد شوید"}, status_code=401)
        return RedirectResponse(url="/login", status_code=302)

    request.state.user = user
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    if path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/login", response_class=HTMLResponse)
def login_page() -> str:
    return (TEMPLATES_DIR / "login.html").read_text(encoding="utf-8")


@app.post("/api/auth/login")
def api_login(request: Request, payload: dict[str, Any] = Body(...)):
    client_ip = request.client.host if request.client else "unknown"
    try:
        auth.check_rate_limit(client_ip)
    except ValueError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc

    user = auth.authenticate(payload.get("username", ""), payload.get("password", ""))
    if not user:
        auth.record_failure(client_ip)
        raise HTTPException(status_code=401, detail="نام کاربری یا رمز عبور اشتباه است.")
    auth.clear_failures(client_ip)
    token = auth.create_session_token(user["username"], user["role"])
    resp = JSONResponse({"ok": True, "user": user})
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=86400 * 7)
    return resp


@app.post("/api/auth/logout")
def api_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.get("/api/auth/me")
def api_me(request: Request):
    return {"user": request.state.user}


@app.post("/api/auth/change-password")
def api_change_password(request: Request, payload: dict[str, Any] = Body(...)):
    current_user = request.state.user
    target_user = (payload.get("username") or current_user["username"]).strip()
    if target_user != current_user["username"] and current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="فقط ادمین می‌تواند رمز دیگران را تغییر دهد.")
    try:
        auth.change_password(target_user, payload.get("new_password", ""))
        return {"ok": True, "message": f"رمز عبور کاربر {target_user} تغییر یافت."}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/admin/users")
def admin_list_users(request: Request):
    if request.state.user["role"] != "admin":
        raise HTTPException(status_code=403, detail="دسترسی فقط برای ادمین")
    return {"users": auth.list_users()}


@app.post("/api/admin/users")
def admin_create_user(request: Request, payload: dict[str, Any] = Body(...)):
    if request.state.user["role"] != "admin":
        raise HTTPException(status_code=403, detail="دسترسی فقط برای ادمین")
    try:
        u = auth.create_user(
            payload.get("username", ""),
            payload.get("password", ""),
            payload.get("role", "user"),
        )
        return {"ok": True, "message": f"کاربر {u['username']} ساخته شد", "user": u}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/admin/users/{username}")
def admin_delete_user(request: Request, username: str):
    if request.state.user["role"] != "admin":
        raise HTTPException(status_code=403, detail="دسترسی فقط برای ادمین")
    try:
        auth.delete_user(username)
        return {"ok": True, "message": f"کاربر {username} حذف شد"}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (TEMPLATES_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/miniapp", response_class=HTMLResponse)
def miniapp() -> str:
    return (TEMPLATES_DIR / "miniapp.html").read_text(encoding="utf-8")


@app.get("/manifest.json")
def pwa_manifest():
    return JSONResponse(
        {
            "name": "ربات نوبت‌دهی سوخت",
            "short_name": "نوبت سوخت",
            "start_url": "/",
            "display": "standalone",
            "background_color": "#0f172a",
            "theme_color": "#12306b",
            "dir": "rtl",
            "lang": "fa",
            "icons": [{"src": "/api/bot/avatar", "sizes": "512x512", "type": "image/png"}],
        }
    )


@app.get("/sw.js")
def pwa_service_worker():
    return PlainTextResponse(
        "self.addEventListener('fetch', function(event) {});",
        media_type="application/javascript",
    )


@app.get("/api/status")
def get_status() -> dict[str, Any]:
    return service.get_status()


@app.post("/api/start")
def start_bot() -> dict[str, Any]:
    try:
        started = service.start()
        return {"ok": True, "message": "پایش شروع شد" if started else "ربات از قبل فعال است"}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/stop")
def stop_bot() -> dict[str, Any]:
    stopped = service.stop()
    return {"ok": True, "message": "ربات متوقف شد" if stopped else "ربات در حال اجرا نبود"}


@app.post("/api/check-now")
def check_now() -> dict[str, Any]:
    msg = service.trigger_check_now()
    return {"ok": True, "message": msg}


@app.post("/api/settings")
def update_settings(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    service.update_settings(payload)
    return {"ok": True, "message": "تنظیمات ذخیره شد"}


@app.post("/api/profiles")
def upsert_profile(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        prof = service.upsert_profile(
            personnel_code=payload.get("personnel_code", ""),
            national_code=payload.get("national_code", ""),
            first_name=payload.get("first_name", ""),
            last_name=payload.get("last_name", ""),
            label=payload.get("label", ""),
            activate=bool(payload.get("activate", True)),
        )
        return {"ok": True, "message": f"کاربر {prof['label']} ذخیره شد", "profile": prof}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/profiles/{profile_id}/select")
def select_profile(profile_id: str) -> dict[str, Any]:
    try:
        prof = service.select_profile(profile_id)
        return {"ok": True, "message": f"کاربر فعال: {prof['label']}"}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.delete("/api/profiles/{profile_id}")
def delete_profile(profile_id: str) -> dict[str, Any]:
    service.delete_profile(profile_id)
    return {"ok": True, "message": "پروفایل حذف شد"}


@app.post("/api/confirm-late")
def confirm_late(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    msg = service.confirm_late_booking(
        chosen_time=payload.get("time"),
        station_url=payload.get("url"),
    )
    return {"ok": True, "message": msg}


@app.post("/api/decline-late")
def decline_late() -> dict[str, Any]:
    msg = service.decline_late_booking()
    return {"ok": True, "message": msg}


@app.post("/api/direct/fetch")
def direct_fetch(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        return service.direct_fetch(
            personnel_code=payload.get("personnel_code", ""),
            national_code=payload.get("national_code", ""),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/direct/book")
def direct_book(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        return service.direct_book(
            personnel_code=payload.get("personnel_code", ""),
            national_code=payload.get("national_code", ""),
            station_url=payload.get("station_url", ""),
            chosen_time=payload.get("time", ""),
            fuel_amount=str(payload.get("fuel_amount", "40")),
            first_name=payload.get("first_name", ""),
            last_name=payload.get("last_name", ""),
            rec_national_code=payload.get("rec_national_code", ""),
            dry_run=bool(payload.get("dry_run", False)),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/direct/cancel")
def direct_cancel(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    try:
        return service.direct_cancel(
            personnel_code=payload.get("personnel_code", ""),
            national_code=payload.get("national_code", ""),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/group/toggle/{profile_id}")
def group_toggle(profile_id: str) -> dict[str, Any]:
    ids = service.toggle_group_profile(profile_id)
    return {"ok": True, "group_profile_ids": ids}


@app.post("/api/group/book")
def group_book(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    try:
        results = service.batch_book(payload.get("profile_ids"))
        return {"ok": True, "results": results}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/screenshot")
def get_screenshot():
    if not SCREENSHOT_PATH.exists():
        raise HTTPException(status_code=404, detail="اسکرین‌شات موجود نیست")
    return FileResponse(SCREENSHOT_PATH, media_type="image/png")


@app.get("/api/bot/avatar")
def get_bot_avatar():
    avatar = Path("bot_avatar.png")
    if not avatar.exists():
        raise HTTPException(status_code=404, detail="آواتار موجود نیست")
    return FileResponse(avatar, media_type="image/png")


@app.post("/api/bot/sync-profile")
def sync_bot_profile():
    return {"ok": True, "results": hub.sync_bot_profile()}


@app.get("/api/export/excel")
def export_excel():
    from exports import export_slots_excel

    slots = service.get_status().get("available_slots") or []
    out_path = export_slots_excel(slots)
    return FileResponse(
        out_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename="available_slots.xlsx",
    )


@app.get("/api/export/pdf")
def export_pdf():
    from exports import export_slots_pdf

    slots = service.get_status().get("available_slots") or []
    out_path = export_slots_pdf(slots)
    return FileResponse(out_path, media_type="application/pdf", filename="available_slots.pdf")


if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host=host, port=port)
