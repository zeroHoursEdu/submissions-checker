"""Air-raid alert lookup for pausing a quiz during a raid."""

from __future__ import annotations

from typing import TYPE_CHECKING

from submissions_checker.services.air_raid.base import (
    ActiveAlert,
    AirRaidProvider,
    AirRaidProviderError,
)

if TYPE_CHECKING:
    from submissions_checker.core.config import Settings

__all__ = [
    "ActiveAlert",
    "AirRaidProvider",
    "AirRaidProviderError",
    "build_air_raid_provider",
]


def build_air_raid_provider(settings: Settings) -> AirRaidProvider | None:
    """The configured provider, or None when the feature is off or unconfigured.

    Opt-in by key presence, the same shape as ``build_dispatcher``. Returning None is a
    supported state: the pause button then reports that the check is unavailable and no
    attempt is ever paused on an unverified claim.
    """
    if not settings.air_raid_pause_enabled or not settings.alerts_in_ua_token:
        return None

    from submissions_checker.services.air_raid.alerts_in_ua import AlertsInUaProvider

    return AlertsInUaProvider(
        token=settings.alerts_in_ua_token,
        base_url=settings.alerts_in_ua_base_url,
        cache_seconds=settings.air_raid_cache_seconds,
    )
