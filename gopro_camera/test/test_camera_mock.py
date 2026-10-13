"""GoProCamera facade against the fake HTTP camera (no hardware)."""

import asyncio
from pathlib import Path

import pytest
from gopro_camera.errors import CameraBusy, CameraRejected, InvalidSetting, NotSupported
from gopro_camera.settings_map import SETTING_BY_NAME
from gopro_camera.status import S

from gopro_camera import camera as camera_mod


def run(coro):
    return asyncio.run(coro)


def test_connect_reads_info_and_state(make_camera, mock_cam):
    cam, rec = make_camera()

    async def go():
        await cam.connect()
        assert cam.connected
        assert cam.transport_name == "usb"
        assert cam.model_name == "HERO13 Black"
        assert cam.hardware_summary()["firmware_supported"] is True
        assert S.BATTERY_PERCENT in cam.statuses
        assert mock_cam.state.wired_usb and mock_cam.state.control == 2  # external control claimed
        assert mock_cam.state.camera_dt[0] != "2024_1_1"  # date/time synced
        await cam.disconnect()

    run(go())
    assert rec.links[0][0] is True and rec.links[-1][0] is False


def test_record_hilight_stop_produces_sync_anchors(make_camera, mock_cam):
    cam, rec = make_camera()

    async def go():
        await cam.connect()
        start = await cam.start_recording()
        assert start.success, start.message
        assert start.command_sent_ns <= start.camera_ack_ns
        assert start.command_sent_ns <= start.encoding_changed_ns  # may precede the ack (fast polling)
        with pytest.raises(CameraBusy):
            await cam.start_recording()
        hl = await cam.hilight()
        assert hl.transport == "usb"
        stop = await cam.stop_recording()
        assert stop.success, stop.message
        assert stop.filename.startswith("100GOPRO/GX01")
        await cam.disconnect()

    run(go())
    kinds = [e.event for e in rec.events]
    assert kinds == [camera_mod.EV_START, camera_mod.EV_HILIGHT, camera_mod.EV_STOP]
    assert mock_cam.state.hilights


def test_anchors_with_hero13_timing(make_camera, mock_cam):
    """HERO13/USB timing: the start reply comes ~470 ms after the command, state queries are held
    meanwhile, and encoding stays on after a stop while the file is finalised. With 1 Hz idle polling
    the driver must sample right after the reply (start) and at the fast rate (stop)."""
    cam, _ = make_camera(status_poll_rate=1.0)
    mock_cam.state.busy_time = 0.43
    mock_cam.state.shutter_reply_delay = 0.04
    mock_cam.state.stop_finalize_time = 0.5

    async def go():
        await cam.connect()
        await asyncio.sleep(0.5)  # land mid-way through an idle 1 s poll period
        start = await cam.start_recording()
        await asyncio.sleep(1.0)
        stop = await cam.stop_recording()
        await cam.disconnect()
        return start, stop

    start, stop = run(go())
    for ev in (start, stop):
        assert ev.success, ev.message
        assert ev.command_sent_ns <= ev.encoding_changed_ns
        # capped: the change cannot precede the command
        assert ev.encoding_changed_uncertainty <= (ev.encoding_changed_ns - ev.command_sent_ns) / 1e9 + 1e-6
    assert (start.camera_ack_ns - start.command_sent_ns) / 1e9 > 0.4
    assert (start.encoding_changed_ns - start.camera_ack_ns) / 1e9 < 0.15  # held query answered with the reply
    assert (stop.encoding_changed_ns - stop.camera_ack_ns) / 1e9 > 0.4  # finalising, not the end of footage
    assert stop.encoding_changed_uncertainty <= 0.25  # fast polling while waiting for the change
    assert cam._fast_poll_until == 0.0  # back to the configured rate once the command is confirmed


def test_hilight_requires_recording(make_camera):
    cam, _ = make_camera()

    async def go():
        await cam.connect()
        with pytest.raises(CameraRejected):
            await cam.hilight()
        await cam.disconnect()

    run(go())


def test_settings_validation_and_reject_while_encoding(make_camera, mock_cam):
    cam, _ = make_camera()
    res = SETTING_BY_NAME["video_resolution"].id

    async def go():
        await cam.connect()
        await cam.set_setting(res, 9)  # 1080
        assert mock_cam.state.settings[res] == 9
        with pytest.raises(InvalidSetting):
            await cam.set_setting(res, 31)  # 8K: spec says not on HERO13
        await cam.start_recording()
        with pytest.raises(CameraBusy):
            await cam.set_setting(res, 1)
        await cam.stop_recording()
        await cam.disconnect()

    run(go())


def test_batch_settings_rollback(make_camera, mock_cam):
    cam, _ = make_camera()
    res = SETTING_BY_NAME["video_resolution"].id
    lcd = SETTING_BY_NAME["lcd_brightness"].id

    async def go():
        await cam.connect()
        await cam.refresh_state()
        before = mock_cam.state.settings[res]
        # make the second item fail on the camera side only (passes local validation)
        mock_cam.state.model_name = "HERO9 Black"
        applied, failed, rolled_back = await cam.set_settings([(res, 4), (SETTING_BY_NAME["video_lens"].id, 12)])
        assert applied == [(res, 4)] and failed and rolled_back
        assert mock_cam.state.settings[res] == before
        _, failed, _ = await cam.set_settings([(lcd, 5)])  # out of spec range 10-100
        assert failed
        await cam.disconnect()

    run(go())


