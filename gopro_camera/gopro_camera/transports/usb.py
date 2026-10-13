"""USB (wired, HTTP over the camera's USB NCM network interface).

The camera's IP is ``172.2X.1YZ.51`` where XYZ are the last three digits of its serial (official HTTP
spec, "Socket Address"); the SDK's ``WiredGoPro`` derives it the same way. The host side of the NCM
link gets an address in the same /24, so without a configured serial we derive XYZ from the host
interface addresses, and fall back to the SDK's mDNS discovery (``_gopro-web._tcp``).

``WiredGoPro.open()`` is intentionally not used: it waits until the camera stops encoding, which
would make the driver hang (or worse, tempt it to stop recording) when it restarts while the camera
is recording. We perform the same steps the SDK does (enable wired USB control, set third-party
client, check API version) without that wait.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess

from gopro_camera.errors import NotConnected, NotSupported
from gopro_camera.transports import sdk_compat
from gopro_camera.transports.base import TransportConfig
from gopro_camera.transports.bundle import TransportBundle
from gopro_camera.transports.http import HttpChannel

logger = logging.getLogger(__name__)

_USB_HOST_RE = re.compile(r"^172\.2(\d)\.1(\d)(\d)\.\d+$")
SUPPORTED_API_VERSION = "2.0"


def detect_usb_serial_suffixes(interface: str | None = None) -> list[tuple[str, str]]:
    """Return [(interface, serial_suffix)] for host interfaces that look like a GoPro NCM link."""
    try:
        out = subprocess.run(["ip", "-j", "-4", "addr"], capture_output=True, text=True, timeout=5, check=True).stdout
        links = json.loads(out)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return []
    found: list[tuple[str, str]] = []
    for link in links:
        ifname = link.get("ifname", "")
        if interface and ifname != interface:
            continue
        for addr in link.get("addr_info", []):
            m = _USB_HOST_RE.match(addr.get("local", ""))
            if m:
                found.append((ifname, "".join(m.groups())))
    return found


async def resolve_serial(cfg: TransportConfig) -> str:
    if cfg.serial:
        return cfg.serial
    if cfg.usb_base_url:
        return "000"
    candidates = detect_usb_serial_suffixes(cfg.usb_interface)
    if len(candidates) == 1:
        logger.info("USB: found GoPro NCM link on %s (serial ...%s)", *candidates[0])
        return candidates[0][1]
    if len(candidates) > 1:
        raise NotConnected(f"USB: several GoPro NCM links {candidates}; set 'serial' or 'usb_interface' to choose one")
    import open_gopro.network.wifi.mdns_scanner as mdns

    try:
        response = await mdns.find_first_ip_addr(sdk_compat.mdns_service_name(), int(cfg.connect_timeout))
    except Exception as e:
        raise NotConnected(f"USB: no GoPro found (no NCM interface address, mDNS: {e!r})") from e
    return str(response.name.split(".")[0])


async def open_usb(cfg: TransportConfig) -> TransportBundle:
    serial = await resolve_serial(cfg)
    sdk = sdk_compat.make_wired(serial, cfg.usb_base_url)
    http = HttpChannel("usb", sdk, cfg.command_timeout)
    # Same steps as WiredGoPro.open() minus the blocking wait for "not encoding".
    await http.enable_wired_usb_control()
    await http.set_third_party_client()
    version = await http.get_version()
    if version != SUPPORTED_API_VERSION:
        raise NotSupported(f"camera reports Open GoPro API {version}; this driver supports {SUPPORTED_API_VERSION}")
    sdk_compat.mark_open(sdk)  # bypass SDK open(), see module docstring
    http.mark_connected(True)
    logger.info("USB: connected to %s (Open GoPro %s)", sdk_compat.base_url(sdk), version)
    return TransportBundle(name="usb", http=http, api_version=version, sdk_objects=[sdk])
