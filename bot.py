#!/usr/bin/env python3
"""
Interactive Telegram bot.
📬 Письма  — morning email digest
📅 Встречи — Apple Calendar view + natural-language event creation
"""

import json
import os
import re
import subprocess
import sys
import time
import logging
from datetime import datetime, timedelta
from typing import Optional

import requests
from google import genai
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

BOT_TOKEN  = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID    = int(os.environ["TELEGRAM_CHAT_ID"])
GEMINI_KEY = os.environ["GEMINI_API_KEY"]
AGENT      = os.path.join(os.path.dirname(__file__), "email_agent.py")

AI  = genai.Client(api_key=GEMINI_KEY)
API = f"https://api.telegram.org/bot{BOT_TOKEN}"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

KEYBOARD = {
    "keyboard": [[{"text": "📬 Письма"}, {"text": "📅 Встречи"}]],
    "resize_keyboard": True,
    "persistent": True,
}


# ── Telegram helpers ──────────────────────────────────────────────────────────

def send(text: str, parse_mode: str = "HTML") -> None:
    limit = 4000
    while text:
        chunk, text = text[:limit], text[limit:]
        requests.post(f"{API}/sendMessage", json={
            "chat_id":      CHAT_ID,
            "text":         chunk,
            "parse_mode":   parse_mode,
            "reply_markup": KEYBOARD,
        }, timeout=15)


def send_plain(text: str) -> None:
    send(text, parse_mode=None)


# ── Apple Calendar via AppleScript ────────────────────────────────────────────

WORK_CALENDARS = ["Рабочий", "Календарь", "Calendar"]

SCRIPT_GET_EVENTS = """
tell application "Calendar"
    activate
    delay 1
    set out to ""
    set d1 to current date
    set d2 to d1 + {days} * days
    repeat with calName in {{{cal_names}}}
        try
            set c to calendar calName
            repeat with e in (every event of c whose start date ≥ d1 and start date ≤ d2)
                set out to out & (summary of e) & "§" & ((start date of e) as string) & "§" & ((end date of e) as string) & "¶"
            end repeat
        end try
    end repeat
    return out
end tell
"""

SCRIPT_ADD_EVENT = """
tell application "Calendar"
    activate
    delay 1
    tell calendar "Рабочий"
        make new event at end with properties {{summary:"{title}", start date:date "{start}", end date:date "{end}", description:"{notes}"}}
    end tell
    reload calendars
end tell
return "ok"
"""

SCRIPT_FIND_EVENT = """
tell application "Calendar"
    activate
    delay 1
    set out to ""
    set searchStr to "{search}"
    set d1 to date "{date_from}"
    set d2 to date "{date_to}"
    repeat with calName in {{{cal_names}}}
        try
            set c to calendar calName
            repeat with e in (every event of c whose start date >= d1 and start date <= d2)
                if summary of e contains searchStr then
                    set out to out & (summary of e) & "§" & ((start date of e) as string) & "§" & ((end date of e) as string) & "§" & calName & "¶"
                end if
            end repeat
        end try
    end repeat
    return out
end tell
"""

SCRIPT_DELETE_EVENT = """
tell application "Calendar"
    activate
    delay 1
    set searchStr to "{search}"
    set d1 to date "{date_from}"
    set d2 to date "{date_to}"
    set deleted to 0
    repeat with calName in {{{cal_names}}}
        try
            set c to calendar calName
            set evs to (every event of c whose start date >= d1 and start date <= d2)
            repeat with e in evs
                if summary of e contains searchStr then
                    delete e
                    set deleted to deleted + 1
                    exit repeat
                end if
            end repeat
        end try
        if deleted > 0 then exit repeat
    end repeat
    reload calendars
    return deleted as string
end tell
"""

SCRIPT_EDIT_EVENT = """
tell application "Calendar"
    activate
    delay 1
    set searchStr to "{search}"
    set d1 to date "{date_from}"
    set d2 to date "{date_to}"
    set found to 0
    repeat with calName in {{{cal_names}}}
        try
            set c to calendar calName
            set evs to (every event of c whose start date >= d1 and start date <= d2)
            repeat with e in evs
                if summary of e contains searchStr then
                    {edit_commands}
                    set found to 1
                    exit repeat
                end if
            end repeat
        end try
        if found > 0 then exit repeat
    end repeat
    reload calendars
    return found as string
end tell
"""

