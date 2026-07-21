"""poll_all: pause gated providers on geo-block, keep last good data."""

from aibar.geoblock import GeoBlockGuard
from aibar.polling import poll_all
from aibar.providers.base import ProviderSnapshot, RateWindow


class FetchSpy:
    """Provider fetch stub returning scripted snapshots; counts calls."""

    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.calls = 0

    def __call__(self, cfg):
        self.calls += 1
        return self.snapshot


def good_snapshot(name="Claude", percent=42.0):
    return ProviderSnapshot(
        provider=name, windows=[RateWindow("Сессия (5ч)", percent)]
    )


def blocked_snapshot(name="Claude"):
    return ProviderSnapshot(provider=name, error="HTTP 403", http_status=403)


class CountingProbe:
    def __init__(self, status):
        self.status = status
        self.calls = 0

    def __call__(self, url):
        self.calls += 1
        return self.status


def make_guard(probe_status):
    return GeoBlockGuard(probe=lambda url: probe_status)


def test_normal_cycle_passes_snapshots_through():
    fetch = FetchSpy(good_snapshot())
    snaps = poll_all(
        {"providers": ["Claude"]}, make_guard(401), {}, registry={"Claude": fetch}
    )
    assert fetch.calls == 1
    assert snaps[0].paused is False
    assert snaps[0].session_percent == 42.0


def test_single_probe_per_cycle_covers_all_gated_providers():
    probe = CountingProbe(401)
    guard = GeoBlockGuard(probe=probe)
    registry = {name: FetchSpy(good_snapshot(name)) for name in
                ("Claude", "Codex", "OpenAI API")}
    poll_all(
        {"providers": list(registry)}, guard, {}, registry=registry
    )
    assert probe.calls == 1  # one anonymous request decides for all three
    assert all(fetch.calls == 1 for fetch in registry.values())


def test_single_403_probe_pauses_all_gated_providers():
    probe = CountingProbe(403)
    guard = GeoBlockGuard(probe=probe)
    registry = {name: FetchSpy(good_snapshot(name)) for name in
                ("Claude", "Codex", "OpenAI API")}
    snaps = poll_all(
        {"providers": list(registry)}, guard, {}, registry=registry
    )
    assert probe.calls == 1
    assert all(fetch.calls == 0 for fetch in registry.values())
    assert all(s.paused for s in snaps)


def test_ungated_providers_keep_working_while_blocked():
    cursor = FetchSpy(good_snapshot("Cursor"))
    claude = FetchSpy(good_snapshot("Claude"))
    snaps = poll_all(
        {"providers": ["Claude", "Cursor"]},
        make_guard(403),
        {},
        registry={"Claude": claude, "Cursor": cursor},
    )
    assert claude.calls == 0 and cursor.calls == 1
    assert snaps[0].paused is True and snaps[1].paused is False


def test_no_probe_at_all_when_no_gated_provider_is_enabled():
    probe = CountingProbe(403)
    guard = GeoBlockGuard(probe=probe)
    cursor = FetchSpy(good_snapshot("Cursor"))
    poll_all({"providers": ["Cursor"]}, guard, {}, registry={"Cursor": cursor})
    assert probe.calls == 0
    assert cursor.calls == 1


def test_geo_403_yields_paused_snapshot_with_last_good_data():
    fetch = FetchSpy(good_snapshot(percent=55.0))
    status = {"value": 401}
    guard = GeoBlockGuard(probe=lambda url: status["value"])
    last_good = {}
    registry = {"Claude": fetch}
    cfg = {"providers": ["Claude"]}

    poll_all(cfg, guard, last_good, registry=registry)  # cycle 1: VPN on
    status["value"] = 403  # VPN went down
    snaps = poll_all(cfg, guard, last_good, registry=registry)

    assert fetch.calls == 1  # cycle 2 never sent the token
    assert snaps[0].paused is True
    assert snaps[0].error is None  # a recognized state, not an error
    assert snaps[0].session_percent == 55.0  # stale data survives


def test_no_token_request_ever_while_geo_blocked():
    fetch = FetchSpy(good_snapshot())
    guard = make_guard(403)
    registry = {"Claude": fetch}
    cfg = {"providers": ["Claude"]}

    snaps = poll_all(cfg, guard, {}, registry=registry)  # first cycle ever
    poll_all(cfg, guard, {}, registry=registry)

    assert fetch.calls == 0  # probe-first: the token never left the machine
    assert snaps[0].paused is True
    assert snaps[0].windows == []  # nothing good was ever fetched


def test_race_vpn_drops_between_probe_and_fetch():
    # Probe said "reachable", but the authenticated fetch hit 403 and the
    # confirming re-probe sees the geo-block.
    statuses = iter([401, 403])  # should_fetch probe, then is_geo_block probe
    guard = GeoBlockGuard(probe=lambda url: next(statuses))
    fetch = FetchSpy(blocked_snapshot())
    snaps = poll_all(
        {"providers": ["Claude"]}, guard, {}, registry={"Claude": fetch}
    )
    assert snaps[0].paused is True
    assert snaps[0].error is None


def test_provider_resumes_after_vpn_returns():
    fetch = FetchSpy(good_snapshot(percent=13.0))
    status = {"value": 403}
    guard = GeoBlockGuard(probe=lambda url: status["value"])
    registry = {"Claude": fetch}
    cfg = {"providers": ["Claude"]}

    poll_all(cfg, guard, {}, registry=registry)  # blocked, no fetch
    status["value"] = 401  # VPN restored
    snaps = poll_all(cfg, guard, {}, registry=registry)

    assert fetch.calls == 1
    assert snaps[0].paused is False
    assert snaps[0].session_percent == 13.0


def test_auth_403_stays_an_error():
    fetch = FetchSpy(blocked_snapshot("Codex"))
    snaps = poll_all(
        {"providers": ["Codex"]}, make_guard(401), {}, registry={"Codex": fetch}
    )
    assert snaps[0].paused is False
    assert snaps[0].error == "HTTP 403"


def test_fetch_exception_becomes_error_snapshot():
    def boom(cfg):
        raise ValueError("kaput")

    snaps = poll_all(
        {"providers": ["Claude"]}, make_guard(401), {}, registry={"Claude": boom}
    )
    assert snaps[0].error == "kaput"


def test_unknown_provider_names_are_skipped():
    snaps = poll_all({"providers": ["Nope"]}, make_guard(401), {}, registry={})
    assert snaps == []
