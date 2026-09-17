#!/usr/bin/env python3
"""Download the third-party browser assets into static/vendor/.

No page may depend on a third-party host at runtime: a quiz must not stall on a CDN
at exam time, and a script fetched from someone else's host on every page load is a
supply-chain risk on every page. So the MediaPipe Face Landmarker bundle (proctoring)
and the Tailwind play-CDN build (styling) are served from this app's own /static.
Idempotent: existing files are kept; every file is checked against
scripts/vendor_assets.sha256 when that pin file exists. Pure stdlib so it runs in the
slim production image (no curl there).

Usage: python scripts/fetch_vendor_assets.py            # fetch + verify
       python scripts/fetch_vendor_assets.py --pin      # rewrite the sha256 pin file
"""

from __future__ import annotations

import hashlib
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "static" / "vendor"
PIN_FILE = ROOT / "scripts" / "vendor_assets.sha256"

_MP = "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14"
_MODEL = (
    "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/"
    "float16/1/face_landmarker.task"
)
# The play-CDN build is a single self-contained script that compiles utility classes
# in the browser, which is what the templates already rely on (no build step). Pinned
# to an exact version so the pin file below stays meaningful.
_TAILWIND = "https://cdn.tailwindcss.com/3.4.17"
# Keys are paths under static/vendor/.
ASSETS: dict[str, str] = {
    "mediapipe/vision_bundle.mjs": f"{_MP}/vision_bundle.mjs",
    "mediapipe/wasm/vision_wasm_internal.js": f"{_MP}/wasm/vision_wasm_internal.js",
    "mediapipe/wasm/vision_wasm_internal.wasm": f"{_MP}/wasm/vision_wasm_internal.wasm",
    "mediapipe/wasm/vision_wasm_nosimd_internal.js": f"{_MP}/wasm/vision_wasm_nosimd_internal.js",
    "mediapipe/wasm/vision_wasm_nosimd_internal.wasm": (
        f"{_MP}/wasm/vision_wasm_nosimd_internal.wasm"
    ),
    "mediapipe/face_landmarker.task": _MODEL,
    "tailwind/tailwind.js": _TAILWIND,
}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fetch(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        return
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    # cdn.tailwindcss.com answers 403 to urllib's default User-Agent.
    req = urllib.request.Request(url, headers={"User-Agent": "submissions-checker-vendor/1"})
    with urllib.request.urlopen(req, timeout=120) as resp, tmp.open("wb") as out:  # noqa: S310
        for chunk in iter(lambda: resp.read(1 << 20), b""):
            out.write(chunk)
    tmp.replace(dest)
    print(f"fetched {dest.relative_to(ROOT)}")


def _load_pins() -> dict[str, str]:
    if not PIN_FILE.exists():
        return {}
    pins: dict[str, str] = {}
    for line in PIN_FILE.read_text(encoding="utf-8").splitlines():
        if line.strip():
            digest, rel = line.split(maxsplit=1)
            pins[rel.strip()] = digest
    return pins


def main(argv: list[str]) -> int:
    for rel, url in ASSETS.items():
        _fetch(url, DEST / rel)

    if "--pin" in argv:
        lines = [f"{_sha256(DEST / rel)}  static/vendor/{rel}" for rel in ASSETS]
        PIN_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"wrote {PIN_FILE.relative_to(ROOT)}")
        return 0

    pins = _load_pins()
    bad = [
        rel for rel in ASSETS if pins.get(f"static/vendor/{rel}") not in (None, _sha256(DEST / rel))
    ]
    if bad:
        print(
            f"checksum mismatch: {', '.join(bad)} — delete static/vendor and re-run",
            file=sys.stderr,
        )
        return 1
    print(f"vendor assets ready in {DEST.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
