"""Contract with the open_gopro SDK: everything the driver uses exists, private use stays isolated."""

import re
from pathlib import Path

import pytest

pytest.importorskip("open_gopro")

from gopro_camera.transports import sdk_compat
from gopro_camera.transports.ble import BleChannel
from gopro_camera.transports.http import HttpChannel

PKG = Path(sdk_compat.__file__).resolve().parents[1]
REQUIREMENTS = Path(__file__).resolve().parents[1] / "requirements.txt"  # gopro_camera/requirements.txt


def test_installed_sdk_has_everything_the_driver_uses():
    assert sdk_compat.missing_requirements() == []
    assert sdk_compat.check_sdk() == sdk_compat.sdk_version()
    assert sdk_compat.version_warning() == ""


def test_supported_version_matches_requirements_pin():
    pin = re.search(r"^open_gopro==(\d+\.\d+)", REQUIREMENTS.read_text(), re.M)
    assert pin and pin.group(1) == sdk_compat.SUPPORTED_SDK


def test_missing_member_is_reported_clearly(monkeypatch):
    monkeypatch.setattr(sdk_compat, "REQUIRED", [("open_gopro", "WiredGoPro._renamed_in_a_new_sdk")])
    sdk_compat.check_sdk.cache_clear()
    try:
        with pytest.raises(sdk_compat.SdkIncompatible, match=r"WiredGoPro\._renamed_in_a_new_sdk"):
            sdk_compat.check_sdk()
    finally:
        sdk_compat.check_sdk.cache_clear()


def test_flags_set_on_instances_take_effect():
    sdk = sdk_compat.make_wired("123", "http://127.0.0.1:9/")
    assert not sdk.is_open
    sdk_compat.mark_open(sdk)
    assert sdk.is_open
    assert sdk_compat.base_url(sdk) == "http://127.0.0.1:9/"
    assert sdk.ip_address == "127.0.0.1"
    assert sdk_compat.mdns_service_name()


def test_no_private_sdk_access_outside_sdk_compat():
    pattern = re.compile(r"sdk\._[a-z]|GoPro\._[A-Za-z]|\._identifier|_handle_cohn|_requests_session")
    offenders = [
        f"{p.relative_to(PKG)}:{i}"
        for p in PKG.rglob("*.py")
        if p.name != "sdk_compat.py"
        for i, line in enumerate(p.read_text().splitlines(), 1)
        if pattern.search(line)
    ]
    assert offenders == []


def test_every_routed_command_exists_on_both_channels():
    """GoProCamera._exec routes ``fn(channel)`` to HTTP or BLE: a method missing on one channel would
    raise AttributeError (a bug) instead of NotSupported (declare it on Channel instead)."""
    src = (PKG / "camera.py").read_text()
    routed = set(re.findall(r"lambda ch: ch\.([a-z_]+)\(", src)) | set(re.findall(r"await ch\.([a-z_]+)\(", src))
    assert routed, "no routed commands found: did _exec change?"
    for name in sorted(routed):
        assert hasattr(HttpChannel, name) and hasattr(BleChannel, name), name
