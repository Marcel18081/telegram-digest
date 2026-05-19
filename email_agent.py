#!/usr/bin/env python3
"""
Personal email assistant for a construction company co-owner.
Reads mail.ru via IMAP, analyses content with Gemini, sends digest to Telegram.
"""

import imaplib
import email
import email.header
import io
import json
import os
import subprocess
import sys
import time
from typing import Optional
import requests
from google import genai
from google.genai import types
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from dotenv import load_dotenv

# Force line-buffered output so logs appear even when running under launchd
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

MAIL_EMAIL      = os.environ["MAIL_EMAIL"]
MAIL_PASSWORD   = os.environ["MAIL_PASSWORD"]
BOT_TOKEN       = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID         = os.environ["TELEGRAM_CHAT_ID"]
GEMINI_KEY      = os.environ["GEMINI_API_KEY"]

IMAP_HOST               = "imap.mail.ru"
IMAP_PORT               = 993
NEW_MAIL_HOURS          = 24       # "fresh" emails for today's digest
UNANSWERED_WINDOW_DAYS  = 14       # look back for unanswered threads
UNANSWERED_THRESHOLD_H  = 4        # flag as ignored after N hours with no reply
MAX_BODY_CHARS          = 3000     # chars of body text passed to Claude per email
MAX_ATTACH_CHARS        = 2000     # chars extracted from each attachment


# ── IMAP ──────────────────────────────────────────────────────────────────────

def connect() -> imaplib.IMAP4_SSL:
    conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    conn.login(MAIL_EMAIL, MAIL_PASSWORD)
    return conn


def decode_str(value: Optional[str]) -> str:
    if not value:
        return ""
    parts = email.header.decode_header(value)
    out = []
    for part, charset in parts:
        if isinstance(part, bytes):
            out.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(part)
    return "".join(out).strip()


def parse_date(msg) -> datetime:
    try:
        return parsedate_to_datetime(msg.get("Date", "")).astimezone(timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def find_folder_by_flag(conn: imaplib.IMAP4_SSL, flag: str) -> Optional[str]:
    _, raw = conn.list()
    for item in raw:
        decoded = item.decode("utf-8", errors="replace")
        if flag.lower() in decoded.lower():
            parts = decoded.split('"')
            if len(parts) >= 2:
                name = next((p for p in reversed(parts) if p.strip()), "").strip()
                if name:
                    return name
    return None


def extract_body(msg) -> str:
    """Extract plain-text body from an email message."""
    text_parts = []
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in cd:
                charset = part.get_content_charset() or "utf-8"
                try:
                    text_parts.append(part.get_payload(decode=True).decode(charset, errors="replace"))
                except Exception:
                    pass
    else:
        charset = msg.get_content_charset() or "utf-8"
        try:
            text_parts.append(msg.get_payload(decode=True).decode(charset, errors="replace"))
        except Exception:
            pass
    return "\n".join(text_parts)[:MAX_BODY_CHARS]


def extract_attachment_text(part) -> str:
    """Try to extract text from a PDF, DOCX, or XLSX attachment."""
    filename = decode_str(part.get_filename() or "")
    data = part.get_payload(decode=True)
    if not data:
        return ""

    ext = filename.lower().split(".")[-1] if "." in filename else ""

    try:
        if ext == "pdf":
            import pdfplumber
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                pages = [p.extract_text() or "" for p in pdf.pages[:5]]
            return ("\n".join(pages))[:MAX_ATTACH_CHARS]

        if ext in ("xlsx", "xls"):
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            rows = []
            for ws in wb.worksheets[:2]:
                for row in ws.iter_rows(max_row=40, values_only=True):
                    row_str = "\t".join(str(c) if c is not None else "" for c in row)
                    if row_str.strip():
                        rows.append(row_str)
            return "\n".join(rows)[:MAX_ATTACH_CHARS]

        if ext == "docx":
            from docx import Document
            doc = Document(io.BytesIO(data))
            return "\n".join(p.text for p in doc.paragraphs)[:MAX_ATTACH_CHARS]

    except Exception as e:
        return f"[не удалось извлечь текст из {filename}: {e}]"

    return f"[вложение: {filename}]"


def fetch_emails(conn: imaplib.IMAP4_SSL, folder: str, since_hours: int,
                 full_body: bool = True) -> list:
    try:
        conn.select(f'"{folder}"', readonly=True)
    except Exception:
        return []

    since = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).strftime("%d-%b-%Y")
    _, uids = conn.search(None, f"SINCE {since}")
    if not uids or not uids[0]:
        return []

    messages = []
    for uid in uids[0].split():
        fetch_type = "(RFC822)" if full_body else "(RFC822.HEADER)"
        _, data = conn.fetch(uid, fetch_type)
        if not data or not data[0]:
            continue
        msg = email.message_from_bytes(data[0][1])

        body = extract_body(msg) if full_body else ""

        attachments = []
        if full_body and msg.is_multipart():
            for part in msg.walk():
                cd = str(part.get("Content-Disposition", ""))
                if "attachment" in cd:
                    text = extract_attachment_text(part)
                    name = decode_str(part.get_filename() or "файл")
                    if text:
                        attachments.append({"name": name, "text": text})

        messages.append({
            "subject":     decode_str(msg.get("Subject")),
            "from":        decode_str(msg.get("From")),
            "to":          decode_str(msg.get("To")),
            "date":        parse_date(msg),
            "message_id":  (msg.get("Message-ID") or "").strip(),
            "in_reply_to": (msg.get("In-Reply-To") or "").strip(),
            "references":  (msg.get("References") or "").strip(),
            "body":        body,
            "attachments": attachments,
        })
    return messages


