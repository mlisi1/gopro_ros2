"""media.offload against the fake camera: copy / move, and every safety rule of gopro_camera.offload."""

import asyncio
import json
from pathlib import Path

import numpy as np
import pytest
from gopro_camera.offload import OffloadConfig, Offloader, verify_copy

av = pytest.importorskip("av")


@pytest.fixture(scope="module")
def mp4_bytes(tmp_path_factory) -> bytes:
    """A real (small) MP4, so verification can open it: 2 s of noise, ~0.5 MB."""
    path = tmp_path_factory.mktemp("mp4") / "v.mp4"
    rng = np.random.default_rng(0)
    with av.open(str(path), "w") as c:
        vs = c.add_stream("mpeg4", rate=15)
        vs.width, vs.height, vs.pix_fmt, vs.bit_rate = 160, 120, "yuv420p", 2_000_000
        for _ in range(30):
            img = rng.integers(0, 255, (120, 160, 3), dtype=np.uint8)
            for pkt in vs.encode(av.VideoFrame.from_ndarray(img, format="rgb24")):
                c.mux(pkt)
        for pkt in vs.encode():
            c.mux(pkt)
    return path.read_bytes()


def _offloader(cam, dest: Path, mode: str = "copy", **kw) -> Offloader:
    cfg = OffloadConfig(
        mode=mode, dest=dest, min_free_bytes=0, retry_backoff=(0.2,), idle_poll=0.05, max_attempts=2, **kw
    )
    off = Offloader(cam, cfg, "C3501324500123")
    cam.offloader = off
    off.start()
    return off


async def _until(pred, timeout: float = 15.0) -> None:
    end = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > end:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.05)


async def _record(cam, seconds: float = 0.3) -> str:
    await cam.start_recording()
    await asyncio.sleep(seconds)
    return (await cam.stop_recording()).filename


def _journal(off: Offloader) -> dict[str, dict]:
    return {f["path"]: f for f in json.loads(off.journal_path.read_text())["files"]}


def test_verify_copy(tmp_path: Path, mp4_bytes: bytes):
    good = tmp_path / "GX010001.MP4"
    good.write_bytes(mp4_bytes)
    assert verify_copy(good, len(mp4_bytes)) == (True, "")
    assert not verify_copy(good, len(mp4_bytes) + 1)[0]  # size differs from the SD
    junk = tmp_path / "GX010002.MP4"
    junk.write_bytes(b"\x01" * 1000)
    assert not verify_copy(junk, 1000)[0]  # right size but not a readable MP4
    assert not verify_copy(tmp_path / "missing.MP4", 1)[0]


def test_copy_keeps_sd_file(make_camera, mock_cam, tmp_path: Path, mp4_bytes: bytes):
    mock_cam.state.video_template = mp4_bytes
    cam, _ = make_camera()

    async def go():
        await cam.connect()
        off = _offloader(cam, tmp_path, "copy")
        path = await _record(cam)
        await _until(lambda: off.summary()["done"] == 1)
        await off.stop()
        await cam.disconnect()
        return off, path

    off, path = asyncio.run(go())
    local = tmp_path / path
    assert local.read_bytes() == mp4_bytes
    assert path in mock_cam.state.files  # copy: SD untouched
    assert _journal(off)[path]["state"] == "copied"
    assert not list(tmp_path.rglob("*.part"))


def test_move_deletes_only_after_verification(make_camera, mock_cam, tmp_path: Path, mp4_bytes: bytes):
    mock_cam.state.video_template = mp4_bytes
    cam, _ = make_camera()

    async def go():
        await cam.connect()
        off = _offloader(cam, tmp_path, "move")
        path = await _record(cam)
        await _until(lambda: off.summary()["done"] == 1)
        await off.stop()
        await cam.disconnect()
        return off, path

    off, path = asyncio.run(go())
    assert (tmp_path / path).read_bytes() == mp4_bytes
    assert path not in mock_cam.state.files
    assert _journal(off)[path]["state"] == "moved"
    assert "100GOPRO/GX010001.MP4" in mock_cam.state.files  # pre-existing footage is never touched


def test_corrupt_copy_never_deletes_sd(make_camera, mock_cam, tmp_path: Path, mp4_bytes: bytes):
    mock_cam.state.video_template = mp4_bytes
    mock_cam.state.serve_wrong_bytes = True
    cam, _ = make_camera()

    async def go():
        await cam.connect()
        off = _offloader(cam, tmp_path, "move")
        path = await _record(cam)
        await _until(lambda: off.summary()["failed"] == 1)
        await off.stop()
        await cam.disconnect()
        return off, path

    off, path = asyncio.run(go())
    assert path in mock_cam.state.files  # SD file kept
    assert not (tmp_path / path).exists()  # bad local copy removed
    entry = _journal(off)[path]
    assert entry["state"] == "failed" and "verification failed" in entry["error"]


