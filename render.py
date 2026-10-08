"""Малювання картинки з графіком (сьогодні + завтра, по рядку на кожну лінію)."""

import io
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

KYIV = ZoneInfo("Europe/Kyiv")
WEEKDAYS = ["понеділок", "вівторок", "середа", "четвер", "пʼятниця", "субота", "неділя"]

S = 2  # масштаб для чіткості
PAD = 24 * S
LABEL_W = 150 * S
CELL_W = 38 * S
HEAD_H = 64 * S
ROW_H = 46 * S
DAY_GAP = 28 * S
DAY_TITLE_H = 40 * S

BG = "#f4f5f7"
CARD = "#ffffff"
GRID = "#d5d9df"
TEXT = "#1d2433"
MUTED = "#6b7280"
OFF = "#9aa3b0"
MAYBE = "#f6d77a"
TODAY_ACCENT = "#ffd400"
TOMORROW_ACCENT = "#c9d3e0"
LINE_COLORS = ["#2f6fed", "#e8590c", "#2b8a3e", "#9c36b5"]
NOW = "#e03131"
NOW_TINT = "#fde2e2"

FONT_DIRS = [
    "/usr/share/fonts/noto/NotoSans-{}.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-{}.ttf",
    "/usr/share/fonts/TTF/DejaVuSans{}.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans{}.ttf",
]


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    if path := os.getenv("FONT_BOLD" if bold else "FONT"):
        return ImageFont.truetype(path, size * S)
    for tpl in FONT_DIRS:
        style = "Bold" if bold else "Regular"
        path = tpl.format(style if "Noto" in tpl else ("-Bold" if bold else ""))
        if os.path.exists(path):
            return ImageFont.truetype(path, size * S)
    return ImageFont.load_default(size * S)


def day_title(ts: int) -> str:
    d = datetime.fromtimestamp(ts, KYIV)
    return f"{d:%d.%m}, {WEEKDAYS[d.weekday()]}"


def halves(state: str) -> tuple[str | None, str | None]:
    """Стан години -> колір першої та другої половини (None = світло є)."""
    return {
        "no": (OFF, OFF),
        "first": (OFF, None),
        "second": (None, OFF),
        "maybe": (MAYBE, MAYBE),
        "mfirst": (MAYBE, None),
        "msecond": (None, MAYBE),
    }.get(state, (None, None))


def _bolt(draw: ImageDraw.ImageDraw, cx: float, cy: float, h: float, color: str) -> None:
    w = h * 0.55
    pts = [
        (cx + w * 0.15, cy - h / 2),
        (cx - w / 2, cy + h * 0.08),
        (cx - w * 0.02, cy + h * 0.08),
        (cx - w * 0.15, cy + h / 2),
        (cx + w / 2, cy - h * 0.08),
        (cx + w * 0.02, cy - h * 0.08),
    ]
    draw.polygon(pts, fill=color)


def _vertical_text(img: Image.Image, text: str, x: int, y: int, w: int, h: int, font) -> None:
    tmp = Image.new("RGBA", (h, w), (0, 0, 0, 0))
    d = ImageDraw.Draw(tmp)
    d.text((h / 2, w / 2), text, font=font, fill=TEXT, anchor="mm")
    tmp = tmp.rotate(90, expand=True)
    img.paste(tmp, (x, y), tmp)


def _now_marker(draw, x0: int, top: int, h: int, now: datetime, f) -> None:
    """Червона риска поточного часу через шапку й рядки + підпис під таблицею."""
    x = x0 + LABEL_W + (now.hour + now.minute / 60) * CELL_W
    draw.line((x, top, x, top + h), fill="#ffffff", width=6 * S)
    draw.line((x, top, x, top + h), fill=NOW, width=3 * S)
    r = 5 * S
    draw.ellipse((x - r, top - r, x + r, top + r), fill=NOW)
    label = f"{now:%H:%M}"
    tw = draw.textlength(label, font=f["now"])
    pw, ph = tw + 14 * S, 22 * S
    # не виходимо за межі таблиці
    px = min(max(x - pw / 2, x0 + LABEL_W), x0 + LABEL_W + 24 * CELL_W - pw)
    py = top + h + 3 * S
    draw.rounded_rectangle((px, py, px + pw, py + ph), radius=ph / 2, fill=NOW)
    draw.text((px + pw / 2, py + ph / 2), label, font=f["now"], fill="#ffffff", anchor="mm")


