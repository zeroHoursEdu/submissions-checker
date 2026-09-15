"""Rebuild `submissions_checker/data/ua_oblasts.json` from geoBoundaries ADM1.

That file maps coordinates to an alerts.in.ua oblast uid for the air-raid quiz pause
(`services/air_raid/geo.py`). It is committed, so this script only needs running to change
the precision or to refresh the boundaries.

Usage:
    curl -sSL -o /tmp/ukr_adm1.geojson \
      "https://www.geoboundaries.org/api/current/gbOpen/UKR/ADM1/"   # -> gjDownloadURL
    # then fetch that release's `simplifiedGeometryGeoJSON` to SOURCE below
    uv run python scripts/build_oblast_boundaries.py 0.01 4 /tmp/ukr_adm1.geojson

Arguments are the RDP epsilon in degrees, the coordinate precision, and the source file.
The committed file was built with `0.01 4`: ~110 KB, and measured against the unsimplified
source over a 0.05 degree grid of Ukrainian land, 0.15% of points fall in an uncovered
border sliver and 0.26% resolve to a neighbouring oblast. A larger epsilon shrinks the file
and widens those errors; see `docs/known_bugs.md` for why that trade was accepted.

Source data: geoBoundaries gbOpen UKR ADM1 (OpenStreetMap), ODbL 1.0.
Holes are preserved deliberately — Kyiv city is a hole in Kyiv oblast, and without it every
Kyiv coordinate resolves to the oblast wrapped around it.
"""

from __future__ import annotations

import json
import pathlib
import sys

# shapeISO -> (alerts.in.ua oblast uid, Ukrainian title as the API spells it)
ISO_TO_UID = {
    "UA-68": (3, "Хмельницька область"),
    "UA-05": (4, "Вінницька область"),
    "UA-56": (5, "Рівненська область"),
    "UA-07": (8, "Волинська область"),
    "UA-12": (9, "Дніпропетровська область"),
    "UA-18": (10, "Житомирська область"),
    "UA-21": (11, "Закарпатська область"),
    "UA-23": (12, "Запорізька область"),
    "UA-26": (13, "Івано-Франківська область"),
    "UA-32": (14, "Київська область"),
    "UA-35": (15, "Кіровоградська область"),
    "UA-09": (16, "Луганська область"),
    "UA-48": (17, "Миколаївська область"),
    "UA-51": (18, "Одеська область"),
    "UA-53": (19, "Полтавська область"),
    "UA-59": (20, "Сумська область"),
    "UA-61": (21, "Тернопільська область"),
    "UA-63": (22, "Харківська область"),
    "UA-65": (23, "Херсонська область"),
    "UA-71": (24, "Черкаська область"),
    "UA-74": (25, "Чернігівська область"),
    "UA-77": (26, "Чернівецька область"),
    "UA-46": (27, "Львівська область"),
    "UA-14": (28, "Донецька область"),
    "UA-43": (29, "Автономна Республіка Крим"),
    "UA-40": (30, "м. Севастополь"),
    "UA-30": (31, "м. Київ"),
}


def perp_dist(p, a, b):
    (x, y), (x1, y1), (x2, y2) = p, a, b
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return ((x - x1) ** 2 + (y - y1) ** 2) ** 0.5
    return abs(dy * x - dx * y + x2 * y1 - y2 * x1) / (dx * dx + dy * dy) ** 0.5


def rdp(points, eps):
    """Ramer-Douglas-Peucker, iterative so a long ring cannot blow the stack."""
    if len(points) < 3:
        return list(points)
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        lo, hi = stack.pop()
        worst, worst_i = 0.0, None
        for i in range(lo + 1, hi):
            d = perp_dist(points[i], points[lo], points[hi])
            if d > worst:
                worst, worst_i = d, i
        if worst_i is not None and worst > eps:
            keep[worst_i] = True
            stack.append((lo, worst_i))
            stack.append((worst_i, hi))
    return [p for p, k in zip(points, keep, strict=True) if k]


def polys_of(geom):
    """[(outer_ring, [hole_ring, ...]), ...] — holes matter: Kyiv city is a hole in Kyiv oblast."""
    raw = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
    return [(poly[0], list(poly[1:])) for poly in raw]


def build(eps: float, ndigits: int, source: pathlib.Path) -> dict:
    src = json.loads(source.read_text())
    regions = []
    for feat in src["features"]:
        iso = feat["properties"].get("shapeISO")
        if iso not in ISO_TO_UID:
            raise SystemExit(f"unmapped shapeISO {iso!r} ({feat['properties'].get('shapeName')})")
        uid, title = ISO_TO_UID[iso]
        polys = []
        for outer, holes in polys_of(feat["geometry"]):
            simple = rdp([(round(x, ndigits), round(y, ndigits)) for x, y in outer], eps)
            if len(simple) < 4:
                continue
            simple_holes = []
            for hole in holes:
                h = rdp([(round(x, ndigits), round(y, ndigits)) for x, y in hole], eps)
                if len(h) >= 4:
                    simple_holes.append([[x, y] for x, y in h])
            polys.append({"outer": [[x, y] for x, y in simple], "holes": simple_holes})
        if not polys:
            raise SystemExit(f"{title} simplified away")
        xs = [x for p in polys for x, _ in p["outer"]]
        ys = [y for p in polys for _, y in p["outer"]]
        regions.append(
            {
                "uid": uid,
                "title": title,
                "bbox": [
                    round(min(xs), ndigits),
                    round(min(ys), ndigits),
                    round(max(xs), ndigits),
                    round(max(ys), ndigits),
                ],
                "polygons": polys,
            }
        )
    regions.sort(key=lambda r: r["uid"])
    return {
        "source": "geoBoundaries gbOpen UKR ADM1 (OpenStreetMap), ODbL 1.0",
        "simplified": {"algorithm": "rdp", "epsilon_deg": eps, "precision": ndigits},
        "regions": regions,
    }


DEST = (
    pathlib.Path(__file__).resolve().parent.parent
    / "src"
    / "submissions_checker"
    / "data"
    / "ua_oblasts.json"
)


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit(f"usage: {sys.argv[0]} <epsilon_deg> <precision> <source.geojson>")
    eps = float(sys.argv[1])
    nd = int(sys.argv[2])
    source = pathlib.Path(sys.argv[3])

    out = build(eps, nd, source)
    blob = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
    points = sum(
        len(poly["outer"]) + sum(len(h) for h in poly["holes"])
        for region in out["regions"]
        for poly in region["polygons"]
    )
    DEST.write_text(blob, encoding="utf-8")
    print(
        f"wrote {DEST.relative_to(DEST.parent.parent.parent.parent)}: "
        f"{len(out['regions'])} regions, {points} points, {len(blob.encode())} bytes "
        f"(eps={eps}, precision={nd})"
    )
