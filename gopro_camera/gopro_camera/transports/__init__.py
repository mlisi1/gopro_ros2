"""Transport adapters over the open_gopro SDK. See :mod:`gopro_camera.transports.base`."""

from __future__ import annotations

import logging

from gopro_camera.errors import GoProError, NotConnected
from gopro_camera.transports import sdk_compat
from gopro_camera.transports.base import TRANSPORTS, CameraSnapshot, TransportConfig
from gopro_camera.transports.ble import BleChannel, build_wireless_sdk
from gopro_camera.transports.bundle import TransportBundle
from gopro_camera.transports.http import HttpChannel

__all__ = [
    "TRANSPORTS",
    "BleChannel",
    "CameraSnapshot",
    "HttpChannel",
    "TransportBundle",
    "TransportConfig",
    "open_ble_channel",
    "open_transport",
]

logger = logging.getLogger(__name__)


async def open_ble_channel(cfg: TransportConfig, name: str = "ble") -> tuple[BleChannel, object]:
    """Open a BLE-only SDK object (primary BLE transport or side channel)."""
    sdk = build_wireless_sdk(cfg, {"BLE"}, null_wifi=True)
    try:
        await sdk.open(timeout=int(cfg.connect_timeout), retries=cfg.ble_retries)
    except Exception as e:
        await sdk.close()
        raise NotConnected(
            f"BLE: could not connect to {cfg.ble_target or 'first GoPro'} ({type(e).__name__}: {e})"
        ) from e
    return BleChannel(name, sdk, cfg.command_timeout), sdk


async def _open_one(cfg: TransportConfig, transport: str) -> TransportBundle:
    if transport == "usb":
        from gopro_camera.transports.usb import open_usb

        return await open_usb(cfg)
    if transport == "wifi_ap":
        from gopro_camera.transports.wifi_ap import open_wifi_ap

        return await open_wifi_ap(cfg)
    if transport == "cohn":
        from gopro_camera.transports.cohn import open_cohn

        return await open_cohn(cfg)
    if transport == "ble":
        ble, sdk = await open_ble_channel(cfg, "ble")
        return TransportBundle(name="ble", ble=ble, api_version=await ble.get_version(), sdk_objects=[sdk])
    raise NotConnected(f"unknown transport '{transport}' (expected one of {TRANSPORTS} or auto)")


async def open_transport(cfg: TransportConfig, auto_order: list[str] | None = None) -> TransportBundle:
    """Open the configured transport (``auto`` tries ``auto_order``), plus the BLE side channel."""
    # Fail with one clear message if the installed SDK lacks anything the driver uses (not per transport).
    sdk_compat.check_sdk()
    if warning := sdk_compat.version_warning():
        logger.warning("%s", warning)
    order = auto_order or ["usb", "cohn", "ble"]
    candidates = order if cfg.transport == "auto" else [cfg.transport]
    errors: list[str] = []
    bundle: TransportBundle | None = None
    for t in candidates:
        try:
            bundle = await _open_one(cfg, t)
            break
        except GoProError as e:
            errors.append(f"{t}: {e}")
            logger.info("transport %s unavailable: %s", t, e)
    if bundle is None:
        raise NotConnected("no transport could be opened: " + "; ".join(errors))
    # USB has no BLE of its own; open a separate BLE link as side channel when requested.
    if cfg.ble_sidechannel and bundle.ble is None and bundle.name == "usb":
        try:
            ble, sdk = await open_ble_channel(cfg, "ble")
            bundle.ble = ble
            bundle.sdk_objects.append(sdk)
        except GoProError as e:
            logger.warning("BLE side channel unavailable, continuing with %s only: %s", bundle.name, e)
    return bundle
