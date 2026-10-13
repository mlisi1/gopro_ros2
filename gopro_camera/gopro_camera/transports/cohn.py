"""COHN (Camera On the Home Network): the camera joined an existing network; HTTPS + basic auth.

Credentials (IP, username, password, certificate) are produced by provisioning over BLE and are
persisted by the SDK's COHN database at ``cohn_credentials_path`` (directory 0700, file 0600). After
that, COHN works without BLE. With ``ble_sidechannel`` the same SDK object also holds BLE, which lets
the SDK refresh the camera IP from the COHN status.
"""

from __future__ import annotations

import logging

from gopro_camera.errors import NotConnected
from gopro_camera.transports import sdk_compat
from gopro_camera.transports.base import TransportConfig
from gopro_camera.transports.ble import BleChannel, build_wireless_sdk
from gopro_camera.transports.bundle import TransportBundle
from gopro_camera.transports.http import HttpChannel

logger = logging.getLogger(__name__)


async def open_cohn(cfg: TransportConfig) -> TransportBundle:
    if not cfg.ble_target:
        raise NotConnected("COHN: 'serial' (or 'ble_name') is required to look up stored credentials")
    interfaces = {"COHN", "BLE"} if cfg.ble_sidechannel else {"COHN"}
    sdk = build_wireless_sdk(cfg, interfaces, null_wifi=True)
    try:
        await sdk.open(timeout=int(cfg.connect_timeout), retries=cfg.ble_retries)
    except Exception as e:
        await sdk.close()
        raise NotConnected(f"COHN: could not open ({type(e).__name__}: {e})") from e
    if not await sdk.cohn.is_configured:
        ble_hint = "" if cfg.ble_sidechannel else " (enable ble_sidechannel or provision from another transport)"
        await sdk.close()
        raise NotConnected(f"COHN: no stored credentials for camera {cfg.ble_target}; call ~/provision_cohn{ble_hint}")
    sdk_compat.mark_cohn_configured(sdk)  # SDK only sets this inside open() when BLE is up
    http = HttpChannel("cohn", sdk, cfg.command_timeout)
    version = await http.get_version()
    http.mark_connected(True)
    ble = BleChannel("ble", sdk, cfg.command_timeout) if cfg.ble_sidechannel and sdk.is_ble_connected else None
    logger.info("COHN: connected to %s", sdk.ip_address)
    return TransportBundle(name="cohn", http=http, ble=ble, api_version=version, sdk_objects=[sdk])
