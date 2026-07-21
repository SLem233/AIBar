"""Geo-block ("no VPN") detection for providers unreachable without VPN.

Claude, Codex and the OpenAI API answer HTTP 403 when reached from a
geo-blocked network. Instead of watching VPN adapters, we check the thing
that actually matters — endpoint reachability — with anonymous requests that
carry NO credentials. Verified live: without VPN the Cloudflare edge answers
403 to anonymous requests on all three domains; with VPN they answer
404/401/200.

Probe-first: every poll cycle ONE anonymous check decides for all gated
providers (the block is shared). On 403 the authenticated fetches are
skipped entirely: no token leaves the machine without VPN.

Detection is a cascade because a single URL is not reliable: the anthropic
usage endpoint was seen answering 429 (rate limit) to anonymous requests,
which masked the geo-block. Ambiguous answers (429, network error) fall
through to the next detector; only a clear 403 blocks, any other clear
status opens. All detectors ambiguous -> fail open, so a fully offline
machine shows honest network errors instead of a misleading "no VPN".
"""

import requests

# Providers that are unreachable without VPN (share one geo-block).
GATED_PROVIDERS = {"Claude", "Codex", "OpenAI API"}

# Anonymous, cheap, geo-blocked at the edge. Order = preference: the
# anthropic root answers a tiny 404 when reachable and is not rate-limited.
DETECTOR_URLS = (
    "https://api.anthropic.com/",
    "https://api.openai.com/v1/models",
    "https://chatgpt.com/backend-api/wham/usage",
)

_AMBIGUOUS = (429, None)  # rate limit / network error: try the next detector

PAUSED_MESSAGE = "Нет VPN — данные не обновляются"


def probe_status(url: str) -> int | None:
    """HTTP status of an unauthenticated GET, or None on network error."""
    try:
        resp = requests.get(
            url,
            headers={"Accept": "application/json", "User-Agent": "AIBar"},
            timeout=10,
            allow_redirects=False,
        )
    except requests.RequestException:
        return None
    return resp.status_code


class GeoBlockGuard:
    """Decides per cycle whether gated providers may be fetched with tokens."""

    def __init__(self, probe=probe_status):
        self._probe = probe

    def geo_blocked(self) -> bool:
        """One anonymous check of the shared geo-block (detector cascade)."""
        for url in DETECTOR_URLS:
            status = self._probe(url)
            if status in _AMBIGUOUS:
                continue
            return status == 403
        return False  # everything ambiguous — fail open

    def is_geo_block(self, provider: str, http_status: int | None) -> bool:
        """Classify a 403 that slipped through (VPN dropped mid-cycle)."""
        if http_status != 403 or provider not in GATED_PROVIDERS:
            return False
        return self.geo_blocked()
