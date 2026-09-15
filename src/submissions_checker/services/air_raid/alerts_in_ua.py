"""alerts.in.ua air-raid provider.

API contract (https://devs.alerts.in.ua): ``GET /v1/alerts/active.json`` with
``Authorization: Bearer <token>`` returns ``{"alerts": [...]}``, where an alert in force has
``alert_type: "air_raid"``, a null ``finished_at`` and a ``location_oblast_uid``.

The payload is national, so one response answers every student. That is not an optimisation:
the upstream rate limit is a dozen requests per minute per IP, and a single raid over a
lecture hall means thirty students pressing the button inside the same minute. The TTL cache
below is what keeps that one upstream call.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from submissions_checker.core.logging import get_logger
from submissions_checker.services.air_raid.base import (
    ActiveAlert,
    AirRaidProvider,
    AirRaidProviderError,
)

logger = get_logger(__name__)

ALERTS_IN_UA_BASE_URL = "https://api.alerts.in.ua"
ALERTS_IN_UA_ACTIVE_PATH = "/v1/alerts/active.json"

_AIR_RAID = "air_raid"
_TIMEOUT_SECONDS = 15


class AlertsInUaProvider(AirRaidProvider):
    def __init__(
        self,
        token: str,
        *,
        base_url: str = ALERTS_IN_UA_BASE_URL,
        cache_seconds: int = 30,
    ) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._cache_seconds = cache_seconds
        self._cache: tuple[float, list[dict[str, Any]]] | None = None
        self._lock = asyncio.Lock()

    async def active_alert(self, region_uid: int) -> ActiveAlert | None:
        alerts = await self._fetch_alerts()
        for alert in alerts:
            if alert.get("alert_type") != _AIR_RAID:
                continue
            if alert.get("finished_at"):
                continue
            raw_uid = alert.get("location_oblast_uid") or alert.get("location_uid")
            try:
                uid = int(raw_uid)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            if uid != region_uid:
                continue
            return ActiveAlert(
                region_uid=uid,
                region_title=str(alert.get("location_oblast") or alert.get("location_title") or ""),
                started_at=alert.get("started_at"),
            )
        return None

    async def _fetch_alerts(self) -> list[dict[str, Any]]:
        async with self._lock:
            now = time.monotonic()
            cached = self._cache
            if cached is not None and now - cached[0] < self._cache_seconds:
                return cached[1]

            try:
                alerts = await self._request_alerts()
            except AirRaidProviderError:
                # Rate-limited or briefly unreachable. A payload from moments ago is a far
                # better basis for "is there a raid" than refusing the pause outright, so
                # serve the stale copy if we have one and let the caller act on it.
                if cached is not None:
                    logger.warning("air_raid_serving_stale_cache", age=now - cached[0])
                    return cached[1]
                raise

            self._cache = (now, alerts)
            return alerts

    async def _request_alerts(self) -> list[dict[str, Any]]:
        url = f"{self._base_url}{ALERTS_IN_UA_ACTIVE_PATH}"
        # Header auth, not `?token=`: the query string is what proxies and access logs keep.
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url, headers=headers, timeout=_TIMEOUT_SECONDS)
        except httpx.HTTPError as exc:
            logger.warning("air_raid_request_failed", error=str(exc))
            raise AirRaidProviderError(f"alerts.in.ua unreachable: {exc}") from exc

        if response.is_error:
            # The body carries the upstream's own reason (rate limit, bad token) and no
            # credential, so it is safe and useful to log, truncated.
            logger.warning(
                "air_raid_error_response",
                status_code=response.status_code,
                body=response.text[:500],
            )
            raise AirRaidProviderError(f"alerts.in.ua returned {response.status_code}")

        try:
            payload = response.json()
            alerts = payload["alerts"]
        except (ValueError, KeyError, TypeError) as exc:
            logger.warning("air_raid_malformed_payload", error=str(exc))
            raise AirRaidProviderError("alerts.in.ua returned an unexpected payload") from exc

        if not isinstance(alerts, list):
            raise AirRaidProviderError("alerts.in.ua returned an unexpected payload")
        return [a for a in alerts if isinstance(a, dict)]
