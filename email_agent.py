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
import sys
from typing import Optional
import requests
from google import genai
from google.genai import types
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from dotenv import load_dotenv

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


# ── Claude AI ────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Ты — многолетний личный ассистент совладельца международной строительной компании (проекты в России, Японии, Индии).
Твоя задача — составить утренний дайджест входящей почты: профессиональный, тезисный, по делу.
Пиши по-русски. Используй HTML-разметку Telegram: <b>жирный</b>, <i>курсив</i>.
Будь краток, но не теряй важное. Действуй как опытный бизнес-ассистент."""

DIGEST_PROMPT = """Вот данные по входящим письмам за последние сутки и напоминания по старым письмам.

{emails_json}

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

В конце — <b>⚡ ПРИОРИТЕТЫ НА СЕГОДНЯ</b>: 3-5 пунктов самого важного.

Пиши конкретно и по делу, как опытный ассистент директора."""


def build_digest_with_gemini(new_emails: list, unanswered: list,
                              sent: list, drafts: list) -> str:
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
    }

    prompt = SYSTEM_PROMPT + "\n\n" + DIGEST_PROMPT.format(
        emails_json=json.dumps(payload, ensure_ascii=False, indent=2),
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
            print(f"Telegram error: {resp.text}", file=sys.stderr)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("Connecting to mail.ru...")
    conn = connect()

    sent_folder   = find_folder_by_flag(conn, "\\Sent")
    drafts_folder = find_folder_by_flag(conn, "\\Drafts")
    print(f"Folders — sent: {sent_folder}, drafts: {drafts_folder}")

    print("Fetching new emails (24h)...")
    new_inbox = fetch_emails(conn, "INBOX", NEW_MAIL_HOURS, full_body=True)

    print("Fetching sent (2 weeks)...")
    sent = fetch_emails(conn, sent_folder, UNANSWERED_WINDOW_DAYS * 24,
                        full_body=False) if sent_folder else []

    print("Fetching all inbox for unanswered check (2 weeks)...")
    all_inbox = fetch_emails(conn, "INBOX", UNANSWERED_WINDOW_DAYS * 24,
                             full_body=False)

    print("Fetching drafts...")
    drafts = fetch_emails(conn, drafts_folder, UNANSWERED_WINDOW_DAYS * 24,
                          full_body=False) if drafts_folder else []

    conn.logout()

    unanswered = find_unanswered(all_inbox, sent)
    # exclude emails already covered in new_inbox
    new_ids = {m["message_id"] for m in new_inbox}
    unanswered_old = [m for m in unanswered if m["message_id"] not in new_ids]

    print(f"New: {len(new_inbox)}, unanswered old: {len(unanswered_old)}, "
          f"sent: {len(sent)}, drafts: {len(drafts)}")

    print("Calling Gemini API...")
    digest = build_digest_with_gemini(new_inbox, unanswered_old, sent, drafts)

    print(digest)
    send_telegram(digest)

    # Write today's marker so fallback job knows digest was sent
    marker = os.path.join(os.path.dirname(__file__), "digest_last_sent")
    with open(marker, "w") as f:
        f.write(datetime.now().strftime("%Y-%m-%d"))

    print("\nSent to Telegram ✓")


if __name__ == "__main__":
    main()
