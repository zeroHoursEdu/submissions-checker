"""In-memory air-raid provider for tests."""

from __future__ import annotations

from submissions_checker.services.air_raid.base import (
    ActiveAlert,
    AirRaidProvider,
    AirRaidProviderError,
)


class FakeAirRaidProvider(AirRaidProvider):
    """Reports an alert for exactly the region uids it was constructed with."""

    def __init__(self, active_uids: set[int] | None = None, *, fail: bool = False) -> None:
        self.active_uids = active_uids or set()
        self.fail = fail
        self.calls: list[int] = []

    async def active_alert(self, region_uid: int) -> ActiveAlert | None:
        self.calls.append(region_uid)
        if self.fail:
            raise AirRaidProviderError("simulated provider outage")
        if region_uid not in self.active_uids:
            return None
        return ActiveAlert(
            region_uid=region_uid,
            region_title=f"region-{region_uid}",
            started_at="2026-09-15T10:00:00.000Z",
        )