def find_unanswered(inbox: list, sent: list) -> list:
    replied_ids: set = set()
    replied_subjects: set = set()
    for s in sent:
        if s["in_reply_to"]:
            replied_ids.add(s["in_reply_to"])
        for ref in s["references"].split():
            replied_ids.add(ref)
        subj = s["subject"].lower().removeprefix("re:").removeprefix("fwd:").strip()
        replied_subjects.add(subj)

    threshold = datetime.now(timezone.utc) - timedelta(hours=UNANSWERED_THRESHOLD_H)
    result = []
    for m in inbox:
        if m["date"] > threshold:
            continue
        if m["message_id"] in replied_ids:
            continue
        subj = m["subject"].lower().removeprefix("re:").removeprefix("fwd:").strip()
        if subj in replied_subjects:
            continue
        result.append(m)
    return result


def classify_action(m: dict, sent: list, drafts: list) -> str:
    """Return a short status tag based on what the user already did."""
    mid = m["message_id"]
    subj_clean = m["subject"].lower().removeprefix("re:").removeprefix("fwd:").strip()

    for s in sent:
        if s["in_reply_to"] == mid or mid in s["references"]:
            return "ответил"
        if "fwd" in s["subject"].lower() and subj_clean in s["subject"].lower():
            return "переслал команде"

    for d in drafts:
        if subj_clean in d["subject"].lower():
            return "черновик ответа есть"

    age_h = (datetime.now(timezone.utc) - m["date"]).total_seconds() / 3600
    if age_h > UNANSWERED_THRESHOLD_H:
        return "нет ответа"
    return "новое"


# ── Calendar (AppleScript) ────────────────────────────────────────────────────

WORK_CALENDARS = ["Рабочий", "Календарь", "Calendar"]

_CAL_SCRIPT = """
tell application "Calendar"
    activate
    delay 1
    set out to ""
    set d1 to date "{date_from}"
    set d2 to date "{date_to}"
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


def _parse_as_date(s: str) -> Optional[datetime]:
    s = s.strip()
    for fmt in (
        "%A, %d %B %Y at %H:%M:%S",
        "%A, %B %d, %Y at %I:%M:%S %p",
        "%A, %d %B %Y г. в %H:%M:%S",
    ):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _as_fmt(dt: datetime) -> str:
    return dt.strftime("%A, %d %B %Y at %H:%M:%S")


def get_upcoming_events(days: int = 7) -> list:
    """Return events from Apple Calendar for the next N days."""
    cal_names = ", ".join(f'"{c}"' for c in WORK_CALENDARS)
    now = datetime.now().replace(hour=0, minute=0, second=0)
    script = _CAL_SCRIPT.format(
        date_from=_as_fmt(now),
        date_to=_as_fmt(now + timedelta(days=days)),
        cal_names=cal_names,
    )
    try:
        raw = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=60,
        ).stdout.strip()
    except Exception as exc:
        log(f"AppleScript error: {exc}")
        return []

    events = []
    for line in raw.split("¶"):
        line = line.strip()
        if not line:
            continue
        parts = line.split("§")
        if len(parts) < 3:
            continue
        start = _parse_as_date(parts[1])
        end   = _parse_as_date(parts[2])
        if start and end:
            events.append({"title": parts[0], "start": start, "end": end})
    events.sort(key=lambda e: e["start"])
    return events


# ── Tasks ──────────────────────────────────────────────────────────────────────

TASKS_FILE = os.path.join(os.path.dirname(__file__), "tasks.json")


def get_open_tasks_for_digest() -> dict:
    """Return open meeting and personal tasks as two lists."""
    if not os.path.exists(TASKS_FILE):
        return {"meeting": [], "personal": []}
    try:
        with open(TASKS_FILE, encoding="utf-8") as f:
            all_tasks = json.load(f)
    except Exception:
        return {"meeting": [], "personal": []}

    return {
        "meeting":  [t for t in all_tasks if t["type"] == "meeting"  and not t["done"]],
        "personal": [t for t in all_tasks if t["type"] == "personal" and not t["done"]],
    }


# ── Claude AI ────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Ты — многолетний личный ассистент совладельца международной строительной компании (проекты в России, Японии, Индии).
Твоя задача — составить утренний дайджест входящей почты: профессиональный, тезисный, по делу.
Пиши по-русски. Используй HTML-разметку Telegram: <b>жирный</b>, <i>курсив</i>.
Будь краток, но не теряй важное. Действуй как опытный бизнес-ассистент."""