def _day_block(img, draw, top: int, title: str, accent: str, sched, day, f, now=None) -> int:
    width = img.width
    table_w = LABEL_W + 24 * CELL_W
    x0 = PAD

    # Заголовок дня з кольоровою плашкою
    draw.rounded_rectangle((x0, top, x0 + table_w, top + DAY_TITLE_H), radius=10 * S, fill=accent)
    draw.text((x0 + 16 * S, top + DAY_TITLE_H / 2), title, font=f["title"], fill=TEXT, anchor="lm")
    top += DAY_TITLE_H + 8 * S

    has_data = day is not None and any(sched.slots(day, ln) for ln in sched.lines)
    if not has_data:
        h = 2 * ROW_H
        draw.rounded_rectangle(
            (x0, top, x0 + table_w, top + h), radius=10 * S, fill=CARD, outline=GRID, width=S
        )
        draw.text(
            (width / 2, top + h / 2),
            "Графік ще не опубліковано",
            font=f["label"],
            fill=MUTED,
            anchor="mm",
        )
        return top + h

    rows = len(sched.lines)
    h = HEAD_H + rows * ROW_H
    draw.rounded_rectangle(
        (x0, top, x0 + table_w, top + h), radius=10 * S, fill=CARD, outline=GRID, width=S
    )

    if now:
        # підсвітка поточної години в шапці
        cx = x0 + LABEL_W + now.hour * CELL_W
        draw.rectangle((cx, top + S, cx + CELL_W, top + HEAD_H), fill=NOW_TINT)

    # Шапка з годинами
    draw.text(
        (x0 + 14 * S, top + HEAD_H / 2),
        "Часові\nпроміжки",
        font=f["label_b"],
        fill=TEXT,
        anchor="lm",
    )
    for i in range(24):
        cx = x0 + LABEL_W + i * CELL_W
        _vertical_text(img, f"{i:02d}-{i + 1:02d}", cx, top, CELL_W, HEAD_H, f["hour"])
        draw.line((cx, top, cx, top + h), fill=GRID, width=S)
    draw.line((x0, top + HEAD_H, x0 + table_w, top + HEAD_H), fill=GRID, width=S)

    for r, line in enumerate(sched.lines):
        y = top + HEAD_H + r * ROW_H
        color = LINE_COLORS[r % len(LINE_COLORS)]
        # кольорова смуга лінії зліва
        draw.rectangle((x0 + 2 * S, y + 6 * S, x0 + 7 * S, y + ROW_H - 6 * S), fill=color)
        draw.text(
            (x0 + 16 * S, y + ROW_H / 2 - 9 * S),
            f"Лінія {r + 1}",
            font=f["label_b"],
            fill=color,
            anchor="lm",
        )
        name = sched.names.get(line, line)
        draw.text(
            (x0 + 16 * S, y + ROW_H / 2 + 10 * S), name, font=f["small"], fill=MUTED, anchor="lm"
        )

        slots = sched.slots(day, line) or {}
        for i in range(24):
            cx = x0 + LABEL_W + i * CELL_W
            a, b = halves(slots.get(str(i + 1), "yes"))
            pad = 3 * S
            if a:
                draw.rectangle((cx + pad, y + pad, cx + CELL_W / 2, y + ROW_H - pad), fill=a)
            if b:
                draw.rectangle(
                    (cx + CELL_W / 2, y + pad, cx + CELL_W - pad, y + ROW_H - pad), fill=b
                )
            if a or b:
                bx = (
                    cx + CELL_W / 2
                    if (a and b)
                    else (cx + CELL_W / 4 if a else cx + 3 * CELL_W / 4)
                )
                _bolt(draw, bx, y + ROW_H / 2, 18 * S, "#ffffff")
        if r < rows - 1:
            # чітке розділення між лініями
            draw.line((x0, y + ROW_H, x0 + table_w, y + ROW_H), fill=TEXT, width=2 * S)
    if now:
        _now_marker(draw, x0, top, h, now, f)
    return top + h


