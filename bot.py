"""Перевірка графіка відключень ДТЕК і надсилання в Telegram.

Запускається раз на 30 хв (systemd timer / cron) або з --loop.
- Перший запуск за добу -> нове повідомлення з картинкою.
- Якщо графік (або лінії sub_type_reason) змінився -> редагуємо це повідомлення.
- Якщо повідомлення видалили -> надсилаємо заново.
- Застарілі/неповні дані з сайту (кеш, порожні відповіді) ігноруємо.
"""

import argparse
import fcntl
import hashlib
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

import dtek
from render import KYIV, day_title, off_intervals, render

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "state.json"
LOCK_FILE = ROOT / ".lock"

load_dotenv(ROOT / ".env")
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
STREET = os.environ["STREET"]  # напр. "вул. Хрещатик"
HOUSE = os.environ["HOUSE"]  # напр. "1/к.1"
API = f"https://api.telegram.org/bot{TOKEN}"

# Відповіді Telegram, які означають, що повідомлення більше немає / не редагується
GONE_ERRORS = (
    "message to edit not found",
    "message can't be edited",
    "message_id_invalid",
)


class TgError(RuntimeError):
    def __init__(self, method: str, description: str):
        super().__init__(f"Telegram {method}: {description}")
        self.description = description.lower()

    @property
    def gone(self) -> bool:
        return any(e in self.description for e in GONE_ERRORS)

    @property
    def not_modified(self) -> bool:
        return "message is not modified" in self.description