def test_never_reads_sd_while_recording(make_camera, mock_cam, tmp_path: Path, mp4_bytes: bytes):
    """A transfer in progress stops *before* a driver start_recording is sent, and resumes when idle."""
    mock_cam.state.video_template = mp4_bytes
    mock_cam.state.download_rate = len(mp4_bytes) / 3  # ~3 s per file
    cam, _ = make_camera()

    async def go():
        await cam.connect()
        off = _offloader(cam, tmp_path, "move")
        first = await _record(cam)
        await _until(lambda: off.current == first)
        await asyncio.sleep(0.5)  # mid-transfer
        second = await _record(cam, 1.0)
        assert mock_cam.state.sd_reads_while_encoding == 0  # stopped before the shutter command
        assert mock_cam.state.downloads_started_while_encoding == 0
        await _until(lambda: off.summary()["done"] == 2, timeout=30)
        await off.stop()
        await cam.disconnect()
        return first, second

    first, second = asyncio.run(go())
    assert mock_cam.state.sd_reads_while_encoding == 0
    for p in (first, second):
        assert (tmp_path / p).read_bytes() == mp4_bytes and p not in mock_cam.state.files


def test_button_start_stops_transfer_within_a_poll(make_camera, mock_cam, tmp_path: Path, mp4_bytes: bytes):
    """A start from the camera button is only seen through status polling (5 Hz here)."""
    mock_cam.state.video_template = mp4_bytes
    mock_cam.state.download_rate = len(mp4_bytes) / 4
    cam, _ = make_camera()

    async def go():
        await cam.connect()
        off = _offloader(cam, tmp_path, "copy")
        first = await _record(cam)
        await _until(lambda: off.current == first)
        await asyncio.sleep(0.5)
        mock_cam.external_shutter(True)
        await _until(lambda: cam.encoding)  # seen by the next status poll
        await _until(lambda: not off._sd_active, timeout=1.0)  # driver stopped reading right away
        await asyncio.sleep(1.0)
        assert not off._sd_active and mock_cam.state.downloads_started_while_encoding == 0  # nothing new
        mock_cam.external_shutter(False)
        await _until(lambda: off.summary()["done"] == 2, timeout=30)  # both recordings, once idle
        await off.stop()
        await cam.disconnect()

    asyncio.run(go())


def test_crash_mid_transfer_keeps_sd_and_resumes(make_camera, mock_cam, tmp_path: Path, mp4_bytes: bytes):
    mock_cam.state.video_template = mp4_bytes
    mock_cam.state.download_rate = len(mp4_bytes) / 3
    cam, _ = make_camera()

    async def go():
        await cam.connect()
        off = _offloader(cam, tmp_path, "move")
        path = await _record(cam)
        await _until(lambda: off.current == path)
        await asyncio.sleep(0.5)
        await off.stop()  # node killed mid-transfer
        assert path in mock_cam.state.files and not (tmp_path / path).exists()
        assert _journal(off)[path]["state"] == "copying"
        mock_cam.state.download_rate = 0
        off2 = _offloader(cam, tmp_path, "move")  # driver restarted: resume from the journal
        await _until(lambda: off2.summary()["done"] == 1)
        await off2.stop()
        await cam.disconnect()
        return path

    path = asyncio.run(go())
    assert (tmp_path / path).read_bytes() == mp4_bytes and path not in mock_cam.state.files


def test_resume_verified_reverifies_before_delete(make_camera, mock_cam, tmp_path: Path, mp4_bytes: bytes):
    """Crash between 'verified' and the SD delete: the local copy is checked again first."""
    path = "100GOPRO/GX010042.MP4"
    mock_cam.add_file(path, mp4_bytes)
    local = tmp_path / path
    local.parent.mkdir(parents=True)
    local.write_bytes(mp4_bytes[:1000])  # local copy went bad meanwhile
    journal = tmp_path / ".gopro_offload_C3501324500123.json"
    journal.write_text(json.dumps({"version": 1, "files": [{"path": path, "mode": "move", "state": "verified"}]}))
    cam, _ = make_camera()

    async def go():
        await cam.connect()
        off = _offloader(cam, tmp_path, "move")
        await _until(lambda: off.summary()["done"] == 1)
        await off.stop()
        await cam.disconnect()

    asyncio.run(go())
    assert local.read_bytes() == mp4_bytes  # copied again, verified, then deleted from the SD
    assert path not in mock_cam.state.files


def test_low_disk_space_waits_and_keeps_sd(make_camera, mock_cam, tmp_path: Path, mp4_bytes: bytes):
    mock_cam.state.video_template = mp4_bytes
    cam, _ = make_camera()

    async def go():
        await cam.connect()
        off = _offloader(cam, tmp_path, "move")
        off.cfg.min_free_bytes = 10**18
        path = await _record(cam)
        await _until(lambda: "disk space" in off.last_error)
        await off.stop()
        await cam.disconnect()
        return path

    path = asyncio.run(go())
    assert path in mock_cam.state.files and not (tmp_path / path).exists()


def test_all_chapters_offloaded_and_copy_entries_never_deleted(make_camera, mock_cam, tmp_path: Path, mp4_bytes):
    mock_cam.state.video_template = mp4_bytes
    cam, _ = make_camera()

    async def go():
        await cam.connect()
        off = _offloader(cam, tmp_path, "copy")
        path = await _record(cam)  # queued; the offloader runs at the next await
        number = path[-8:-4]
        mock_cam.add_file(f"100GOPRO/GX02{number}.MP4", mp4_bytes)  # second chapter of the same recording
        off.cfg.mode = "move"  # switched later: entries queued as copy stay on the SD
        await _until(lambda: off.summary()["done"] == 2)
        await off.stop()
        await cam.disconnect()
        return path, f"100GOPRO/GX02{number}.MP4"

    first, second = asyncio.run(go())
    assert (tmp_path / first).exists() and (tmp_path / second).exists()
    assert first in mock_cam.state.files and second in mock_cam.state.files
