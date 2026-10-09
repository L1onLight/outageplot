"""Перевірка графіка відключень ДТЕК і надсилання в Telegram.

Запускається раз на 30 хв (systemd timer / cron) або з --loop.
- Перший запуск за добу -> нове повідомлення з картинкою.
- Кожен наступний запуск -> редагуємо це повідомлення (на картинці позначка
  поточного часу). Примітка «Графік оновився» з'являється лише коли графік
  (або лінії sub_type_reason) справді змінився і лишається до наступної зміни.
- Якщо повідомлення видалили -> надсилаємо заново.
- Застарілі/неповні дані з сайту (кеш, порожні відповіді) ігноруємо.

Поточне відключення за адресою (аварійні роботи тощо):
- рядок «⚡ Зараз світла немає» у підписі графіка;
- для позапланових відключень - одне окреме повідомлення на подію, яке далі
  тихо редагується (перенесення часу відновлення, «світло відновлено»);
- пінг-відповідь лише коли обіцяний час уже минув, а ДТЕК переніс його далі.
- Уночі (23:00-08:00) усе надсилається без звуку.
"""

import argparse
import fcntl
import hashlib
import html
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


QUIET_HOURS = (23, 8)  # з 23:00 до 08:00 сповіщення без звуку
PING_INTERVAL = 60 * 60  # пінг про перенесення часу - не частіше ніж раз на годину
RESTORE_AFTER = 2  # стільки перевірок поспіль без відключення = світло відновлено
# sub_type відключень за графіком - вони вже є на картинці, окремо не сповіщаємо
SCHEDULED_MARKERS = ("графік", "стабілізаційн")


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


def parse_dtek_time(value: str | None) -> datetime | None:
    """'18:42 08.10.2026' (формат getHomeNum) -> datetime за Києвом."""
    try:
        return datetime.strptime(value or "", "%H:%M %d.%m.%Y").replace(tzinfo=KYIV)
    except ValueError:
        return None


def short(value: str) -> str:
    """'18:42 08.10.2026' -> '18:42 08.10'"""
    dt = parse_dtek_time(value)
    return f"{dt:%H:%M %d.%m}" if dt else value


def quiet_now() -> bool:
    h = datetime.now(KYIV).hour
    return h >= QUIET_HOURS[0] or h < QUIET_HOURS[1]


def unscheduled(o: dict) -> bool:
    return not any(m in o["type"].lower() for m in SCHEDULED_MARKERS)


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
    if o := s.outage:
        line = f"⚡ <b>Зараз світла немає</b>: {html.escape(o['type'])} · з {short(o['start'])}"
        if o["end"]:
            line += f" · орієнтовно до {short(o['end'])}"
        parts.append(line)
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


def silent() -> str:
    return "true" if quiet_now() else "false"


def send(photo: bytes, text: str) -> int:
    msg = tg(
        "sendPhoto",
        {
            "chat_id": CHAT_ID,
            "caption": text,
            "parse_mode": "HTML",
            "disable_notification": silent(),
        },
        photo,
    )
    return msg["message_id"]


def send_text(text: str, reply_to: int | None = None) -> int:
    data = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_notification": silent(),
    }
    if reply_to:
        data["reply_parameters"] = json.dumps(
            {"message_id": reply_to, "allow_sending_without_reply": True}
        )
    return tg("sendMessage", data)["message_id"]


def edit_text(message_id: int, text: str) -> None:
    tg(
        "editMessageText",
        {"chat_id": CHAT_ID, "message_id": message_id, "text": text, "parse_mode": "HTML"},
    )


def delete(message_id: int) -> None:
    try:
        tg("deleteMessage", {"chat_id": CHAT_ID, "message_id": message_id})
    except TgError as e:
        log(f"Не вдалося видалити {message_id}: {e}")


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


