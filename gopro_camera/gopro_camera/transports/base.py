"""Shared transport types.

A *transport* (usb | wifi_ap | cohn | ble) is how we reach the camera. It produces one or two
*channels*:

- an :class:`~gopro_camera.transports.http.HttpChannel` (USB, Wi-Fi AP, COHN), and/or
- a :class:`~gopro_camera.transports.ble.BleChannel` (BLE, either primary or as side channel).

Channels are thin adapters over ``open_gopro`` SDK objects. They normalise results to plain Python
types, apply timeouts, serialise commands (one in-flight command per channel) and map failures to the
typed errors in :mod:`gopro_camera.errors`.
"""

from __future__ import annotations

import os
import stat
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gopro_camera.errors import NotSupported

TRANSPORTS = ("usb", "wifi_ap", "cohn", "ble")


@dataclass
class TransportConfig:
    transport: str = "usb"
    serial: str | None = None  # full serial or (at least) the trailing digits
    ble_name: str | None = None  # alternative BLE identifier (e.g. "GoPro 1234")
    ble_sidechannel: bool = False
    usb_interface: str | None = None
    usb_base_url: str | None = None  # override the derived http://172.2X.1YZ.51:8080/ (tests, NAT)
    cohn_credentials_path: str = "~/.ros/gopro/cohn_credentials.json"
    wifi_ap_interface: str | None = None
    wifi_ap_sudo_password: str | None = None
    connect_timeout: float = 30.0
    command_timeout: float = 10.0
    ble_retries: int = 3
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ble_target(self) -> str | None:
        """Identifier the SDK uses for BLE scanning (matched against the advertised name)."""
        if self.ble_name:
            return self.ble_name.removeprefix("GoPro ").strip()
        if self.serial:
            return self.serial[-4:]
        return None


@dataclass
class CameraSnapshot:
    """Raw camera state: status/setting IDs -> numeric (or string) values."""

    statuses: dict[int, Any]
    settings: dict[int, Any]


class Channel(ABC):
    """Common command interface. GoProCamera routes commands with ``fn(channel)`` over whichever
    channel is connected, so a command only one transport supports is declared here and raises
    NotSupported on the others (it is then retried on the next channel, never an AttributeError).
    """

    kind: str  # "http" | "ble"

    def __init__(self, name: str) -> None:
        self.name = name  # transport name that owns the channel, e.g. "usb" or "ble"

    @property
    @abstractmethod
    def is_connected(self) -> bool: ...

    async def power_down(self) -> None:
        raise NotSupported(f"power down is not available over {self.kind}")


def ensure_private_file(path: str | os.PathLike[str]) -> Path:
    """Create the parent dir (0700) and the file (0600) used to persist credentials."""
    p = Path(os.path.expanduser(str(path)))
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(p.parent, stat.S_IRWXU)
    except PermissionError:
        pass
    if not p.exists():
        fd = os.open(p, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
    os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    return p
