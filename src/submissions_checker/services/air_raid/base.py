"""The air-raid alert contract, kept behind an interface.

One implementation talks to alerts.in.ua; another is a test double. Routes depend on this
module, never on the HTTP client, which is what keeps every functional test off the network.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class AirRaidProviderError(Exception):
    """The alert source could not be consulted.

    Deliberately distinct from "no alert": a provider that is merely unreachable must never
    be read as an all-clear, and must never be read as a raid either. The caller refuses the
    pause and says why.
    """


@dataclass(frozen=True)
class ActiveAlert:
    """An air-raid alert currently in force over a region."""

    region_uid: int
    region_title: str
    started_at: str | None = None


class AirRaidProvider(ABC):
    @abstractmethod
    async def active_alert(self, region_uid: int) -> ActiveAlert | None:
        """The air-raid alert in force over this region, or None if there is none.

        Raises ``AirRaidProviderError`` if the source could not be consulted.
        """
