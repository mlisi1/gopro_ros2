"""Hardware tests against a real camera. Skipped unless ``pytest --hardware``.

Environment:
    GOPRO_TRANSPORT  usb | cohn | ble | wifi_ap   (default usb)
    GOPRO_SERIAL     serial or trailing digits     (needed for cohn/ble, optional for usb)
    GOPRO_BLE_SIDE   1 to also open the BLE side channel
    GOPRO_SOAK_S     soak test duration in seconds; the soak test (long SD recording) only runs
                     when this is set explicitly, e.g. GOPRO_SOAK_S=3600

These tests record to the SD card. They never delete media they did not create.
"""

import asyncio
import itertools
import logging
import os
import statistics
import tempfile
import time
from pathlib import Path

import pytest
from gopro_camera.camera import CameraConfig, GoProCamera
from gopro_camera.status import S, as_int
from gopro_camera.stream import StreamReceiver, stream_url
from gopro_camera.transports import TransportConfig

pytestmark = pytest.mark.hardware
log = logging.getLogger("gopro_hw_test")


@pytest.fixture(autouse=True)
def _propagate_logs():
    """Let pytest's live logging (-o log_cli=true) show test and driver logs (not the SDK's per-request dumps).

    The launch_testing pytest plugin imports ``launch.logging``, whose logger class sets
    ``propagate = False`` on every logger created afterwards, hiding them from pytest's root handlers.
    """
    for name, lg in list(logging.root.manager.loggerDict.items()):
        if isinstance(lg, logging.Logger) and name.startswith("gopro"):
            lg.propagate = True


def _camera() -> GoProCamera:
    tcfg = TransportConfig(
        transport=os.environ.get("GOPRO_TRANSPORT", "usb"),
        serial=os.environ.get("GOPRO_SERIAL") or None,
        ble_sidechannel=os.environ.get("GOPRO_BLE_SIDE") == "1",
    )
    return GoProCamera(CameraConfig(transport=tcfg))


def run(coro, timeout=600):
    return asyncio.run(asyncio.wait_for(coro, timeout))


def test_connect_and_info():
    cam = _camera()

    async def go():
        await cam.connect()
        try:
            hw = cam.hardware_summary()
            assert hw.get("model_name") and hw.get("serial_number")
            assert hw["firmware_supported"], (
                f"firmware {hw.get('firmware_version')} < {hw.get('min_supported_firmware')}"
            )
            assert S.ENCODING in cam.statuses
        finally:
            await cam.disconnect()

    run(go())


def test_settings_round_trip():
    cam = _camera()

    async def go():
        await cam.connect()
        try:
            await cam.refresh_state()
            caps = {c["name"]: c for c in cam.capabilities()}
            sdef = caps.get("led") or caps.get("gps")
            assert sdef, "neither LED nor GPS setting available"
            before = as_int(cam.settings.get(sdef["id"]))
            target = next(o for o in sdef["options"] if o != before)
            await cam.set_setting(sdef["id"], target)
            await cam.refresh_state()
            assert as_int(cam.settings.get(sdef["id"])) == target
            if before >= 0:
                await cam.set_setting(sdef["id"], before)
        finally:
            await cam.disconnect()

    run(go())


def test_record_10s_hilight_and_download():
    cam = _camera()

    async def go():
        await cam.connect()
        try:
            await cam.load_preset(group="video")
            start = await cam.start_recording()
            assert start.success, start.message
            await asyncio.sleep(5)
            await cam.hilight()
            await asyncio.sleep(5)
            stop = await cam.stop_recording()
            assert stop.success and stop.filename, stop.message
            log.info(
                f"anchors: sent->ack {(start.camera_ack_ns - start.command_sent_ns) / 1e6:.0f} ms, "
                f"ack->encoding {(start.encoding_changed_ns - start.camera_ack_ns) / 1e6:.0f} ms "
                f"(+-{start.encoding_changed_uncertainty * 1e3:.0f} ms)"
            )
            if cam.http is not None:
                with tempfile.TemporaryDirectory() as d:
                    ok, failed = await cam.download_media([stop.filename], Path(d))
                    assert ok and not failed
                    assert Path(ok[0]).stat().st_size > 1_000_000
        finally:
            await cam.disconnect()

    run(go())


