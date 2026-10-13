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


async def _close_ble(ble: BleChannel, sdk: object) -> None:
    await ble.close()
    try:
        await sdk.close()  # type: ignore[attr-defined]
    except Exception as e:  # closing must never raise
        logger.warning("error while closing BLE: %r", e)


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
    # BLE side channel *before* USB: enabling wired USB control locks the camera UI ("USB CONNECTED"),
    # which also closes its pairing screen, and with the HERO12/13 bond bug (gopro/OpenGoPro#476) a BLE
    # connection may only succeed while that screen is open.
    pre_ble: tuple[BleChannel, object] | None = None
    tried_ble = False
    if cfg.ble_sidechannel and "usb" in candidates:
        tried_ble = True
        try:
            pre_ble = await open_ble_channel(cfg, "ble")
        except GoProError as e:
            logger.warning("BLE side channel unavailable (retried in the background): %s", e)
    for t in candidates:
        try:
            bundle = await _open_one(cfg, t)
            break
        except GoProError as e:
            errors.append(f"{t}: {e}")
            logger.info("transport %s unavailable: %s", t, e)
    if bundle is None and pre_ble is not None and cfg.transport == "usb":
        # The cable is not there (yet): run on BLE; GoProCamera attaches USB when it appears. Needed while
        # the bond bug forces pairing with USB unplugged (the USB connection locks the camera UI).
        logger.warning("USB not available yet; running on the BLE side channel until the cable is connected")
        ble, sdk = pre_ble
        return TransportBundle(name="usb", ble=ble, api_version=await ble.get_version(), sdk_objects=[sdk])
    if bundle is None:
        if pre_ble is not None:
            await _close_ble(*pre_ble)
        raise NotConnected("no transport could be opened: " + "; ".join(errors))
    if pre_ble is not None:
        if bundle.name == "usb" and bundle.ble is None:
            bundle.ble = pre_ble[0]
            bundle.sdk_objects.append(pre_ble[1])
        else:  # another transport was chosen (auto) that brings its own BLE, or none is wanted
            await _close_ble(*pre_ble)
    # USB has no BLE of its own; open a separate BLE link as side channel when requested.
    elif cfg.ble_sidechannel and not tried_ble and bundle.ble is None and bundle.name == "usb":
        try:
            ble, sdk = await open_ble_channel(cfg, "ble")
            bundle.ble = ble
            bundle.sdk_objects.append(sdk)
        except GoProError as e:
            logger.warning("BLE side channel unavailable, continuing with %s only: %s", bundle.name, e)
    return bundle
