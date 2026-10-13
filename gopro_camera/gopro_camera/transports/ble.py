"""BLE channel over the open_gopro SDK (``WirelessGoPro``).

Used either as the primary transport (``transport: ble``) or as a side channel next to USB/COHN
(``ble_sidechannel: true``) for push status, wake-from-sleep, Wi-Fi AP control, COHN provisioning and
RTMP livestream setup. BLE calls are natively async in the SDK (bleak), so they run directly on the
camera's asyncio loop; one command is in flight at a time.

The SDK object is created with ``maintain_state=False``: the SDK's own ready-lock would otherwise
block every command while the camera is encoding, and its ``open()`` would wait for encoding to
stop, which is incompatible with adopting an ongoing recording after a driver restart. The driver
does its own busy/encoding checks and keep-alive instead.
"""

from __future__ import annotations

import asyncio
import datetime
import enum
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from gopro_camera.errors import (
    CameraRejected,
    CommandTimeout,
    InvalidSetting,
    NotConnected,
    NotSupported,
    TransportError,
)
from gopro_camera.transports.base import CameraSnapshot, Channel, TransportConfig, ensure_private_file

logger = logging.getLogger(__name__)

StatusCb = Callable[[dict[int, Any], str], Awaitable[None]]


def plain(value: Any) -> Any:
    """SDK values (GoProIntEnum, bool, bytes, ...) -> plain int/str for messages."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, enum.Enum):
        return int(value.value) if isinstance(value.value, int) else str(value.value)
    if isinstance(value, bytes | bytearray):
        return value.decode(errors="replace")
    return value


def plain_dict(d: dict[Any, Any]) -> dict[int, Any]:
    out: dict[int, Any] = {}
    for k, v in (d or {}).items():
        try:
            out[int(k)] = plain(v)
        except (TypeError, ValueError):
            continue
    return out


def make_null_wifi_controller() -> type:
    """A Wi-Fi controller that does nothing.

    The SDK's default ``WifiCli`` requires an en_US locale and a detectable Wi-Fi interface even when
    only BLE or COHN is used; inject this one in those modes.
    """
    from open_gopro.network.wifi.controller import SsidState, WifiController

    class NullWifiController(WifiController):
        def __init__(self, interface: str | None = None, password: str | None = None) -> None:
            super().__init__(interface, password)

        async def connect(self, ssid: str, password: str, timeout: float = 15) -> bool:
            return False

        async def disconnect(self) -> bool:
            return True

        def current(self) -> tuple[str | None, Any]:
            return None, SsidState.DISCONNECTED

        def available_interfaces(self) -> list[str]:
            return []

        def power(self, power: bool) -> bool:
            return True

        @property
        def is_on(self) -> bool:
            return False

    return NullWifiController


def build_wireless_sdk(cfg: TransportConfig, interfaces: set[str], null_wifi: bool) -> Any:
    """Construct a ``WirelessGoPro`` for the requested SDK interfaces ({"BLE", "WIFI_AP", "COHN"})."""
    from open_gopro import WirelessGoPro

    db = ensure_private_file(cfg.cohn_credentials_path)
    kwargs: dict[str, Any] = {
        "target": cfg.ble_target,
        "interfaces": {WirelessGoPro.Interface[i] for i in interfaces},
        "cohn_db": db,
        "maintain_state": False,
    }
    if null_wifi:
        kwargs["wifi_adapter"] = make_null_wifi_controller()
    else:
        kwargs["host_wifi_interface"] = cfg.wifi_ap_interface
        kwargs["host_sudo_password"] = cfg.wifi_ap_sudo_password
    return WirelessGoPro(**kwargs)


class BleChannel(Channel):
    kind = "ble"

    def __init__(self, name: str, sdk: Any, command_timeout: float = 10.0) -> None:
        super().__init__(name)
        self.sdk = sdk
        self.command_timeout = command_timeout
        self._lock = asyncio.Lock()
        self._push_tasks: list[asyncio.Task[None]] = []
        self._setting_facades: dict[int, Any] | None = None

    @property
    def is_connected(self) -> bool:
        try:
            return bool(self.sdk.is_ble_connected)
        except Exception:
            return False

    async def _run(
        self, factory: Callable[[], Awaitable[Any]], what: str, *, timeout: float | None = None, check: bool = True
    ) -> Any:
        if not self.is_connected:
            raise NotConnected(f"{what}: BLE not connected")
        timeout = timeout or self.command_timeout
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout)
        except TimeoutError as e:
            raise CommandTimeout(f"{what}: BLE channel busy for {timeout:.0f}s") from e
        try:
            resp = await asyncio.wait_for(factory(), timeout)
        except TimeoutError as e:
            raise CommandTimeout(f"{what}: no BLE answer within {timeout:.0f}s") from e
        except (CameraRejected, NotSupported, InvalidSetting):
            raise
        except Exception as e:
            name = type(e).__name__
            if name == "ResponseTimeout":
                raise CommandTimeout(f"{what}: BLE response timeout") from e
            if name == "GoProNotOpened":
                raise NotConnected(f"{what}: BLE not opened") from e
            raise TransportError(f"{what}: BLE error {name}: {e}") from e
        finally:
            self._lock.release()
        if check and hasattr(resp, "ok") and not resp.ok:
            raise CameraRejected(f"{what} rejected by camera over BLE (status {getattr(resp, 'status', '?')})")
        return resp

    # ------------------------------------------------------------------ basic commands
    async def keep_alive(self) -> None:
        await self._run(lambda: self.sdk.keep_alive(), "BLE keep alive", timeout=5, check=False)

    async def set_third_party_client(self) -> None:
        await self._run(lambda: self.sdk.ble_command.set_third_party_client_info(), "set client info")

    async def set_camera_control_external(self) -> None:
        from open_gopro.models import proto

        await self._run(
            lambda: self.sdk.ble_command.set_camera_control(
                camera_control_status=proto.EnumCameraControlStatus.CAMERA_EXTERNAL_CONTROL
            ),
            "claim camera control",
        )

    async def set_camera_control_idle(self) -> None:
        from open_gopro.models import proto

        await self._run(
            lambda: self.sdk.ble_command.set_camera_control(
                camera_control_status=proto.EnumCameraControlStatus.CAMERA_IDLE
            ),
            "release camera control",
        )

    async def get_version(self) -> str:
        return str((await self._run(lambda: self.sdk.ble_command.get_open_gopro_api_version(), "get version")).data)

    async def get_hardware_info(self) -> dict[str, Any]:
        r = await self._run(lambda: self.sdk.ble_command.get_hardware_info(), "get hardware info")
        d = r.data
        return d.model_dump() if hasattr(d, "model_dump") else dict(d)

    async def set_shutter(self, on: bool) -> None:
        from open_gopro.models.constants import Toggle

        await self._run(
            lambda: self.sdk.ble_command.set_shutter(shutter=Toggle.ENABLE if on else Toggle.DISABLE),
            "shutter " + ("start" if on else "stop"),
        )

    async def hilight_moment(self) -> None:
        await self._run(lambda: self.sdk.ble_command.tag_hilight(), "hilight moment")

    async def sleep(self) -> None:
        await self._run(lambda: self.sdk.ble_command.sleep(), "sleep")

    async def power_down(self) -> None:
        await self._run(lambda: self.sdk.ble_command.power_down(), "power down")

    async def enable_wifi_ap(self, enable: bool) -> None:
        await self._run(
            lambda: self.sdk.ble_command.enable_wifi_ap(enable=enable), f"wifi ap {'on' if enable else 'off'}"
        )

    async def set_turbo(self, enable: bool) -> None:
        from open_gopro.models.constants import Toggle

        await self._run(
            lambda: self.sdk.ble_command.set_turbo_mode(mode=Toggle.ENABLE if enable else Toggle.DISABLE),
            "turbo transfer",
        )

    # ------------------------------------------------------------------ state
    async def get_state(self, timeout: float | None = None) -> CameraSnapshot:
        st = await self._run(lambda: self.sdk.ble_command.get_camera_statuses(), "get statuses", timeout=timeout)
        se = await self._run(lambda: self.sdk.ble_command.get_camera_settings(), "get settings", timeout=timeout)
        return CameraSnapshot(statuses=plain_dict(st.data), settings=plain_dict(se.data))

    async def get_capabilities(self) -> dict[int, list[int]]:
        r = await self._run(lambda: self.sdk.ble_command.get_camera_capabilities(), "get capabilities", check=False)
        if not r.ok or not isinstance(r.data, dict):
            return {}
        out: dict[int, list[int]] = {}
        for k, v in r.data.items():
            try:
                vals = v if isinstance(v, list) else [v]
                out[int(k)] = [int(plain(x)) for x in vals]
            except (TypeError, ValueError):
                continue
        return out

    async def subscribe_statuses(self, callback: StatusCb) -> None:
        """Register for push notifications of all statuses; ``callback(changed, 'ble_push')``."""
        result = await self._run(
            lambda: self.sdk.ble_command.get_observable_for_all_statuses(), "register statuses", check=False
        )
        observable = result.unwrap()

        async def _consume() -> None:
            async for update in observable.observe():
                try:
                    await callback(plain_dict(update), "ble_push")
                except Exception:  # never let a consumer error kill the push task
                    logger.exception("status push callback failed")

        self._push_tasks.append(asyncio.create_task(_consume(), name="gopro-ble-status-push"))

    async def stop_push(self) -> None:
        for t in self._push_tasks:
            t.cancel()
        await asyncio.gather(*self._push_tasks, return_exceptions=True)
        self._push_tasks.clear()

    # ------------------------------------------------------------------ settings / presets
    def _facades(self) -> dict[int, Any]:
        if self._setting_facades is None:
            from open_gopro.api.builders import BleSettingFacade

            self._setting_facades = {
                int(v._identifier): v for v in vars(self.sdk.ble_setting).values() if isinstance(v, BleSettingFacade)
            }
        return self._setting_facades

    def supports_setting(self, setting_id: int) -> bool:
        return setting_id in self._facades()

    async def set_setting(self, setting_id: int, option: int) -> None:
        facade = self._facades().get(setting_id)
        if facade is None:
            raise NotSupported(f"setting {setting_id} is not known to the installed open_gopro SDK; use HTTP")
        r = await self._run(lambda: facade.set(option), f"set setting {setting_id}={option}", check=False)
        if not r.ok:
            raise InvalidSetting(
                f"setting {setting_id} option {option} rejected by camera over BLE (status {r.status})"
            )

    async def load_preset(self, preset_id: int) -> None:
        await self._run(lambda: self.sdk.ble_command.load_preset(preset=preset_id), f"load preset {preset_id}")

    async def load_preset_group(self, group_id: int) -> None:
        await self._run(lambda: self.sdk.ble_command.load_preset_group(group=group_id), f"load preset group {group_id}")

    async def get_presets(self, include_hidden: bool = False) -> dict[str, Any]:
        from google.protobuf.json_format import MessageToDict

        r = await self._run(
            lambda: self.sdk.ble_command.get_preset_status(include_hidden=include_hidden), "get presets"
        )
        return MessageToDict(r.data, use_integers_for_enums=True)

    async def last_captured(self) -> str | None:
        r = await self._run(lambda: self.sdk.ble_command.get_last_captured_media(), "last captured media", check=False)
        media = getattr(r.data, "media", None)
        if r.ok and media is not None and getattr(media, "folder", "") and getattr(media, "file", ""):
            return f"{media.folder}/{media.file}"
        return None

    # ------------------------------------------------------------------ time
    async def set_date_time(self, dt: datetime.datetime, tz_offset_min: int, is_dst: bool) -> None:
        await self._run(
            lambda: self.sdk.ble_command.set_date_time_tz_dst(date_time=dt, tz_offset=tz_offset_min, is_dst=is_dst),
            "set date/time",
        )

    async def get_date_time(self) -> datetime.datetime:
        r = await self._run(lambda: self.sdk.ble_command.get_date_time_tz_dst(), "get date/time")
        return r.data.datetime.replace(tzinfo=None)

    # ------------------------------------------------------------------ livestream
    async def livestream_status(self) -> int:
        from open_gopro.models import proto

        r = await self._run(
            lambda: self.sdk.ble_command.register_livestream_status(
                register=[proto.EnumRegisterLiveStreamStatus.REGISTER_LIVE_STREAM_STATUS_STATUS]
            ),
            "livestream status",
        )
        return int(r.data.live_stream_status)

    async def set_livestream_mode(self, **kwargs: Any) -> None:
        await self._run(lambda: self.sdk.ble_command.set_livestream_mode(**kwargs), "configure livestream")

    # ------------------------------------------------------------------ networking / COHN
    async def cohn_status(self) -> Any:
        r = await self._run(lambda: self.sdk.ble_command.cohn_get_status(register=False), "COHN status")
        return r.data

    async def connect_camera_to_ap(self, ssid: str, password: str, timeout: float = 90.0) -> None:
        from returns.pipeline import is_successful

        try:
            result = await asyncio.wait_for(self.sdk.access_point.connect(ssid, password), timeout)
        except TimeoutError as e:
            raise CommandTimeout(f"camera did not join '{ssid}' within {timeout:.0f}s") from e
        if not is_successful(result):
            raise CameraRejected(f"camera failed to join '{ssid}': {result.failure()}")

    async def configure_cohn(self, force_reprovision: bool, timeout: float = 120.0) -> Any:
        from returns.pipeline import is_successful

        try:
            result = await asyncio.wait_for(self.sdk.cohn.configure(force_reprovision=force_reprovision), timeout)
        except TimeoutError as e:
            raise CommandTimeout(f"COHN provisioning timed out after {timeout:.0f}s") from e
        except RuntimeError as e:
            raise NotSupported(f"COHN not supported / not ready: {e}") from e
        if not is_successful(result):
            raise CameraRejected(f"COHN provisioning failed: {result.failure()}")
        return result.unwrap()

    async def close(self) -> None:
        await self.stop_push()
