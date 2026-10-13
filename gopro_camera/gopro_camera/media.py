"""Media list parsing, filtering and download orchestration (HTTP only).

Media list format (official spec, ``GET /gopro/media/list``)::

    {"id": "...", "media": [{"d": "100GOPRO", "fs": [{"n": "GX010001.MP4", "cre": "1696600000",
                                                      "mod": "...", "s": "123456"}, ...]}]}

For single items ``s`` is the size in bytes; for grouped items (``g`` present) ``s`` is the number of
files in the group.

Camera timestamps (``cre``/``mod``) are "seconds since epoch" of the *camera's local wall clock*: the
camera stores local time without a zone, so the value equals the local time read as if it were UTC.
:func:`to_camera_epoch` converts a host time to that convention so "since mission start" filters work
when the camera clock was synced from the host (``on_connect.sync_datetime``).
"""

from __future__ import annotations

import calendar
import datetime
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class MediaEntry:
    folder: str
    filename: str
    created: int
    modified: int
    size_bytes: int
    is_group: bool
    group_type: str
    group_size: int

    @property
    def path(self) -> str:
        return f"{self.folder}/{self.filename}"


def _int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def parse_media_list(data: dict[str, Any]) -> list[MediaEntry]:
    entries: list[MediaEntry] = []
    for directory in data.get("media", []) or []:
        folder = str(directory.get("d", ""))
        for item in directory.get("fs", []) or []:
            grouped = "g" in item
            entries.append(
                MediaEntry(
                    folder=folder,
                    filename=str(item.get("n", "")),
                    created=_int(item.get("cre")),
                    modified=_int(item.get("mod")),
                    size_bytes=0 if grouped else _int(item.get("s")),
                    is_group=grouped,
                    group_type=str(item.get("t", "")) if grouped else "",
                    group_size=_int(item.get("s")) if grouped else 0,
                )
            )
    entries.sort(key=lambda e: (e.created, e.path))
    return entries


def to_camera_epoch(host_time: datetime.datetime | float, camera_clock_is_local: bool = True) -> int:
    """Convert a host time (aware datetime or POSIX seconds) to the camera's ``cre`` convention."""
    if not isinstance(host_time, datetime.datetime):
        host_time = datetime.datetime.fromtimestamp(float(host_time), tz=datetime.UTC)
    if host_time.tzinfo is None:
        host_time = host_time.astimezone()
    if not camera_clock_is_local:
        return int(host_time.timestamp())
    local = host_time.astimezone()  # host local zone (what the camera was synced to)
    return calendar.timegm(local.replace(tzinfo=None).timetuple())


def filter_since(entries: list[MediaEntry], since_camera_epoch: int | None) -> list[MediaEntry]:
    if not since_camera_epoch:
        return list(entries)
    return [e for e in entries if e.created >= since_camera_epoch]


def local_path_for(dest: Path, camera_path: str) -> Path:
    """Keep the camera folder structure: <dest>/<100GOPRO>/<GX010001.MP4>. Rejects path traversal."""
    parts = [p for p in camera_path.split("/") if p]
    if not parts or any(p in (".", "..") for p in parts):
        raise ValueError(f"invalid camera path '{camera_path}'")
    return dest.joinpath(*parts)


ProgressFn = Callable[[int, int, str, int, int], None]  # file_index, file_count, path, done, total