SCRIPT_ADD_REMINDER = """
tell application "Calendar"
    activate
    delay 1
    set searchStr to "{search}"
    set d1 to date "{date_from}"
    set d2 to date "{date_to}"
    set found to 0
    repeat with calName in {{{cal_names}}}
        try
            set c to calendar calName
            set evs to (every event of c whose start date >= d1 and start date <= d2)
            repeat with e in evs
                if summary of e contains searchStr then
                    tell e
                        make new sound alarm at end with properties {{trigger interval:{minutes}}}
                    end tell
                    set found to 1
                    exit repeat
                end if
            end repeat
        end try
        if found > 0 then exit repeat
    end repeat
    reload calendars
    return found as string
end tell
"""


def run_applescript(script: str) -> str:
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True, timeout=60,
    )
    return result.stdout.strip()


def parse_as_date(s: str) -> Optional[datetime]:
    """Parse AppleScript date string: 'Wednesday, 20 May 2026 at 12:00:00'"""
    s = s.strip()
    for fmt in (
        "%A, %d %B %Y at %H:%M:%S",   # Wednesday, 20 May 2026 at 12:00:00
        "%A, %B %d, %Y at %I:%M:%S %p",  # Wednesday, May 20, 2026 at 12:00:00 PM
        "%A, %d %B %Y г. в %H:%M:%S",
    ):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def get_events(days: int = 7) -> list:
    cal_names = ", ".join(f'"{c}"' for c in WORK_CALENDARS)
    raw = run_applescript(SCRIPT_GET_EVENTS.format(days=days, cal_names=cal_names))
    events = []
    for line in raw.split("¶"):
        line = line.strip()
        if not line:
            continue
        parts = line.split("§")
        if len(parts) < 3:
            continue
        title, start_str, end_str = parts[0], parts[1], parts[2]
        start = parse_as_date(start_str)
        end   = parse_as_date(end_str)
        if not start or not end:
            log.warning(f"Cannot parse date: {start_str!r}")
            continue
        events.append({"title": title, "start": start, "end": end})
    events.sort(key=lambda e: e["start"])
    return events


def format_events(events: list, days: int = 7) -> str:
    if not events:
        return "📅 Встреч на ближайшие {} дней нет.".format(days)

    lines = [f"<b>📅 Встречи на {days} дней</b>\n"]
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
                label = e["start"].strftime("%-d %B")
            lines.append(f"\n<b>{label}:</b>")

        duration = int((e["end"] - e["start"]).total_seconds() / 60)
        dur_str = f"{duration // 60}ч" if duration >= 60 else f"{duration}мин"
        lines.append(f"  • {e['start'].strftime('%H:%M')} — {e['title']} ({dur_str})")

    return "\n".join(lines)


def as_fmt(dt: datetime) -> str:
    return dt.strftime("%A, %d %B %Y at %H:%M:%S")


def add_event(title: str, start: datetime, end: datetime, notes: str = "") -> bool:
    script = SCRIPT_ADD_EVENT.format(
        title=title.replace('"', "'"),
        start=as_fmt(start),
        end=as_fmt(end),
        notes=notes.replace('"', "'"),
    )
    return run_applescript(script) == "ok"


def _search_window(date_hint: Optional[str]) -> tuple:
    """Return (date_from, date_to) strings for AppleScript search."""
    if date_hint:
        try:
            d = datetime.fromisoformat(date_hint).replace(hour=0, minute=0, second=0)
            return as_fmt(d), as_fmt(d.replace(hour=23, minute=59, second=59))
        except Exception:
            pass
    now = datetime.now().replace(hour=0, minute=0, second=0)
    return as_fmt(now), as_fmt(now + timedelta(days=30))


def delete_event(search: str, date_hint: Optional[str] = None) -> bool:
    cal_names = ", ".join(f'"{c}"' for c in WORK_CALENDARS)
    d_from, d_to = _search_window(date_hint)
    script = SCRIPT_DELETE_EVENT.format(
        search=search.replace('"', "'"),
        date_from=d_from, date_to=d_to,
        cal_names=cal_names,
    )
    return run_applescript(script).strip() == "1"


