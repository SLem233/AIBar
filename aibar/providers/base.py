"""Common data model for provider usage snapshots."""

import base64
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class RateWindow:
    """One rate-limit window (e.g. 5h session or weekly quota)."""

    label: str
    used_percent: float
    resets_at: datetime | None = None

    def reset_countdown(self, now: datetime | None = None) -> str:
        if self.resets_at is None:
            return ""
        now = now or datetime.now(timezone.utc)
        delta = self.resets_at - now
        total = int(delta.total_seconds())
        if total <= 0:
            return "сейчас"
        days, rem = divmod(total, 86400)
        hours, rem = divmod(rem, 3600)
        minutes = rem // 60
        if days:
            return f"{days}д {hours}ч"
        if hours:
            return f"{hours}ч {minutes}м"
        return f"{minutes}м"


@dataclass
class ProviderSnapshot:
    """Result of one usage poll for a provider."""

    provider: str
    plan: str = ""
    windows: list[RateWindow] = field(default_factory=list)
    extra: dict = field(default_factory=dict)
    error: str | None = None
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def session_percent(self) -> float | None:
        return self.windows[0].used_percent if self.windows else None

    @property
    def weekly_percent(self) -> float | None:
        return self.windows[1].used_percent if len(self.windows) > 1 else None


def parse_iso8601(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def parse_unix(value: int | float | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None


def format_date(dt: datetime | None) -> str:
    return dt.astimezone().strftime("%d.%m.%Y") if dt else ""


PERIOD_MONTHS = {"month": 1, "quarter": 3, "year": 12}

_MONTH_DAYS = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]


def add_months(dt: datetime, months: int) -> datetime:
    """Shift by whole months, clamping the day (31 Jan + 1 mo -> 28/29 Feb)."""
    total = dt.year * 12 + (dt.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    max_day = 29 if month == 2 and leap else _MONTH_DAYS[month - 1]
    return dt.replace(year=year, month=month, day=min(dt.day, max_day))


def subscription_renewal(cfg: dict | None, prefix: str) -> str:
    """Next renewal date from user settings ('' if unset).

    For providers whose APIs expose no billing anchor: the user enters a paid
    date (<prefix>_renewal_date) and cycle (<prefix>_renewal_period:
    month/quarter/year); past dates roll forward by the cycle automatically.
    """
    raw = str((cfg or {}).get(f"{prefix}_renewal_date") or "").strip()
    if not raw:
        return ""
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.%y"):
        try:
            dt = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
            break
        except ValueError:
            continue
    else:
        return ""
    months = PERIOD_MONTHS.get((cfg or {}).get(f"{prefix}_renewal_period") or "month", 1)
    now = datetime.now(timezone.utc)
    for _ in range(600):  # bounded roll-forward
        if dt >= now:
            break
        dt = add_months(dt, months)
    return format_date(dt)


def looks_like_api_key(value: str) -> bool:
    """True if the value could be an API key: one line of printable ASCII."""
    return (
        len(value) >= 8
        and all(33 <= ord(c) <= 126 for c in value)
    )


def decode_jwt_payload(token: str) -> dict:
    """Decode a JWT payload without verifying the signature."""
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, json.JSONDecodeError):
        return {}
