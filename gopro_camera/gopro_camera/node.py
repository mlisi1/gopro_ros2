"""gopro_camera LifecycleNode: parameters and ROS interfaces around :class:`GoProCamera`.

Threading model
- One asyncio loop per camera in a dedicated thread (:class:`AsyncLoopThread`); all camera I/O runs there.
- Service and action callbacks are ``async def`` and *await* an rclpy Future completed from the
  asyncio thread (``ros_await``), so no executor thread blocks on network I/O while they run.
- Lifecycle transitions and parameter callbacks must return synchronously in rclpy; they wait on the
  asyncio future with a bounded timeout. Run the node in a MultiThreadedExecutor (``main`` does).
- Callback groups: services/actions (reentrant), timers (mutually exclusive), subscriptions.
  Image data is published straight from the stream receiver thread.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import sys
import threading
import time
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import rclpy
from builtin_interfaces.msg import Time as TimeMsg
from diagnostic_msgs.msg import DiagnosticStatus
from diagnostic_updater import DiagnosticStatusWrapper, Updater
from rcl_interfaces.msg import ParameterDescriptor, SetParametersResult
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup, ReentrantCallbackGroup
from rclpy.executors import ExternalShutdownException, MultiThreadedExecutor
from rclpy.lifecycle import LifecycleNode, LifecycleState, TransitionCallbackReturn
from rclpy.parameter import Parameter
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy, qos_profile_sensor_data
from rclpy.signals import SignalHandlerOptions
from sensor_msgs.msg import CameraInfo, CompressedImage, Image
from std_msgs.msg import Empty
from std_srvs.srv import Trigger

from gopro_camera import settings_map as smap
from gopro_camera.camera import CameraConfig, GoProCamera, RecordingEventData
from gopro_camera.errors import GoProError, NotConnected
from gopro_camera.loop_bridge import AsyncLoopThread
from gopro_camera.media import to_camera_epoch
from gopro_camera.offload import MODES as OFFLOAD_MODES
from gopro_camera.offload import OffloadConfig, Offloader
from gopro_camera.spec_tables import WEBCAM_PARAMS
from gopro_camera.status import DiagThresholds, evaluate, raw_numeric, status_fields
from gopro_camera.stream import StreamReceiver, load_calibration, scale_calibration, stream_url
from gopro_camera.transports import TransportConfig
from gopro_camera_interfaces.action import DownloadMedia
from gopro_camera_interfaces.action import ProvisionCohn as ProvisionCohnAction
from gopro_camera_interfaces.msg import (
    CohnStatus,
    HardwareInfo,
    MediaItem,
    Preset,
    PresetGroup,
    RecordingEvent,
    SettingCapability,
    SettingValue,
    Status,
)
from gopro_camera_interfaces.srv import (
    Capture,
    DeleteMedia,
    GetCapabilities,
    GetCohnStatus,
    GetHardwareInfo,
    GetMediaAsset,
    GetPresets,
    GetSettings,
    ListMedia,
    LoadPreset,
    ProvisionCohn,
    SetSettings,
    SetWifiAp,
    StartLivestream,
)

try:  # H.264 passthrough type preferred by Foxglove; optional dependency
    from foxglove_msgs.msg import CompressedVideo  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - depends on installed packages
    CompressedVideo = None

SERVICE_TIMEOUT = 120.0

# (name, default, description). settings.* are generated from settings_map.
PARAMS: list[tuple[str, Any, str]] = [
    ("transport", "usb", "usb | wifi_ap | cohn | ble | auto"),
    ("auto_order", ["usb", "cohn", "ble"], "transports tried by transport=auto (add wifi_ap if wanted)"),
    ("ble_sidechannel", False, "also keep a BLE link (push status, wake, AP/COHN/livestream control)"),
    ("serial", "", "camera serial (or its last digits); selects the camera on USB/BLE/COHN"),
    ("ble_name", "", "BLE advertised name, e.g. 'GoPro 1234' (alternative to serial for BLE)"),
    ("usb_interface", "", "host NCM interface of the camera (optional; disambiguates several cameras)"),
    ("usb_base_url", "", "override camera HTTP base URL (tests / NAT); empty = derived from serial"),
    ("cohn_credentials_path", "~/.ros/gopro/cohn_credentials.json", "COHN credentials DB (0600)"),
    ("wifi_ap.interface", "", "host Wi-Fi interface used to join the camera AP"),
    ("wifi_ap.sudo_password", "", "sudo password for nmcli if needed (prefer passwordless policy)"),
    ("connect_timeout", 30.0, "seconds per connection attempt"),
    ("command_timeout", 10.0, "seconds per camera command"),
    ("claim_control", True, "claim external control on connect (camera UI returns to idle)"),
    ("autostart", True, "configure+activate automatically at startup, retrying until connected"),
    ("autostart_retry_period", 5.0, "seconds between autostart configure attempts"),
    ("on_connect.sync_datetime", True, "set camera date/time/timezone from host clock on connect"),
    ("on_connect.load_preset", "", "preset id or group (video|photo|timelapse) to load on configure"),
    ("on_connect.apply_settings", True, "apply settings.* parameters on configure"),
    ("stop_recording_on_deactivate", False, "stop an ongoing recording on lifecycle deactivate"),
    ("sleep_on_shutdown", False, "put the camera to sleep on shutdown (never while recording)"),
    ("status.poll_rate", 1.0, "HTTP status poll rate [Hz] (BLE push used when available)"),
    ("status.use_ble_push", True, "use BLE push notifications for status when BLE is connected"),
    ("status.timeout", 3.0, "seconds per status query"),
    ("status.publish_rate", 1.0, "~/status publish rate [Hz]"),
    ("keep_alive_period", 3.0, "keep-alive period [s] (spec recommends 3.0)"),
    ("reconnect.enabled", True, "reconnect with exponential backoff on link loss"),
    ("reconnect.backoff_max", 30.0, "max reconnect backoff [s]"),
    ("reconnect.link_loss_threshold", 3, "consecutive failed status queries before link loss"),
    ("stream.enabled", False, "start the live stream on activate"),
    ("stream.mode", "webcam", "webcam (USB/COHN, Wi-Fi on newer models) | preview (Wi-Fi AP)"),
    ("stream.protocol", "TS", "webcam protocol: TS (UDP MPEG-TS) | RTSP"),
    ("stream.resolution", "", "webcam resolution: spec id or label (e.g. 1080p); empty = camera default"),
    ("stream.fov", "", "webcam FOV: spec id or label (e.g. Linear); empty = camera default"),
    ("stream.port", 8554, "UDP port the camera streams to"),
    ("stream.publish_raw", True, "publish decoded ~/image_raw"),
    ("stream.publish_h264", True, "publish H.264 passthrough ~/image_h264"),
    ("stream.max_rate", 0.0, "max ~/image_raw rate [Hz], 0 = every frame"),
    ("stream.scale", 1.0, "downscale factor for ~/image_raw"),
    ("stream.latency_offset", 1.5, "seconds subtracted from receive time when stamping frames"),
    ("stream.frame_id", "gopro_optical_frame", "frame_id of images and camera_info"),
    ("camera_info_url", "", "calibration YAML (file://, package:// or path); equidistant model"),
    ("media.download_dir", "~/gopro_media", "default download directory"),
    (
        "media.offload",
        "off",
        "after each recording, when the camera is idle: off | copy (SD + local copy) | "
        "move (local copy, then deleted from the SD after verification)",
    ),
    ("media.offload_dir", "", "where offloaded files go; empty = media.download_dir"),
    ("media.offload_min_free_gb", 5.0, "local disk space to keep free; transfers wait otherwise"),
    ("media.offload_settle_s", 10.0, "camera idle this long before a transfer (it finishes writing the file)"),
    ("media.camera_clock_is_local", True, "camera media timestamps are local wall time (GoPro default)"),
    ("diagnostics.running_on_battery", True, "battery warnings only matter when not powered by USB"),
    ("diagnostics.battery_warn_percent", 20, ""),
    ("diagnostics.battery_error_percent", 10, ""),
    ("diagnostics.sd_warn_minutes", 30.0, ""),
    ("diagnostics.sd_error_minutes", 5.0, ""),
]

LIVE_PARAMS = {
    "status.poll_rate",
    "status.publish_rate",
    "keep_alive_period",
    "stream.max_rate",
    "stream.scale",
    "stream.latency_offset",
    "stream.publish_raw",
    "stream.publish_h264",
    "stream.frame_id",
    "media.download_dir",
    "media.offload",
    "media.offload_min_free_gb",
    "media.offload_settle_s",
    "reconnect.enabled",
    "reconnect.backoff_max",
    "on_connect.sync_datetime",
    "on_connect.load_preset",
    "on_connect.apply_settings",
    "stop_recording_on_deactivate",
    "sleep_on_shutdown",
    "camera_info_url",
    "stream.mode",
    "stream.protocol",
    "stream.resolution",
    "stream.fov",
    "stream.port",
    "stream.enabled",
    "autostart",
} | {n for n, _, _ in PARAMS if n.startswith("diagnostics.")}


def _time_msg(ns: int) -> TimeMsg:
    ns = max(int(ns), 0)
    return TimeMsg(sec=ns // 1_000_000_000, nanosec=ns % 1_000_000_000)


def _ns(t: TimeMsg) -> int:
    return int(t.sec) * 1_000_000_000 + int(t.nanosec)


def _offload_mode(value: Any) -> str:
    """media.offload value -> off | copy | move (False from an unquoted YAML `off` means off)."""
    if value is False or str(value).strip().lower() in ("off", "false", ""):
        return "off"
    return str(value).strip().lower()


def _webcam_param(kind: str, value: str) -> int | None:
    value = str(value).strip()
    if not value:
        return None
    if value.lstrip("-").isdigit():
        v = int(value)
        return None if v < 0 else v
    table = WEBCAM_PARAMS.get(kind, {})
    for k, label in table.items():
        if label.casefold() == value.casefold():
            return k
    raise ValueError(f"stream {kind} '{value}' unknown; valid: {table}")


class GoProNode(LifecycleNode):
    def __init__(self, node_name: str = "gopro_camera", **kwargs: Any) -> None:
        super().__init__(node_name, **kwargs)
        for name, default, desc in PARAMS:
            # media.offload: YAML reads an unquoted `off` as false, so accept a bool too
            dyn = name == "media.offload"
            self.declare_parameter(name, default, ParameterDescriptor(description=desc, dynamic_typing=dyn))
        for sdef in smap.SETTING_DEFS.values():
            self.declare_parameter(
                sdef.param_name,
                None,
                ParameterDescriptor(description=smap.describe(sdef), dynamic_typing=True),
            )

        self._bridge = AsyncLoopThread(f"gopro-{self.get_name()}")
        self._camera: GoProCamera | None = None
        self._active = False
        self._applying_internal = False
        self._stream: StreamReceiver | None = None
        self._stream_wanted = False
        self._calib: dict[str, Any] | None = None
        self._hw_msg: HardwareInfo | None = None
        self._link_reason = "not configured"

        self._srv_group = ReentrantCallbackGroup()
        self._timer_group = MutuallyExclusiveCallbackGroup()
        self._sub_group = MutuallyExclusiveCallbackGroup()

        latched = QoSProfile(
            depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL, reliability=ReliabilityPolicy.RELIABLE
        )
        events_qos = QoSProfile(
            depth=100, durability=DurabilityPolicy.TRANSIENT_LOCAL, reliability=ReliabilityPolicy.RELIABLE
        )
        self._pub_status = self.create_publisher(Status, "~/status", 10)
        self._pub_event = self.create_publisher(RecordingEvent, "~/recording_event", events_qos)
        self._pub_hw = self.create_publisher(HardwareInfo, "~/hardware_info", latched)
        self._pub_raw = self.create_publisher(Image, "~/image_raw", qos_profile_sensor_data)
        if CompressedVideo is not None:
            self._pub_h264 = self.create_publisher(CompressedVideo, "~/image_h264", 50)
        else:
            self.get_logger().warning(
                "foxglove_msgs not installed: ~/image_h264 uses sensor_msgs/CompressedImage (format 'h264')"
            )
            self._pub_h264 = self.create_publisher(CompressedImage, "~/image_h264", 50)
        self._pub_info = self.create_publisher(CameraInfo, "~/camera_info", qos_profile_sensor_data)

        self._diag = Updater(self)
        self._diag.setHardwareID("gopro (not connected)")
        for item in ("link", "battery", "sd_card", "thermal", "stream", "offload"):
            self._diag.add(f"gopro {item}", lambda stat, _i=item: self._diag_task(stat, _i))

        self.create_subscription(
            Empty, "~/hilight_trigger", self._on_hilight_trigger, 10, callback_group=self._sub_group
        )

        g = self._srv_group
        srv = self.create_service
        srv(Capture, "~/start_recording", self._srv_start_recording, callback_group=g)
        srv(Capture, "~/stop_recording", self._srv_stop_recording, callback_group=g)
        srv(Capture, "~/take_photo", self._srv_take_photo, callback_group=g)
        srv(Capture, "~/hilight", self._srv_hilight, callback_group=g)
        srv(LoadPreset, "~/load_preset", self._srv_load_preset, callback_group=g)
        srv(GetPresets, "~/get_presets", self._srv_get_presets, callback_group=g)
        srv(SetSettings, "~/set_settings", self._srv_set_settings, callback_group=g)
        srv(GetSettings, "~/get_settings", self._srv_get_settings, callback_group=g)
        srv(GetCapabilities, "~/get_capabilities", self._srv_get_capabilities, callback_group=g)
        srv(GetHardwareInfo, "~/get_hardware_info", self._srv_get_hardware_info, callback_group=g)
        srv(Trigger, "~/sync_datetime", self._srv_sync_datetime, callback_group=g)
        srv(Trigger, "~/sleep", self._srv_sleep, callback_group=g)
        srv(Trigger, "~/power_down", self._srv_power_down, callback_group=g)
        srv(Trigger, "~/wake", self._srv_wake, callback_group=g)
        srv(SetWifiAp, "~/set_wifi_ap", self._srv_set_wifi_ap, callback_group=g)
        srv(ProvisionCohn, "~/provision_cohn", self._srv_provision_cohn, callback_group=g)
        srv(GetCohnStatus, "~/get_cohn_status", self._srv_get_cohn_status, callback_group=g)
        srv(Trigger, "~/start_stream", self._srv_start_stream, callback_group=g)
        srv(Trigger, "~/stop_stream", self._srv_stop_stream, callback_group=g)
        srv(StartLivestream, "~/start_livestream", self._srv_start_livestream, callback_group=g)
        srv(Trigger, "~/stop_livestream", self._srv_stop_livestream, callback_group=g)
        srv(ListMedia, "~/list_media", self._srv_list_media, callback_group=g)
        srv(DeleteMedia, "~/delete_media", self._srv_delete_media, callback_group=g)
        srv(GetMediaAsset, "~/get_media_asset", self._srv_get_media_asset, callback_group=g)

        self._download_server = ActionServer(
            self,
            DownloadMedia,
            "~/download_media",
            execute_callback=self._act_download,
            goal_callback=lambda _g: GoalResponse.ACCEPT,
            cancel_callback=lambda _g: CancelResponse.ACCEPT,
            callback_group=g,
        )
        self._cohn_server = ActionServer(
            self,
            ProvisionCohnAction,
            "~/provision_cohn",
            execute_callback=self._act_provision_cohn,
            goal_callback=lambda _g: GoalResponse.ACCEPT,
            cancel_callback=lambda _g: CancelResponse.REJECT,
            callback_group=g,
        )

        self._status_timer = self.create_timer(
            1.0 / max(self.p("status.publish_rate"), 0.01), self._publish_status, callback_group=self._timer_group
        )
        self.add_on_set_parameters_callback(self._on_params)

        self._autostart_timer = None
        if self.p("autostart"):
            self._autostart_timer = self.create_timer(0.2, self._autostart, callback_group=self._timer_group)

    # ================================================================== helpers
    def p(self, name: str) -> Any:
        return self.get_parameter(name).value

    def now_ns(self) -> int:
        return self.get_clock().now().nanoseconds

    def _wake(self) -> None:
        ex = self.executor
        if ex is not None:
            ex.wake()

    async def _call(
        self, fn: Callable[[GoProCamera], Coroutine[Any, Any, Any]], timeout: float = SERVICE_TIMEOUT
    ) -> Any:
        """Await ``fn(camera)`` on the camera loop without blocking this executor thread."""
        cam = self._camera
        if cam is None:
            raise NotConnected("node is not configured (no camera)")
        return await self._bridge.ros_await(asyncio.wait_for(fn(cam), timeout), self._wake)

    def _run_sync(self, coro: Coroutine[Any, Any, Any], timeout: float) -> Any:
        """Only for lifecycle transitions / parameter callbacks (rclpy requires a sync return)."""
        return self._bridge.run_sync(asyncio.wait_for(coro, timeout), timeout + 5.0)

    def _err(self, what: str, e: BaseException) -> str:
        if isinstance(e, GoProError):
            msg = f"{what} failed: {e}"
        elif isinstance(e, TimeoutError | concurrent.futures.TimeoutError):
            msg = f"{what} timed out"
        else:
            msg = f"{what} failed unexpectedly: {type(e).__name__}: {e}"
        cam = self._camera
        if cam is not None and cam.context() not in msg:
            msg += f" [{cam.context()}]"
        self.get_logger().error(msg)
        return msg

    def _camera_config(self) -> CameraConfig:
        tcfg = TransportConfig(
            transport=self.p("transport"),
            serial=self.p("serial") or None,
            ble_name=self.p("ble_name") or None,
            ble_sidechannel=self.p("ble_sidechannel"),
            usb_interface=self.p("usb_interface") or None,
            usb_base_url=self.p("usb_base_url") or None,
            cohn_credentials_path=self.p("cohn_credentials_path"),
            wifi_ap_interface=self.p("wifi_ap.interface") or None,
            wifi_ap_sudo_password=self.p("wifi_ap.sudo_password") or None,
            connect_timeout=float(self.p("connect_timeout")),
            command_timeout=float(self.p("command_timeout")),
        )
        return CameraConfig(
            transport=tcfg,
            auto_order=list(self.p("auto_order")),
            keep_alive_period=float(self.p("keep_alive_period")),
            status_poll_rate=float(self.p("status.poll_rate")),
            status_timeout=float(self.p("status.timeout")),
            use_ble_push=self.p("status.use_ble_push"),
            claim_control=self.p("claim_control"),
            sync_datetime_on_connect=self.p("on_connect.sync_datetime"),
            reconnect=self.p("reconnect.enabled"),
            reconnect_backoff_max=float(self.p("reconnect.backoff_max")),
            link_loss_threshold=int(self.p("reconnect.link_loss_threshold")),
            camera_clock_is_local=self.p("media.camera_clock_is_local"),
        )

    def _settings_from_params(self) -> list[tuple[int, int]]:
        items = []
        for sdef in smap.SETTING_DEFS.values():
            param = self.get_parameter(sdef.param_name)
            if param.type_ == Parameter.Type.NOT_SET:
                continue
            items.append((sdef.id, smap.parse_option(sdef, param.value)))
        return items

    # ================================================================== lifecycle
    def _autostart(self) -> None:
        state = self._state_machine.current_state[1]
        if state == "unconfigured":
            if self.trigger_configure() != TransitionCallbackReturn.SUCCESS:
                period = float(self.p("autostart_retry_period"))
                self.get_logger().warning(f"autostart: configure failed; retrying in {period:.0f}s")
                self._autostart_timer.cancel()
                self._autostart_timer = self.create_timer(period, self._autostart, callback_group=self._timer_group)
                return
        if self._state_machine.current_state[1] == "inactive":
            self.trigger_activate()
        if self._autostart_timer is not None:
            self._autostart_timer.cancel()
            self._autostart_timer = None

    def on_configure(self, state: LifecycleState) -> TransitionCallbackReturn:
        log = self.get_logger()
        try:
            self._calib = load_calibration(self.p("camera_info_url"))
        except Exception as e:
            log.error(f"camera_info_url '{self.p('camera_info_url')}' unreadable: {e}")
            return TransitionCallbackReturn.FAILURE
        try:
            settings_items = self._settings_from_params()
        except GoProError as e:
            log.error(f"invalid settings.* parameter: {e}")
            return TransitionCallbackReturn.FAILURE
        cfg = self._camera_config()
        cam = GoProCamera(cfg, now_ns=self.now_ns, listener=self)
        self._camera = cam
        try:
            self._run_sync(cam.connect(), cfg.transport.connect_timeout * 4 + 10)
        except Exception as e:
            self._err(f"connect ({cfg.transport.transport})", e)
            self._camera = None
            return TransitionCallbackReturn.FAILURE
        log.info(f"connected: {cam.context()}")
        self._publish_hardware_info()
        try:
            self._run_sync(self._start_offloader(cam), 10)
        except Exception as e:
            self._err("start media offload", e)
        preset = str(self.p("on_connect.load_preset")).strip()
        if preset:
            if cam.encoding:
                log.warning("on_connect.load_preset skipped: camera is recording (adopted)")
            else:
                try:
                    pid = int(preset) if preset.lstrip("-").isdigit() else -1
                    self._run_sync(cam.load_preset(pid, "" if pid >= 0 else preset), 30)
                except Exception as e:
                    self._err(f"load preset '{preset}'", e)
        if self.p("on_connect.apply_settings") and settings_items:
            if cam.encoding:
                log.warning("settings.* not applied: camera is recording (adopted); they apply on next configure")
            else:
                try:
                    applied, failed, _ = self._run_sync(cam.set_settings(settings_items, rollback=False), 120)
                    for sid, opt, err in failed:
                        log.error(f"setting {smap.lookup(sid).param_name}={opt} not applied: {err}")
                    log.info(f"applied {len(applied)} setting(s) from parameters")
                except Exception as e:
                    self._err("apply settings", e)
        return TransitionCallbackReturn.SUCCESS

    def on_activate(self, state: LifecycleState) -> TransitionCallbackReturn:
        self._active = True
        if self.p("stream.enabled"):
            self._stream_wanted = True
            self._bridge.submit(self._start_stream_async())
        return super().on_activate(state)

    def on_deactivate(self, state: LifecycleState) -> TransitionCallbackReturn:
        self._active = False
        self._stream_wanted = False
        self._stop_receiver()
        cam = self._camera
        if cam is not None:
            try:
                self._run_sync(cam.stop_stream(), 20)
            except Exception as e:
                self._err("stop stream", e)
            if self.p("stop_recording_on_deactivate") and cam.encoding:
                try:
                    self._run_sync(cam.stop_recording(), 30)
                except Exception as e:
                    self._err("stop recording on deactivate", e)
        return super().on_deactivate(state)

    def _teardown(self, sleep: bool) -> None:
        self._stop_receiver()
        cam = self._camera
        if cam is not None and cam.offloader is not None:
            try:  # an interrupted transfer leaves the SD file untouched and resumes next time
                self._run_sync(cam.offloader.stop(), 10)
            except Exception as e:
                self._err("stop media offload", e)
            cam.offloader = None
        if cam is not None:
            try:
                self._run_sync(cam.disconnect(release_control=True, sleep=sleep), 30)
            except Exception as e:
                self._err("disconnect", e)
        self._camera = None

    def on_cleanup(self, state: LifecycleState) -> TransitionCallbackReturn:
        self._teardown(sleep=False)
        return TransitionCallbackReturn.SUCCESS

    def on_shutdown(self, state: LifecycleState) -> TransitionCallbackReturn:
        self._active = False
        self._teardown(sleep=bool(self.p("sleep_on_shutdown")))
        return TransitionCallbackReturn.SUCCESS

    def on_error(self, state: LifecycleState) -> TransitionCallbackReturn:
        self._teardown(sleep=False)
        return TransitionCallbackReturn.SUCCESS

    def destroy_node(self) -> None:
        if self._camera is not None:
            self._teardown(sleep=False)
        self._bridge.stop()
        super().destroy_node()

    # ================================================================== camera listener (asyncio thread)
    def on_state(self, camera: GoProCamera) -> None:
        pass

    def on_link(self, camera: GoProCamera, connected: bool, reason: str) -> None:
        self._link_reason = reason
        if connected:
            self.get_logger().info(f"camera link up: {reason}")
            self._publish_hardware_info()
            if self._active and self._stream_wanted and not camera.stream_mode:
                self._bridge.submit(self._start_stream_async())
        elif reason.startswith(("disconnected by driver", "camera put to sleep", "camera powered down")):
            self.get_logger().info(f"camera link closed: {reason}")
        else:
            self.get_logger().error(f"camera link down: {reason} [{camera.context()}]")

    def on_recording_event(self, camera: GoProCamera, ev: RecordingEventData) -> None:
        msg = self._event_msg(ev)
        self._pub_event.publish(msg)
        log = self.get_logger().info if ev.success else self.get_logger().error
        log(f"recording event {ev.event}: {ev.message} file={ev.filename or '-'} via {ev.transport}")

    def _event_msg(self, ev: RecordingEventData) -> RecordingEvent:
        cam = self._camera
        m = RecordingEvent()
        m.header.stamp = _time_msg(ev.command_sent_ns)
        m.header.frame_id = self.p("stream.frame_id")
        m.event = ev.event
        m.camera_serial = cam.serial if cam else ""
        m.transport = ev.transport
        m.command_sent = _time_msg(ev.command_sent_ns)
        m.camera_ack = _time_msg(ev.camera_ack_ns)
        m.encoding_changed = _time_msg(ev.encoding_changed_ns)
        m.encoding_changed_uncertainty = float(ev.encoding_changed_uncertainty)
        m.success = ev.success
        m.message = ev.message
        m.filename = ev.filename
        m.encoding_duration = int(ev.encoding_duration)
        return m

    async def _start_offloader(self, cam: GoProCamera) -> None:
        """Runs on the camera loop. Created even when off, so a live switch to copy/move works."""
        dest = str(self.p("media.offload_dir")) or str(self.p("media.download_dir"))
        cfg = OffloadConfig(
            mode=_offload_mode(self.p("media.offload")),
            dest=Path(dest).expanduser(),
            min_free_bytes=int(float(self.p("media.offload_min_free_gb")) * 1e9),
            settle_s=float(self.p("media.offload_settle_s")),
        )
        if cfg.mode not in OFFLOAD_MODES:
            self.get_logger().error(f"media.offload '{cfg.mode}' invalid (expected {OFFLOAD_MODES}); offload is off")
            cfg.mode = "off"
        off = Offloader(cam, cfg, cam.serial)
        cam.offloader = off
        off.start()
        if cfg.mode != "off":
            self.get_logger().info(f"media offload: {cfg.mode} to {cfg.dest} (only while the camera is idle)")

    # ================================================================== status / diagnostics
    def _publish_status(self) -> None:
        cam = self._camera
        if not self._active or cam is None:
            return
        st = cam.status_snapshot
        m = Status()
        m.header.stamp = self.get_clock().now().to_msg()
        m.header.frame_id = self.p("stream.frame_id")
        m.connected = cam.connected
        m.transport = cam.transport_name
        m.http_connected = bool(cam.http and cam.http.is_connected)
        m.ble_connected = bool(cam.ble and cam.ble.is_connected)
        m.status_source = cam.status_source
        age = cam.status_age
        m.status_age = float(age) if age != float("inf") else -1.0
        for k, v in status_fields(st).items():
            setattr(m, k, v)
        m.stream_active = bool(cam.stream_mode)
        m.stream_mode = cam.stream_mode
        m.livestream_active = cam.livestream_active
        m.raw_status_ids, m.raw_status_values = raw_numeric(st)
        if cam.offloader is not None:
            o = cam.offloader.summary()
            m.offload_mode, m.offload_pending, m.offload_failed = o["mode"], o["pending"], o["failed"]
            m.offload_current = o["current"]
        self._pub_status.publish(m)

    def _diag_task(self, stat: DiagnosticStatusWrapper, item: str) -> DiagnosticStatusWrapper:
        cam = self._camera
        if item == "offload":
            off = cam.offloader if cam is not None else None
            if off is None or off.mode == "off":
                stat.summary(DiagnosticStatus.OK, "off")
                return stat
            o = off.summary()
            level = DiagnosticStatus.WARN if o["failed"] or "disk space" in o["last_error"] else DiagnosticStatus.OK
            busy = f"transferring {o['current']}" if o["current"] else "idle"
            stat.summary(level, f"{o['mode']}: {busy}, {o['pending']} pending, {o['failed']} failed")
            for k, v in o.items():
                stat.add(k, str(v))
            return stat
        if cam is None:
            stat.summary(DiagnosticStatus.WARN if item == "link" else DiagnosticStatus.OK, self._link_reason)
            return stat
        th = DiagThresholds(
            battery_warn_percent=int(self.p("diagnostics.battery_warn_percent")),
            battery_error_percent=int(self.p("diagnostics.battery_error_percent")),
            sd_warn_minutes=float(self.p("diagnostics.sd_warn_minutes")),
            sd_error_minutes=float(self.p("diagnostics.sd_error_minutes")),
            status_stale_s=max(5.0, 3.0 / max(float(self.p("status.poll_rate")), 0.01)),
        )
        rx = self._stream
        items = evaluate(
            cam.status_snapshot,
            connected=cam.connected,
            status_age=cam.status_age,
            transport=cam.transport_name or str(self.p("transport")),
            stream_active=bool(cam.stream_mode) and rx is not None,
            stream_frame_age=rx.last_packet_age if rx is not None else None,
            th=th,
            running_on_battery=bool(self.p("diagnostics.running_on_battery")),
        )
        for it in items:
            if it.name == item:
                stat.summary(bytes([it.level]), it.message)
                for k, v in it.values.items():
                    stat.add(k, v)
                if item == "stream" and rx is not None:
                    s = rx.stats
                    stat.add("packets", str(s.packets))
                    stat.add("frames_published", str(s.frames_published))
                    stat.add("pts_gaps (dropped)", str(s.pts_gaps))
                    stat.add("reconnects", str(s.reconnects))
                return stat
        stat.summary(DiagnosticStatus.OK, "inactive" if item == "stream" else "ok")
        return stat

    def _publish_hardware_info(self) -> None:
        cam = self._camera
        if cam is None:
            return
        hw = cam.hardware_summary()
        m = HardwareInfo()
        m.model_name = str(hw.get("model_name", ""))
        m.model_number = int(hw.get("model_number", 0) or 0)
        m.serial_number = str(hw.get("serial_number", ""))
        m.firmware_version = str(hw.get("firmware_version", ""))
        m.ap_ssid = str(hw.get("ap_ssid", ""))
        m.ap_mac_address = str(hw.get("ap_mac_addr", ""))
        m.open_gopro_version = str(hw.get("open_gopro_version", ""))
        m.min_supported_firmware = str(hw.get("min_supported_firmware", ""))
        m.firmware_supported = bool(hw.get("firmware_supported", False))
        m.transports_available = [x for x in [cam.transport_name, "ble" if cam.ble else ""] if x]
        self._hw_msg = m
        self._pub_hw.publish(m)
        self._diag.setHardwareID(f"gopro {m.model_name} {m.serial_number}".strip())
        if m.min_supported_firmware and not m.firmware_supported:
            self.get_logger().warning(
                f"firmware {m.firmware_version} is below the Open GoPro minimum {m.min_supported_firmware} "
                f"for {m.model_name}; update the camera firmware"
            )
        elif not m.min_supported_firmware and m.model_name:
            self.get_logger().warning(f"model '{m.model_name}' is not in the Open GoPro supported-cameras table")

    # ================================================================== parameters
    def _on_params(self, params: list[Parameter]) -> SetParametersResult:
        cam = self._camera
        setting_items: list[tuple[int, int]] = []
        for prm in params:
            if prm.name.startswith(smap.PARAM_PREFIX):
                try:
                    sdef = smap.lookup(prm.name)
                    if prm.type_ == Parameter.Type.NOT_SET:
                        continue
                    opt = smap.parse_option(sdef, prm.value)
                    if cam is not None and cam.connected:
                        cam.check_settings_allowed()
                        cam.validate_setting(sdef.id, opt)
                    setting_items.append((sdef.id, opt))
                except GoProError as e:
                    return SetParametersResult(successful=False, reason=str(e))
            elif prm.name == "media.offload" and _offload_mode(prm.value) not in OFFLOAD_MODES:
                return SetParametersResult(successful=False, reason=f"media.offload must be one of {OFFLOAD_MODES}")
            elif prm.name in ("stream.resolution", "stream.fov"):
                try:
                    _webcam_param("res" if prm.name.endswith("resolution") else "fov", prm.value)
                except ValueError as e:
                    return SetParametersResult(successful=False, reason=str(e))
            elif prm.name not in LIVE_PARAMS and self._camera is not None:
                self.get_logger().info(f"parameter {prm.name} changes take effect on next configure")
        if setting_items and cam is not None and cam.connected and not self._applying_internal:
            try:
                _applied, failed, _ = self._run_sync(cam.set_settings(setting_items, rollback=True), 60)
            except Exception as e:
                return SetParametersResult(successful=False, reason=self._err("set settings", e))
            if failed:
                return SetParametersResult(successful=False, reason="; ".join(err for _, _, err in failed))
        self._apply_live_params(params)
        return SetParametersResult(successful=True)

    def _apply_live_params(self, params: list[Parameter]) -> None:
        cam = self._camera
        for prm in params:
            if cam is not None:
                if prm.name == "status.poll_rate":
                    cam.cfg.status_poll_rate = float(prm.value)
                elif prm.name == "keep_alive_period":
                    cam.cfg.keep_alive_period = float(prm.value)
                elif prm.name == "reconnect.enabled":
                    cam.cfg.reconnect = bool(prm.value)
                elif prm.name == "reconnect.backoff_max":
                    cam.cfg.reconnect_backoff_max = float(prm.value)
            rx = self._stream
            if rx is not None:
                if prm.name == "stream.max_rate":
                    rx.max_rate = float(prm.value)
                elif prm.name == "stream.scale":
                    rx.scale = float(prm.value)
            off = cam.offloader if cam is not None else None
            if off is not None:
                if prm.name == "media.offload":
                    off.cfg.mode = _offload_mode(prm.value)
                    self._bridge.loop.call_soon_threadsafe(off.wake)  # asyncio objects: camera loop only
                elif prm.name == "media.offload_min_free_gb":
                    off.cfg.min_free_bytes = int(float(prm.value) * 1e9)
                elif prm.name == "media.offload_settle_s":
                    off.cfg.settle_s = float(prm.value)
            if prm.name == "status.publish_rate" and float(prm.value) > 0:
                self._status_timer.timer_period_ns = int(1e9 / float(prm.value))
                self._status_timer.reset()

    # ================================================================== stream
    async def _start_stream_async(self) -> str:
        cam = self._camera
        if cam is None:
            raise NotConnected("not configured")
        mode = str(self.p("stream.mode"))
        protocol = str(self.p("stream.protocol")).upper()
        port = int(self.p("stream.port"))
        try:
            res = _webcam_param("res", self.p("stream.resolution"))
            fov = _webcam_param("fov", self.p("stream.fov"))
        except ValueError as e:
            raise GoProError(str(e)) from e
        try:
            msg = await cam.start_stream(mode, res, fov, port, protocol if mode == "webcam" else None)
        except GoProError as e:
            self._err("start stream", e)
            raise
        ip = cam.http.sdk.ip_address if cam.http is not None else ""
        self._start_receiver(stream_url(mode, protocol, port, ip))
        return msg

    def _start_receiver(self, url: str) -> None:
        self._stop_receiver()
        rx = StreamReceiver(
            url,
            self._on_packet if self.p("stream.publish_h264") else None,
            self._on_frame if self.p("stream.publish_raw") else None,
            max_rate=float(self.p("stream.max_rate")),
            scale=float(self.p("stream.scale")),
            now_ns=self.now_ns,
        )
        rx.start()
        self._stream = rx
        self.get_logger().info(f"receiving stream from {url}")

    def _stop_receiver(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream = None

    def _stamp(self, recv_ns: int) -> TimeMsg:
        return _time_msg(recv_ns - int(float(self.p("stream.latency_offset")) * 1e9))

    def _on_packet(self, data: bytes, keyframe: bool, recv_ns: int) -> None:
        if not self._active:
            return
        stamp = self._stamp(recv_ns)
        frame_id = self.p("stream.frame_id")
        if CompressedVideo is not None:
            m = CompressedVideo()
            m.timestamp = stamp
            m.frame_id = frame_id
            m.data = data
            m.format = "h264"
        else:
            m = CompressedImage()
            m.header.stamp = stamp
            m.header.frame_id = frame_id
            m.format = "h264"
            m.data = data
        self._pub_h264.publish(m)

    def _on_frame(self, img: Any, recv_ns: int) -> None:
        if not self._active:
            return
        stamp = self._stamp(recv_ns)
        frame_id = self.p("stream.frame_id")
        h, w = img.shape[:2]
        m = Image()
        m.header.stamp = stamp
        m.header.frame_id = frame_id
        m.height, m.width = h, w
        m.encoding = "bgr8"
        m.is_bigendian = 0
        m.step = w * 3
        m.data = img.tobytes()
        self._pub_raw.publish(m)
        if self._calib is not None:
            c = scale_calibration(self._calib, w, h)
            info = CameraInfo()
            info.header = m.header
            info.width, info.height = c["width"], c["height"]
            info.distortion_model = c["distortion_model"]
            info.d = c["D"]
            info.k = c["K"]
            info.r = c["R"]
            info.p = c["P"] if len(c["P"]) == 12 else [*c["K"][0:3], 0.0, *c["K"][3:6], 0.0, *c["K"][6:9], 0.0]
            self._pub_info.publish(info)

    # ================================================================== services: capture
    async def _capture(self, resp: Any, what: str, fn: Callable[[GoProCamera], Coroutine[Any, Any, Any]]) -> Any:
        try:
            ev = await self._call(fn)
            resp.success, resp.message, resp.event = ev.success, ev.message, self._event_msg(ev)
        except Exception as e:
            resp.success, resp.message = False, self._err(what, e)
        return resp

    async def _srv_start_recording(self, req: Capture.Request, resp: Capture.Response) -> Capture.Response:
        return await self._capture(resp, "start recording", lambda c: c.start_recording())

    async def _srv_stop_recording(self, req: Capture.Request, resp: Capture.Response) -> Capture.Response:
        return await self._capture(resp, "stop recording", lambda c: c.stop_recording())

    async def _srv_take_photo(self, req: Capture.Request, resp: Capture.Response) -> Capture.Response:
        return await self._capture(resp, "take photo", lambda c: c.take_photo())

    async def _srv_hilight(self, req: Capture.Request, resp: Capture.Response) -> Capture.Response:
        return await self._capture(resp, "hilight", lambda c: c.hilight())

    def _on_hilight_trigger(self, _msg: Empty) -> None:
        cam = self._camera
        if cam is None:
            self.get_logger().warning("hilight trigger ignored: not configured")
            return
        fut = self._bridge.submit(cam.hilight())

        def _done(f: concurrent.futures.Future[Any]) -> None:
            if (e := f.exception()) is not None:
                self._err("hilight (topic trigger)", e)

        fut.add_done_callback(_done)

    # ================================================================== services: presets / settings
    async def _srv_load_preset(self, req: LoadPreset.Request, resp: LoadPreset.Response) -> LoadPreset.Response:
        try:
            resp.active_preset_id = int(await self._call(lambda c: c.load_preset(req.preset_id, req.group)))
            resp.success, resp.message = True, f"preset {resp.active_preset_id} active"
        except Exception as e:
            resp.success, resp.message = False, self._err("load preset", e)
        return resp

    async def _srv_get_presets(self, req: GetPresets.Request, resp: GetPresets.Response) -> GetPresets.Response:
        try:
            groups = await self._call(lambda c: c.get_presets(req.include_hidden))
            for g in groups:
                gm = PresetGroup(id=g["id"], name=g["name"], can_add_preset=g["can_add_preset"])
                for p in g["presets"]:
                    pm = Preset(
                        id=p["id"],
                        title=p["title"],
                        group_id=p["group_id"],
                        is_active=p["is_active"],
                        is_modified=p["is_modified"],
                        user_defined=p["user_defined"],
                        is_fixed=p["is_fixed"],
                    )
                    pm.settings = [self._setting_value(sid, opt) for sid, opt in p["settings"]]
                    if p["is_active"]:
                        resp.active_preset_id = p["id"]
                    gm.presets.append(pm)
                resp.groups.append(gm)
            resp.success, resp.message = True, f"{sum(len(g.presets) for g in resp.groups)} presets"
        except Exception as e:
            resp.success, resp.message = False, self._err("get presets", e)
        return resp

    @staticmethod
    def _setting_value(sid: int, opt: Any) -> SettingValue:
        sv = SettingValue(id=int(sid))
        try:
            sdef = smap.lookup(int(sid))
            sv.name = sdef.name
            sv.option = int(opt)
            sv.option_label = sdef.label(int(opt))
        except (GoProError, TypeError, ValueError):
            sv.option = -1
            sv.option_label = str(opt)
        return sv

    @staticmethod
    def _resolve_setting(sv: SettingValue) -> tuple[int, int]:
        sdef = smap.lookup(sv.id if sv.id > 0 else sv.name)
        opt = sv.option if sv.option >= 0 else smap.parse_option(sdef, sv.option_label)
        return sdef.id, opt

    async def _srv_set_settings(self, req: SetSettings.Request, resp: SetSettings.Response) -> SetSettings.Response:
        try:
            items = [self._resolve_setting(sv) for sv in req.settings]
            applied, failed, rolled_back = await self._call(lambda c: c.set_settings(items, not req.no_rollback))
            resp.applied = [self._setting_value(s, o) for s, o in applied]
            resp.failed = [self._setting_value(s, o) for s, o, _ in failed]
            resp.errors = [err for _, _, err in failed]
            resp.rolled_back = rolled_back
            resp.success = not failed
            resp.message = "all settings applied" if not failed else "; ".join(resp.errors)
            if failed:
                self.get_logger().error(f"set_settings: {resp.message}")
            else:
                self._sync_setting_params(applied)
        except Exception as e:
            resp.success, resp.message = False, self._err("set settings", e)
        return resp

    def _sync_setting_params(self, applied: list[tuple[int, int]]) -> None:
        """Keep settings.* parameters consistent with what the service applied."""
        self._applying_internal = True
        try:
            self.set_parameters([Parameter(smap.lookup(s).param_name, value=int(o)) for s, o in applied])
        finally:
            self._applying_internal = False

    async def _srv_get_settings(self, req: GetSettings.Request, resp: GetSettings.Response) -> GetSettings.Response:
        try:
            settings = await self._call(lambda c: c.get_settings())
            resp.settings = [self._setting_value(s, o) for s, o in sorted(settings.items()) if s in smap.SETTING_DEFS]
            resp.success, resp.message = (
                True,
                (f"{len(resp.settings)} settings (values for settings outside the current preset/mode are undefined)"),
            )
        except Exception as e:
            resp.success, resp.message = False, self._err("get settings", e)
        return resp

    async def _srv_get_capabilities(
        self, req: GetCapabilities.Request, resp: GetCapabilities.Response
    ) -> GetCapabilities.Response:
        cam = self._camera
        if cam is None:
            resp.success, resp.message = False, "not configured"
            return resp
        if self._hw_msg is not None:
            resp.hardware = self._hw_msg
        for c in cam.capabilities():
            sc = SettingCapability(id=c["id"], name=c["name"], title=c["title"], settable_over_ble=c["ble"])
            sc.options, sc.option_labels = c["options"], c["labels"]
            sc.runtime_options = c["runtime_options"]
            if c["range"]:
                sc.range_min, sc.range_max = c["range"]
            sc.current = c["current"]
            resp.settings.append(sc)
        resp.features = cam.features()
        resp.transports_active = [x for x in [cam.transport_name, "ble" if cam.ble else ""] if x]
        resp.success, resp.message = True, f"{len(resp.settings)} settings for {cam.model_name or 'unknown model'}"
        return resp

    async def _srv_get_hardware_info(
        self, req: GetHardwareInfo.Request, resp: GetHardwareInfo.Response
    ) -> GetHardwareInfo.Response:
        if self._hw_msg is None:
            resp.success, resp.message = False, "not configured"
        else:
            resp.success, resp.info = True, self._hw_msg
        return resp

    # ================================================================== services: misc
    async def _trigger(self, resp: Any, what: str, fn: Callable[[GoProCamera], Coroutine[Any, Any, Any]]) -> Any:
        try:
            result = await self._call(fn)
            resp.success, resp.message = True, str(result) if isinstance(result, str) else f"{what}: ok"
        except Exception as e:
            resp.success, resp.message = False, self._err(what, e)
        return resp

    async def _srv_sync_datetime(self, req: Trigger.Request, resp: Trigger.Response) -> Trigger.Response:
        return await self._trigger(resp, "sync date/time", lambda c: c.sync_datetime())

    async def _srv_sleep(self, req: Trigger.Request, resp: Trigger.Response) -> Trigger.Response:
        return await self._trigger(resp, "sleep", lambda c: c.sleep())

    async def _srv_power_down(self, req: Trigger.Request, resp: Trigger.Response) -> Trigger.Response:
        return await self._trigger(resp, "power down", lambda c: c.power_down())

    async def _srv_wake(self, req: Trigger.Request, resp: Trigger.Response) -> Trigger.Response:
        return await self._trigger(resp, "wake", lambda c: c.ensure_connected())

    async def _srv_set_wifi_ap(self, req: SetWifiAp.Request, resp: SetWifiAp.Response) -> SetWifiAp.Response:
        return await self._trigger(
            resp, f"wifi AP {'on' if req.enable else 'off'}", lambda c: c.set_wifi_ap(req.enable)
        )

    @staticmethod
    def _cohn_msg(d: dict[str, Any]) -> CohnStatus:
        m = CohnStatus()
        for k in ("supported", "provisioned", "connected", "enabled", "credentials_stored"):
            setattr(m, k, bool(d.get(k, False)))
        for k in ("status", "state", "ssid", "ip_address", "mac_address"):
            setattr(m, k, str(d.get(k, "")))
        return m

    async def _srv_provision_cohn(
        self, req: ProvisionCohn.Request, resp: ProvisionCohn.Response
    ) -> ProvisionCohn.Response:
        try:
            st = await self._call(lambda c: c.provision_cohn(req.ssid, req.password, req.force_reprovision), 300)
            resp.status = self._cohn_msg(st)
            resp.success, resp.message = True, "COHN provisioned; credentials stored"
        except Exception as e:
            resp.success, resp.message = False, self._err("provision COHN", e)
        return resp

    async def _act_provision_cohn(self, goal_handle: Any) -> ProvisionCohnAction.Result:
        goal = goal_handle.request
        result = ProvisionCohnAction.Result()

        def stage(text: str) -> None:
            self.get_logger().info(f"COHN provisioning: {text}")
            goal_handle.publish_feedback(ProvisionCohnAction.Feedback(stage=text))

        try:
            st = await self._call(
                lambda c: c.provision_cohn(goal.ssid, goal.password, goal.force_reprovision, stage), 300
            )
            result.status = self._cohn_msg(st)
            result.success, result.message = True, "COHN provisioned; credentials stored"
            goal_handle.succeed()
        except Exception as e:
            result.success, result.message = False, self._err("provision COHN", e)
            goal_handle.abort()
        return result

    async def _srv_get_cohn_status(
        self, req: GetCohnStatus.Request, resp: GetCohnStatus.Response
    ) -> GetCohnStatus.Response:
        try:
            resp.status = self._cohn_msg(await self._call(lambda c: c.cohn_status()))
            resp.success, resp.message = True, "ok"
        except Exception as e:
            resp.success, resp.message = False, self._err("get COHN status", e)
        return resp

    async def _srv_start_stream(self, req: Trigger.Request, resp: Trigger.Response) -> Trigger.Response:
        if not self._active:
            resp.success, resp.message = False, "node is not active"
            return resp
        self._stream_wanted = True
        try:
            resp.message = await self._bridge.ros_await(self._start_stream_async(), self._wake)
            resp.success = True
        except Exception as e:
            self._stream_wanted = False
            resp.success, resp.message = False, self._err("start stream", e)
        return resp

    async def _srv_stop_stream(self, req: Trigger.Request, resp: Trigger.Response) -> Trigger.Response:
        self._stream_wanted = False
        self._stop_receiver()
        return await self._trigger(resp, "stop stream", lambda c: c.stop_stream())

    async def _srv_start_livestream(
        self, req: StartLivestream.Request, resp: StartLivestream.Response
    ) -> StartLivestream.Response:
        return await self._trigger(
            resp,
            "start livestream",
            lambda c: c.start_livestream(
                req.url,
                req.minimum_bitrate,
                req.maximum_bitrate,
                req.starting_bitrate,
                req.encode,
                req.window_size if req.window_size >= 0 else None,
                req.lens if req.lens >= 0 else None,
            ),
        )

    async def _srv_stop_livestream(self, req: Trigger.Request, resp: Trigger.Response) -> Trigger.Response:
        return await self._trigger(resp, "stop livestream", lambda c: c.stop_livestream())

    # ================================================================== services: media
    def _since(self, since_mission_start: bool, since: TimeMsg) -> int | None:
        cam = self._camera
        local = bool(self.p("media.camera_clock_is_local"))
        if since_mission_start and cam is not None:
            return cam.mission_start_camera_epoch()
        if _ns(since) > 0:
            return to_camera_epoch(_ns(since) / 1e9, local)
        return None

    async def _srv_list_media(self, req: ListMedia.Request, resp: ListMedia.Response) -> ListMedia.Response:
        try:
            since = self._since(req.since_mission_start, req.since)
            for e in await self._call(lambda c: c.list_media(since)):
                resp.items.append(
                    MediaItem(
                        path=e.path,
                        folder=e.folder,
                        filename=e.filename,
                        created=e.created,
                        modified=e.modified,
                        size_bytes=e.size_bytes,
                        is_group=e.is_group,
                        group_type=e.group_type,
                        group_size=e.group_size,
                    )
                )
            resp.success, resp.message = True, f"{len(resp.items)} item(s)"
        except Exception as e:
            resp.success, resp.message = False, self._err("list media", e)
        return resp

    async def _srv_delete_media(self, req: DeleteMedia.Request, resp: DeleteMedia.Response) -> DeleteMedia.Response:
        try:
            resp.deleted, resp.failed = await self._call(lambda c: c.delete_media(list(req.paths), req.confirm))
            resp.success = not resp.failed
            resp.message = f"deleted {len(resp.deleted)}, failed {len(resp.failed)}"
        except Exception as e:
            resp.success, resp.message = False, self._err("delete media", e)
        return resp

    def _download_dir(self, override: str = "") -> Path:
        return Path(override or str(self.p("media.download_dir"))).expanduser()

    async def _srv_get_media_asset(
        self, req: GetMediaAsset.Request, resp: GetMediaAsset.Response
    ) -> GetMediaAsset.Response:
        try:
            filename = req.path.split("/")[-1]
            suffix = {"thumbnail": ".jpg", "screennail": ".jpg", "gpmf": ".gpmf", "telemetry": ".bin"}.get(req.kind, "")
            local = (
                Path(req.local_path).expanduser()
                if req.local_path
                else self._download_dir() / req.kind / (filename + suffix)
            )
            out = await self._call(lambda c: c.get_media_asset(req.kind, req.path, local))
            if isinstance(out, dict):
                resp.info_json = json.dumps(out)
            else:
                resp.local_file = str(out)
            resp.success, resp.message = True, "ok"
        except Exception as e:
            resp.success, resp.message = False, self._err(f"get {req.kind}", e)
        return resp

    async def _act_download(self, goal_handle: Any) -> DownloadMedia.Result:
        goal = goal_handle.request
        result = DownloadMedia.Result()
        last_fb = [0.0]

        def progress(i: int, n: int, path: str, done: int, total: int) -> None:
            now = time.monotonic()
            if now - last_fb[0] < 0.2 and done != total:
                return
            last_fb[0] = now
            fb = DownloadMedia.Feedback(
                current_file=path, file_index=i, file_count=n, bytes_done=done, bytes_total=max(total, 0)
            )
            frac = (done / total) if total > 0 else 0.0
            fb.overall_progress = float((i + frac) / max(n, 1))
            goal_handle.publish_feedback(fb)

        try:
            paths = list(goal.paths)
            if not paths:
                since = self._since(goal.since_mission_start, goal.since)
                paths = [e.path for e in await self._call(lambda c: c.list_media(since)) if not e.is_group]
            dest = self._download_dir(goal.destination)
            downloaded, failed = await self._call(
                lambda c: c.download_media(
                    paths,
                    dest,
                    skip_existing=goal.skip_existing,
                    turbo=not goal.no_turbo,
                    progress=progress,
                    cancelled=lambda: goal_handle.is_cancel_requested,
                ),
                timeout=24 * 3600,
            )
            result.downloaded, result.failed = downloaded, failed
            if goal_handle.is_cancel_requested:
                result.success, result.message = False, f"canceled after {len(downloaded)} file(s)"
                goal_handle.canceled()
            else:
                result.success = not failed
                result.message = f"downloaded {len(downloaded)}/{len(paths)} to {dest}"
                goal_handle.succeed()
        except Exception as e:
            result.success, result.message = False, self._err("download media", e)
            goal_handle.abort()
        return result


def main(args: list[str] | None = None) -> None:
    # Handle SIGINT ourselves so the context stays valid while the camera is released on shutdown.
    rclpy.init(args=args, signal_handler_options=SignalHandlerOptions.NO)
    node = GoProNode()
    executor = MultiThreadedExecutor(num_threads=8)
    executor.add_node(node)
    try:
        executor.spin()
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        # Disconnect cleanly but never stop a recording.
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
        _exit_without_finalizing_stuck_threads()


def _exit_without_finalizing_stuck_threads() -> None:
    """Skip interpreter finalization while SDK request threads are still alive.

    A request to an unreachable camera outlives its timeout: the SDK keeps retrying in its worker
    thread for up to ~35 s. Finalizing the interpreter while such a daemon thread runs native code
    (requests / its event loop) occasionally segfaults (seen as exit -11 after a clean shutdown).
    Everything that matters is done by now (camera released, node destroyed, rclpy shut down), so
    flush and leave immediately.
    """
    others = [t for t in threading.enumerate() if t is not threading.main_thread() and t.daemon and t.is_alive()]
    if not others:
        return
    logging.shutdown()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
