"""Minimaler Cron-Parser (5 Felder: Minute Stunde Tag Monat Wochentag)."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

FIELDS = [("minute", 0, 59), ("hour", 0, 23), ("day", 1, 31), ("month", 1, 12), ("weekday", 0, 6)]
NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10,
    "nov": 11, "dec": 12, "sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6,
}


class CronError(ValueError):
    pass


def _value(token: str, lo: int, hi: int, weekday: bool) -> int:
    t = token.lower()
    if t in NAMES:
        return NAMES[t]
    try:
        v = int(t)
    except ValueError as exc:
        raise CronError(f"Ungültiger Wert '{token}'") from exc
    if weekday and v == 7:
        v = 0
    if not lo <= v <= hi:
        raise CronError(f"Wert {v} außerhalb {lo}-{hi}")
    return v


def _parse_field(expr: str, lo: int, hi: int, weekday: bool = False) -> set[int]:
    out: set[int] = set()
    for part in expr.split(","):
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            try:
                step = int(step_s)
            except ValueError as exc:
                raise CronError(f"Ungültige Schrittweite '{step_s}'") from exc
            if step <= 0:
                raise CronError("Schrittweite muss > 0 sein")
        if part in ("*", ""):
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = _value(a, lo, hi + (1 if weekday else 0), weekday), _value(b, lo, hi + (1 if weekday else 0), False)
            if weekday and end == 7:
                end = 6 if start > 0 else 6
        else:
            start = _value(part, lo, hi + (1 if weekday else 0), weekday)
            end = start if "/" not in expr else hi
        if start > end:
            raise CronError(f"Bereich {start}-{end} ungültig")
        out.update(range(start, end + 1, step))
    return out


class Cron:
    def __init__(self, expr: str):
        parts = expr.split()
        if len(parts) != 5:
            raise CronError("Ein Cron-Ausdruck braucht 5 Felder: Minute Stunde Tag Monat Wochentag")
        self.expr = expr
        self.minutes = _parse_field(parts[0], 0, 59)
        self.hours = _parse_field(parts[1], 0, 23)
        self.days = _parse_field(parts[2], 1, 31)
        self.months = _parse_field(parts[3], 1, 12)
        self.weekdays = _parse_field(parts[4], 0, 6, weekday=True)
        self.day_any = parts[2] == "*"
        self.wday_any = parts[4] == "*"

    def _day_matches(self, dt: datetime) -> bool:
        wd = (dt.weekday() + 1) % 7  # Python: Montag=0 -> Cron: Sonntag=0
        dom = dt.day in self.days
        dow = wd in self.weekdays
        if self.day_any and self.wday_any:
            return True
        if self.day_any:
            return dow
        if self.wday_any:
            return dom
        return dom or dow  # klassische Cron-Semantik

    def next_after(self, after: datetime, tz: str = "Europe/Berlin") -> datetime:
        """Nächster Zeitpunkt strikt nach ``after`` (Rückgabe zeitzonenbehaftet in tz)."""
        zone = ZoneInfo(tz)
        local = after.astimezone(zone).replace(second=0, microsecond=0) + timedelta(minutes=1)
        limit = local + timedelta(days=366 * 5)
        while local < limit:
            if local.month not in self.months:
                local = (local.replace(day=1, hour=0, minute=0) + timedelta(days=32)).replace(day=1)
                continue
            if not self._day_matches(local):
                local = local.replace(hour=0, minute=0) + timedelta(days=1)
                continue
            if local.hour not in self.hours:
                local = local.replace(minute=0) + timedelta(hours=1)
                continue
            if local.minute not in self.minutes:
                local += timedelta(minutes=1)
                continue
            # Zeitzonenumstellung: Zeitpunkt normalisieren
            return local.replace(tzinfo=zone)
        raise CronError("Kein Ausführungszeitpunkt innerhalb von 5 Jahren")


def cron_for(kind: str, time_hm: str | None = None, weekday: int | None = None, day: int | None = None,
             custom: str | None = None) -> str:
    """Erzeugt den Cron-Ausdruck für die vordefinierten Zeitplantypen."""
    if kind == "custom":
        if not custom:
            raise CronError("Benutzerdefinierter Zeitplan ohne Cron-Ausdruck")
        Cron(custom)
        return custom.strip()
    hh, mm = 3, 0
    if time_hm:
        try:
            hh_s, mm_s = time_hm.split(":")
            hh, mm = int(hh_s), int(mm_s)
        except ValueError as exc:
            raise CronError("Uhrzeit im Format HH:MM angeben") from exc
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise CronError("Ungültige Uhrzeit")
    if kind == "daily":
        return f"{mm} {hh} * * *"
    if kind == "weekly":
        wd = 0 if weekday is None else int(weekday) % 7
        return f"{mm} {hh} * * {wd}"
    if kind == "monthly":
        d = 1 if day is None else int(day)
        if not 1 <= d <= 28:
            raise CronError("Für monatliche Zeitpläne Tag 1–28 wählen")
        return f"{mm} {hh} {d} * *"
    raise CronError(f"Unbekannter Zeitplantyp {kind}")
