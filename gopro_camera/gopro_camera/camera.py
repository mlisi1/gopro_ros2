"""Transport-agnostic camera facade (asyncio; runs on the per-camera loop thread).

Responsibilities:
- open the configured transport (+ optional BLE side channel), claim control, read hardware info;
- keep the link alive and track camera state (BLE push when available, HTTP/BLE polling otherwise);
- route each command to a channel that can execute it (HTTP preferred for heavy work, BLE fallback),
  with timeouts, typed errors and a record of which transport executed it;
- guard commands that need an idle camera, and never stop a recording on its own;
- produce recording sync anchors (command sent / camera ack / observed encoding change);
- detect link loss and reconnect with exponential backoff, adopting an ongoing recording.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, TypeVar

from gopro_camera import settings_map as smap
from gopro_camera.errors import (
    CameraBusy,
    CameraRejected,
    CommandTimeout,
    GoProError,
    InvalidSetting,
    NotConnected,
    NotSupported,
    TransportError,
)
from gopro_camera.media import MediaEntry, filter_since, local_path_for, parse_media_list
from gopro_camera.status import SD_OK, SD_UNKNOWN, S, as_flag, as_int, value_label
from gopro_camera.transports import BleChannel, HttpChannel, TransportBundle, TransportConfig, open_transport
from gopro_camera.transports.base import Channel, ensure_private_file

logger = logging.getLogger(__name__)
T = TypeVar("T")

# RecordingEvent.msg constants (kept in sync by test_camera.py)
EV_START, EV_STOP, EV_HILIGHT, EV_PHOTO, EV_ADOPTED, EV_EXTERNAL_START, EV_EXTERNAL_STOP = range(7)


@dataclass
class CameraConfig:
    transport: TransportConfig = field(default_factory=TransportConfig)
    auto_order: list[str] = field(default_factory=lambda: ["usb", "cohn", "ble"])
    keep_alive_period: float = 3.0  # spec best practice: every 3.0 s
    status_poll_rate: float = 1.0
    status_timeout: float = 3.0  # per status query; link loss after link_loss_threshold failures
    use_ble_push: bool = True
    claim_control: bool = True
    sync_datetime_on_connect: bool = True
    reconnect: bool = True
    reconnect_backoff_initial: float = 1.0
    reconnect_backoff_max: float = 30.0
    link_loss_threshold: int = 3
    busy_wait_timeout: float = 5.0
    encoding_confirm_timeout: float = 10.0
    camera_clock_is_local: bool = True


@dataclass
class RecordingEventData:
    event: int
    success: bool = True
    message: str = ""
    transport: str = ""
    command_sent_ns: int = 0
    camera_ack_ns: int = 0
    encoding_changed_ns: int = 0
    encoding_changed_uncertainty: float = 0.0
    filename: str = ""
    encoding_duration: int = -1


class CameraListener(Protocol):
    def on_state(self, camera: GoProCamera) -> None: ...
    def on_recording_event(self, camera: GoProCamera, event: RecordingEventData) -> None: ...
    def on_link(self, camera: GoProCamera, connected: bool, reason: str) -> None: ...


class _NullListener:
    def on_state(self, camera: GoProCamera) -> None:
        pass

    def on_recording_event(self, camera: GoProCamera, event: RecordingEventData) -> None:
        pass

    def on_link(self, camera: GoProCamera, connected: bool, reason: str) -> None:
        pass


def _preset_title(preset: dict[str, Any]) -> str:
    if preset.get("customName"):
        return str(preset["customName"])
    title_id = preset.get("titleId")
    try:
        from open_gopro.models import proto

        name = proto.EnumPresetTitle.Name(int(title_id))  # type: ignore[arg-type]
        title = name.removeprefix("PRESET_TITLE_").replace("_", " ").title()
    except Exception:
        title = f"Preset {preset.get('id')}"
    if preset.get("titleNumber"):
        title += f" {preset['titleNumber']}"
    return title


def normalize_presets(raw: dict[str, Any], active_id: int) -> list[dict[str, Any]]:
    """HTTP JSON and BLE protobuf (MessageToDict) preset status -> one plain structure."""
    groups = []
    for g in raw.get("presetGroupArray", []) or []:
        presets = []
        for p in g.get("presetArray", []) or []:
            pid = as_int(p.get("id"))
            presets.append(
                {
                    "id": pid,
                    "title": _preset_title(p),
                    "group_id": as_int(g.get("id")),
                    "is_active": pid == active_id,
                    "is_modified": bool(p.get("isModified", False)),
                    "user_defined": bool(p.get("userDefined", False)),
                    "is_fixed": bool(p.get("isFixed", False)),
                    "settings": [(as_int(s.get("id")), as_int(s.get("value"))) for s in (p.get("settingArray") or [])],
                }
            )
        gid = as_int(g.get("id"))
        try:
            from open_gopro.models import proto

            gname = proto.EnumPresetGroup.Name(gid).removeprefix("PRESET_GROUP_ID_").lower()
        except Exception:
            gname = str(gid)
        groups.append(
            {"id": gid, "name": gname, "can_add_preset": bool(g.get("canAddPreset", False)), "presets": presets}
        )
    return groups


def preset_group_id(group: str) -> int:
    """'video' | 'photo' | 'timelapse' | numeric -> spec EnumPresetGroup value."""
    if group.strip().lstrip("-").isdigit():
        return int(group)
    from open_gopro.models import proto

    key = "PRESET_GROUP_ID_" + group.strip().upper()
    try:
        return int(proto.EnumPresetGroup.Value(key))
    except ValueError as e:
        names = [n.removeprefix("PRESET_GROUP_ID_").lower() for n in proto.EnumPresetGroup.keys()]
        raise CameraRejected(f"unknown preset group '{group}' (expected one of {names})") from e


class GoProCamera:
    def __init__(
        self,
        config: CameraConfig,
        now_ns: Callable[[], int] | None = None,
        listener: CameraListener | None = None,
    ) -> None:
        self.cfg = config
        self.now_ns = now_ns or time.time_ns
        self.listener: CameraListener = listener or _NullListener()
        self.bundle: TransportBundle | None = None
        self.hardware: dict[str, Any] = {}
        self.statuses: dict[int, Any] = {}
        self.settings: dict[int, Any] = {}
        self.runtime_capabilities: dict[int, list[int]] = {}
        self.status_snapshot: dict[int, Any] = {}
        self.settings_snapshot: dict[int, Any] = {}
        self.status_source = ""
        self.last_update_mono = 0.0
        self.connected = False
        self.sleeping = False
        self.stream_mode = ""  # "" | webcam | preview
        self.livestream_active = False
        self.mission_start_ns = self.now_ns()
        self._tasks: list[asyncio.Task[Any]] = []
        self._reconnect_task: asyncio.Task[None] | None = None
        self._state_cond: asyncio.Condition | None = None
        self._fast_poll_until = 0.0
        self._poll_failures = 0
        self._last_obs_ns = 0
        self._enc_transition: tuple[bool, int, float] | None = None  # (new value, t_ns, uncertainty)
        self._expected_encoding: bool | None = None
        self._last_encoding_duration = -1
        self._ble_push_active = False
        self._connect_lock: asyncio.Lock | None = None
        self.last_command_transport = ""
        self._next_sidechannel_try = 0.0
        self._side_tasks: set[asyncio.Task[Any]] = set()

    # ================================================================== properties
    @property
    def transport_name(self) -> str:
        return self.bundle.name if self.bundle else ""

    @property
    def http(self) -> HttpChannel | None:
        return self.bundle.http if self.bundle else None

    @property
    def ble(self) -> BleChannel | None:
        return self.bundle.ble if self.bundle else None

    @property
    def encoding(self) -> bool:
        return as_flag(self.statuses.get(S.ENCODING))

    @property
    def busy(self) -> bool:
        return as_flag(self.statuses.get(S.BUSY))

    @property
    def model_name(self) -> str:
        return str(self.hardware.get("model_name", ""))

    @property
    def serial(self) -> str:
        return str(self.hardware.get("serial_number", "") or self.cfg.transport.serial or "")

    @property
    def status_age(self) -> float:
        return time.monotonic() - self.last_update_mono if self.last_update_mono else float("inf")

    def context(self) -> str:
        """What the camera is doing, for error messages."""
        return (
            f"transport={self.transport_name or 'none'} model={self.model_name or '?'} "
            f"preset={self.statuses.get(S.PRESET, '?')} group={self.statuses.get(S.PRESET_GROUP, '?')} "
            f"encoding={self.encoding} busy={self.busy} stream={self.stream_mode or 'off'}"
        )

    def features(self) -> list[str]:
        f = ["status", "keep_alive"]
        if self.http or self.ble:
            f += ["record", "photo", "hilight", "presets", "settings", "datetime", "sleep"]
        if self.http:
            f += ["media_list", "media_download", "media_delete", "media_assets", "preview_stream"]
            model = smap.spec_model_name(self.model_name) or ""
            # Spec: webcam over Wi-Fi is not supported on HERO9/10/11 (incl. Mini).
            old = any(m in model for m in ("HERO9", "HERO10", "HERO11"))
            if self.transport_name in ("usb", "cohn") or not old:
                f.append("webcam_stream")
        if self.ble:
            f += ["power_down", "wifi_ap", "cohn_provision", "livestream", "ble_push_status"]
        return f

    # ================================================================== connection
    def _ensure_sync(self) -> None:
        if self._state_cond is None:
            self._state_cond = asyncio.Condition()
            self._connect_lock = asyncio.Lock()

    async def connect(self) -> None:
        """Open transport, claim control, read hardware info and state, start background tasks."""
        self._ensure_sync()
        assert self._connect_lock is not None
        async with self._connect_lock:
            await self._stop_tasks()
            if self.bundle is not None:
                await self.bundle.close()
                self.bundle = None
            self.bundle = await asyncio.wait_for(
                open_transport(self.cfg.transport, self.cfg.auto_order), self.cfg.transport.connect_timeout * 3
            )
            try:
                await self._post_connect()
            except Exception:
                await self.bundle.close()
                self.bundle = None
                raise
            self.connected = True
            self.sleeping = False
        self.listener.on_link(self, True, f"connected via {self.transport_name}")

    async def _post_connect(self) -> None:
        if self.cfg.claim_control:
            try:
                await self._exec("claim control", lambda ch: ch.set_camera_control_external(), prefer="http")
            except GoProError as e:  # not supported on every model (e.g. HERO9): keep going
                logger.warning("could not claim external control: %s", e)
        if self.ble is not None and self.http is not None:
            try:
                await self.ble.set_third_party_client()
            except GoProError:
                pass
        try:
            self.hardware = await self._exec_value("get hardware info", lambda ch: ch.get_hardware_info())
        except GoProError as e:
            logger.warning("could not read hardware info: %s", e)
        self.hardware["open_gopro_version"] = self.bundle.api_version if self.bundle else ""
        await self.refresh_state()
        if self.encoding:
            # Driver (re)started while the camera records: adopt, never stop.
            now = self.now_ns()
            self._last_encoding_duration = as_int(self.statuses.get(S.VIDEO_ENCODING_DURATION))
            self._emit(
                RecordingEventData(
                    event=EV_ADOPTED,
                    message=f"camera already recording for {self._last_encoding_duration}s; adopted",
                    transport=self.transport_name,
                    command_sent_ns=now,
                    encoding_duration=self._last_encoding_duration,
                )
            )
        if self.cfg.sync_datetime_on_connect:
            try:
                msg = await self._sync_datetime()
                logger.info("date/time sync: %s", msg)
            except GoProError as e:
                logger.warning("date/time sync failed: %s", e)
        if self.ble is not None:
            try:
                self.runtime_capabilities = await self.ble.get_capabilities()
            except GoProError as e:
                logger.info("capabilities query over BLE not available: %s", e)
        self._ble_push_active = False
        if self.ble is not None and self.cfg.use_ble_push:
            try:
                await self.ble.subscribe_statuses(self._on_push)
                self._ble_push_active = True
            except Exception as e:
                logger.warning("BLE status push registration failed, polling instead: %s", e)
        self._poll_failures = 0
        self._tasks = [
            asyncio.create_task(self._poll_loop(), name="gopro-status-poll"),
            asyncio.create_task(self._keep_alive_loop(), name="gopro-keep-alive"),
        ]

    async def disconnect(self, release_control: bool = True, sleep: bool = False) -> None:
        if self._reconnect_task is not None:
            self._reconnect_task.cancel()
            await asyncio.gather(self._reconnect_task, return_exceptions=True)
            self._reconnect_task = None
        await self._stop_tasks()
        if self.bundle is not None:
            # Best effort, bounded: never let an unreachable camera hold up shutdown.
            reachable = self.connected
            if self.stream_mode and reachable:
                try:
                    await asyncio.wait_for(self.stop_stream(), 5.0)
                except (GoProError, TimeoutError) as e:
                    logger.warning("stopping stream on disconnect failed: %s", e)
            self.stream_mode = ""
            if reachable and sleep and not self.encoding:
                try:
                    await asyncio.wait_for(self._exec("sleep", lambda ch: ch.sleep()), 3.0)
                except (GoProError, TimeoutError) as e:
                    logger.warning("sleep on disconnect failed: %s", e)
            elif reachable and release_control and self.cfg.claim_control:
                try:
                    await asyncio.wait_for(
                        self._exec("release control", lambda ch: ch.set_camera_control_idle(), prefer="http"), 3.0
                    )
                except (GoProError, TimeoutError) as e:
                    logger.info("release control failed: %s", e)
            await self.bundle.close()
            self.bundle = None
        was_connected = self.connected
        self.connected = False
        if was_connected:
            self.listener.on_link(self, False, "disconnected by driver")

    async def _stop_tasks(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _on_link_lost(self, reason: str) -> None:
        if not self.connected:
            return
        self.connected = False
        logger.error("link lost (%s): %s", self.context(), reason)
        self.listener.on_link(self, False, reason)
        if self.cfg.reconnect and not self.sleeping and (self._reconnect_task is None or self._reconnect_task.done()):
            self._reconnect_task = asyncio.create_task(self._reconnect_loop(), name="gopro-reconnect")

    async def _reconnect_loop(self) -> None:
        delay = self.cfg.reconnect_backoff_initial
        attempt = 0
        # Our own poll/keep-alive tasks must stop, but not this task.
        await self._stop_tasks()
        while True:
            attempt += 1
            try:
                await self.connect()
                logger.info("reconnected via %s after %d attempt(s)", self.transport_name, attempt)
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("reconnect attempt %d failed: %s; retrying in %.0fs", attempt, e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.cfg.reconnect_backoff_max)

    async def ensure_connected(self) -> None:
        if self.connected and self.bundle is not None:
            return
        if self.sleeping:
            # A command after sleep: reconnecting (BLE connect / USB re-enumeration) wakes the camera.
            await self.connect()
            return
        raise NotConnected("camera not connected", self.context())

    # ================================================================== routing
    def _channels(self, prefer: str) -> list[Channel]:
        chans: list[Channel] = []
        if self.bundle is None:
            return chans
        order = [self.bundle.http, self.bundle.ble] if prefer == "http" else [self.bundle.ble, self.bundle.http]
        return [c for c in order if c is not None and c.is_connected]

    @staticmethod
    def _label(ch: Channel) -> str:
        return "ble" if ch.kind == "ble" else ch.name

    async def _exec(
        self,
        what: str,
        fn: Callable[[Any], Awaitable[T]],
        *,
        prefer: str = "http",
        only: str | None = None,
        fallback: bool = True,
    ) -> tuple[T, str]:
        """Run ``fn(channel)`` on the preferred channel; fall back on transport errors only."""
        chans = self._channels(prefer)
        if only is not None:
            chans = [c for c in chans if c.kind == only]
        if not chans:
            need = f" (needs {only.upper()})" if only else ""
            raise NotConnected(f"{what}: no connected channel{need}", self.context())
        last_err: GoProError | None = None
        for ch in chans:
            try:
                result = await fn(ch)
                self.last_command_transport = self._label(ch)
                return result, self._label(ch)
            except (AttributeError, NotSupported) as e:
                last_err = NotSupported(f"{what} not available over {self._label(ch)}: {e}", self.context())
            except TransportError as e:
                last_err = e
                e.context = e.context or self.context()
                if not fallback:
                    break
                logger.warning("%s failed over %s (%s); trying next channel", what, self._label(ch), e)
            except GoProError as e:
                e.context = e.context or self.context()
                raise
        assert last_err is not None
        raise last_err

    async def _exec_value(self, what: str, fn: Callable[[Any], Awaitable[T]], **kw: Any) -> T:
        return (await self._exec(what, fn, **kw))[0]

    # ================================================================== state tracking
    async def refresh_state(self, timeout: float | None = None) -> None:
        snap = await self._exec_value("get state", lambda ch: ch.get_state(timeout=timeout))
        await self._apply_update(snap.statuses, snap.settings, "http_poll" if self.http else "ble_poll")

    async def _on_push(self, changed: dict[int, Any], source: str) -> None:
        await self._apply_update(changed, None, source)

    async def _apply_update(self, statuses: dict[int, Any], settings: dict[int, Any] | None, source: str) -> None:
        self._ensure_sync()
        assert self._state_cond is not None
        t = self.now_ns()
        prev_enc = self.encoding if S.ENCODING in self.statuses else None
        prev_preset = self.statuses.get(S.PRESET)
        if prev_enc:
            self._last_encoding_duration = as_int(self.statuses.get(S.VIDEO_ENCODING_DURATION))
        self.statuses.update(statuses)
        if settings is not None:
            self.settings.update(settings)
        self.status_source = source
        self.last_update_mono = time.monotonic()
        # Immutable copies for other threads (ROS timers read these, never the live dicts).
        self.status_snapshot = dict(self.statuses)
        self.settings_snapshot = dict(self.settings)
        if S.ENCODING in statuses and prev_enc is not None and self.encoding != prev_enc:
            new = self.encoding
            uncertainty = (t - self._last_obs_ns) / 1e9 if self._last_obs_ns else 0.0
            if source == "ble_push":
                uncertainty = 0.0  # pushed on change; residual is BLE latency (tens of ms)
            self._enc_transition = (new, t, uncertainty)
            if self._expected_encoding is not new:
                ev = EV_EXTERNAL_START if new else EV_EXTERNAL_STOP
                data = RecordingEventData(
                    event=ev,
                    message="encoding changed without a driver command (camera button, remote, SD full, overheat...)",
                    transport=self.transport_name,
                    command_sent_ns=t,
                    encoding_changed_ns=t,
                    encoding_changed_uncertainty=uncertainty,
                    encoding_duration=-1 if new else self._last_encoding_duration,
                )
                if not new:
                    self._spawn(self._emit_with_filename(data))
                else:
                    self._emit(data)
        self._last_obs_ns = t
        if prev_preset is not None and self.statuses.get(S.PRESET) != prev_preset and self.ble is not None:
            self._spawn(self._refresh_capabilities())
        async with self._state_cond:
            self._state_cond.notify_all()
        self.listener.on_state(self)

    async def _refresh_capabilities(self) -> None:
        try:
            if self.ble is not None:
                self.runtime_capabilities = await self.ble.get_capabilities()
        except GoProError:
            pass

    async def _poll_loop(self) -> None:
        while True:
            fast = time.monotonic() < self._fast_poll_until
            base = 1.0 / max(self.cfg.status_poll_rate, 0.01)
            if self._ble_push_active:
                base = max(base, 5.0)  # statuses arrive by push; poll only for settings / liveness
            period = 0.2 if fast else base
            try:
                if self.ble is not None and not self.ble.is_connected:
                    if self.http is None:
                        raise NotConnected("BLE disconnected")
                    if self._ble_push_active:
                        logger.warning("BLE side channel lost; falling back to HTTP polling")
                        self._ble_push_active = False
                    await self._maybe_reopen_sidechannel()
                await asyncio.wait_for(self.refresh_state(self.cfg.status_timeout), self.cfg.status_timeout + 1.0)
                self._poll_failures = 0
            except asyncio.CancelledError:
                raise
            except (GoProError, TimeoutError) as e:
                self._poll_failures += 1
                logger.debug("status poll failed (%d): %s", self._poll_failures, e)
                if self._poll_failures >= self.cfg.link_loss_threshold:
                    await self._on_link_lost(f"{self._poll_failures} consecutive status failures: {e}")
                    return
            await asyncio.sleep(period)

    async def _maybe_reopen_sidechannel(self) -> None:
        """Retry the separate BLE side channel (USB transport) at most every 30 s."""
        if self.bundle is None or self.bundle.name != "usb" or time.monotonic() < self._next_sidechannel_try:
            return
        self._next_sidechannel_try = time.monotonic() + 30.0
        from gopro_camera.transports import open_ble_channel

        try:
            ble, sdk = await open_ble_channel(self.cfg.transport, "ble")
        except GoProError as e:
            logger.info("BLE side channel still unavailable: %s", e)
            return
        old = self.bundle.ble
        if old is not None:
            await old.close()
            try:
                await old.sdk.close()
            except Exception:
                pass
            self.bundle.sdk_objects = [o for o in self.bundle.sdk_objects if o is not old.sdk]
        self.bundle.ble = ble
        self.bundle.sdk_objects.append(sdk)
        if self.cfg.use_ble_push:
            try:
                await ble.subscribe_statuses(self._on_push)
                self._ble_push_active = True
            except Exception as e:
                logger.warning("BLE push registration failed: %s", e)
        logger.info("BLE side channel re-established")

    async def _keep_alive_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.keep_alive_period)
            for ch in [self.http, self.ble]:
                if ch is None or not ch.is_connected:
                    continue
                try:
                    await ch.keep_alive()
                except GoProError as e:
                    logger.debug("keep alive over %s failed: %s", self._label(ch), e)

    async def wait_for_encoding(self, target: bool, since_ns: int, timeout: float) -> tuple[int, float] | None:
        """Wait until the camera reports encoding == target (transition after ``since_ns``)."""
        self._ensure_sync()
        assert self._state_cond is not None
        self._fast_poll_until = time.monotonic() + timeout
        deadline = time.monotonic() + timeout

        def _done() -> bool:
            tr = self._enc_transition
            return tr is not None and tr[0] is target and tr[1] >= since_ns

        async with self._state_cond:
            while not _done():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                try:
                    await asyncio.wait_for(self._state_cond.wait(), remaining)
                except TimeoutError:
                    return None
        assert self._enc_transition is not None
        return self._enc_transition[1], self._enc_transition[2]

    async def _require_idle(self, what: str, allow_encoding: bool = False) -> None:
        await self.ensure_connected()
        if self.encoding and not allow_encoding:
            hs = " (HindSight active)" if as_flag(self.statuses.get(S.HINDSIGHT)) else ""
            raise CameraBusy(f"{what}: not allowed while the camera is encoding{hs}", self.context())
        if self.busy:
            self._ensure_sync()
            assert self._state_cond is not None
            self._fast_poll_until = time.monotonic() + self.cfg.busy_wait_timeout
            deadline = time.monotonic() + self.cfg.busy_wait_timeout
            async with self._state_cond:
                while self.busy:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise CameraBusy(
                            f"{what}: camera busy for more than {self.cfg.busy_wait_timeout:.0f}s", self.context()
                        )
                    try:
                        await asyncio.wait_for(self._state_cond.wait(), remaining)
                    except TimeoutError:
                        continue

    def _spawn(self, coro: Any) -> None:
        """Fire-and-forget task that is kept referenced until it finishes."""
        task = asyncio.create_task(coro)
        self._side_tasks.add(task)
        task.add_done_callback(self._side_tasks.discard)

    def _emit(self, ev: RecordingEventData) -> None:
        try:
            self.listener.on_recording_event(self, ev)
        except Exception:
            logger.exception("recording event listener failed")

    async def _emit_with_filename(self, ev: RecordingEventData) -> None:
        ev.filename = await self._last_captured_retry()
        self._emit(ev)

    async def _last_captured_retry(self, attempts: int = 4) -> str:
        for _ in range(attempts):
            try:
                path = await self._exec_value("last captured media", lambda ch: ch.last_captured())
                if path:
                    return path
            except GoProError as e:
                logger.debug("last captured media failed: %s", e)
            await asyncio.sleep(0.5)
        return ""

    # ================================================================== capture
    def _preset_group_name(self) -> str:
        gid = as_int(self.statuses.get(S.PRESET_GROUP))
        try:
            from open_gopro.models import proto

            return proto.EnumPresetGroup.Name(gid).removeprefix("PRESET_GROUP_ID_").lower()
        except Exception:
            return ""

    def _check_sd(self, what: str) -> None:
        sd = as_int(self.statuses.get(S.PRIMARY_STORAGE), SD_UNKNOWN)
        if sd not in (SD_OK, SD_UNKNOWN):
            raise CameraRejected(f"{what}: {value_label(S.PRIMARY_STORAGE, sd)}", self.context())

    async def start_recording(self) -> RecordingEventData:
        await self.ensure_connected()
        if self.encoding:
            raise CameraBusy("start recording: camera is already encoding", self.context())
        if self.stream_mode == "webcam":
            raise CameraRejected("start recording: not possible in webcam mode; stop the stream first", self.context())
        if self._preset_group_name() == "photo":
            raise CameraRejected("start recording: current preset is a photo preset; use ~/take_photo", self.context())
        self._check_sd("start recording")
        await self._require_idle("start recording")
        ev = RecordingEventData(event=EV_START)
        self._expected_encoding = True
        try:
            ev.command_sent_ns = self.now_ns()
            _, ev.transport = await self._exec("start recording", lambda ch: ch.set_shutter(True))
            ev.camera_ack_ns = self.now_ns()
            obs = await self.wait_for_encoding(True, ev.command_sent_ns, self.cfg.encoding_confirm_timeout)
        finally:
            self._expected_encoding = None
        if obs is None:
            ev.success = False
            ev.message = f"camera acknowledged but encoding not observed within {self.cfg.encoding_confirm_timeout}s"
        else:
            ev.encoding_changed_ns, ev.encoding_changed_uncertainty = obs
            ev.message = "recording started"
        self._emit(ev)
        return ev

    async def stop_recording(self) -> RecordingEventData:
        await self.ensure_connected()
        if not self.encoding:
            raise CameraRejected("stop recording: camera is not encoding", self.context())
        ev = RecordingEventData(event=EV_STOP)
        self._expected_encoding = False
        try:
            ev.command_sent_ns = self.now_ns()
            _, ev.transport = await self._exec("stop recording", lambda ch: ch.set_shutter(False))
            ev.camera_ack_ns = self.now_ns()
            obs = await self.wait_for_encoding(False, ev.command_sent_ns, self.cfg.encoding_confirm_timeout)
        finally:
            self._expected_encoding = None
        ev.encoding_duration = self._last_encoding_duration
        if obs is None:
            ev.success = False
            ev.message = "camera acknowledged but end of encoding not observed"
        else:
            ev.encoding_changed_ns, ev.encoding_changed_uncertainty = obs
            ev.message = "recording stopped"
            ev.filename = await self._last_captured_retry()
        self._emit(ev)
        return ev

    async def take_photo(self) -> RecordingEventData:
        await self.ensure_connected()
        group = self._preset_group_name()
        if group and group != "photo":
            raise CameraRejected(
                f"take photo: current preset group is '{group}'; load a photo preset first", self.context()
            )
        self._check_sd("take photo")
        await self._require_idle("take photo")
        ev = RecordingEventData(event=EV_PHOTO)
        self._expected_encoding = True
        try:
            ev.command_sent_ns = self.now_ns()
            _, ev.transport = await self._exec("take photo", lambda ch: ch.set_shutter(True))
            ev.camera_ack_ns = self.now_ns()
            obs = await self.wait_for_encoding(True, ev.command_sent_ns, self.cfg.encoding_confirm_timeout)
            self._expected_encoding = False
            if obs is not None:
                ev.encoding_changed_ns, ev.encoding_changed_uncertainty = obs
                await self.wait_for_encoding(False, ev.encoding_changed_ns, self.cfg.encoding_confirm_timeout)
        finally:
            self._expected_encoding = None
        ev.message = "photo captured" if obs is not None else "camera acknowledged; capture not observed"
        ev.success = obs is not None
        ev.filename = await self._last_captured_retry()
        self._emit(ev)
        return ev

    async def hilight(self) -> RecordingEventData:
        await self.ensure_connected()
        if not self.encoding:
            raise CameraRejected("hilight: only possible while recording", self.context())
        ev = RecordingEventData(event=EV_HILIGHT)
        ev.command_sent_ns = self.now_ns()
        _, ev.transport = await self._exec("hilight", lambda ch: ch.hilight_moment())
        ev.camera_ack_ns = self.now_ns()
        ev.encoding_duration = as_int(self.statuses.get(S.VIDEO_ENCODING_DURATION))
        ev.message = "hilight tagged"
        self._emit(ev)
        return ev

    # ================================================================== presets & settings
    async def get_presets(self, include_hidden: bool = False) -> list[dict[str, Any]]:
        await self.ensure_connected()
        raw = await self._exec_value("get presets", lambda ch: ch.get_presets(include_hidden))
        return normalize_presets(raw, as_int(self.statuses.get(S.PRESET)))

    async def load_preset(self, preset_id: int = -1, group: str = "") -> int:
        await self._require_idle("load preset")
        if preset_id >= 0:
            await self._exec(f"load preset {preset_id}", lambda ch: ch.load_preset(preset_id))
        elif group:
            gid = preset_group_id(group)
            await self._exec(f"load preset group {group}", lambda ch: ch.load_preset_group(gid))
        else:
            raise CameraRejected("load preset: give preset_id >= 0 or a group")
        await asyncio.sleep(0.3)
        await self.refresh_state()
        await self._refresh_capabilities()
        return as_int(self.statuses.get(S.PRESET))

    def validate_setting(self, setting_id: int, option: int) -> smap.SettingDef:
        sdef = smap.lookup(setting_id)
        smap.validate_option(sdef, option, self.model_name or None, self.runtime_capabilities.get(setting_id))
        return sdef

    def check_settings_allowed(self) -> None:
        if not self.connected:
            raise NotConnected("settings: camera not connected", self.context())
        if self.encoding:
            hs = " (HindSight active)" if as_flag(self.statuses.get(S.HINDSIGHT)) else ""
            raise CameraBusy(f"settings cannot be changed while the camera is encoding{hs}", self.context())

    async def set_setting(self, setting_id: int, option: int, validate: bool = True) -> str:
        self.check_settings_allowed()
        if validate:
            self.validate_setting(setting_id, option)
        await self._require_idle(f"set setting {setting_id}")

        async def _set(ch: Any) -> None:
            if ch.kind == "ble" and not ch.supports_setting(setting_id):
                raise NotSupported(f"setting {setting_id} unknown to SDK BLE layer")
            await ch.set_setting(setting_id, option)

        try:
            _, used = await self._exec(f"set setting {setting_id}={option}", _set)
        except InvalidSetting as e:
            if e.supported_options:
                self.runtime_capabilities[setting_id] = e.supported_options
            raise
        self.settings[setting_id] = option
        return used

    async def set_settings(
        self, items: list[tuple[int, int]], rollback: bool = True
    ) -> tuple[list[tuple[int, int]], list[tuple[int, int, str]], bool]:
        """Atomic-ish batch: validate all, apply in order, roll back applied ones on failure."""
        self.check_settings_allowed()
        failed: list[tuple[int, int, str]] = []
        for sid, opt in items:
            try:
                self.validate_setting(sid, opt)
            except InvalidSetting as e:
                failed.append((sid, opt, str(e)))
        if failed:
            return [], failed, False
        previous = {sid: self.settings.get(sid) for sid, _ in items}
        applied: list[tuple[int, int]] = []
        for sid, opt in items:
            try:
                await self.set_setting(sid, opt, validate=False)
                applied.append((sid, opt))
            except GoProError as e:
                failed.append((sid, opt, str(e)))
                break
        rolled_back = False
        if failed and rollback and applied:
            for sid, _ in reversed(applied):
                prev = previous.get(sid)
                if prev is None:
                    continue
                try:
                    await self.set_setting(sid, int(prev), validate=False)
                except GoProError as e:
                    logger.error("rollback of setting %s to %s failed: %s", sid, prev, e)
            rolled_back = True
        return applied, failed, rolled_back

    async def get_settings(self, refresh: bool = True) -> dict[int, Any]:
        if refresh:
            await self.ensure_connected()
            await self.refresh_state()
        return dict(self.settings)

    def capabilities(self) -> list[dict[str, Any]]:
        model = self.model_name or None
        out = []
        for sdef in smap.SETTING_DEFS.values():
            if not smap.is_supported_by_model(sdef, model):
                continue
            opts = smap.options_for_model(sdef, model)
            out.append(
                {
                    "id": sdef.id,
                    "name": sdef.name,
                    "title": sdef.title,
                    "options": list(opts),
                    "labels": list(opts.values()),
                    "runtime_options": list(self.runtime_capabilities.get(sdef.id, [])),
                    "range": sdef.value_range,
                    "current": as_int(self.settings.get(sdef.id)),
                    "ble": bool(self.ble and self.ble.supports_setting(sdef.id)),
                }
            )
        return out

    # ================================================================== time / power
    async def sync_datetime(self) -> str:
        await self.ensure_connected()
        return await self._sync_datetime()

    async def _sync_datetime(self) -> str:
        if self.encoding:
            return "skipped: camera is encoding"
        from open_gopro.util import get_current_dst_aware_time

        dt, tz_offset, is_dst = get_current_dst_aware_time()
        _, used = await self._exec("set date/time", lambda ch: ch.set_date_time(dt, tz_offset, is_dst))
        msg = f"set to {dt.replace(microsecond=0).isoformat()} (utc offset {tz_offset} min, dst={is_dst}) via {used}"
        try:
            cam = await self._exec_value("get date/time", lambda ch: ch.get_date_time())
            host = datetime.datetime.now()
            offset = (cam - host.replace(microsecond=0)).total_seconds()
            msg += f"; camera-host offset {offset:+.0f}s (1s resolution)"
        except GoProError:
            pass
        return msg

    async def sleep(self) -> None:
        await self.ensure_connected()
        if self.encoding:
            raise CameraBusy("sleep refused: camera is encoding (it would stop the recording)", self.context())
        await self._exec("sleep", lambda ch: ch.sleep())
        self.sleeping = True
        await self._stop_tasks()
        self.connected = False
        self.listener.on_link(self, False, "camera put to sleep")

    async def power_down(self) -> None:
        await self.ensure_connected()
        if self.encoding:
            raise CameraBusy("power down refused: camera is encoding", self.context())
        await self._exec("power down", lambda ch: ch.power_down(), only="ble")
        self.sleeping = True
        await self._stop_tasks()
        self.connected = False
        self.listener.on_link(self, False, "camera powered down")

    # ================================================================== streams
    async def start_stream(
        self, mode: str, resolution: int | None, fov: int | None, port: int | None, protocol: str | None
    ) -> str:
        await self.ensure_connected()
        if self.http is None:
            raise NotSupported("stream: needs an HTTP transport (usb, wifi_ap or cohn)", self.context())
        if self.stream_mode:
            return f"{self.stream_mode} stream already active"
        if self.encoding:
            # The SDK's own stream helpers stop the shutter first; we refuse instead of killing a recording.
            raise CameraBusy("stream: refusing to start while recording (it would stop the recording)", self.context())
        if mode == "webcam":
            if "webcam_stream" not in self.features():
                raise NotSupported(f"webcam stream not supported on {self.model_name} over {self.transport_name}")
            status = await self.http.webcam_status()
            if as_int(status.get("status"), 0) in (2, 3):  # spec: 2 high / 3 low power preview
                await self.http.webcam_stop()
            r = await self.http.webcam_start(resolution, fov, port, protocol)
            if as_int(r.get("error"), 0) != 0:
                raise CameraRejected(f"webcam start failed: {r}", self.context())
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                st = await self.http.webcam_status()
                if as_int(st.get("status"), 0) in (2, 3):
                    break
                await asyncio.sleep(0.5)
            else:
                raise CommandTimeout("webcam did not reach preview state within 15s", self.context())
        elif mode == "preview":
            await self.http.preview_stream(False)
            await self.http.preview_stream(True, port)
        else:
            raise CameraRejected(f"unknown stream mode '{mode}' (webcam|preview)")
        self.stream_mode = mode
        return f"{mode} stream started"

    async def stop_stream(self) -> str:
        if not self.stream_mode or self.http is None:
            self.stream_mode = ""
            return "no stream active"
        mode = self.stream_mode
        try:
            if mode == "webcam":
                await self.http.webcam_stop()
                await self.http.webcam_exit()
            else:
                await self.http.preview_stream(False)
        finally:
            self.stream_mode = ""
        return f"{mode} stream stopped"

    # ================================================================== livestream (BLE)
    async def start_livestream(
        self,
        url: str,
        minimum_bitrate: int,
        maximum_bitrate: int,
        starting_bitrate: int,
        encode: bool,
        window_size: int | None,
        lens: int | None,
        timeout: float = 45.0,
    ) -> str:
        await self.ensure_connected()
        if self.ble is None:
            raise NotSupported("livestream: requires BLE (enable ble_sidechannel)", self.context())
        if self.encoding:
            raise CameraBusy("livestream: camera is encoding", self.context())
        if not url:
            raise CameraRejected("livestream: empty RTMP URL")
        from open_gopro.models import proto

        await self.ble.set_livestream_mode(
            url=url,
            minimum_bitrate=minimum_bitrate,
            maximum_bitrate=maximum_bitrate,
            starting_bitrate=starting_bitrate,
            encode=encode,
            window_size=window_size,
            lens=lens,
        )
        ready = int(proto.EnumLiveStreamStatus.LIVE_STREAM_STATE_READY)
        streaming = int(proto.EnumLiveStreamStatus.LIVE_STREAM_STATE_STREAMING)
        await self._wait_livestream({ready, streaming}, timeout, "ready (is the camera connected to a network?)")
        self._expected_encoding = True
        try:
            await self.ble.set_shutter(True)
            await self._wait_livestream({streaming}, timeout, "streaming")
        finally:
            self._expected_encoding = None
        self.livestream_active = True
        return f"livestreaming to {url}"

    async def _wait_livestream(self, wanted: set[int], timeout: float, what: str) -> None:
        assert self.ble is not None
        deadline = time.monotonic() + timeout
        last = -1
        while time.monotonic() < deadline:
            last = await self.ble.livestream_status()
            if last in wanted:
                return
            await asyncio.sleep(1.0)
        raise CommandTimeout(f"livestream not {what} within {timeout:.0f}s (last state {last})", self.context())

    async def stop_livestream(self) -> str:
        await self.ensure_connected()
        if self.ble is None:
            raise NotSupported("livestream: requires BLE", self.context())
        self._expected_encoding = False
        try:
            await self.ble.set_shutter(False)
        finally:
            self._expected_encoding = None
        self.livestream_active = False
        return "livestream stopped"

    # ================================================================== networking (BLE)
    async def set_wifi_ap(self, enable: bool) -> None:
        await self.ensure_connected()
        if self.ble is None:
            raise NotSupported("wifi AP control requires BLE (enable ble_sidechannel)", self.context())
        if not enable and self.transport_name == "wifi_ap":
            raise CameraRejected("refusing to disable the AP that the active wifi_ap transport uses")
        await self.ble.enable_wifi_ap(enable)

    async def provision_cohn(
        self, ssid: str, password: str, force: bool, stage: Callable[[str], None] | None = None
    ) -> dict[str, Any]:
        await self.ensure_connected()
        if self.ble is None:
            raise NotSupported("COHN provisioning requires BLE (enable ble_sidechannel)", self.context())
        say = stage or (lambda _s: None)
        if ssid:
            say(f"connecting camera to '{ssid}'")
            await self.ble.connect_camera_to_ap(ssid, password)
        say("provisioning COHN certificate and credentials")
        await self.ble.configure_cohn(force)
        ensure_private_file(self.cfg.transport.cohn_credentials_path)
        say("reading COHN status")
        return await self.cohn_status()

    async def cohn_status(self) -> dict[str, Any]:
        out: dict[str, Any] = {"supported": False, "credentials_stored": False}
        sdk = self.ble.sdk if self.ble is not None else (self.http.sdk if self.http is not None else None)
        cohn = getattr(sdk, "cohn", None)
        if cohn is not None:
            try:
                creds = cohn.credentials
                out["credentials_stored"] = bool(creds and creds.is_complete)
                if creds:
                    out["ip_address"] = creds.ip_address
            except Exception:
                pass
        if self.ble is not None:
            from open_gopro.models import proto

            st = await self.ble.cohn_status()
            out.update(
                supported=True,
                provisioned=int(st.status) == int(proto.EnumCOHNStatus.COHN_PROVISIONED),
                connected=int(st.state) == int(proto.EnumCOHNNetworkState.COHN_STATE_NetworkConnected),
                status=proto.EnumCOHNStatus.Name(st.status),
                state=proto.EnumCOHNNetworkState.Name(st.state),
                ssid=getattr(st, "ssid", ""),
                ip_address=getattr(st, "ipaddress", "") or out.get("ip_address", ""),
                mac_address=getattr(st, "macaddress", ""),
                enabled=bool(getattr(st, "enabled", False)),
            )
        elif self.transport_name == "cohn":
            out.update(supported=True, provisioned=out["credentials_stored"], connected=self.connected)
        return out

    # ================================================================== media (HTTP)
    def _need_http(self, what: str) -> HttpChannel:
        if self.http is None or not self.http.is_connected:
            raise NotSupported(f"{what}: needs an HTTP transport (usb, wifi_ap or cohn)", self.context())
        return self.http

    def mission_start_camera_epoch(self) -> int:
        from gopro_camera.media import to_camera_epoch

        return to_camera_epoch(self.mission_start_ns / 1e9, self.cfg.camera_clock_is_local)

    async def list_media(self, since_camera_epoch: int | None = None) -> list[MediaEntry]:
        await self.ensure_connected()
        http = self._need_http("list media")
        return filter_since(parse_media_list(await http.media_list()), since_camera_epoch)

    async def delete_media(self, paths: list[str], confirm: bool) -> tuple[list[str], list[str]]:
        if not confirm:
            raise CameraRejected("delete media: set confirm=true to delete files")
        await self.ensure_connected()
        http = self._need_http("delete media")
        if self.encoding:
            raise CameraBusy("delete media: camera is encoding", self.context())
        deleted, failed = [], []
        for p in paths:
            try:
                await http.delete_file(p)
                deleted.append(p)
            except GoProError as e:
                logger.error("delete %s failed: %s", p, e)
                failed.append(p)
        return deleted, failed

    async def get_media_asset(self, kind: str, path: str, local: Path) -> Path | dict[str, Any]:
        await self.ensure_connected()
        http = self._need_http(f"get {kind}")
        if kind == "info":
            return await http.media_info(path)
        return await http.get_asset(kind, path, local)

    async def download_media(
        self,
        paths: list[str],
        dest: Path,
        *,
        skip_existing: bool = True,
        turbo: bool = True,
        progress: Callable[[int, int, str, int, int], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> tuple[list[str], list[str]]:
        await self.ensure_connected()
        http = self._need_http("download media")
        sizes: dict[str, int] = {}
        try:
            for e in parse_media_list(await http.media_list()):
                sizes[e.path] = e.size_bytes
        except GoProError:
            pass
        use_turbo = turbo and not self.encoding
        if use_turbo:
            try:
                await http.set_turbo(True)
            except GoProError as e:
                logger.info("turbo transfer not available: %s", e)
                use_turbo = False
        downloaded: list[str] = []
        failed: list[str] = []
        try:
            for i, p in enumerate(paths):
                if cancelled is not None and cancelled():
                    break
                local = local_path_for(dest, p)
                if skip_existing and local.exists() and sizes.get(p) and local.stat().st_size == sizes[p]:
                    downloaded.append(str(local))
                    continue

                def _prog(done: int, total: int, _i: int = i, _p: str = p) -> None:
                    if progress is not None:
                        progress(_i, len(paths), _p, done, total or sizes.get(_p, 0))

                try:
                    await http.download(p, local, _prog, cancelled)
                    downloaded.append(str(local))
                except asyncio.CancelledError:
                    break
                except GoProError as e:
                    logger.error("download %s failed: %s", p, e)
                    failed.append(p)
        finally:
            if use_turbo:
                try:
                    await http.set_turbo(False)
                except GoProError as e:
                    logger.warning("could not disable turbo transfer: %s", e)
        return downloaded, failed

    # ================================================================== hardware info
    def hardware_summary(self) -> dict[str, Any]:
        hw = dict(self.hardware)
        model = hw.get("model_name", "")
        fw = str(hw.get("firmware_version", ""))
        minimum = smap.min_firmware_for(smap.spec_model_name(model) or model) or ""
        hw["min_supported_firmware"] = minimum
        hw["firmware_supported"] = bool(minimum and fw and smap.firmware_at_least(fw, minimum))
        return hw
