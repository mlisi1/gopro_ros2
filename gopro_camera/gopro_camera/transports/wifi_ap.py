"""Wi-Fi AP: the host joins the camera's access point (camera at 10.5.5.9:8080, per spec).

The SDK enables the AP over BLE, reads SSID/password over BLE and joins it with the host's Wi-Fi
interface (nmcli on Linux). BLE therefore stays connected and is exposed as a channel too.
"""

from __future__ import annotations

import logging

from gopro_camera.errors import NotConnected
from gopro_camera.transports.base import TransportConfig
from gopro_camera.transports.ble import BleChannel, build_wireless_sdk
from gopro_camera.transports.bundle import TransportBundle
from gopro_camera.transports.http import HttpChannel

logger = logging.getLogger(__name__)


async def open_wifi_ap(cfg: TransportConfig) -> TransportBundle:
    sdk = build_wireless_sdk(cfg, {"BLE", "WIFI_AP"}, null_wifi=False)
    try:
        await sdk.open(timeout=int(cfg.connect_timeout), retries=cfg.ble_retries)
    except Exception as e:
        await sdk.close()
        raise NotConnected(f"Wi-Fi AP: could not connect ({type(e).__name__}: {e})") from e
    http = HttpChannel("wifi_ap", sdk, cfg.command_timeout)
    ble = BleChannel("wifi_ap", sdk, cfg.command_timeout)
    version = await http.get_version()
    http.mark_connected(True)
    logger.info("Wi-Fi AP: connected to camera %s", sdk.identifier)
    return TransportBundle(name="wifi_ap", http=http, ble=ble, api_version=version, sdk_objects=[sdk])
