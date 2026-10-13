import argparse
import asyncio
from pathlib import Path

import pytest
from gopro_tools.pull import expand_chapters


def test_expand_chapters():
    avail = ["100GOPRO/GX010042.MP4", "100GOPRO/GX020042.MP4", "100GOPRO/GX010043.MP4", "100GOPRO/GOPR0044.JPG"]
    assert expand_chapters(["100GOPRO/GX010042.MP4"], avail) == ["100GOPRO/GX010042.MP4", "100GOPRO/GX020042.MP4"]
    assert expand_chapters(["100GOPRO/GOPR0044.JPG"], avail) == ["100GOPRO/GOPR0044.JPG"]


def test_pull_direct_time_window(tmp_path: Path):
    pytest.importorskip("aiohttp")
    pytest.importorskip("open_gopro")
    from gopro_camera.testing.mock_camera import MockCamera
    from gopro_tools.pull import pull_direct

    cam = MockCamera()
    url = cam.start()
    try:
        cam.add_file("100GOPRO/GX010050.MP4", b"\x02" * 1000)  # created "now" (camera clock)
        args = argparse.Namespace(
            transport="usb",
            serial="",
            usb_base_url=url,
            cohn_credentials=str(tmp_path / "c.json"),
            since="2024-01-01T00:00",
            until=None,
            bag=None,
            event_topic="",
            dest=tmp_path / "dl",
            overwrite=False,
            no_turbo=False,
        )
        assert asyncio.run(pull_direct(args)) == 0
        got = sorted(p.name for p in (tmp_path / "dl" / "100GOPRO").iterdir())
        assert got == ["GX010050.MP4"]  # the mock's 2023 file is outside the window
        assert cam.state.control == 0  # passive: control never claimed
    finally:
        cam.stop()
