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


def next_monthly_anniversary(anchor: datetime, now: datetime | None = None) -> datetime:
    """Next monthly recurrence of the anchor's day-of-month (billing day)."""
    now = now or datetime.now(timezone.utc)
    year, month = now.year, now.month
    for _ in range(3):
        day = min(anchor.day, [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
        candidate = now.replace(year=year, month=month, day=day, hour=anchor.hour, minute=anchor.minute, second=0, microsecond=0)
        if candidate > now:
            return candidate
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return candidate


def billing_renewal_date(cfg: dict | None, key: str) -> str:
    """Next billing date from a user-set day-of-month setting ('' if unset).

    Providers whose APIs expose no billing anchor rely on this.
    """
    try:
        day = int((cfg or {}).get(key) or 0)
    except (TypeError, ValueError):
        return ""
    if not 1 <= day <= 31:
        return ""
    anchor = datetime(2000, 1, day, tzinfo=timezone.utc)
    return format_date(next_monthly_anniversary(anchor))


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