def fresh_state(s: dtek.Schedule, message_id: int, sig: str, note: str | None = None) -> dict:
    return {
        "day": s.today,
        "message_id": message_id,
        "sig": sig,
        "note": note,
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


def outage_text(st: dict, now: datetime) -> str:
    restored = st.get("restored_at")
    if restored:
        parts = [f"✅ <b>Світло відновлено ~{restored}</b>", f"💡 {STREET}, {HOUSE}", ""]
        parts.append(f"Причина: {html.escape(st['type'])}")
    else:
        parts = [f"⚡ <b>Світла немає: {html.escape(st['type'])}</b>", f"💡 {STREET}, {HOUSE}", ""]
    parts.append(f"З {short(st['start'])}")
    if ends := st["ends"]:
        # Попередні обіцянки закреслені; лише останні дві, щоб рядок не розростався
        old = "".join(f"<s>{short(e)}</s> " for e in ends[-3:-1])
        if len(ends) > 3:
            old = "… " + old
        line = f"{'Обіцяли до' if restored else 'Орієнтовно до'} {old}<b>{short(ends[-1])}</b>"
        if len(ends) > 1:
            line += f" (перенесено о {st['end_changed_at']})"
        parts.append(line)
        end = parse_dtek_time(ends[-1])
        if not restored and end and end < now:
            parts.append("⏳ Орієнтовний час минув, чекаємо оновлення від ДТЕК")
    if st.get("updated"):
        parts.append(f"\n<i>Оновлення ДТЕК: {st['updated']}</i>")
    return "\n".join(parts)


def ping(st: dict, text: str, now: datetime) -> None:
    """Одна відповідь на повідомлення про відключення; попередню видаляємо."""
    if ping_id := st.pop("ping_id", None):
        delete(ping_id)
    st["ping_id"] = send_text(text, reply_to=st["message_id"])
    st["last_ping"] = now.timestamp()
    log(f"Пінг {st['ping_id']}: {text}")


def check_outage(s: dtek.Schedule, state: dict) -> None:
    """Окреме повідомлення про позапланове відключення. Змінює state["outage"]."""
    st = state.get("outage") or {}
    new, old = parse_dtek_time(s.outage_updated), parse_dtek_time(st.get("updated"))
    if new and old and new < old:
        log(f"Дані про відключення старі ({s.outage_updated} < {st['updated']}) - пропускаю")
        return

    now = datetime.now(KYIV)
    o = s.outage
    if o:
        log(f"Відключення: {o}, оновлення ДТЕК: {s.outage_updated}")
    active = bool(st.get("message_id")) and not st.get("restored_at")

    if o and unscheduled(o):
        if not active:
            st = {
                "type": o["type"],
                "start": o["start"],
                "ends": [o["end"]] if o["end"] else [],
                "updated": s.outage_updated,
            }
            st["text"] = outage_text(st, now)
            st["message_id"] = send_text(st["text"])
            state["outage"] = st
            log(f"Нове повідомлення про відключення {st['message_id']}")
            return
        # Та сама подія (навіть якщо ДТЕК змінив причину чи час початку) - лише редагуємо
        st.pop("missing", None)
        st.pop("missing_since", None)
        st.update(type=o["type"], start=o["start"])
        ends = st["ends"]
        if o["end"] and (not ends or ends[-1] != o["end"]):
            prev = parse_dtek_time(ends[-1]) if ends else None
            ends.append(o["end"])
            st["end_changed_at"] = f"{now:%H:%M}"
            log(f"Час відновлення змінився: {ends}")
            # Пінгуємо лише тих, хто вже дочекався обіцяного часу і не отримав світла
            if prev and prev < now and now.timestamp() - st.get("last_ping", 0) >= PING_INTERVAL:
                ping(st, f"⏳ Час відновлення перенесено на <b>{short(o['end'])}</b>", now)
    elif active:
        st.setdefault("missing_since", f"{now:%H:%M}")
        st["missing"] = st.get("missing", 0) + 1
        if st["missing"] < RESTORE_AFTER:
            log(f"Відключення немає ({st['missing']}/{RESTORE_AFTER}) - чекаю підтвердження")
            return
        st["restored_at"] = st["missing_since"]
        if ping_id := st.pop("ping_id", None):
            delete(ping_id)
        log(f"Світло відновлено ~{st['restored_at']}")
    else:
        return

    st["updated"] = s.outage_updated or st.get("updated")
    text = outage_text(st, now)
    if text == st.get("text"):
        return
    try:
        edit_text(st["message_id"], text)
        log(f"Оновлено повідомлення про відключення {st['message_id']}")
    except TgError as e:
        if e.gone and not st.get("restored_at"):
            st["message_id"] = send_text(text)
            log(f"Повідомлення про відключення зникло - надіслав нове {st['message_id']}")
        elif not (e.gone or e.not_modified):
            raise
    st["text"] = text


def check(force: bool = False) -> None:
    s = dtek.fetch(STREET, HOUSE)
    state = load_state()
    log(f"Лінії: {s.lines}, оновлення ДТЕК: {s.updated}")
    try:
        check_outage(s, state)
    except TgError as e:  # тимчасова помилка - повтор наступного запуску, графік не блокуємо
        log(f"Відключення: {e}")
    try:
        check_schedule(s, state, force)
    finally:
        save_state(state)


def check_schedule(s: dtek.Schedule, state: dict, force: bool) -> None:
    """Повідомлення з графіком. Змінює state на місці."""
    sig = signature(s)
    title = f"{STREET}, {HOUSE}"
    day = state.get("day")
    if not force and day and s.today < day:
        log("Сайт віддав вчорашні дані (кеш) - пропускаю")
        return

    if force or not state.get("message_id") or s.today > day:
        mid = send(render(s, title), caption(s, None))
        log(f"Нове повідомлення {mid}")
        state.update(fresh_state(s, mid, sig))
        return

    if reason := regression(s, state):
        log(f"Ігнорую відповідь сайту: {reason}")
        return

    if state.get("sig") == sig:
        # Графік той самий - оновлюємо лише позначку часу, примітку лишаємо попередню
        note = state.get("note")
    else:
        note = f"Графік оновився о {datetime.now(KYIV):%H:%M}"
        if sorted(state.get("lines", [])) != sorted(s.lines):
            note += " (змінились лінії)"

    try:
        edit(state["message_id"], render(s, title), caption(s, note))
        log(
            f"Оновлено повідомлення {state['message_id']}"
            + (" (новий графік)" if state.get("sig") != sig else "")
        )
    except TgError as e:
        if e.gone:
            mid = send(render(s, title), caption(s, note))
            log(f"Повідомлення {state['message_id']} недоступне - надіслав нове {mid}")
            state.update(fresh_state(s, mid, sig, note))
            return
        if not e.not_modified:
            raise  # тимчасова помилка (429, 5xx) - спробуємо наступного запуску
        log("Повідомлення вже актуальне")

    state.update(
        sig=sig,
        note=note,
        lines=s.lines,
        updated=s.updated,
        has_tomorrow=has_day(s, s.tomorrow),
    )


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
        if (o := s.outage) and unscheduled(o):
            st = {"type": o["type"], "start": o["start"], "ends": [o["end"]] if o["end"] else []}
            st["updated"] = s.outage_updated
            print("\n---\n" + outage_text(st, datetime.now(KYIV)))
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