DIGEST_PROMPT = """Вот полные данные для утреннего дайджеста: письма, встречи на неделю, задачи.

{payload_json}

Составь утренний дайджест в следующей структуре:

<b>📋 ДАЙДЖЕСТ — {date}</b>

Раздел 1: <b>🔴 ТРЕБУЮТ ДЕЙСТВИЙ</b> — письма без ответа или с важными задачами. Для каждого:
  • Тема + от кого
  • 1-2 предложения: о чём письмо
  • Что нужно сделать / дедлайн если есть
  • Статус (ответил / переслал / черновик / нет ответа)

Раздел 2: <b>🟡 К СВЕДЕНИЮ</b> — информационные письма, не требующие срочной реакции

Раздел 3: <b>📝 ЧЕРНОВИКИ</b> — что готово но не отправлено (если есть)

Раздел 4: <b>✅ ОБРАБОТАНО ВЧЕРА</b> — на что уже ответил или переслал

Раздел 5: <b>📅 ВСТРЕЧИ НА НЕДЕЛЮ</b> — по дням, с временем и длительностью. Если встреч нет — «Свободная неделя».

Раздел 6: <b>📋 ЗАДАЧИ ПО ВСТРЕЧАМ</b> — открытые задачи, связанные с переговорами. Если нет — «Задач нет».

Раздел 7: <b>📝 ЛИЧНЫЕ ЗАДАЧИ</b> — открытые личные дела. Если нет — «Задач нет».

В конце — <b>⚡ ПРИОРИТЕТЫ НА СЕГОДНЯ</b>: 3-5 пунктов самого важного, с учётом всех разделов.

Пиши конкретно и по делу, как опытный ассистент директора."""


def build_digest_with_gemini(new_emails: list, unanswered: list,
                              sent: list, drafts: list,
                              events: list, tasks: dict) -> str:
    client = genai.Client(api_key=GEMINI_KEY)

    def email_to_dict(m: dict, status: str) -> dict:
        age_h = int((datetime.now(timezone.utc) - m["date"]).total_seconds() / 3600)
        d = {
            "от":      m["from"],
            "тема":    m["subject"],
            "дата":    m["date"].strftime("%d.%m %H:%M"),
            "возраст": f"{age_h}ч",
            "статус":  status,
            "текст":   m["body"][:1500] if m["body"] else "(текст недоступен)",
        }
        if m["attachments"]:
            d["вложения"] = [
                {"файл": a["name"], "содержимое": a["text"][:800]}
                for a in m["attachments"]
            ]
        return d

    def event_to_dict(e: dict) -> dict:
        duration = int((e["end"] - e["start"]).total_seconds() / 60)
        dur_str = f"{duration // 60}ч" if duration >= 60 else f"{duration}мин"
        return {
            "название": e["title"],
            "день":     e["start"].strftime("%-d %B, %A"),
            "время":    e["start"].strftime("%H:%M"),
            "длина":    dur_str,
        }

    def task_to_dict(t: dict) -> dict:
        d: dict = {"задача": t["title"]}
        if t.get("due_date"):
            d["дедлайн"] = t["due_date"]
        if t.get("linked_meeting"):
            d["встреча"] = t["linked_meeting"]
        return d

    payload = {
        "новые_письма": [
            email_to_dict(m, classify_action(m, sent, drafts))
            for m in sorted(new_emails, key=lambda x: x["date"], reverse=True)[:20]
        ],
        "без_ответа_старые": [
            email_to_dict(m, classify_action(m, sent, drafts))
            for m in sorted(unanswered, key=lambda x: x["date"])[:15]
        ],
        "черновики": [
            {"тема": d["subject"], "кому": d["to"], "возраст": f"{int((datetime.now(timezone.utc) - d['date']).total_seconds()/3600)}ч"}
            for d in drafts[:10]
        ],
        "встречи_на_неделю": [event_to_dict(e) for e in events],
        "задачи_по_встречам": [task_to_dict(t) for t in tasks.get("meeting", [])],
        "личные_задачи": [task_to_dict(t) for t in tasks.get("personal", [])],
    }

    prompt = SYSTEM_PROMPT + "\n\n" + DIGEST_PROMPT.format(
        payload_json=json.dumps(payload, ensure_ascii=False, indent=2),
        date=datetime.now().strftime("%d.%m.%Y, %A"),
    )

    response = client.models.generate_content(
        model="gemini-3-flash-preview",
        contents=prompt,
    )
    return response.text


