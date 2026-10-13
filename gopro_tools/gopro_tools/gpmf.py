"""gopro-gpmf: extract GPMF telemetry (IMU, GPS, frame timestamps) from GoPro MP4s.

Parser choice (evaluated per CLAUDE.md, rather than writing our own):
- ``telemetry-parser`` (AdrianEddy, Rust + Python wheels, MIT/Apache-2.0): parses GPMF for all recent
  GoPros (and other brands), provides camera-frame-normalised IMU, wheels for x86_64 *and* aarch64
  (Jetson). Chosen.
- ``py-gpmf-parser`` (pybind11 bindings of GoPro's C gpmf-parser): faithful, but x86_64-only wheels
  and no declared license on PyPI.
- ``gpmf`` (pure Python, 0.1): unmaintained.

Times in the outputs are seconds from the start of the MP4 (the first video frame), which is what
``gopro-align`` maps to ROS time.
"""

from __future__ import annotations

import argparse
import csv
import datetime
import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("gopro-gpmf")


@dataclass
class ImuSample:
    t: float  # s from video start
    gyro: tuple[float, float, float]  # rad/s, camera frame (telemetry-parser normalisation)
    accel: tuple[float, float, float]  # m/s^2


@dataclass
class GpsSample:
    t: float
    lat: float
    lon: float
    alt: float
    speed_2d: float
    speed_3d: float
    fix: int  # 0 none, 2 2D, 3 3D
    precision: float  # DOP * 100 as reported (GPSP)
    utc_ms: int  # GPS UTC time of the block (ms since epoch), 0 if unknown


@dataclass
class Telemetry:
    path: str
    camera: str
    model: str
    duration: float
    frame_times: list[float] = field(default_factory=list)  # PTS of every video frame [s]
    creation_time: str = ""  # MP4 creation_time tag (camera clock)
    imu: list[ImuSample] = field(default_factory=list)
    gps: list[GpsSample] = field(default_factory=list)


def _video_info(path: Path) -> tuple[float, list[float], str]:
    import av

    with av.open(str(path)) as c:
        vs = c.streams.video[0]
        tb = float(vs.time_base)
        start = vs.start_time or 0
        pts = sorted(((p.pts - start) * tb) for p in c.demux(vs) if p.pts is not None)
        dur = float(vs.duration * tb) if vs.duration else (pts[-1] if pts else 0.0)
        created = str(c.metadata.get("creation_time", ""))
    return dur, pts, created


def video_meta(path: str | Path) -> tuple[float | None, str, str]:
    """(frame rate, 'WxH', camera firmware) of an MP4; GoPro stores the firmware as a metadata tag."""
    import av

    with av.open(str(path)) as c:
        vs = c.streams.video[0]
        rate = vs.average_rate or vs.guessed_rate
        fps = round(float(rate), 3) if rate else None
        return fps, f"{vs.width}x{vs.height}", str(c.metadata.get("firmware", ""))


def _gps_from_blocks(telemetry: list[dict[str, Any]]) -> list[GpsSample]:
    out: list[GpsSample] = []
    blocks = []
    for blk in telemetry:
        g = blk.get("GPS")
        if not g or "Data" not in g:
            continue
        blocks.append(g)
    for i, g in enumerate(blocks):
        data = g["Data"]
        scale = g.get("Scale") or [1, 1, 1, 1, 1]
        t0 = float(g.get("TimestampUs", 0)) / 1e6
        t1 = float(blocks[i + 1].get("TimestampUs", 0)) / 1e6 if i + 1 < len(blocks) else t0 + 1.0
        n = max(len(data), 1)
        fix = int(g.get("0x47505346", 0) or 0)  # GPSF
        prec = float(g.get("0x47505350", 0) or 0)  # GPSP
        utc = int(g.get("0x47505355", 0) or 0) if str(g.get("0x47505355", "")).isdigit() else 0  # GPSU
        for j, row in enumerate(data):
            vals = [float(v) / float(s) for v, s in zip(row, scale, strict=False)]
            vals += [0.0] * (5 - len(vals))
            out.append(GpsSample(t0 + (t1 - t0) * j / n, *vals[:5], fix=fix, precision=prec, utc_ms=utc))
    return out


def extract(path: str | Path, with_frames: bool = True) -> Telemetry:
    import telemetry_parser

    path = Path(path)
    parser = telemetry_parser.Parser(str(path))
    tel = Telemetry(path=str(path), camera=str(parser.camera), model=str(parser.model), duration=0.0)
    for s in parser.normalized_imu() or []:
        g = s.get("gyro") or (0.0, 0.0, 0.0)
        a = s.get("accl") or (0.0, 0.0, 0.0)
        tel.imu.append(
            ImuSample(
                t=float(s["timestamp_ms"]) / 1e3,
                gyro=(math.radians(g[0]), math.radians(g[1]), math.radians(g[2])),  # deg/s -> rad/s
                accel=(float(a[0]), float(a[1]), float(a[2])),
            )
        )
    tel.gps = _gps_from_blocks(parser.telemetry() or [])
    if with_frames:
        tel.duration, tel.frame_times, tel.creation_time = _video_info(path)
    return tel


def write_outputs(tel: Telemetry, out_dir: Path, fmt: str = "csv") -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(tel.path).stem
    files: list[Path] = []
    if fmt == "json":
        p = out_dir / f"{stem}_telemetry.json"
        p.write_text(
            json.dumps(
                {
                    "path": tel.path,
                    "camera": tel.camera,
                    "model": tel.model,
                    "duration": tel.duration,
                    "creation_time": tel.creation_time,
                    "frame_times": tel.frame_times,
                    "imu": [s.__dict__ for s in tel.imu],
                    "gps": [s.__dict__ for s in tel.gps],
                }
            )
        )
        return [p]
    with open(out_dir / f"{stem}_imu.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t", "gx", "gy", "gz", "ax", "ay", "az"])
        for s in tel.imu:
            w.writerow([f"{s.t:.6f}", *s.gyro, *s.accel])
        files.append(Path(f.name))
    with open(out_dir / f"{stem}_gps.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t", "lat", "lon", "alt", "speed_2d", "speed_3d", "fix", "precision", "utc_ms"])
        for g in tel.gps:
            w.writerow([f"{g.t:.6f}", g.lat, g.lon, g.alt, g.speed_2d, g.speed_3d, g.fix, g.precision, g.utc_ms])
        files.append(Path(f.name))
    with open(out_dir / f"{stem}_frames.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["frame", "t"])
        for i, t in enumerate(tel.frame_times):
            w.writerow([i, f"{t:.6f}"])
        files.append(Path(f.name))
    return files


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mp4", nargs="+", type=Path)
    ap.add_argument("--out", type=Path, default=Path("."), help="output directory")
    ap.add_argument("--format", choices=["csv", "json"], default="csv")
    ap.add_argument("--no-frames", action="store_true", help="skip demuxing frame timestamps")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    for mp4 in args.mp4:
        tel = extract(mp4, with_frames=not args.no_frames)
        files = write_outputs(tel, args.out, args.format)
        log.info(
            "%s: %s, %d IMU, %d GPS samples, %d frames (%.1fs) -> %s",
            mp4,
            tel.model or tel.camera,
            len(tel.imu),
            len(tel.gps),
            len(tel.frame_times),
            tel.duration,
            ", ".join(str(f) for f in files),
        )


def utc_from_creation_time(text: str) -> datetime.datetime | None:
    try:
        return datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


if __name__ == "__main__":
    main()
