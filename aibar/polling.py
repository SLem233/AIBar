"""One poll cycle over the configured providers (pure logic, no Qt).

Wraps the provider registry with the geo-block guard: paused providers are
not fetched (no token leaves the machine), and their tiles keep the last
successfully fetched data, flagged with paused=True.
"""

from dataclasses import replace

from .geoblock import GATED_PROVIDERS, GeoBlockGuard
from .providers import PROVIDERS
from .providers.base import ProviderSnapshot


def _paused(name: str, last_good: ProviderSnapshot | None) -> ProviderSnapshot:
    if last_good is None:
        return ProviderSnapshot(provider=name, paused=True)
    return replace(last_good, paused=True, error=None, http_status=None)


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
        elif not snap.error:
            last_good[name] = snap
        snapshots.append(snap)
    return snapshots
