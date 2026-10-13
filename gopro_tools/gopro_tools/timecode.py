"""Machine-readable on-screen clock for sync calibration (ArUco markers).

The calibration page shows the host clock (``Date.now()``, the same clock as the driver's
RecordingEvent stamps) as three ArUco markers, redrawn every display refresh:

- a DICT_4X4_1000 marker: ``hi`` = (ms mod 1e6) // 1000   (seconds within a 1000 s window)
- a DICT_5X5_1000 marker: ``lo`` = (ms mod 1e6) %  1000   (milliseconds)
- a DICT_6X6_1000 marker: ``check`` = (7 hi + 13 lo + 101) mod 1000

Each value lives in its own dictionary, so decoding does not depend on marker order or camera
orientation, and the checksum rejects frames that blend two display refreshes (or a false detection).
The full time is recovered by unwrapping ``ms mod 1e6`` around a reference (e.g. the command time).

A frame shows the clock value at the moment the browser drew it; the monitor shows it a little later
(display lag, typically 10-30 ms), so a decoded value is *earlier* than the true capture time by
that lag. Callers pass ``display_lag_s`` to correct for it.
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

import numpy as np

DICTS = ("DICT_4X4_1000", "DICT_5X5_1000", "DICT_6X6_1000")  # roles: hi, lo, check
MODULO_MS = 1_000_000
DECODE_WIDTH = 1920  # frames are downscaled to this width before detection (markers are large)


def checksum(hi: int, lo: int) -> int:
    return (7 * hi + 13 * lo + 101) % 1000


def encode(ms: int) -> tuple[int, int, int]:
    v = int(ms) % MODULO_MS
    hi, lo = divmod(v, 1000)
    return hi, lo, checksum(hi, lo)


def unwrap(value_mod: int, reference_ms: int) -> int:
    """Full ms time whose ``mod MODULO_MS`` is ``value_mod``, nearest to ``reference_ms``."""
    base = reference_ms - reference_ms % MODULO_MS
    return min((base + k * MODULO_MS + value_mod for k in (-1, 0, 1)), key=lambda t: abs(t - reference_ms))


# ------------------------------------------------------------------------------------------ markers
def _aruco() -> Any:
    import cv2

    return cv2.aruco


@cache
def _dictionary(name: str) -> Any:
    aruco = _aruco()
    return aruco.getPredefinedDictionary(getattr(aruco, name))


@cache
def marker_bits(name: str) -> tuple[int, list[str]]:
    """(marker size n, inner n*n bit strings for ids 0..999); '1' = white cell."""
    aruco = _aruco()
    d = _dictionary(name)
    n = int(d.markerSize)
    out = []
    for idx in range(1000):
        if hasattr(aruco, "generateImageMarker"):  # OpenCV >= 4.7
            img = aruco.generateImageMarker(d, idx, n + 2)
        else:
            img = aruco.drawMarker(d, idx, n + 2)
        out.append("".join("1" if img[r + 1][c + 1] > 127 else "0" for r in range(n) for c in range(n)))
    return n, out


def markers_json() -> str:
    return json.dumps([{"name": nm, "size": marker_bits(nm)[0], "bits": marker_bits(nm)[1]} for nm in DICTS])


def render(ms: int, width: int = 1280, height: int = 720) -> np.ndarray:
    """Draw the calibration screen for ``ms`` (same layout as the page); BGR uint8 image."""
    img = np.full((height, width, 3), 255, np.uint8)
    side, gap, x0, y0 = _layout(width, height)
    for k, (name, value) in enumerate(zip(DICTS, encode(ms), strict=True)):
        n, bits = marker_bits(name)
        cell = side // (n + 2)
        x, y = x0 + k * (side + gap), y0
        img[y : y + cell * (n + 2), x : x + cell * (n + 2)] = 0
        for i, b in enumerate(bits[value]):
            if b == "1":
                r, c = divmod(i, n)
                yy, xx = y + (r + 1) * cell, x + (c + 1) * cell
                img[yy : yy + cell, xx : xx + cell] = 255
    return img


def _layout(width: int, height: int) -> tuple[int, int, int, int]:
    side = int(min(width / 3.8, height * 0.6))
    gap = int(side * 0.25)
    x0 = (width - (3 * side + 2 * gap)) // 2
    y0 = int((height - side) * 0.4)
    return side, gap, x0, y0


PAGE = """<!doctype html>
<!-- gopro-calibrate-sync: host clock (Unix ms) as ArUco markers, redrawn every display refresh. -->
<html><head><meta charset="utf-8"><title>gopro sync clock</title>
<style>html,body{margin:0;height:100%;background:#fff;overflow:hidden}canvas{display:block}</style>
</head><body><canvas id="c"></canvas><script>
const c = document.getElementById("c"), ctx = c.getContext("2d");
let D = null;
fetch("markers.json").then(r => r.json()).then(d => { D = d; requestAnimationFrame(draw); });
function size() { c.width = innerWidth * devicePixelRatio; c.height = innerHeight * devicePixelRatio;
  c.style.width = innerWidth + "px"; c.style.height = innerHeight + "px"; }
addEventListener("resize", size); size();
function marker(x, y, side, m, value) {
  const n = m.size, cell = Math.floor(side / (n + 2)), bits = m.bits[value];
  ctx.fillStyle = "#000"; ctx.fillRect(x, y, cell * (n + 2), cell * (n + 2));
  ctx.fillStyle = "#fff";
  for (let i = 0; i < n * n; i++) if (bits[i] === "1")
    ctx.fillRect(x + (i % n + 1) * cell, y + (Math.floor(i / n) + 1) * cell, cell, cell);
}
function draw() {
  const ms = Date.now(), v = ms % 1000000, hi = Math.floor(v / 1000), lo = v % 1000;
  const vals = [hi, lo, (7 * hi + 13 * lo + 101) % 1000];
  const W = c.width, H = c.height, side = Math.floor(Math.min(W / 3.8, H * 0.6));
  const gap = Math.floor(side * 0.25), x0 = Math.floor((W - (3 * side + 2 * gap)) / 2);
  const y0 = Math.floor((H - side) * 0.4);
  ctx.fillStyle = "#fff"; ctx.fillRect(0, 0, W, H);
  for (let k = 0; k < 3; k++) marker(x0 + k * (side + gap), y0, side, D[k], vals[k]);
  ctx.fillStyle = "#000"; ctx.font = Math.floor(H * 0.08) + "px monospace"; ctx.textAlign = "center";
  ctx.fillText((ms / 1000).toFixed(3), W / 2, y0 + side + (H - y0 - side) * 0.6);
  requestAnimationFrame(draw);
}
</script></body></html>
"""


# ------------------------------------------------------------------------------------------ decoding
def _detect(gray: np.ndarray, name: str) -> list[int]:
    aruco = _aruco()
    d = _dictionary(name)
    if hasattr(aruco, "ArucoDetector"):  # OpenCV >= 4.7
        _, ids, _ = aruco.ArucoDetector(d, aruco.DetectorParameters()).detectMarkers(gray)
    else:
        _, ids, _ = aruco.detectMarkers(gray, d, parameters=aruco.DetectorParameters_create())
    return [] if ids is None else sorted({int(i) for i in ids.flatten()})


def decode(image_bgr: np.ndarray) -> int | None:
    """Clock value (ms mod MODULO_MS) shown in the image, or None if unreadable / inconsistent."""
    import cv2

    h, w = image_bgr.shape[:2]
    if w > DECODE_WIDTH:
        image_bgr = cv2.resize(image_bgr, (DECODE_WIDTH, round(h * DECODE_WIDTH / w)), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    his, los, chks = (_detect(gray, name) for name in DICTS)
    found = {(hi, lo) for hi in his for lo in los if checksum(hi, lo) in chks}
    if len(found) != 1:
        return None
    hi, lo = found.pop()
    return hi * 1000 + lo


# ------------------------------------------------------------------------------------------ video
@dataclass
class FirstFrame:
    """Host time of the first video frame, estimated from the frames that show a readable clock."""

    t_s: float  # median estimate [s, Unix]
    estimates: list[float] = field(default_factory=list)  # one per decoded frame: shown time - frame pts
    frames_tried: int = 0

    @property
    def spread_s(self) -> float:
        return max(self.estimates) - min(self.estimates) if self.estimates else 0.0


def first_frame_time(
    mp4: str | Path,
    reference_s: float,
    display_lag_s: float = 0.0,
    max_frames: int = 30,
    want: int = 5,
) -> FirstFrame | None:
    """Host time of frame 0 of ``mp4``, from up to ``want`` readable frames among the first ``max_frames``.

    ``reference_s`` (any time within ~8 min of the recording, e.g. the command time) unwraps the
    clock value. Each readable frame i gives ``shown_i + display_lag - pts_i``; the median is returned.
    """
    import av

    estimates: list[float] = []
    tried = 0
    ref_ms = round(reference_s * 1000)
    with av.open(str(mp4)) as c:
        vs = c.streams.video[0]
        start = vs.start_time or 0
        for frame in c.decode(vs):
            if tried >= max_frames or len(estimates) >= want:
                break
            tried += 1
            pts = float((frame.pts - start) * vs.time_base) if frame.pts is not None else 0.0
            w = min(frame.width, DECODE_WIDTH)
            img = frame.reformat(width=w, height=round(frame.height * w / frame.width), format="bgr24").to_ndarray()
            value = decode(img)
            if value is not None:
                estimates.append(unwrap(value, ref_ms) / 1000 + display_lag_s - pts)
    if not estimates:
        return None
    return FirstFrame(statistics.median(estimates), estimates, tried)


def summarize(values: Iterable[float]) -> dict[str, float]:
    v = list(values)
    med = statistics.median(v)
    return {
        "n": len(v),
        "mean": statistics.fmean(v),
        "median": med,
        "sd": statistics.stdev(v) if len(v) > 1 else 0.0,
        "robust_sd": 1.4826 * statistics.median(abs(x - med) for x in v),  # MAD-based
        "min": min(v),
        "max": max(v),
    }


def inliers(values: list[float], k: float = 4.0, floor: float = 0.010) -> list[bool]:
    """Mask of values within ``max(k * robust sd, floor)`` of the median (e.g. drop a late ack)."""
    s = summarize(values)
    limit = max(k * s["robust_sd"], floor)
    return [abs(x - s["median"]) <= limit for x in values]
