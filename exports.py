from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from playwright.sync_api import sync_playwright

from fuel_service import launch_browser

EXPORT_DIR = Path("exports")


def export_slots_excel(slots: list[dict[str, Any]], filename: str = "available_slots.xlsx") -> Path:
    """Export discovered open fuel slots to a styled RTL Excel (.xlsx) file."""
    EXPORT_DIR.mkdir(exist_ok=True)
    out_path = EXPORT_DIR / filename

    wb = Workbook()
    ws = wb.active
    ws.title = "نوبت‌های خالی سوخت"
    ws.views.sheetView[0].rightToLeft = True

    headers = ["ردیف", "روز", "جایگاه", "تعداد ساعت خالی", "ساعت‌های خالی", "لینک رزرو"]
    ws.append(headers)

    header_fill = PatternFill(start_color="12306B", end_color="12306B", fill_type="solid")
    header_font = Font(name="Tahoma", size=11, bold=True, color="FFFFFF")
    cell_font = Font(name="Tahoma", size=10)

    for col_idx in range(1, len(headers) + 1):
        c = ws.cell(row=1, column=col_idx)
        c.fill = header_fill
        c.font = header_font
        c.alignment = Alignment(horizontal="center", vertical="center")

    for idx, s in enumerate(slots, start=1):
        times = s.get("times", [])
        row = [
            idx,
            s.get("day", "-"),
            s.get("station", "-"),
            len(times),
            "، ".join(times),
            s.get("url", ""),
        ]
        ws.append(row)
        for col_idx in range(1, len(headers) + 1):
            c = ws.cell(row=idx + 1, column=col_idx)
            c.font = cell_font
            c.alignment = Alignment(horizontal="center" if col_idx != 5 else "right", vertical="center", wrap_text=True)

    widths = {1: 8, 2: 22, 3: 26, 4: 18, 5: 48, 6: 42}
    for col_idx, width in widths.items():
        col_letter = ws.cell(row=1, column=col_idx).column_letter
        ws.column_dimensions[col_letter].width = width

    wb.save(out_path)
    return out_path


def export_slots_pdf(slots: list[dict[str, Any]], filename: str = "available_slots.pdf") -> Path:
    """Export discovered open fuel slots to a styled RTL Persian PDF via Playwright."""
    EXPORT_DIR.mkdir(exist_ok=True)
    out_path = EXPORT_DIR / filename

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows_html = ""
    if not slots:
        rows_html = '<tr><td colspan="5" style="text-align:center;">نوبت خالی ثبت نشده است.</td></tr>'
    else:
        for idx, s in enumerate(slots, start=1):
            times = s.get("times", [])
            badges = " ".join(f'<span class="badge">{html.escape(t)}</span>' for t in times)
            rows_html += (
                f"<tr>"
                f"<td>{idx}</td>"
                f"<td>{html.escape(str(s.get('day', '-')))}</td>"
                f"<td><strong>{html.escape(str(s.get('station', '-')))}</strong></td>"
                f"<td>{len(times)}</td>"
                f"<td>{badges}</td>"
                f"</tr>"
            )

    doc_html = f"""<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
<meta charset="UTF-8" />
<style>
  body {{ font-family: Tahoma, "Segoe UI", sans-serif; margin: 24px; color: #0f172a; }}
  h1 {{ font-size: 18px; color: #12306b; margin-bottom: 4px; }}
  .meta {{ font-size: 12px; color: #475569; margin-bottom: 16px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
  th {{ background: #12306b; color: #fff; padding: 10px 8px; border: 1px solid #cbd5e1; }}
  td {{ padding: 8px; border: 1px solid #cbd5e1; vertical-align: middle; text-align: center; }}
  td:last-child {{ text-align: right; }}
  .badge {{ display: inline-block; background: #e0f2fe; color: #0369a1; border: 1px solid #7dd3fc; padding: 2px 7px; border-radius: 6px; margin: 2px; font-weight: bold; }}
</style>
</head>
<body>
  <h1>⛽ گزارش نوبت‌های خالی سوخت</h1>
  <div class="meta">زمان تهیه گزارش: {now_str} | تعداد جایگاه/روز دارای ظرفیت: {len(slots)}</div>
  <table>
    <thead>
      <tr>
        <th>ردیف</th>
        <th>روز</th>
        <th>جایگاه</th>
        <th>تعداد</th>
        <th>ساعت‌های خالی</th>
      </tr>
    </thead>
    <tbody>
      {rows_html}
    </tbody>
  </table>
</body>
</html>"""

    with sync_playwright() as p:
        browser = launch_browser(p, headless=True)
        try:
            page = browser.new_page()
            page.set_content(doc_html, wait_until="load")
            page.pdf(path=str(out_path), format="A4", print_background=True)
        finally:
            browser.close()

    return out_path
