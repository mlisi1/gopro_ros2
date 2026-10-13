"""The set of channels produced by opening one transport."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from gopro_camera.transports.ble import BleChannel
from gopro_camera.transports.http import HttpChannel

logger = logging.getLogger(__name__)


@dataclass
class TransportBundle:
    name: str  # usb | wifi_ap | cohn | ble
    http: HttpChannel | None = None
    ble: BleChannel | None = None
    api_version: str = ""
    sdk_objects: list[Any] = field(default_factory=list)

    @property
    def primary_kind(self) -> str:
        return "http" if self.http is not None else "ble"

    async def close(self) -> None:
        if self.ble is not None:
            await self.ble.close()
        if self.http is not None:
            self.http.mark_connected(False)
        for sdk in self.sdk_objects:
            try:
                await sdk.close()
            except Exception as e:  # closing must never raise
                logger.warning("error while closing %s SDK object: %r", self.name, e)