def test_external_recording_and_adoption(make_camera, mock_cam):
    cam, rec = make_camera()

    async def go():
        await cam.connect()
        mock_cam.external_shutter(True)
        for _ in range(50):
            await asyncio.sleep(0.1)
            if cam.encoding:
                break
        assert cam.encoding
        await cam.disconnect()
        # "driver restart" while recording: adopted, never stopped
        cam2, rec2 = make_camera()
        await cam2.connect()
        assert cam2.encoding and mock_cam.state.statuses[S.ENCODING] == 1
        await cam2.disconnect()
        assert mock_cam.state.statuses[S.ENCODING] == 1
        return rec2

    rec2 = run(go())
    assert any(e.event == camera_mod.EV_EXTERNAL_START for e in rec.events)
    assert rec2.events[0].event == camera_mod.EV_ADOPTED


def test_presets_and_photo_guard(make_camera, mock_cam):
    cam, _ = make_camera()

    async def go():
        await cam.connect()
        groups = await cam.get_presets()
        assert {g["name"] for g in groups} == {"video", "photo", "timelapse"}
        with pytest.raises(CameraRejected):
            await cam.take_photo()  # video preset active
        assert await cam.load_preset(group="photo") == 65536
        ev = await cam.take_photo()
        assert ev.success and ev.filename.endswith(".JPG")
        with pytest.raises(CameraRejected):
            await cam.start_recording()  # photo preset active
        await cam.disconnect()

    run(go())


def test_media_list_download_delete(make_camera, mock_cam, tmp_path: Path):
    cam, _ = make_camera()

    async def go():
        await cam.connect()
        items = await cam.list_media()
        assert [i.path for i in items] == ["100GOPRO/GX010001.MP4"]
        assert await cam.list_media(cam.mission_start_camera_epoch()) == []
        progress = []
        _ok, failed = await cam.download_media(
            ["100GOPRO/GX010001.MP4", "100GOPRO/NOPE.MP4"], tmp_path, progress=lambda *a: progress.append(a)
        )
        assert failed == ["100GOPRO/NOPE.MP4"]
        assert (tmp_path / "100GOPRO" / "GX010001.MP4").stat().st_size == 300_000
        assert progress and progress[-1][3] == 300_000
        assert not mock_cam.state.turbo  # turbo disabled again
        gpmf = await cam.get_media_asset("gpmf", "100GOPRO/GX010001.MP4", tmp_path / "g.bin")
        assert Path(gpmf).read_bytes() == b"gpmf-data"
        with pytest.raises(CameraRejected):
            await cam.delete_media(["100GOPRO/GX010001.MP4"], confirm=False)
        deleted, _ = await cam.delete_media(["100GOPRO/GX010001.MP4"], confirm=True)
        assert deleted and not mock_cam.state.files
        await cam.disconnect()

    run(go())


def test_webcam_refused_while_recording(make_camera, mock_cam):
    cam, _ = make_camera()

    async def go():
        await cam.connect()
        assert "started" in await cam.start_stream("webcam", 12, 4, 8554, None)
        assert mock_cam.state.webcam_status == 2
        with pytest.raises(CameraRejected):
            await cam.start_recording()
        await cam.stop_stream()
        assert mock_cam.state.webcam_status == 0
        await cam.start_recording()
        with pytest.raises(CameraBusy):
            await cam.start_stream("webcam", None, None, None, None)
        assert mock_cam.state.statuses[S.ENCODING] == 1  # recording untouched
        await cam.stop_recording()
        await cam.disconnect()

    run(go())


def test_preview_stream_alongside_recording(make_camera, mock_cam):
    """HERO13/USB: the preview stream runs while recording (live view + SD), in either order."""
    cam, _ = make_camera()

    async def go():
        await cam.connect()
        await cam.start_recording()
        assert "started" in await cam.start_stream("preview", None, None, 8556, None)
        assert mock_cam.state.preview_on and mock_cam.state.statuses[S.ENCODING] == 1
        await cam.stop_stream()
        await cam.stop_recording()
        assert "started" in await cam.start_stream("preview", None, None, 8556, None)
        assert (await cam.start_recording()).success  # recording while previewing
        await cam.stop_recording()
        await cam.stop_stream()
        assert not mock_cam.state.preview_on
        await cam.disconnect()

    run(go())


def test_link_loss_and_reconnect_keeps_recording(make_camera, mock_cam):
    cam, rec = make_camera()

    async def go():
        await cam.connect()
        await cam.start_recording()
        mock_cam.set_offline(True)
        for _ in range(100):
            await asyncio.sleep(0.1)
            if not cam.connected:
                break
        assert not cam.connected
        mock_cam.set_offline(False)
        for _ in range(150):
            await asyncio.sleep(0.1)
            if cam.connected:
                break
        assert cam.connected and cam.encoding
        await cam.stop_recording()
        await cam.disconnect()

    run(go())
    assert [c for c, _ in rec.links][:3] == [True, False, True]
    assert any(e.event == camera_mod.EV_ADOPTED for e in rec.events)


def test_sleep_refused_without_ble(make_camera, mock_cam):
    """Over USB alone a sleeping camera cannot be woken (its network interface disappears)."""
    cam, _ = make_camera()

    async def go():
        await cam.connect()
        with pytest.raises(NotSupported, match="BLE"):
            await cam.sleep()
        assert not mock_cam.state.asleep and cam.connected
        await cam.disconnect(sleep=True)  # sleep_on_shutdown: skipped, camera stays awake
        assert not mock_cam.state.asleep

    run(go())


def test_sleep_refused_while_encoding_then_sleeps(make_camera, mock_cam):
    cam, _ = make_camera(allow_sleep_without_ble=True)

    async def go():
        await cam.connect()
        await cam.start_recording()
        with pytest.raises(CameraBusy):
            await cam.sleep()
        await cam.stop_recording()
        await cam.sleep()
        assert mock_cam.state.asleep and cam.sleeping and not cam.connected
        await cam.disconnect()

    run(go())
