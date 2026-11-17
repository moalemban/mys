from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


class StateStorage:
    """Persists JSON state and booking history to Supabase REST API with local JSON fallback."""

    def __init__(self) -> None:
        self.supabase_url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
        self.supabase_key = os.getenv("SUPABASE_KEY", "").strip()

    @property
    def enabled(self) -> bool:
        return bool(self.supabase_url and self.supabase_key)

    def _headers(self, prefer: str = "") -> dict[str, str]:
        h = {
            "apikey": self.supabase_key,
            "Authorization": f"Bearer {self.supabase_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if prefer:
            h["Prefer"] = prefer
        return h

    def get_json(self, key: str, local_path: Path) -> dict[str, Any] | None:
        if self.enabled:
            try:
                q = urllib.parse.quote(key, safe="")
                url = f"{self.supabase_url}/rest/v1/app_kv?key=eq.{q}&select=value"
                req = urllib.request.Request(url, headers=self._headers(), method="GET")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    rows = json.loads(resp.read().decode("utf-8"))
                    if rows and isinstance(rows, list) and "value" in rows[0]:
                        return rows[0]["value"]
            except Exception:
                pass
        if local_path.exists():
            try:
                return json.loads(local_path.read_text(encoding="utf-8"))
            except Exception:
                return None
        return None

    def set_json(self, key: str, value: dict[str, Any], local_path: Path) -> None:
        local_path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        if self.enabled:
            try:
                url = f"{self.supabase_url}/rest/v1/app_kv?on_conflict=key"
                payload = json.dumps({"key": key, "value": value}).encode("utf-8")
                req = urllib.request.Request(
                    url,
                    data=payload,
                    headers=self._headers(prefer="resolution=merge-duplicates,return=minimal"),
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10):
                    pass
            except Exception:
                pass

    def add_booking_history(self, entry: dict[str, str]) -> None:
        if not self.enabled:
            return
        try:
            url = f"{self.supabase_url}/rest/v1/booking_history"
            payload = json.dumps({
                "booked_at": entry.get("time", ""),
                "user_name": entry.get("user", ""),
                "station": entry.get("station", ""),
                "slot": entry.get("slot", ""),
                "fuel": entry.get("fuel", ""),
                "tracking": entry.get("tracking", ""),
            }).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=payload,
                headers=self._headers(prefer="return=minimal"),
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10):
                pass
        except Exception:
            pass
