"""HTTP channel (USB, Wi-Fi AP, COHN) over the open_gopro SDK.

The SDK's HTTP layer uses the blocking ``requests`` library inside ``async def`` methods. Awaiting
those directly on the per-camera asyncio loop would freeze it (status, keep-alive, BLE notifications)
for the whole duration of a request, and ``asyncio.wait_for`` could not enforce timeouts. So every
SDK HTTP coroutine is executed in a worker thread with its own short-lived event loop. This is safe
because the SDK objects are created with ``maintain_state=False`` (no loop-bound locks or tasks on the
HTTP path).

Lanes (serialisation):
- ``cmd``   : control commands, one in flight per channel;
- ``query`` : state queries and keep-alive, which the spec allows at any time; not serialised;
- ``media`` : downloads, one at a time, independent of commands so status keeps flowing.

Endpoints that SDK 0.22 does not wrap (hilight moment, sleep, raw camera state including setting IDs
newer than the SDK) are sent as ``HttpMessage`` objects through the SDK's own ``_get_json`` (via
:mod:`gopro_camera.transports.sdk_compat`) so that session handling, COHN auth headers and
certificates stay in the SDK. Endpoint paths are taken from
the official OpenAPI spec (https://gopro.github.io/OpenGoPro/http).
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import threading
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from gopro_camera.errors import (
    CameraRejected,
    CommandTimeout,
    InvalidSetting,
    NotConnected,
    TransportError,
)
from gopro_camera.transports import sdk_compat
from gopro_camera.transports.base import CameraSnapshot, Channel

logger = logging.getLogger(__name__)


def _in_daemon_thread(func: Callable[[], Any], name: str = "gopro-http") -> asyncio.Future[Any]:
    """Run ``func`` in a daemon thread and return an asyncio future for its result.

    Not a ThreadPoolExecutor on purpose: its workers are joined at interpreter exit, so a request
    stuck in the SDK's retry loop (camera unplugged) would delay process shutdown.
    """
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[Any] = loop.create_future()

    def _set(result: Any, exc: BaseException | None) -> None:
        if fut.done():
            return
        if exc is not None:
            fut.set_exception(exc)
        else:
            fut.set_result(result)

    def _target() -> None:
        try:
            result, exc = func(), None
        except BaseException as e:  # forwarded to the awaiting coroutine
            result, exc = None, e
        try:
            loop.call_soon_threadsafe(_set, result, exc)
        except RuntimeError:  # loop already closed (shutdown)
            pass

    threading.Thread(target=_target, name=name, daemon=True).start()
    return fut


ProgressCb = Callable[[int, int], None]


def _thread_run(factory: Callable[[], Coroutine[Any, Any, Any]]) -> Any:
    return asyncio.run(factory())


def _to_int_keys(d: dict[str, Any]) -> dict[int, Any]:
    out: dict[int, Any] = {}
    for k, v in (d or {}).items():
        try:
            out[int(k)] = v
        except (TypeError, ValueError):
            continue
    return out


class HttpChannel(Channel):
    kind = "http"

    def __init__(self, name: str, sdk: Any, command_timeout: float = 10.0) -> None:
        super().__init__(name)
        self.sdk = sdk
        self.command_timeout = command_timeout
        self._locks: dict[str, asyncio.Lock] = {}
        self._connected = False

    # ------------------------------------------------------------------ plumbing
    @property
    def is_connected(self) -> bool:
        return self._connected

    def mark_connected(self, value: bool) -> None:
        self._connected = value

    def _lock(self, lane: str) -> asyncio.Lock | None:
        if lane == "query":
            return None
        if lane not in self._locks:
            self._locks[lane] = asyncio.Lock()
        return self._locks[lane]

    async def _run(
        self,
        factory: Callable[[], Coroutine[Any, Any, Any]],
        what: str,
        *,
        timeout: float | None = None,
        lane: str = "cmd",
        check: bool = True,
    ) -> Any:
        """Run an SDK HTTP coroutine in a worker thread; returns the GoProResp."""
        timeout = timeout or self.command_timeout
        lock = self._lock(lane)
        if lock is not None:
            try:
                await asyncio.wait_for(lock.acquire(), timeout)
            except TimeoutError as e:
                raise CommandTimeout(f"{what}: {self.name} HTTP channel busy for {timeout:.0f}s") from e
        release_now = True
        loop = asyncio.get_running_loop()
        fut = _in_daemon_thread(lambda: _thread_run(factory))
        try:
            resp = await asyncio.wait_for(asyncio.shield(fut), timeout)
        except TimeoutError as e:
            if lock is not None:
                # Keep the lane locked until the orphaned request really finishes: one in flight.
                release_now = False
                fut.add_done_callback(lambda _f: loop.call_soon_threadsafe(lock.release))
            raise CommandTimeout(f"{what}: no answer over {self.name} HTTP within {timeout:.0f}s") from e
        except Exception as e:  # SDK raises ResponseTimeout / requests errors / GoProNotOpened
            name = type(e).__name__
            if name == "GoProNotOpened":
                raise NotConnected(f"{what}: {self.name} HTTP not opened") from e
            raise TransportError(f"{what}: {self.name} HTTP error {name}: {e}") from e
        finally:
            if lock is not None and release_now:
                lock.release()
        if check and not resp.ok:
            raise CameraRejected(f"{what} rejected by camera over {self.name} HTTP: {resp.data!r}")
        return resp

    async def get_raw(
        self,
        endpoint: str,
        what: str,
        *,
        args: dict[str, Any] | None = None,
        lane: str = "cmd",
        timeout: float | None = None,
        check: bool = True,
    ) -> Any:
        from open_gopro.domain.communicator_interface import HttpMessage

        args = {k: v for k, v in (args or {}).items() if v is not None}
        msg = HttpMessage(endpoint, None, arguments=list(args) or None)
        return await self._run(
            lambda: sdk_compat.get_json(self.sdk, msg, **args), what, lane=lane, timeout=timeout, check=check
        )

    # ------------------------------------------------------------------ connection
    async def get_version(self) -> str:
        r = await self._run(lambda: self.sdk.http_command.get_open_gopro_api_version(), "get version", lane="query")
        return str(r.data)

    async def enable_wired_usb_control(self) -> None:
        from open_gopro.models.constants import Toggle

        await self._run(lambda: self.sdk.http_command.wired_usb_control(control=Toggle.ENABLE), "wired usb control")

    async def set_third_party_client(self) -> None:
        await self._run(lambda: self.sdk.http_command.set_third_party_client_info(), "set client info")

    async def set_camera_control_external(self) -> None:
        from open_gopro.models.constants import CameraControl

        await self._run(
            lambda: self.sdk.http_command.set_camera_control(mode=CameraControl.EXTERNAL), "claim camera control"
        )

    async def set_camera_control_idle(self) -> None:
        from open_gopro.models.constants import CameraControl

        await self._run(lambda: self.sdk.http_command.set_camera_control(mode=CameraControl.IDLE), "release control")

    async def keep_alive(self) -> None:
        await self._run(lambda: self.sdk.http_command.set_keep_alive(), "keep alive", lane="query", timeout=5)

    async def get_hardware_info(self) -> dict[str, Any]:
        r = await self.get_raw("gopro/camera/info", "get hardware info", lane="query")
        return dict(r.data or {})

    # ------------------------------------------------------------------ state
    async def get_state(self, timeout: float | None = None) -> CameraSnapshot:
        r = await self.get_raw("gopro/camera/state", "get camera state", lane="query", timeout=timeout)
        data = r.data or {}
        return CameraSnapshot(
            statuses=_to_int_keys(data.get("status", {})), settings=_to_int_keys(data.get("settings", {}))
        )

    # ------------------------------------------------------------------ capture
    async def set_shutter(self, on: bool) -> None:
        from open_gopro.models.constants import Toggle

        await self._run(
            lambda: self.sdk.http_command.set_shutter(shutter=Toggle.ENABLE if on else Toggle.DISABLE),
            "shutter " + ("start" if on else "stop"),
        )

    async def hilight_moment(self) -> None:
        await self.get_raw("gopro/media/hilight/moment", "hilight moment")

    async def sleep(self) -> None:
        await self.get_raw("gp/gpControl/command/system/sleep", "sleep")

    # ------------------------------------------------------------------ settings / presets
    async def set_setting(self, setting_id: int, option: int) -> None:
        from open_gopro.api.builders import HttpSetting
        from open_gopro.models.constants import SettingId

        try:
            identifier: Any = SettingId(setting_id)
        except ValueError:
            identifier = setting_id  # newer than the SDK: the URL only needs the integer
        setting = HttpSetting(self.sdk, identifier)
        r = await self._run(lambda: setting.set(option), f"set setting {setting_id}={option}", check=False)
        if r.ok:
            return
        data = r.data if isinstance(r.data, dict) else {}
        supported: list[int] = []
        raw_opts = data.get("supported_options") or []
        if isinstance(raw_opts, dict):
            raw_opts = raw_opts.get("options") or raw_opts.get("supported_options") or []
        for o in raw_opts:
            if isinstance(o, dict) and "id" in o:
                supported.append(int(o["id"]))
        reasons = {
            1: "setting not available",
            2: "setting does not exist",
            3: "option not available",
            4: "option does not exist",
        }
        reason = reasons.get(int(data.get("error", 0) or 0), f"camera error {data!r}")
        raise InvalidSetting(f"setting {setting_id} option {option} rejected: {reason}", supported_options=supported)

    async def load_preset(self, preset_id: int) -> None:
        await self._run(lambda: self.sdk.http_command.load_preset(preset=preset_id), f"load preset {preset_id}")

    async def load_preset_group(self, group_id: int) -> None:
        await self._run(
            lambda: self.sdk.http_command.load_preset_group(group=group_id), f"load preset group {group_id}"
        )

    async def get_presets(self, include_hidden: bool = False) -> dict[str, Any]:
        r = await self._run(
            lambda: self.sdk.http_command.get_preset_status(include_hidden=include_hidden), "get presets", lane="query"
        )
        return dict(r.data or {})

    # ------------------------------------------------------------------ time
    async def set_date_time(self, dt: datetime.datetime, tz_offset_min: int, is_dst: bool) -> None:
        await self._run(
            lambda: self.sdk.http_command.set_date_time(date_time=dt, tz_offset=tz_offset_min, is_dst=is_dst),
            "set date/time",
        )

    async def get_date_time(self) -> datetime.datetime:
        r = await self.get_raw("gopro/camera/get_date_time", "get date/time", lane="query")
        d = r.data or {}
        y, mo, da = (int(x) for x in str(d["date"]).split("_"))
        h, mi, s = (int(x) for x in str(d["time"]).split("_"))
        return datetime.datetime(y, mo, da, h, mi, s)

    # ------------------------------------------------------------------ streaming
    # Webcam endpoints are sent raw (not via the SDK's WebcamResponse model): the spec allows a status
    # value (4 = unavailable) the SDK enum lacks, which would make the SDK parser fail and retry.
    async def webcam_start(
        self, resolution: int | None, fov: int | None, port: int | None, protocol: str | None
    ) -> dict[str, Any]:
        args = {"res": resolution, "fov": fov, "port": port, "protocol": protocol}
        r = await self.get_raw("gopro/webcam/start", "webcam start", args=args, check=False, timeout=15)
        return dict(r.data or {}) if isinstance(r.data, dict) else {"error": -1}

    async def webcam_stop(self) -> dict[str, Any]:
        r = await self.get_raw("gopro/webcam/stop", "webcam stop", check=False)
        return dict(r.data or {}) if isinstance(r.data, dict) else {}

    async def webcam_exit(self) -> dict[str, Any]:
        r = await self.get_raw("gopro/webcam/exit", "webcam exit", check=False)
        return dict(r.data or {}) if isinstance(r.data, dict) else {}

    async def webcam_status(self) -> dict[str, Any]:
        r = await self.get_raw("gopro/webcam/status", "webcam status", lane="query", check=False)
        return dict(r.data or {}) if isinstance(r.data, dict) else {}

    async def preview_stream(self, enable: bool, port: int | None = None) -> None:
        from open_gopro.models.constants import Toggle

        await self._run(
            lambda: self.sdk.http_command.set_preview_stream(
                mode=Toggle.ENABLE if enable else Toggle.DISABLE, port=port
            ),
            "preview stream " + ("start" if enable else "stop"),
        )

    # ------------------------------------------------------------------ media
    async def set_turbo(self, enable: bool) -> None:
        from open_gopro.models.constants import Toggle

        await self._run(
            lambda: self.sdk.http_command.set_turbo_mode(mode=Toggle.ENABLE if enable else Toggle.DISABLE),
            "turbo transfer",
        )

    async def media_list(self) -> dict[str, Any]:
        r = await self.get_raw("gopro/media/list", "media list", lane="query", timeout=30)
        return dict(r.data or {})

    async def media_info(self, path: str) -> dict[str, Any]:
        r = await self.get_raw("gopro/media/info", f"media info {path}", args={"path": path}, lane="query")
        return dict(r.data or {})

    async def last_captured(self) -> str | None:
        r = await self.get_raw("gopro/media/last_captured", "last captured media", lane="query", check=False)
        d = r.data if isinstance(r.data, dict) else {}
        if r.ok and d.get("folder") and d.get("file"):
            return f"{d['folder']}/{d['file']}"
        return None

    async def delete_file(self, path: str) -> None:
        await self._run(lambda: self.sdk.http_command.delete_file(path=path), f"delete {path}")

    async def get_asset(self, kind: str, path: str, local: Path) -> Path:
        cmds = {
            "gpmf": self.sdk.http_command.get_gpmf_data,
            "thumbnail": self.sdk.http_command.get_thumbnail,
            "screennail": self.sdk.http_command.get_screennail,
            "telemetry": self.sdk.http_command.get_telemetry,
        }
        if kind not in cmds:
            raise CameraRejected(f"unknown media asset kind '{kind}'")
        local.parent.mkdir(parents=True, exist_ok=True)
        await self._run(
            lambda: cmds[kind](camera_file=path, local_file=local), f"get {kind} {path}", lane="media", timeout=120
        )
        return local

    async def download(
        self,
        path: str,
        local: Path,
        progress: ProgressCb | None = None,
        cancelled: Callable[[], bool] | None = None,
        timeout: float = 3600.0,
    ) -> Path:
        """Stream ``videos/DCIM/<path>`` to ``local`` with progress and cancellation.

        Uses the SDK's requests session and request arguments (COHN auth/cert), but iterates the body
        itself because the SDK download helper has neither progress nor cancellation.
        """
        from open_gopro.domain.communicator_interface import HttpMessage

        local.parent.mkdir(parents=True, exist_ok=True)
        tmp = local.with_suffix(local.suffix + ".part")
        url, req_args, session = sdk_compat.download_request(self.sdk, HttpMessage("videos/DCIM/" + path, None))
        stop = threading.Event()

        def _work() -> None:
            with session.get(url, stream=True, timeout=(10, 60), **req_args) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("Content-Length", 0) or 0)
                done = 0
                with open(tmp, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=1 << 20):
                        if stop.is_set() or (cancelled is not None and cancelled()):
                            raise asyncio.CancelledError
                        f.write(chunk)
                        done += len(chunk)
                        if progress is not None:
                            progress(done, total)
            tmp.replace(local)

        lock = self._lock("media")
        assert lock is not None
        async with lock:
            fut = _in_daemon_thread(_work, "gopro-download")
            try:
                await asyncio.wait_for(asyncio.shield(fut), timeout)
            except (asyncio.CancelledError, TimeoutError) as e:
                stop.set()
                await asyncio.gather(fut, return_exceptions=True)
                tmp.unlink(missing_ok=True)
                if isinstance(e, TimeoutError):
                    raise CommandTimeout(f"download {path} timed out after {timeout:.0f}s") from e
                raise
            except Exception as e:
                tmp.unlink(missing_ok=True)
                raise TransportError(f"download {path} over {self.name} failed: {type(e).__name__}: {e}") from e
        return local