def log(msg: str) -> None:
    print(f"[{datetime.now(KYIV):%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    tmp.replace(STATE_FILE)


def parse_updated(value: str | None) -> datetime | None:
    try:
        return datetime.strptime(value or "", "%d.%m.%Y %H:%M")
    except ValueError:
        return None


def has_day(s: dtek.Schedule, day: int | None) -> bool:
    return day is not None and any(s.slots(day, ln) for ln in s.lines)


def signature(s: dtek.Schedule) -> str:
    """Хеш лише змістовної частини. Лінії сортуємо, щоб зміна порядку не рахувалась змінами."""
    lines = sorted(s.lines)
    payload = {
        "lines": lines,
        "today": [s.slots(s.today, ln) for ln in lines],
        "tomorrow": [s.slots(s.tomorrow, ln) for ln in lines],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def caption(s: dtek.Schedule, note: str | None) -> str:
    parts = []
    if note:
        parts.append(f"🔄 <b>{note}</b>")
    parts.append(f"💡 <b>{STREET}, {HOUSE}</b>")
    for label, day in (("Сьогодні", s.today), ("Завтра", s.tomorrow)):
        if not has_day(s, day):
            parts.append(f"\n<b>{label}</b>: графік ще не опубліковано")
            continue
        parts.append(f"\n<b>{label} · {day_title(day)}</b>")
        for i, ln in enumerate(s.lines, 1):
            iv = off_intervals(s.slots(day, ln))
            parts.append(
                f"Лінія {i} ({s.names.get(ln, ln)}): {', '.join(iv) if iv else 'без відключень'}"
            )
    parts.append(f"\n<i>Оновлення ДТЕК: {s.updated}</i>")
    return "\n".join(parts)


def tg(method: str, data: dict, photo: bytes | None = None) -> dict:
    files = {"photo": ("schedule.png", photo, "image/png")} if photo else None
    r = requests.post(f"{API}/{method}", data=data, files=files, timeout=60)
    res = r.json()
    if not res.get("ok"):
        raise TgError(method, res.get("description", str(res)))
    return res["result"]


def send(photo: bytes, text: str) -> int:
    msg = tg("sendPhoto", {"chat_id": CHAT_ID, "caption": text, "parse_mode": "HTML"}, photo)
    return msg["message_id"]


def edit(message_id: int, photo: bytes, text: str) -> None:
    media = {
        "type": "photo",
        "media": "attach://photo",
        "caption": text,
        "parse_mode": "HTML",
    }
    tg(
        "editMessageMedia",
        {"chat_id": CHAT_ID, "message_id": message_id, "media": json.dumps(media)},
        photo,
    )


def message_exists(message_id: int) -> bool:
    """Bot API не вміє читати повідомлення, тому пробуємо «порожнє» редагування:
    існуюче дає "message is not modified", видалене - "message to edit not found"."""
    try:
        tg(
            "editMessageReplyMarkup",
            {"chat_id": CHAT_ID, "message_id": message_id, "reply_markup": "{}"},
        )
        return True
    except TgError as e:
        if e.not_modified:
            return True
        if e.gone:
            return False
        raise


def fresh_state(s: dtek.Schedule, message_id: int, sig: str) -> dict:
    return {
        "day": s.today,
        "message_id": message_id,
        "sig": sig,
        "lines": s.lines,
        "updated": s.updated,
        "has_tomorrow": has_day(s, s.tomorrow),
    }


def regression(s: dtek.Schedule, state: dict) -> str | None:
    """Причина вважати відповідь сайту застарілою/неповною, або None."""
    if not s.lines:
        return "порожні лінії"
    if not has_day(s, s.today):
        return "немає графіка на сьогодні"
    if state.get("has_tomorrow") and not has_day(s, s.tomorrow):
        return "зник графік на завтра"
    new, old = parse_updated(s.updated), parse_updated(state.get("updated"))
    if new and old and new < old:
        return f"старіша версія ({s.updated} < {state['updated']})"
    return None


def check(force: bool = False) -> None:
    s = dtek.fetch(STREET, HOUSE)
    sig = signature(s)
    state = load_state()
    title = f"{STREET}, {HOUSE}"
    log(f"Лінії: {s.lines}, оновлення ДТЕК: {s.updated}")

    day = state.get("day")
    if not force and day and s.today < day:
        log("Сайт віддав вчорашні дані (кеш) - пропускаю")
        return

    if force or not state.get("message_id") or s.today > day:
        mid = send(render(s, title), caption(s, None))
        log(f"Нове повідомлення {mid}")
        save_state(fresh_state(s, mid, sig))
        return

    if reason := regression(s, state):
        log(f"Ігнорую відповідь сайту: {reason}")
        return

    if not message_exists(state["message_id"]):
        mid = send(render(s, title), caption(s, None))
        log(f"Повідомлення {state['message_id']} видалене - перевідправив як {mid}")
        save_state(fresh_state(s, mid, sig))
        return

    if state.get("sig") == sig:
        log("Без змін")
        return

    note = f"Графік оновився о {datetime.now(KYIV):%H:%M}"
    if sorted(state.get("lines", [])) != sorted(s.lines):
        note += " (змінились лінії)"
    try:
        edit(state["message_id"], render(s, title), caption(s, note))
        log(f"Оновлено повідомлення {state['message_id']}")
    except TgError as e:
        if e.not_modified:
            # у повідомленні вже саме це - лише фіксуємо стан, без сповіщення
            log("Повідомлення вже актуальне")
            state.update(
                sig=sig,
                lines=s.lines,
                updated=s.updated,
                has_tomorrow=has_day(s, s.tomorrow),
            )
            save_state(state)
            return
        if not e.gone:
            raise  # тимчасова помилка (429, 5xx) - спробуємо наступного запуску
        mid = send(render(s, title), caption(s, note))
        log(f"Повідомлення не редагується ({e.description}) - надіслав нове {mid}")
        save_state(fresh_state(s, mid, sig))
        return

    state.update(
        sig=sig,
        lines=s.lines,
        updated=s.updated,
        has_tomorrow=has_day(s, s.tomorrow),
    )
    save_state(state)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true", help="працювати постійно, перевірка кожні 30 хв")
    ap.add_argument("--force", action="store_true", help="надіслати нове повідомлення примусово")
    ap.add_argument("--preview", metavar="PNG", help="лише зберегти картинку, без Telegram")
    args = ap.parse_args()

    if args.preview:
        s = dtek.fetch(STREET, HOUSE)
        Path(args.preview).write_bytes(render(s, f"{STREET}, {HOUSE}"))
        print(caption(s, None))
        return

    # Один екземпляр одночасно (timer + --loop не задублюють повідомлення)
    lock = open(LOCK_FILE, "w")  # noqa: SIM115 - лок тримаємо до кінця процесу
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("Вже запущено інший екземпляр - виходжу")
        return

    while True:
        try:
            check(args.force)
        except Exception as e:  # noqa: BLE001 - у циклі не падаємо
            log(f"Помилка: {e}")
            if not args.loop:
                sys.exit(1)
        if not args.loop:
            return
        args.force = False
        time.sleep(30 * 60)


if __name__ == "__main__":
    main()
