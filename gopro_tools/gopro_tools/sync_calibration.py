"""Start-sync calibration: where the first video frame lies relative to the START command's ack.

Over HTTP the camera answers the shutter-start command only once recording runs, and it holds state
queries meanwhile, so the observed *encoding* change comes late and with ~0.6 s uncertainty. The
ack time, however, has a stable relation to the first frame (HERO13/USB: first frame ~37 ms before
the ack, sd ~7 ms). ``gopro-calibrate-sync`` measures that offset per camera configuration by
filming an on-screen clock; ``gopro-align`` then uses ``camera_ack + start_offset`` as the anchor.

File format (YAML; later files and more specific entries win)::

    calibrations:
      - model: HERO13 Black        # as reported in the MP4 (telemetry-parser)
        transport: usb             # RecordingEvent.transport of the START command
        fps: 29.97                 # optional; omitted = any frame rate
        resolution: 5312x2988      # optional
        firmware: H24.01.02.04.00  # optional; a match ranks higher, a mismatch only warns
        start_offset_s: -0.037     # first frame time - camera_ack
        uncertainty_s: 0.024
        ...                        # sd_s, n, display_lag_s, settings, measured, note (informational)
"""

from __future__ import annotations

import dataclasses
import logging
import os
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("gopro-sync-calibration")

USER_FILE = Path("~/.ros/gopro/sync_calibration.yaml")
FPS_TOLERANCE = 0.005  # relative


@dataclass
class SyncCalibration:
    model: str
    transport: str
    start_offset_s: float
    uncertainty_s: float
    fps: float | None = None
    resolution: str = ""
    firmware: str = ""
    sd_s: float = 0.0
    n: int = 0
    display_lag_s: float = 0.0
    settings: dict[str, str] = field(default_factory=dict)
    measured: str = ""
    note: str = ""
    source: str = ""  # file it was loaded from (not saved)

    def key(self) -> tuple[str, str, float | None, str, str]:
        fps = round(self.fps, 2) if self.fps else None
        return (_norm(self.model), _norm(self.transport), fps, self.resolution, self.firmware)

    def describe(self) -> str:
        fps = f" {self.fps:g} fps" if self.fps else ""
        res = f" {self.resolution}" if self.resolution else ""
        return (
            f"{self.model}/{self.transport}{fps}{res} fw {self.firmware or 'any'}: "
            f"{self.start_offset_s * 1e3:+.0f} ms +-{self.uncertainty_s * 1e3:.0f} ms (n={self.n})"
        )

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d.pop("source")
        return {k: v for k, v in d.items() if v not in (None, "", {})}


@dataclass
class RecordingInfo:
    """What is known about a recording, to pick a calibration."""

    model: str = ""
    transport: str = ""
    fps: float | None = None
    resolution: str = ""
    firmware: str = ""


def _norm(s: str) -> str:
    return " ".join(str(s).split()).casefold()


def packaged_file() -> Path:
    return Path(str(resources.files("gopro_tools") / "data" / "sync_calibration.yaml"))


def default_files() -> list[Path]:
    """Packaged defaults, then the user's file (which wins on equal specificity)."""
    files = [packaged_file()]
    user = USER_FILE.expanduser()
    if user.exists():
        files.append(user)
    return files


def load(paths: list[Path]) -> list[SyncCalibration]:
    out: list[SyncCalibration] = []
    names = {f.name for f in dataclasses.fields(SyncCalibration)}
    for p in paths:
        data = yaml.safe_load(Path(p).expanduser().read_text()) or {}
        for raw in data.get("calibrations") or []:
            entry = {k: v for k, v in raw.items() if k in names}
            if "fps" in entry and entry["fps"] is not None:
                entry["fps"] = float(entry["fps"])
            entry["settings"] = {str(k): str(v) for k, v in (entry.get("settings") or {}).items()}
            out.append(SyncCalibration(**entry, source=str(p)))
    return out


def save(path: Path, cal: SyncCalibration) -> None:
    """Add ``cal`` to the YAML file at ``path``, replacing an entry for the same configuration."""
    path = Path(path).expanduser()
    entries = load([path]) if path.exists() else []
    entries = [e for e in entries if e.key() != cal.key()] + [cal]
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "# gopro-calibrate-sync results; see gopro_tools/sync_calibration.py for the format\n"
    text += yaml.safe_dump({"calibrations": [e.to_dict() for e in entries]}, sort_keys=False)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def match(cals: list[SyncCalibration], rec: RecordingInfo) -> SyncCalibration | None:
    """Most specific calibration for this recording (None if model/transport/fps do not match)."""
    best: tuple[float, int, SyncCalibration] | None = None
    for i, c in enumerate(cals):
        if not rec.model or _norm(c.model) != _norm(rec.model):
            continue
        if not rec.transport or _norm(c.transport) != _norm(rec.transport):
            continue
        score = 0.0
        if c.fps:
            if not rec.fps or abs(c.fps - rec.fps) > FPS_TOLERANCE * c.fps:
                continue
            score += 2
        if c.resolution:
            if rec.resolution and c.resolution != rec.resolution:
                continue
            score += 1
        if c.firmware and rec.firmware == c.firmware:
            score += 1
        if best is None or (score, i) > best[:2]:
            best = (score, i, c)
    if best is None:
        return None
    cal = best[2]
    if cal.firmware and rec.firmware and cal.firmware != rec.firmware:
        log.warning("sync calibration measured on firmware %s, recording is %s", cal.firmware, rec.firmware)
    return cal