# ── Telegram ─────────────────────────────────────────────────────────────────

def send_telegram(text: str) -> None:
    """Send message, splitting if over Telegram's 4096-char limit."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    limit = 4000

    chunks = []
    while len(text) > limit:
        split_at = text.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = limit
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip()
    chunks.append(text)

    for chunk in chunks:
        resp = requests.post(url, json={
            "chat_id":    CHAT_ID,
            "text":       chunk,
            "parse_mode": "HTML",
        }, timeout=15)
        if not resp.ok:
            log(f"Telegram error: {resp.text}")


# ── Main ──────────────────────────────────────────────────────────────────────

def connect_with_retry(max_attempts: int = 4, delay: int = 20) -> imaplib.IMAP4_SSL:
    """Try to connect to IMAP, retrying on failure (network may not be ready after wake)."""
    last_exc: Exception = RuntimeError("no attempts made")
    for attempt in range(1, max_attempts + 1):
        try:
            return connect()
        except Exception as exc:
            last_exc = exc
            log(f"IMAP connection attempt {attempt}/{max_attempts} failed: {exc}")
            if attempt < max_attempts:
                log(f"Retrying in {delay}s...")
                time.sleep(delay)
    raise last_exc


def main() -> None:
    log("Starting email agent")
    log("Connecting to mail.ru...")
    conn = connect_with_retry()

    sent_folder   = find_folder_by_flag(conn, "\\Sent")
    drafts_folder = find_folder_by_flag(conn, "\\Drafts")
    log(f"Folders — sent: {sent_folder}, drafts: {drafts_folder}")

    log("Fetching new emails (24h)...")
    new_inbox = fetch_emails(conn, "INBOX", NEW_MAIL_HOURS, full_body=True)

    log("Fetching sent (2 weeks)...")
    sent = fetch_emails(conn, sent_folder, UNANSWERED_WINDOW_DAYS * 24,
                        full_body=False) if sent_folder else []

    log("Fetching all inbox for unanswered check (2 weeks)...")
    all_inbox = fetch_emails(conn, "INBOX", UNANSWERED_WINDOW_DAYS * 24,
                             full_body=False)

    log("Fetching drafts...")
    drafts = fetch_emails(conn, drafts_folder, UNANSWERED_WINDOW_DAYS * 24,
                          full_body=False) if drafts_folder else []

    conn.logout()

    unanswered = find_unanswered(all_inbox, sent)
    new_ids = {m["message_id"] for m in new_inbox}
    unanswered_old = [m for m in unanswered if m["message_id"] not in new_ids]

    log(f"New: {len(new_inbox)}, unanswered old: {len(unanswered_old)}, "
        f"sent: {len(sent)}, drafts: {len(drafts)}")

    log("Fetching calendar events...")
    events = get_upcoming_events(days=7)
    log(f"Events: {len(events)}")

    log("Reading tasks...")
    tasks = get_open_tasks_for_digest()
    log(f"Tasks — meeting: {len(tasks['meeting'])}, personal: {len(tasks['personal'])}")

    log("Calling Gemini API...")
    digest = build_digest_with_gemini(new_inbox, unanswered_old, sent, drafts, events, tasks)

    print(digest, flush=True)
    send_telegram(digest)

    marker = os.path.join(os.path.dirname(__file__), "digest_last_sent")
    with open(marker, "w") as f:
        f.write(datetime.now().strftime("%Y-%m-%d"))

    log("Sent to Telegram ✓")


if __name__ == "__main__":
    main()
