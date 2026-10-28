"""Расписания задач и расчёт следующего запуска (локальное время, без внешних библиотек).

Виды: once — однократно; interval — каждые N минут; daily — ежедневно; weekly — по дням недели;
monthly — в день месяца (если такого дня нет — в последний день месяца); on_connect — после подключения.
"""
from __future__ import annotations

import calendar
import datetime as dt
import re
from dataclasses import asdict, dataclass, field

KINDS = ("once", "interval", "daily", "weekly", "monthly", "on_connect")
WEEKDAYS_RU = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


@dataclass
class Schedule:
    kind: str = "daily"
    time: str = "08:00"                     # ЧЧ:ММ для daily/weekly/monthly
    weekdays: list[int] = field(default_factory=lambda: [0])   # 0 = понедельник
    day: int = 1                            # для monthly
    interval_minutes: int = 60              # для interval
    at: str = ""                            # ISO-дата/время для once
    start: str = ""                         # ISO: начало для interval (по умолчанию — момент создания)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Schedule":
        names = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in (d or {}).items() if k in names})


def _hm(s: Schedule) -> tuple[int, int]:
    m = _TIME.match(s.time or "")
    if not m:
        raise ValueError("Время указывается как ЧЧ:ММ (например, 08:30).")
    return int(m.group(1)), int(m.group(2))


def parse_dt(text: str) -> dt.datetime:
    text = (text or "").strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError("Дата и время указываются как ГГГГ-ММ-ДД ЧЧ:ММ.")


def validate(s: Schedule) -> list[str]:
    errs = []
    if s.kind not in KINDS:
        return [f"Неизвестный вид расписания: {s.kind}."]
    try:
        if s.kind in ("daily", "weekly", "monthly"):
            _hm(s)
        if s.kind == "weekly" and (not s.weekdays or any(w not in range(7) for w in s.weekdays)):
            errs.append("Выберите хотя бы один день недели.")
        if s.kind == "monthly" and not (1 <= int(s.day) <= 31):
            errs.append("День месяца — от 1 до 31.")
        if s.kind == "interval" and not (1 <= int(s.interval_minutes) <= 7 * 24 * 60):
            errs.append("Интервал — от 1 минуты до 7 дней.")
        if s.kind == "once":
            parse_dt(s.at)
        if s.kind == "interval" and s.start:
            parse_dt(s.start)
    except (ValueError, TypeError) as exc:
        errs.append(str(exc))
    return errs


def next_run(s: Schedule, after: dt.datetime) -> dt.datetime | None:
    """Ближайший запуск строго позже after (None — запусков больше не будет или запуск по событию)."""
    after = after.replace(microsecond=0)
    if s.kind == "on_connect":
        return None
    if s.kind == "once":
        at = parse_dt(s.at)
        return at if at > after else None
    if s.kind == "interval":
        step = dt.timedelta(minutes=int(s.interval_minutes))
        start = parse_dt(s.start) if s.start else after
        if start > after:
            return start
        k = (after - start) // step + 1
        return start + k * step
    h, m = _hm(s)
    if s.kind == "daily":
        cand = after.replace(hour=h, minute=m, second=0)
        return cand if cand > after else cand + dt.timedelta(days=1)
    if s.kind == "weekly":
        days = set(int(w) for w in s.weekdays)
        for i in range(0, 8):
            d = after.date() + dt.timedelta(days=i)
            if d.weekday() in days:
                cand = dt.datetime(d.year, d.month, d.day, h, m)
                if cand > after:
                    return cand
        return None
    if s.kind == "monthly":
        y, mo = after.year, after.month
        for _ in range(14):
            last = calendar.monthrange(y, mo)[1]
            cand = dt.datetime(y, mo, min(int(s.day), last), h, m)
            if cand > after:
                return cand
            mo += 1
            if mo == 13:
                y, mo = y + 1, 1
        return None
    return None


def describe(s: Schedule) -> str:
    if s.kind == "on_connect":
        return "После подключения к серверу"
    if s.kind == "once":
        return f"Однократно {s.at.replace('T', ' ')[:16]}"
    if s.kind == "interval":
        n = int(s.interval_minutes)
        return f"Каждые {n} мин" if n < 60 or n % 60 else f"Каждые {n // 60} ч"
    if s.kind == "daily":
        return f"Ежедневно в {s.time}"
    if s.kind == "weekly":
        return "Еженедельно: " + ", ".join(WEEKDAYS_RU[w] for w in sorted(set(s.weekdays))) + f" в {s.time}"
    if s.kind == "monthly":
        extra = " (если дня нет в месяце — в последний день)" if int(s.day) > 28 else ""
        return f"Ежемесячно, {s.day}-го числа в {s.time}{extra}"
    return s.kind
