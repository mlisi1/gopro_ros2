"""Every use of ``open_gopro`` internals, in one place, checked against the installed SDK.

The driver relies on a few private SDK members (CLAUDE.md: prefer the SDK, isolate and document what
it lacks). Each one is used here and nowhere else, with the reason:

- ``GoProBase._get_json``: send endpoints that SDK 0.22 does not wrap (hilight moment, sleep, raw
  camera state incl. setting IDs newer than the SDK, webcam endpoints whose answers the SDK model
  rejects) while keeping the SDK's session, COHN auth headers and certificates.
- ``_base_url``, ``_build_http_request_args``, ``_requests_session``, ``_handle_cohn``: stream a media
  download ourselves (progress + cancellation, which the SDK download helper lacks) with the SDK's
  URL and request arguments; ``_base_url`` is also overridden for ``usb_base_url`` (tests / NAT).
- ``_open``: mark a ``WiredGoPro`` open without ``open()``, which waits until the camera stops
  encoding (the driver must adopt an ongoing recording, never wait for or stop it).
- ``_is_cohn_configured``: the SDK sets it inside ``open()`` only when BLE is up; COHN-only needs it.
- ``WiredGoPro._MDNS_SERVICE_NAME``: USB discovery without a known serial.
- ``BleSettingFacade._identifier``: map BLE setting facades to setting IDs.

``check_sdk()`` verifies these and the public SDK API the driver calls, so an SDK upgrade fails at
connect time with one clear message instead of an ``AttributeError`` in the middle of a mission.
``test_sdk_compat.py`` runs the same check in CI. The supported version is pinned in requirements.txt.
"""

from __future__ import annotations

import importlib
import importlib.metadata
from functools import cache
from typing import Any

from gopro_camera.errors import NotSupported

SUPPORTED_SDK = "0.22"  # major.minor of the pinned open_gopro (requirements.txt)


class SdkIncompatible(NotSupported):
    """The installed open_gopro does not provide what the driver needs."""


# (module, attribute path) that must exist. Private members first, then the public API in use.
REQUIRED: list[tuple[str, str]] = (
    [
        ("open_gopro.gopro_base", "GoProBase._get_json"),
        ("open_gopro.gopro_base", "GoProBase._build_http_request_args"),
        ("open_gopro.gopro_base", "GoProBase._requests_session"),
        ("open_gopro.gopro_base", "GoProBase.is_open"),  # reads _open (instance attribute, see mark_open)
        ("open_gopro", "WiredGoPro._base_url"),
        ("open_gopro", "WiredGoPro._MDNS_SERVICE_NAME"),
        ("open_gopro", "WirelessGoPro._base_url"),
        ("open_gopro", "WirelessGoPro._handle_cohn"),
        ("open_gopro", "WirelessGoPro.is_http_connected"),  # reads _is_cohn_configured (see mark_cohn_configured)
        ("open_gopro.features", "CohnFeature.configure"),
        ("open_gopro.features", "CohnFeature.is_configured"),
        ("open_gopro.features", "CohnFeature.credentials"),
        ("open_gopro.features", "AccessPointFeature.connect"),
        ("open_gopro.api.builders", "BleSettingFacade"),
        ("open_gopro.api.builders", "HttpSetting"),
        ("open_gopro.domain.communicator_interface", "HttpMessage"),
        ("open_gopro.models.constants", "Toggle"),
        ("open_gopro.models.constants", "CameraControl"),
        ("open_gopro.models.constants", "SettingId"),
        ("open_gopro.models", "proto.EnumPresetGroup"),
        ("open_gopro.models", "proto.EnumPresetTitle"),
        ("open_gopro.models", "proto.EnumCameraControlStatus"),
        ("open_gopro.models", "proto.EnumLiveStreamStatus"),
        ("open_gopro.models", "proto.EnumRegisterLiveStreamStatus"),
        ("open_gopro.models", "proto.EnumCOHNStatus"),
        ("open_gopro.models", "proto.EnumCOHNNetworkState"),
        ("open_gopro.util", "get_current_dst_aware_time"),
        ("open_gopro.network.wifi.controller", "WifiController"),
        ("open_gopro.network.wifi.controller", "SsidState"),
        ("open_gopro.network.wifi.mdns_scanner", "find_first_ip_addr"),
        ("open_gopro", "WirelessGoPro.Interface"),
        ("open_gopro", "WirelessGoPro.keep_alive"),
        ("returns.pipeline", "is_successful"),
    ]
    + [
        ("open_gopro.api", f"HttpCommands.{m}")
        for m in (
            "wired_usb_control",
            "set_third_party_client_info",
            "set_camera_control",
            "set_keep_alive",
            "get_open_gopro_api_version",
            "set_shutter",
            "load_preset",
            "load_preset_group",
            "get_preset_status",
            "set_date_time",
            "set_preview_stream",
            "set_turbo_mode",
            "delete_file",
            "get_gpmf_data",
            "get_thumbnail",
            "get_screennail",
            "get_telemetry",
        )
    ]
    + [
        ("open_gopro.api", f"BleCommands.{m}")
        for m in (
            "set_third_party_client_info",
            "set_camera_control",
            "get_open_gopro_api_version",
            "get_hardware_info",
            "set_shutter",
            "tag_hilight",
            "sleep",
            "power_down",
            "enable_wifi_ap",
            "set_turbo_mode",
            "get_camera_statuses",
            "get_camera_settings",
            "get_camera_capabilities",
            "get_observable_for_all_statuses",
            "load_preset",
            "load_preset_group",
            "get_preset_status",
            "get_last_captured_media",
            "set_date_time_tz_dst",
            "get_date_time_tz_dst",
            "register_livestream_status",
            "set_livestream_mode",
            "cohn_get_status",
        )
    ]
)