def test_offload_copy_then_move():
    """Records two short clips: the first is copied (stays on the SD), the second moved (deleted from
    the SD after verification). Only files recorded by this test are ever deleted."""
    from gopro_camera.offload import OffloadConfig, Offloader, verify_copy

    cam = _camera()

    async def record() -> str:
        await cam.load_preset(group="video")
        assert (await cam.start_recording()).success
        await asyncio.sleep(3)
        stop = await cam.stop_recording()
        assert stop.success and stop.filename, stop.message
        return stop.filename

    async def until(pred, timeout=120.0):
        end = time.monotonic() + timeout
        while not pred():
            assert time.monotonic() < end, "offload did not finish"
            await asyncio.sleep(0.5)

    async def go():
        await cam.connect()
        with tempfile.TemporaryDirectory() as d:
            off = Offloader(cam, OffloadConfig(mode="copy", dest=Path(d), min_free_bytes=0), cam.serial)
            cam.offloader = off
            off.start()
            try:
                copied = await record()
                await until(lambda: off.summary()["done"] == 1)
                off.cfg.mode = "move"
                moved = await record()
                await until(lambda: off.summary()["done"] == 2)
                on_sd = {m.path: m.size_bytes for m in await cam.list_media()}
                assert copied in on_sd, "copy mode must keep the SD file"
                assert moved not in on_sd, "move mode deletes the SD file after verification"
                for p in (copied, moved):
                    assert verify_copy(Path(d) / p, (Path(d) / p).stat().st_size)[0]
                log.info(f"offload: copied {copied} (kept on SD), moved {moved}")
            finally:
                await off.stop()
                cam.offloader = None
        await cam.disconnect()

    run(go())


def test_stream_60s_latency_and_drops():
    """Webcam stream for 60 s: frames received, dropped frames (PTS gaps), inter-arrival jitter.

    Absolute latency needs an external reference (see README "Stream latency").
    """
    cam = _camera()
    arrivals: list[float] = []

    async def go():
        await cam.connect()
        try:
            if "webcam_stream" not in cam.features():
                pytest.skip("webcam stream not available on this transport/model")
            await cam.start_stream("webcam", None, None, 8554, "TS")
            rx = StreamReceiver(
                stream_url("webcam", "TS", 8554, cam.http.sdk.ip_address),
                None,
                lambda img, t: arrivals.append(time.monotonic()),
            )
            rx.start()
            await asyncio.sleep(60)
            rx.stop()
            await cam.stop_stream()
            gaps = [b - a for a, b in itertools.pairwise(arrivals)]
            p99 = sorted(gaps)[int(len(gaps) * 0.99)]
            log.info(
                f"frames={len(arrivals)} pts_gaps={rx.stats.pts_gaps} reopens={rx.stats.reconnects} "
                f"mean_dt={statistics.mean(gaps) * 1e3:.1f} ms p99_dt={p99 * 1e3:.1f} ms"
            )
            assert len(arrivals) > 60 * 20
            assert rx.stats.pts_gaps < len(arrivals) * 0.02
        finally:
            await cam.disconnect()

    run(go(), timeout=180)


@pytest.mark.skipif(
    not os.environ.get("GOPRO_SOAK_S"), reason="long recording: set GOPRO_SOAK_S (e.g. 3600) to run the soak test"
)
def test_soak_recording_with_status():
    """Long recording with status monitoring: no link loss, no thermal shutdown."""
    duration = float(os.environ["GOPRO_SOAK_S"])
    cam = _camera()

    async def go():
        await cam.connect()
        try:
            await cam.load_preset(group="video")
            assert (await cam.start_recording()).success
            t0 = time.monotonic()
            worst_age = 0.0
            while time.monotonic() - t0 < duration:
                await asyncio.sleep(10)
                worst_age = max(worst_age, cam.status_age)
                assert cam.connected, "link lost during soak"
                assert cam.encoding, "recording stopped during soak (SD full / overheat?)"
                log.info(
                    f"t={time.monotonic() - t0:.0f}s batt={cam.statuses.get(S.BATTERY_PERCENT)} "
                    f"hot={cam.statuses.get(S.OVERHEATING)} sd_left={cam.statuses.get(S.REMAINING_VIDEO_TIME)}s"
                )
            stop = await cam.stop_recording()
            assert stop.success
            assert worst_age < 10
        finally:
            await cam.disconnect()

    run(go(), timeout=duration + 300)
