#!/usr/bin/env python3
"""
Calendar notifications:
  18:00 — tomorrow's meetings
  11:00 — this week's meetings
"""
import os
import subprocess
import sys
import requests
from datetime import datetime, timedelta
from typing import Optional
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]

WORK_CALENDARS = ["Рабочий", "Календарь", "Calendar"]


def run_applescript(script: str) -> str:
    r = subprocess.run(["osascript", "-e", script],
                       capture_output=True, text=True, timeout=60)
    return r.stdout.strip()


def parse_as_date(s: str) -> Optional[datetime]:
    for fmt in ("%A, %d %B %Y at %H:%M:%S",
                "%A, %B %d, %Y at %I:%M:%S %p"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
    return None


def get_events(date_from: datetime, date_to: datetime) -> list:
    cal_names = ", ".join(f'"{c}"' for c in WORK_CALENDARS)
    script = f"""
tell application "Calendar"
    activate
    delay 1
    set out to ""
    set d1 to date "{date_from.strftime('%A, %d %B %Y at %H:%M:%S')}"
    set d2 to date "{date_to.strftime('%A, %d %B %Y at %H:%M:%S')}"
    repeat with calName in {{{cal_names}}}
        try
            set c to calendar calName
            repeat with e in (every event of c whose start date >= d1 and start date <= d2)
                set out to out & (summary of e) & "§" & ((start date of e) as string) & "§" & ((end date of e) as string) & "¶"
            end repeat
        end try
    end repeat
    return out
end tell
"""
    raw = run_applescript(script)
    events = []
    for line in raw.split("¶"):
        line = line.strip()
        if not line:
            continue
        parts = line.split("§")
        if len(parts) < 3:
            continue
        start = parse_as_date(parts[1])
        end   = parse_as_date(parts[2])
        if start and end:
            events.append({"title": parts[0], "start": start, "end": end})
    events.sort(key=lambda e: e["start"])
    return events


def format_day(events: list, label: str) -> str:
    if not events:
        return f"{label}: встреч нет"
    lines = [f"<b>{label}:</b>"]
    for e in events:
        duration = int((e["end"] - e["start"]).total_seconds() / 60)
        dur_str = f"{duration // 60}ч" if duration >= 60 else f"{duration}мин"
        lines.append(f"  • {e['start'].strftime('%H:%M')} — {e['title']} ({dur_str})")
    return "\n".join(lines)


def send(text: str) -> None:
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
        timeout=15,
    )


def notify_evening() -> None:
    """18:00 — tomorrow's meetings."""
    tomorrow = datetime.now().replace(hour=0, minute=0, second=0) + timedelta(days=1)
    tomorrow_end = tomorrow.replace(hour=23, minute=59, second=59)
    events = get_events(tomorrow, tomorrow_end)

    date_label = tomorrow.strftime("%-d %B, %A")
    if not events:
        send(f"📅 <b>Завтра ({date_label})</b> — встреч нет. Свободный день!")
    else:
        lines = [f"📅 <b>Встречи на завтра ({date_label}):</b>\n"]
        for e in events:
            duration = int((e["end"] - e["start"]).total_seconds() / 60)
            dur_str = f"{duration // 60}ч" if duration >= 60 else f"{duration}мин"
            lines.append(f"• {e['start'].strftime('%H:%M')} — <b>{e['title']}</b> ({dur_str})")
        send("\n".join(lines))


def notify_morning() -> None:
    """11:00 — this week's meetings grouped by day."""
    today = datetime.now().replace(hour=0, minute=0, second=0)
    week_end = today + timedelta(days=7)
    events = get_events(today, week_end)

    if not events:
        send("📅 <b>Встречи на неделю</b> — ничего запланировано. Чистая неделя!")
        return

    lines = ["📅 <b>Встречи на неделю:</b>\n"]
    current_day = None
    now = datetime.now()

    for e in events:
        day = e["start"].date()
        if day != current_day:
            current_day = day
            delta = (day - now.date()).days
            if delta == 0:
                label = "Сегодня"
            elif delta == 1:
                label = "Завтра"
            else:
                label = e["start"].strftime("%-d %B, %A")
            lines.append(f"\n<b>{label}:</b>")
        duration = int((e["end"] - e["start"]).total_seconds() / 60)
        dur_str = f"{duration // 60}ч" if duration >= 60 else f"{duration}мин"
        lines.append(f"  • {e['start'].strftime('%H:%M')} — {e['title']} ({dur_str})")

    send("\n".join(lines))


if __name__ == "__main__":
    hour = datetime.now().hour
    if hour < 14:
        notify_morning()   # 11:00 run
    else:
        notify_evening()   # 18:00 run
