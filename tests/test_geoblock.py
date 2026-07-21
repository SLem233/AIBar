"""GeoBlockGuard: detector cascade — ambiguous answers fall through."""

import requests

from aibar import geoblock
from aibar.geoblock import DETECTOR_URLS, GATED_PROVIDERS, GeoBlockGuard


class CascadeProbe:
    """Fake probe returning scripted statuses in order; records every call."""

    def __init__(self, *statuses):
        self.statuses = list(statuses)
        self.calls: list[str] = []

    def __call__(self, url: str):
        self.calls.append(url)
        return self.statuses.pop(0) if self.statuses else None


# ---- probe_status ---------------------------------------------------------

def test_probe_status_returns_http_code(monkeypatch):
    class Resp:
        status_code = 403

    monkeypatch.setattr(
        geoblock.requests, "get", lambda url, **kwargs: Resp()
    )
    assert geoblock.probe_status("https://api.anthropic.com/") == 403


def test_probe_status_sends_no_authorization(monkeypatch):
    seen = {}

    class Resp:
        status_code = 401

    def fake_get(url, headers=None, timeout=None, **kwargs):
        seen["headers"] = headers or {}
        return Resp()

    monkeypatch.setattr(geoblock.requests, "get", fake_get)
    geoblock.probe_status("https://chatgpt.com/backend-api/wham/usage")
    assert "Authorization" not in seen["headers"]


def test_probe_status_none_on_network_error(monkeypatch):
    def fake_get(url, **kwargs):
        raise requests.ConnectionError("no route")

    monkeypatch.setattr(geoblock.requests, "get", fake_get)
    assert geoblock.probe_status("https://api.openai.com/v1/models") is None


# ---- detector cascade -----------------------------------------------------

def test_gated_providers_are_the_three_vpn_dependent_ones():
    assert GATED_PROVIDERS == {"Claude", "Codex", "OpenAI API"}


def test_403_on_first_detector_means_blocked():
    probe = CascadeProbe(403)
    guard = GeoBlockGuard(probe=probe)
    assert guard.geo_blocked() is True
    assert probe.calls == [DETECTOR_URLS[0]]


def test_clear_answer_on_first_detector_means_open():
    probe = CascadeProbe(404)  # anthropic root answers 404 when reachable
    guard = GeoBlockGuard(probe=probe)
    assert guard.geo_blocked() is False
    assert probe.calls == [DETECTOR_URLS[0]]


def test_rate_limited_detector_falls_through_to_the_next():
    # The bug seen live: anthropic answered 429 and masked the geo-block.
    probe = CascadeProbe(429, 403)
    guard = GeoBlockGuard(probe=probe)
    assert guard.geo_blocked() is True
    assert probe.calls == list(DETECTOR_URLS[:2])


def test_rate_limited_then_reachable_means_open():
    probe = CascadeProbe(429, 401)
    guard = GeoBlockGuard(probe=probe)
    assert guard.geo_blocked() is False


def test_network_error_falls_through_to_the_next_detector():
    probe = CascadeProbe(None, 403)
    guard = GeoBlockGuard(probe=probe)
    assert guard.geo_blocked() is True


def test_all_detectors_ambiguous_fails_open():
    # Fully offline: fetches must run and report honest network errors.
    probe = CascadeProbe(None, None, None)
    guard = GeoBlockGuard(probe=probe)
    assert guard.geo_blocked() is False
    assert probe.calls == list(DETECTOR_URLS)


# ---- is_geo_block: the probe-passed-but-fetch-403 race ---------------------

def test_race_vpn_drop_between_probe_and_fetch_is_geo_block():
    guard = GeoBlockGuard(probe=CascadeProbe(403))
    assert guard.is_geo_block("Claude", 403) is True


def test_403_with_reachable_endpoints_is_a_real_auth_error():
    guard = GeoBlockGuard(probe=CascadeProbe(404))
    assert guard.is_geo_block("Codex", 403) is False


def test_ungated_provider_403_is_never_geo_block():
    probe = CascadeProbe(403)
    guard = GeoBlockGuard(probe=probe)
    assert guard.is_geo_block("Cursor", 403) is False
    assert probe.calls == []


def test_non_403_status_never_probes():
    probe = CascadeProbe(403)
    guard = GeoBlockGuard(probe=probe)
    assert guard.is_geo_block("Claude", 401) is False
    assert guard.is_geo_block("Claude", None) is False
    assert probe.calls == []