def sdk_version() -> str:
    try:
        return importlib.metadata.version("open_gopro")
    except importlib.metadata.PackageNotFoundError:
        return ""


def missing_requirements() -> list[str]:
    missing = []
    for module, path in REQUIRED:
        try:
            obj: Any = importlib.import_module(module)
        except ImportError:
            missing.append(f"{module} (module)")
            continue
        for part in path.split("."):
            if not hasattr(obj, part):
                missing.append(f"{module}.{path}")
                break
            obj = getattr(obj, part)
    return missing


@cache
def check_sdk() -> str:
    """Raise SdkIncompatible unless the installed SDK has everything the driver uses; return its version."""
    version = sdk_version()
    if not version:
        raise SdkIncompatible("open_gopro is not installed (pip install -r gopro_camera/requirements.txt)")
    missing = missing_requirements()
    if missing:
        raise SdkIncompatible(
            f"open_gopro {version} lacks what the driver uses ({', '.join(missing)}); "
            f"install the supported version {SUPPORTED_SDK}.x (gopro_camera/requirements.txt)"
        )
    return version


def version_warning() -> str:
    """Non-empty if the installed SDK passes check_sdk() but is not the tested major.minor."""
    v = sdk_version()
    if v and ".".join(v.split(".")[:2]) != SUPPORTED_SDK:
        return f"open_gopro {v} is not the tested version {SUPPORTED_SDK}.x; behaviour may differ"
    return ""


# ------------------------------------------------------------------------------------------ accessors
def get_json(sdk: Any, message: Any, **kwargs: Any) -> Any:
    """GET an endpoint the SDK does not wrap, through the SDK's own HTTP transport."""
    return sdk._get_json(message, **kwargs)


def download_request(sdk: Any, message: Any) -> tuple[str, dict[str, Any], Any]:
    """(url, request kwargs, requests session) for streaming ``message`` ourselves."""
    handle_cohn = getattr(sdk, "_handle_cohn", None)  # WirelessGoPro only (COHN auth headers / cert)
    if handle_cohn is not None:
        message = handle_cohn(message)
    return sdk._base_url + message.build_url(), sdk._build_http_request_args(message), sdk._requests_session


def base_url(sdk: Any) -> str:
    return str(sdk._base_url)


def mark_open(sdk: Any) -> None:
    """Treat a WiredGoPro as opened without ``open()`` (which waits for encoding to stop)."""
    sdk._open = True
    if not sdk.is_open:  # instance attribute: only its effect can be checked
        raise SdkIncompatible(f"open_gopro {sdk_version()}: cannot mark the wired SDK object open (_open)")


def mark_cohn_configured(sdk: Any) -> None:
    """COHN-only: the SDK sets this inside ``open()`` only when BLE is connected."""
    sdk._is_cohn_configured = True
    if not sdk.is_http_connected:
        raise SdkIncompatible(f"open_gopro {sdk_version()}: cannot mark COHN configured (_is_cohn_configured)")


def mdns_service_name() -> str:
    from open_gopro import WiredGoPro

    return str(WiredGoPro._MDNS_SERVICE_NAME)


def setting_facade_id(facade: Any) -> int:
    return int(facade._identifier)


def make_wired(serial: str, base_url_override: str | None) -> Any:
    """``WiredGoPro`` (state not maintained), optionally with an overridden base URL (tests, NAT)."""
    from urllib.parse import urlparse

    from open_gopro import WiredGoPro

    class _WiredGoPro(WiredGoPro):
        @property
        def _base_url(self) -> str:
            return base_url_override if base_url_override else super()._base_url

        @property
        def ip_address(self) -> str:
            return (urlparse(base_url_override).hostname or "") if base_url_override else super().ip_address

    return _WiredGoPro(serial, maintain_state=False)