def edit_event(search: str, date_hint: Optional[str],
               new_start: Optional[datetime], new_duration: Optional[int],
               new_title: Optional[str]) -> bool:
    cal_names = ", ".join(f'"{c}"' for c in WORK_CALENDARS)
    d_from, d_to = _search_window(date_hint)

    cmds = []
    if new_title:
        cmds.append(f'set summary of e to "{new_title.replace(chr(34), chr(39))}"')
    if new_start:
        cmds.append(f'set start date of e to date "{as_fmt(new_start)}"')
        end_dt = new_start + timedelta(minutes=new_duration or 60)
        cmds.append(f'set end date of e to date "{as_fmt(end_dt)}"')
    elif new_duration:
        # shift end date only
        cmds.append(f'set end date of e to (start date of e) + {new_duration * 60}')

    if not cmds:
        return False

    script = SCRIPT_EDIT_EVENT.format(
        search=search.replace('"', "'"),
        date_from=d_from, date_to=d_to,
        cal_names=cal_names,
        edit_commands="\n                    ".join(cmds),
    )
    return run_applescript(script).strip() == "1"


def add_reminder(search: str, date_hint: Optional[str], minutes_before: int) -> bool:
    cal_names = ", ".join(f'"{c}"' for c in WORK_CALENDARS)
    d_from, d_to = _search_window(date_hint)
    script = SCRIPT_ADD_REMINDER.format(
        search=search.replace('"', "'"),
        date_from=d_from, date_to=d_to,
        cal_names=cal_names,
        minutes=-abs(minutes_before),  # negative = before event
    )
    return run_applescript(script).strip() == "1"


# ── Gemini NLP ────────────────────────────────────────────────────────────────

NLP_PROMPT = """\
Сегодня: {today}. Время: {time}. Завтра: {tomorrow}.

Пользователь написал: «{text}»

Верни JSON-массив действий (только JSON, без markdown, без пояснений).

Поддерживаемые типы:

Создать встречу:
{{"type":"create_event","title":"название","start":"YYYY-MM-DDTHH:MM","duration_minutes":60,"notes":""}}

Показать встречи:
{{"type":"show_events","days":7}}

Удалить встречу:
{{"type":"delete_event","search":"ключевое слово из названия","date":"YYYY-MM-DD"}}

Редактировать встречу (перенести время/дату, переименовать, изменить длительность):
{{"type":"edit_event","search":"ключевое слово","date":"YYYY-MM-DD","new_start":"YYYY-MM-DDTHH:MM","new_duration_minutes":60,"new_title":null}}

Поставить напоминание:
{{"type":"add_reminder","search":"ключевое слово","date":"YYYY-MM-DD","minutes_before":30}}

Непонятный запрос:
{{"type":"unknown","reply":"текст ответа на русском"}}

Правила:
- В одном сообщении может быть несколько действий — верни все
- "завтра" = {tomorrow}, "в понедельник/вторник/..." = ближайший такой день
- Если время не указано — ставь 09:00, длительность по умолчанию — 60 минут
- В delete/edit/reminder: "search" — ключевое слово из названия (например "Альфа", "Газпром", "совет")
- "date" — дата встречи которую ищем (YYYY-MM-DD), если не указана — null
- В edit_event: поля которые не меняются — null
- "за 15 минут", "за полчаса", "за час" → minutes_before = 15/30/60
"""


