"""Status parsing -> typed Status message fields + diagnostics.

Status IDs are resolved *by their title in the official spec* (``spec_tables.STATUSES``) at import
time, so a renumbered or missing status fails loudly instead of silently reading the wrong value.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gopro_camera.spec_tables import STATUS_VALUES, STATUSES

_BY_TITLE = {title.casefold(): sid for sid, title in STATUSES.items()}


def status_id(title: str) -> int:
    try:
        return _BY_TITLE[title.casefold()]
    except KeyError as e:
        raise KeyError(f"status '{title}' not found in spec tables; regenerate spec_tables.py") from e


class S:
    """Status IDs used by the driver, looked up from the spec by title."""

    BATTERY_PRESENT = status_id("Battery Present")
    BATTERY_BARS = status_id("Internal Battery Bars")
    BATTERY_PERCENT = status_id("Internal Battery Percentage")
    OVERHEATING = status_id("Overheating")
    COLD = status_id("Cold")
    BUSY = status_id("Busy")
    ENCODING = status_id("Encoding")
    READY = status_id("Ready")
    VIDEO_ENCODING_DURATION = status_id("Video Encoding Duration")
    WIRELESS_ENABLED = status_id("Wireless Connections Enabled")
    PREVIEW_STREAM = status_id("Preview Stream")
    PRIMARY_STORAGE = status_id("Primary Storage")
    REMAINING_PHOTOS = status_id("Remaining Photos")
    REMAINING_VIDEO_TIME = status_id("Remaining Video Time")
    PHOTOS = status_id("Photos")
    VIDEOS = status_id("Videos")
    OTA = status_id("OTA")
    SD_REMAINING = status_id("SD Card Remaining")
    ACTIVE_HILIGHTS = status_id("Active Hilights")
    GPS_LOCK = status_id("GPS Lock")
    AP_MODE = status_id("AP Mode")
    MICROPHONE = status_id("Microphone Accessory")
    PRESET_GROUP = status_id("Preset Group")
    PRESET = status_id("Preset")
    PRESET_MODIFIED = status_id("Preset Modified")
    MEDIA_MOD = status_id("Media Mod State")
    LENS_TYPE = status_id("Lens Type")
    HINDSIGHT = status_id("Hindsight")
    SD_WRITE_SPEED_ERROR = status_id("SD Card Write Speed Error")
    SD_ERRORS = status_id("SD Card Errors")
    CAMERA_CONTROL = status_id("Camera Control ID")
    USB_CONNECTED = status_id("USB Connected")
    USB_CONTROLLED = status_id("USB Controlled")
    SD_CAPACITY = status_id("SD Card Capacity")
    CAMERA_NAME = status_id("Camera Name")


SD_OK = next(v for v, m in STATUS_VALUES[S.PRIMARY_STORAGE].items() if m.casefold() == "ok")
SD_UNKNOWN = next(v for v, m in STATUS_VALUES[S.PRIMARY_STORAGE].items() if m.casefold() == "unknown")


def as_int(value: Any, default: int = -1) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def as_flag(value: Any) -> bool:
    return as_int(value, 0) != 0


def value_label(sid: int, value: Any) -> str:
    v = as_int(value)
    return STATUS_VALUES.get(sid, {}).get(v, str(value) if value is not None else "unknown")


def status_fields(st: dict[int, Any]) -> dict[str, Any]:
    """Camera statuses (ID -> value) -> Status.msg field values (link/driver fields excluded)."""
    g = st.get
    return {
        "battery_present": as_int(g(S.BATTERY_PRESENT)),
        "battery_bars": as_int(g(S.BATTERY_BARS)),
        "battery_percent": as_int(g(S.BATTERY_PERCENT)),
        "busy": as_flag(g(S.BUSY)),
        "encoding": as_flag(g(S.ENCODING)),
        "ready": as_flag(g(S.READY)),
        "video_encoding_duration": as_int(g(S.VIDEO_ENCODING_DURATION)),
        "hindsight_active": as_flag(g(S.HINDSIGHT)),
        "active_hilights": as_int(g(S.ACTIVE_HILIGHTS)),
        "photos_on_sd": as_int(g(S.PHOTOS)),
        "videos_on_sd": as_int(g(S.VIDEOS)),
        "sd_status": as_int(g(S.PRIMARY_STORAGE), SD_UNKNOWN),
        "sd_status_text": value_label(S.PRIMARY_STORAGE, g(S.PRIMARY_STORAGE)),
        "sd_remaining_kb": as_int(g(S.SD_REMAINING)),
        "remaining_video_time": as_int(g(S.REMAINING_VIDEO_TIME)),
        "remaining_photos": as_int(g(S.REMAINING_PHOTOS)),
        "sd_write_speed_error": as_int(g(S.SD_WRITE_SPEED_ERROR)),
        "sd_errors": as_int(g(S.SD_ERRORS)),
        "sd_capacity_kb": as_int(g(S.SD_CAPACITY)),
        "overheating": as_int(g(S.OVERHEATING)),
        "cold": as_int(g(S.COLD)),
        "gps_lock": as_int(g(S.GPS_LOCK)),
        "preset_id": as_int(g(S.PRESET)),
        "preset_group": as_int(g(S.PRESET_GROUP)),
        "preset_modified": as_int(g(S.PRESET_MODIFIED)),
        "lens_type": as_int(g(S.LENS_TYPE)),
        "media_mod_state": as_int(g(S.MEDIA_MOD)),
        "microphone_accessory": as_int(g(S.MICROPHONE)),
        "ap_mode": as_int(g(S.AP_MODE)),
        "wireless_enabled": as_int(g(S.WIRELESS_ENABLED)),
        "usb_connected": as_int(g(S.USB_CONNECTED)),
        "usb_controlled": as_int(g(S.USB_CONTROLLED)),
        "camera_control": as_int(g(S.CAMERA_CONTROL)),
        "preview_stream_active": as_int(g(S.PREVIEW_STREAM)),
        "ota_state": as_int(g(S.OTA)),
        "camera_name": str(g(S.CAMERA_NAME) or ""),
    }


def raw_numeric(st: dict[int, Any]) -> tuple[list[int], list[int]]:
    ids: list[int] = []
    vals: list[int] = []
    for k in sorted(st):
        v = st[k]
        if isinstance(v, bool | int) or (isinstance(v, str) and v.lstrip("-").isdigit()):
            ids.append(int(k))
            vals.append(int(v))
    return ids, vals


# ---------------------------------------------------------------------------------- diagnostics
OK, WARN, ERROR = 0, 1, 2


@dataclass
class DiagThresholds:
    battery_warn_percent: int = 20
    battery_error_percent: int = 10
    sd_warn_minutes: float = 30.0
    sd_error_minutes: float = 5.0
    status_stale_s: float = 5.0
    stream_stall_s: float = 3.0


@dataclass
class DiagItem:
    name: str
    level: int
    message: str
    values: dict[str, str]


def evaluate(
    st: dict[int, Any],
    *,
    connected: bool,
    status_age: float,
    transport: str,
    stream_active: bool,
    stream_frame_age: float | None,
    th: DiagThresholds,
    running_on_battery: bool = True,
) -> list[DiagItem]:
    items: list[DiagItem] = []

    if not connected:
        items.append(DiagItem("link", ERROR, f"disconnected ({transport})", {"transport": transport}))
    elif status_age > th.status_stale_s:
        items.append(DiagItem("link", WARN, f"no status for {status_age:.1f}s", {"transport": transport}))
    else:
        items.append(DiagItem("link", OK, f"connected via {transport}", {"transport": transport}))

    pct = as_int(st.get(S.BATTERY_PERCENT))
    bars = as_int(st.get(S.BATTERY_BARS))
    charging = STATUS_VALUES.get(S.BATTERY_BARS, {}).get(bars, "").casefold() == "charging"
    vals = {"percent": str(pct), "bars": value_label(S.BATTERY_BARS, bars)}
    if pct < 0:
        items.append(DiagItem("battery", OK, "unknown", vals))
    elif running_on_battery and not charging and pct <= th.battery_error_percent:
        items.append(DiagItem("battery", ERROR, f"battery critical {pct}%", vals))
    elif running_on_battery and not charging and pct <= th.battery_warn_percent:
        items.append(DiagItem("battery", WARN, f"battery low {pct}%", vals))
    else:
        items.append(DiagItem("battery", OK, f"{pct}%" + (" (charging)" if charging else ""), vals))

    sd = as_int(st.get(S.PRIMARY_STORAGE), SD_UNKNOWN)
    rem_s = as_int(st.get(S.REMAINING_VIDEO_TIME))
    vals = {"status": value_label(S.PRIMARY_STORAGE, sd), "remaining_video_s": str(rem_s)}
    if sd not in (SD_OK, SD_UNKNOWN):
        items.append(DiagItem("sd_card", ERROR, value_label(S.PRIMARY_STORAGE, sd), vals))
    elif as_flag(st.get(S.SD_WRITE_SPEED_ERROR)):
        items.append(DiagItem("sd_card", WARN, "SD card write speed error", vals))
    elif 0 <= rem_s < th.sd_error_minutes * 60:
        items.append(DiagItem("sd_card", ERROR, f"SD almost full ({rem_s // 60} min left)", vals))
    elif 0 <= rem_s < th.sd_warn_minutes * 60:
        items.append(DiagItem("sd_card", WARN, f"SD low ({rem_s // 60} min left)", vals))
    else:
        items.append(DiagItem("sd_card", OK, value_label(S.PRIMARY_STORAGE, sd), vals))

    hot, cold = as_flag(st.get(S.OVERHEATING)), as_flag(st.get(S.COLD))
    if hot:
        items.append(DiagItem("thermal", ERROR, "camera overheating", {}))
    elif cold:
        items.append(DiagItem("thermal", WARN, "camera too cold", {}))
    else:
        items.append(DiagItem("thermal", OK, "ok", {}))

    if stream_active:
        if stream_frame_age is None or stream_frame_age > th.stream_stall_s:
            age = "never" if stream_frame_age is None else f"{stream_frame_age:.1f}s ago"
            items.append(DiagItem("stream", ERROR, f"stream stalled (last frame {age})", {}))
        else:
            items.append(DiagItem("stream", OK, "receiving", {"frame_age_s": f"{stream_frame_age:.2f}"}))
    return items
