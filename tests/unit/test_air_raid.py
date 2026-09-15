"""Unit coverage for the air-raid pause's two external pieces: geography and alerts.in.ua.

Neither test touches the network. `resolve_region` reads the bundled oblast boundary file,
and the provider is driven against a patched `httpx.AsyncClient` (the pattern in
`test_email_providers.py`).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from submissions_checker.core.config import Settings
from submissions_checker.services.air_raid import build_air_raid_provider
from submissions_checker.services.air_raid.alerts_in_ua import (
    ALERTS_IN_UA_ACTIVE_PATH,
    AlertsInUaProvider,
)
from submissions_checker.services.air_raid.base import AirRaidProviderError
from submissions_checker.services.air_raid.fake import FakeAirRaidProvider
from submissions_checker.services.air_raid.geo import resolve_region

# ── geo: coordinates to an alerts.in.ua oblast uid ───────────────────────────

# (name, lat, lng, expected alerts.in.ua oblast uid)
CITIES = [
    ("Kyiv city", 50.4501, 30.5234, 31),
    ("Kharkiv", 49.9935, 36.2304, 22),
    ("Lviv", 49.8397, 24.0297, 27),
    ("Odesa", 46.4825, 30.7233, 18),
    ("Dnipro", 48.4647, 35.0462, 9),
    ("Zaporizhzhia", 47.8388, 35.1396, 12),
    ("Vinnytsia", 49.2331, 28.4682, 4),
    ("Uzhhorod", 48.6208, 22.2879, 11),
    ("Chernivtsi", 48.2915, 25.9403, 26),
    ("Sumy", 50.9077, 34.7981, 20),
    ("Mykolaiv", 46.9750, 31.9946, 17),
    ("Sevastopol", 44.6166, 33.5254, 30),
]


@pytest.mark.parametrize("name,lat,lng,expected", CITIES)
def test_resolve_region_maps_known_cities(name: str, lat: float, lng: float, expected: int) -> None:
    resolved = resolve_region(lat, lng)
    assert resolved is not None, name
    assert resolved.uid == expected, f"{name} resolved to {resolved.title}"


def test_kyiv_city_beats_the_oblast_that_surrounds_it() -> None:
    """The city is an enclave inside Kyiv oblast — the smaller region has to win."""
    city = resolve_region(50.4501, 30.5234)
    oblast = resolve_region(49.7950, 30.1310)  # Bila Tserkva, in the oblast proper
    assert city is not None and oblast is not None
    assert city.uid == 31
    assert oblast.uid == 14


@pytest.mark.parametrize(
    "name,lat,lng",
    [
        ("Warsaw", 52.2297, 21.0122),
        ("Minsk", 53.9006, 27.5590),
        ("Bucharest", 44.4268, 26.1025),
        ("Black Sea", 43.2000, 32.0000),
        ("Moscow", 55.7558, 37.6173),
        ("null island", 0.0, 0.0),
    ],
)
def test_resolve_region_rejects_points_outside_ukraine(name: str, lat: float, lng: float) -> None:
    assert resolve_region(lat, lng) is None, name


def test_resolve_region_rejects_impossible_coordinates() -> None:
    assert resolve_region(91.0, 30.0) is None
    assert resolve_region(50.0, 181.0) is None


def test_every_oblast_uid_is_covered_exactly_once() -> None:
    """27 regions, matching the uid table alerts.in.ua publishes."""
    from submissions_checker.services.air_raid.geo import load_regions

    uids = [r.uid for r in load_regions()]
    assert len(uids) == 27
    assert len(set(uids)) == 27
    # The gaps (1, 2, 6, 7) are unused upstream; 31 is Kyiv city.
    assert min(uids) == 3
    assert max(uids) == 31


# ── alerts.in.ua provider ────────────────────────────────────────────────────


def _alert(
    uid: str, *, alert_type: str = "air_raid", finished: str | None = None
) -> dict[str, Any]:
    return {
        "id": 1,
        "location_title": "Харківська область",
        "location_type": "oblast",
        "started_at": "2026-09-15T10:00:00.000Z",
        "finished_at": finished,
        "alert_type": alert_type,
        "location_uid": uid,
        "location_oblast_uid": uid,
        "location_oblast": "Харківська область",
    }


def _patch_httpx(payload: dict[str, Any] | None = None, *, status: int = 200, text: str = ""):
    """Patch httpx.AsyncClient inside the provider module and return the mock request fn."""
    response = MagicMock()
    response.status_code = status
    response.is_error = status >= 400
    response.text = text
    response.json = MagicMock(return_value=payload if payload is not None else {})
    request = AsyncMock(return_value=response)

    client = MagicMock()
    client.get = request
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return patch(
        "submissions_checker.services.air_raid.alerts_in_ua.httpx.AsyncClient",
        return_value=ctx,
    ), request


def _provider(**kw: Any) -> AlertsInUaProvider:
    return AlertsInUaProvider(token="tok-123", cache_seconds=0, **kw)


async def test_provider_sends_a_bearer_token_to_the_documented_endpoint() -> None:
    patcher, request = _patch_httpx({"alerts": [_alert("22")]})
    with patcher:
        alert = await _provider().active_alert(22)

    assert alert is not None
    assert alert.region_uid == 22
    url = request.await_args.args[0]
    headers = request.await_args.kwargs["headers"]
    assert url.endswith(ALERTS_IN_UA_ACTIVE_PATH)
    assert headers["Authorization"] == "Bearer tok-123"
    # The token must never travel in the query string, where proxies log it.
    assert "tok-123" not in url


async def test_provider_matches_only_the_requested_oblast() -> None:
    patcher, _ = _patch_httpx({"alerts": [_alert("22")]})
    with patcher:
        provider = _provider()
        assert await provider.active_alert(22) is not None
        assert await provider.active_alert(27) is None


async def test_provider_ignores_finished_alerts() -> None:
    patcher, _ = _patch_httpx({"alerts": [_alert("22", finished="2026-09-15T11:00:00.000Z")]})
    with patcher:
        assert await _provider().active_alert(22) is None


async def test_provider_ignores_other_alert_types() -> None:
    """An artillery or chemical warning is not an air raid."""
    patcher, _ = _patch_httpx({"alerts": [_alert("22", alert_type="artillery_shelling")]})
    with patcher:
        assert await _provider().active_alert(22) is None


async def test_provider_handles_an_empty_alert_list() -> None:
    patcher, _ = _patch_httpx({"alerts": []})
    with patcher:
        assert await _provider().active_alert(22) is None


async def test_provider_caches_within_its_ttl() -> None:
    """alerts.in.ua rate-limits hard; a lecture hall pressing the button must be one call."""
    patcher, request = _patch_httpx({"alerts": [_alert("22")]})
    with patcher:
        provider = AlertsInUaProvider(token="tok-123", cache_seconds=60)
        for _ in range(5):
            await provider.active_alert(22)

    assert request.await_count == 1


async def test_provider_raises_on_an_error_response() -> None:
    patcher, _ = _patch_httpx(status=429, text="rate limit exceeded")
    with patcher:
        with pytest.raises(AirRaidProviderError):
            await _provider().active_alert(22)


async def test_provider_raises_on_a_malformed_payload() -> None:
    patcher, _ = _patch_httpx({"unexpected": "shape"})
    with patcher:
        with pytest.raises(AirRaidProviderError):
            await _provider().active_alert(22)


async def test_provider_serves_a_stale_cache_when_rate_limited() -> None:
    """Better a 30-second-old truth than refusing a pause during a real raid."""
    patcher, request = _patch_httpx({"alerts": [_alert("22")]})
    with patcher:
        provider = AlertsInUaProvider(token="tok-123", cache_seconds=0)
        assert await provider.active_alert(22) is not None

    patcher2, _ = _patch_httpx(status=429, text="too many requests")
    with patcher2:
        # The live call fails, but the previous payload is still good enough to act on.
        assert await provider.active_alert(22) is not None


# ── builder (opt-in by token presence) ───────────────────────────────────────


def _settings(**kw: Any) -> Settings:
    base: dict[str, Any] = {
        "secret_key": "test-secret-key-minimum-32-chars-long",
        "database_url": "postgresql+asyncpg://t:t@localhost:5432/t",
    }
    base.update(kw)
    return Settings(**base)


def test_builder_returns_none_without_a_token() -> None:
    assert build_air_raid_provider(_settings()) is None


def test_builder_returns_a_provider_with_a_token() -> None:
    provider = build_air_raid_provider(_settings(alerts_in_ua_token="tok"))
    assert isinstance(provider, AlertsInUaProvider)


def test_builder_respects_the_feature_switch() -> None:
    assert (
        build_air_raid_provider(_settings(alerts_in_ua_token="tok", air_raid_pause_enabled=False))
        is None
    )


# ── the test double ──────────────────────────────────────────────────────────


async def test_fake_provider_reports_only_the_regions_it_was_given() -> None:
    fake = FakeAirRaidProvider(active_uids={22})
    assert await fake.active_alert(22) is not None
    assert await fake.active_alert(27) is None


async def test_fake_provider_can_simulate_an_outage() -> None:
    fake = FakeAirRaidProvider(active_uids={22}, fail=True)
    with pytest.raises(AirRaidProviderError):
        await fake.active_alert(22)