def parse_intent(text: str) -> list:
    today = datetime.now()
    tomorrow = (today + timedelta(days=1)).strftime("%Y-%m-%d")
    prompt = NLP_PROMPT.format(
        today=today.strftime("%Y-%m-%d"),
        time=today.strftime("%H:%M"),
        tomorrow=tomorrow,
        text=text,
    )
    try:
        resp = AI.models.generate_content(model="gemini-3-flash-preview", contents=prompt)
        raw = resp.text.strip()
        raw = re.sub(r"^```json\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        result = json.loads(raw)
        return result if isinstance(result, list) else [result]
    except Exception as e:
        log.error(f"Gemini parse error: {e}")
        return [{"type": "unknown", "reply": "Не удалось распознать запрос."}]


# ── Handlers ──────────────────────────────────────────────────────────────────

def handle_digest() -> None:
    send("⏳ Собираю дайджест, это займёт ~30 секунд...")
    try:
        result = subprocess.run(
            [sys.executable, AGENT],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            send(f"❌ Ошибка при запуске агента:\n<code>{result.stderr[-500:]}</code>")
    except subprocess.TimeoutExpired:
        send("❌ Таймаут при запуске агента.")


def handle_meetings() -> None:
    events = get_events(days=7)
    send(format_events(events, days=7))


def handle_text(text: str) -> None:
    intents = parse_intent(text)
    created = []

    for intent in intents:
        itype = intent.get("type")

        if itype == "show_events":
            days = intent.get("days", 7)
            events = get_events(days=days)
            send(format_events(events, days=days))

        elif itype == "create_event":
            try:
                start = datetime.fromisoformat(intent["start"])
                duration = intent.get("duration_minutes", 60)
                end = start + timedelta(minutes=duration)
                title = intent["title"]
                notes = intent.get("notes", "")
                ok = add_event(title, start, end, notes)
                if ok:
                    delta = (start.date() - datetime.now().date()).days
                    day_label = "Завтра" if delta == 1 else ("Сегодня" if delta == 0 else start.strftime("%-d %B"))
                    created.append(f"• <b>{title}</b> — {day_label}, {start.strftime('%H:%M')}–{end.strftime('%H:%M')}")
                else:
                    send(f"❌ Не удалось добавить: {intent.get('title', '?')}")
            except Exception as e:
                log.error(f"create_event error: {e}")
                send(f"❌ Ошибка при создании: {e}")

        elif itype == "delete_event":
            search = intent.get("search", "")
            date_hint = intent.get("date")
            ok = delete_event(search, date_hint)
            if ok:
                send(f"🗑 Встреча <b>{search}</b> удалена из календаря.")
            else:
                send(f"❌ Встреча «{search}» не найдена. Уточни название или дату.")

        elif itype == "edit_event":
            search = intent.get("search", "")
            date_hint = intent.get("date")
            new_start = datetime.fromisoformat(intent["new_start"]) if intent.get("new_start") else None
            new_dur = intent.get("new_duration_minutes")
            new_title = intent.get("new_title")
            ok = edit_event(search, date_hint, new_start, new_dur, new_title)
            if ok:
                parts = []
                if new_title:
                    parts.append(f"название → <b>{new_title}</b>")
                if new_start:
                    parts.append(f"время → <b>{new_start.strftime('%-d %B, %H:%M')}</b>")
                if new_dur and not new_start:
                    parts.append(f"длительность → <b>{new_dur} мин</b>")
                send(f"✏️ Встреча «{search}» обновлена: {', '.join(parts)}.")
            else:
                send(f"❌ Встреча «{search}» не найдена. Уточни название или дату.")

        elif itype == "add_reminder":
            search = intent.get("search", "")
            date_hint = intent.get("date")
            minutes = intent.get("minutes_before", 15)
            ok = add_reminder(search, date_hint, minutes)
            if ok:
                send(f"⏰ Напоминание за <b>{minutes} мин</b> до встречи «{search}» установлено.")
            else:
                send(f"❌ Встреча «{search}» не найдена. Уточни название или дату.")

        else:
            reply = intent.get("reply", "")
            send(reply if reply else (
                "Вот что я умею:\n\n"
                "📬 <b>Письма</b> — утренний дайджест почты\n"
                "📅 <b>Встречи</b> — расписание на неделю\n\n"
                "Или напишите:\n"
                "• «добавь встречу с Пащенко завтра в 14:00»\n"
                "• «созвон с командой в пятницу в 10:00 на 2 часа»\n"
                "• «покажи встречи на 3 дня»"
            ))

    if created:
        send("✅ Добавлено в календарь:\n\n" + "\n".join(created))


def handle_start() -> None:
    send(
        "👋 Привет! Я твой персональный ассистент.\n\n"
        "📬 <b>Письма</b> — дайджест входящей почты\n"
        "📅 <b>Встречи</b> — календарь и управление встречами\n\n"
        "Можешь писать текстом: «добавь встречу...»"
    )


# ── Polling loop ──────────────────────────────────────────────────────────────

def run() -> None:
    log.info("Bot started. Polling...")
    offset = 0

    while True:
        try:
            resp = requests.get(
                f"{API}/getUpdates",
                params={"offset": offset, "timeout": 30, "allowed_updates": ["message"]},
                timeout=35,
            )
            if not resp.ok:
                log.warning(f"getUpdates error: {resp.text}")
                time.sleep(5)
                continue

            for update in resp.json().get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                chat_id = msg.get("chat", {}).get("id")
                text = msg.get("text", "").strip()

                # Only respond to authorized user
                if chat_id != CHAT_ID:
                    continue

                log.info(f"Message: {text!r}")

                if text in ("/start", "/help"):
                    handle_start()
                elif text == "📬 Письма":
                    handle_digest()
                elif text == "📅 Встречи":
                    handle_meetings()
                elif text:
                    handle_text(text)

        except requests.exceptions.Timeout:
            continue
        except Exception as e:
            log.error(f"Polling error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    run()
