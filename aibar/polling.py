"""One poll cycle over the configured providers (pure logic, no Qt).

Wraps the provider registry with the geo-block guard: paused providers are
not fetched (no token leaves the machine), and their tiles keep the last
successfully fetched data, flagged with paused=True.

Обычная ошибка опроса (429 от Anthropic, 5xx, обрыв сети) обрабатывается тем же
приёмом, но флагом stale=True и с сохранением текста ошибки: кольца держат
последние удачные значения, а рядом висит ⚠. Гасить их в прочерк нельзя —
данные в памяти есть, и в мини-режиме провайдер без окон вообще пропадает из
виджета. Дезинформировать бесконечно тоже нельзя, поэтому у переживших данных
есть срок годности.
"""

from dataclasses import replace
from datetime import datetime, timezone

from .geoblock import GATED_PROVIDERS, GeoBlockGuard
from .providers import PROVIDERS
from .providers.base import ProviderSnapshot

# Дольше этого срока старые проценты — уже не «последнее известное», а вымысел.
STALE_MAX_AGE_SECONDS = 6 * 3600


def _paused(name: str, last_good: ProviderSnapshot | None) -> ProviderSnapshot:
    if last_good is None:
        return ProviderSnapshot(provider=name, paused=True)
    return replace(last_good, paused=True, error=None, http_status=None)


def _stale(
    snap: ProviderSnapshot,
    last_good: ProviderSnapshot | None,
    now: datetime | None = None,
) -> ProviderSnapshot:
    """Ошибочный снапшот, дополненный последними удачными окнами."""
    if last_good is None or not last_good.windows:
        return snap  # ничего удачного ещё не приходило — рисовать нечего
    now = now or datetime.now(timezone.utc)
    if (now - last_good.fetched_at).total_seconds() > STALE_MAX_AGE_SECONDS:
        return snap
    return replace(
        snap,
        windows=last_good.windows,
        plan=snap.plan or last_good.plan,
        extra=snap.extra or last_good.extra,
        stale=True,
    )


def poll_all(
    cfg: dict,
    guard: GeoBlockGuard,
    last_good: dict[str, ProviderSnapshot],
    registry: dict | None = None,
) -> list[ProviderSnapshot]:
    registry = PROVIDERS if registry is None else registry
    names = [n for n in cfg.get("providers") or [] if n in registry]
    # The geo-block is shared, so one anonymous check decides for all gated
    # providers this cycle.
    gated = [n for n in names if n in GATED_PROVIDERS]
    blocked = bool(gated) and guard.geo_blocked()
    snapshots = []
    for name in names:
        if blocked and name in GATED_PROVIDERS:
            snapshots.append(_paused(name, last_good.get(name)))
            continue
        try:
            snap = registry[name](cfg)
        except Exception as exc:  # a provider crash must not kill polling
            snap = ProviderSnapshot(provider=name, error=str(exc))
        if guard.is_geo_block(name, snap.http_status):
            snap = _paused(name, last_good.get(name))
        elif snap.error:
            snap = _stale(snap, last_good.get(name))
        else:
            last_good[name] = snap
        snapshots.append(snap)
    return snapshots
