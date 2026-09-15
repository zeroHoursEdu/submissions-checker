"""Coordinates to an alerts.in.ua oblast uid, offline.

alerts.in.ua has no latitude/longitude endpoint — every one of its APIs is keyed by a
``location_uid`` — so the mapping has to happen here. ``data/ua_oblasts.json`` carries
simplified oblast boundaries (geoBoundaries gbOpen ADM1, from OpenStreetMap, ODbL 1.0),
reduced with Ramer-Douglas-Peucker at 0.01° and rounded to four decimals: about 110 KB,
which is small enough to ship and needs no geometry dependency.

Accuracy is deliberately oblast-level. Against the unsimplified source over a 0.05° grid of
Ukrainian land, 0.15% of points fall in a border sliver this file does not cover (they
resolve to nothing, so no pause is granted) and 0.26% land in a neighbouring oblast. That
is the right trade for a control whose job is "is this student's region under alert" — but
it is why `resolve_region` is one pure function: swapping in finer geometry is one file.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_DATA_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "ua_oblasts.json"

# Ukraine's overall envelope. Cheap reject before any polygon work.
_LAT_MIN, _LAT_MAX = 43.0, 53.0
_LNG_MIN, _LNG_MAX = 21.5, 41.0


@dataclass(frozen=True)
class Region:
    """One alerts.in.ua region: its uid, its Ukrainian title, and its geometry."""

    uid: int
    title: str
    bbox: tuple[float, float, float, float]  # lng_min, lat_min, lng_max, lat_max
    polygons: tuple[tuple[tuple[tuple[float, float], ...], ...], ...]
    # Each polygon is (outer_ring, *hole_rings). Holes matter: Kyiv city is a hole in
    # Kyiv oblast, and without it every Kyiv coordinate would resolve to the oblast.

    @property
    def bbox_area(self) -> float:
        lng_min, lat_min, lng_max, lat_max = self.bbox
        return (lng_max - lng_min) * (lat_max - lat_min)


@lru_cache(maxsize=1)
def load_regions() -> tuple[Region, ...]:
    """Parse the bundled boundary file once per process."""
    raw = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
    regions = []
    for entry in raw["regions"]:
        polygons = tuple(
            tuple(
                tuple((float(x), float(y)) for x, y in ring)
                for ring in ([poly["outer"], *poly["holes"]])
            )
            for poly in entry["polygons"]
        )
        bbox = entry["bbox"]
        regions.append(
            Region(
                uid=int(entry["uid"]),
                title=str(entry["title"]),
                bbox=(float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])),
                polygons=polygons,
            )
        )
    return tuple(regions)


def _point_in_ring(lng: float, lat: float, ring: tuple[tuple[float, float], ...]) -> bool:
    """Ray casting. Pure Python — no shapely, nothing to install."""
    inside = False
    count = len(ring)
    j = count - 1
    for i in range(count):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > lat) != (yj > lat):
            crossing = (xj - xi) * (lat - yi) / (yj - yi) + xi
            if lng < crossing:
                inside = not inside
        j = i
    return inside


def _contains(region: Region, lng: float, lat: float) -> bool:
    for polygon in region.polygons:
        outer, *holes = polygon
        if not _point_in_ring(lng, lat, outer):
            continue
        if any(_point_in_ring(lng, lat, hole) for hole in holes):
            continue
        return True
    return False


def resolve_region(lat: float, lng: float) -> Region | None:
    """The alerts.in.ua region containing these coordinates, or None if outside coverage.

    When a point is inside more than one region — which happens wherever a city of special
    status sits within an oblast — the smallest region wins, so Kyiv resolves to Kyiv rather
    than to the oblast wrapped around it.
    """
    if not (_LAT_MIN <= lat <= _LAT_MAX and _LNG_MIN <= lng <= _LNG_MAX):
        return None

    matches = [
        region
        for region in load_regions()
        if region.bbox[0] <= lng <= region.bbox[2]
        and region.bbox[1] <= lat <= region.bbox[3]
        and _contains(region, lng, lat)
    ]
    if not matches:
        return None
    return min(matches, key=lambda r: r.bbox_area)