def render(sched, title: str, now: datetime | None = None) -> bytes:
    now = (now or datetime.now(KYIV)).astimezone(KYIV)
    f = {
        "now": _font(12, True),
        "h1": _font(20, True),
        "title": _font(17, True),
        "label": _font(14),
        "label_b": _font(14, True),
        "small": _font(12),
        "hour": _font(12),
    }
    table_w = LABEL_W + 24 * CELL_W
    width = table_w + 2 * PAD
    rows = max(len(sched.lines), 1)
    block_h = DAY_TITLE_H + 8 * S + HEAD_H + rows * ROW_H
    legend_h = 40 * S
    height = PAD + 60 * S + 2 * block_h + DAY_GAP + legend_h + PAD

    img = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(img)

    draw.text((PAD, PAD), title, font=f["h1"], fill=TEXT)
    draw.text(
        (PAD, PAD + 32 * S),
        f"Графік оновлено ДТЕК: {sched.updated}  ·  Зараз (Київ): {now:%d.%m.%Y %H:%M}",
        font=f["label"],
        fill=MUTED,
    )

    y = PAD + 60 * S
    # риску малюємо лише якщо "сьогодні" з графіка справді збігається з поточною датою
    is_today = datetime.fromtimestamp(sched.today, KYIV).date() == now.date()
    y = _day_block(
        img,
        draw,
        y,
        f"Сьогодні · {day_title(sched.today)}",
        TODAY_ACCENT,
        sched,
        sched.today,
        f,
        now if is_today else None,
    )
    y += DAY_GAP
    tomorrow_title = f"Завтра · {day_title(sched.tomorrow)}" if sched.tomorrow else "Завтра"
    y = _day_block(img, draw, y, tomorrow_title, TOMORROW_ACCENT, sched, sched.tomorrow, f)

    # Легенда
    y += 14 * S
    x = PAD
    for kind, label in [
        ("yes", "Світло є"),
        ("no", "Світла немає"),
        ("first", "Немає перші 30 хв"),
        ("second", "Немає другі 30 хв"),
        ("maybe", "Можливе відключення"),
    ]:
        a, b = halves(kind)
        box = 22 * S
        draw.rectangle((x, y, x + box, y + box), fill=CARD, outline=GRID, width=S)
        if a:
            draw.rectangle((x, y, x + box / 2, y + box), fill=a)
        if b:
            draw.rectangle((x + box / 2, y, x + box, y + box), fill=b)
        draw.text((x + box + 8 * S, y + box / 2), label, font=f["small"], fill=TEXT, anchor="lm")
        x += box + 8 * S + int(draw.textlength(label, font=f["small"])) + 22 * S

    img = img.crop((0, 0, width, y + 22 * S + PAD))
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def off_intervals(slots: dict[str, str] | None) -> list[str]:
    """Години без світла у вигляді "03:00–06:30"."""
    if not slots:
        return []
    half = []
    for i in range(24):
        a, b = halves(slots.get(str(i + 1), "yes"))
        half += [a == OFF, b == OFF]
    res, start = [], None
    for i, off in enumerate(half + [False]):
        if off and start is None:
            start = i
        elif not off and start is not None:
            res.append(f"{start // 2:02d}:{start % 2 * 30:02d}–{i // 2:02d}:{i % 2 * 30:02d}")
            start = None
    return res
