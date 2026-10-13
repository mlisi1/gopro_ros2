from pathlib import Path

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")
from gopro_tools import timecode as tc  # noqa: E402

MS = 1_791_885_778_857


def _warp(img: np.ndarray) -> np.ndarray:
    """Screen filmed at an angle, rotated camera, slight blur."""
    h, w = img.shape[:2]
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    dst = np.float32([[80, 40], [w - 30, 90], [w - 120, h - 20], [20, h - 90]])
    out = cv2.warpPerspective(img, cv2.getPerspectiveTransform(src, dst), (w, h), borderValue=(90, 90, 90))
    return cv2.GaussianBlur(cv2.rotate(out, cv2.ROTATE_90_CLOCKWISE), (5, 5), 1.5)


def test_encode_checksum_and_unwrap():
    hi, lo, chk = tc.encode(MS)
    assert hi * 1000 + lo == MS % tc.MODULO_MS and chk == tc.checksum(hi, lo)
    assert tc.unwrap(MS % tc.MODULO_MS, MS + 40_000) == MS
    # Reference just after a wrap of the 1000 s window
    assert tc.unwrap(999_990, 1_791_886_000_005) == 1_791_885_999_990


def test_inliers_drop_a_late_ack():
    # first frame - ack [s] of a real HERO13/USB run: recording 7 had an ack ~40 ms late
    v = [-0.0445, -0.045, -0.042, -0.051, -0.041, -0.0438, -0.0396, -0.0813, -0.0352, -0.034]
    keep = tc.inliers(v)
    assert keep == [True] * 7 + [False] + [True] * 2
    assert tc.inliers([0.0, 0.0, 0.0, 0.005]) == [True] * 4  # 10 ms floor when the spread is tiny


def test_decode_rendered_and_warped():
    assert tc.decode(tc.render(MS)) == MS % tc.MODULO_MS
    assert tc.decode(_warp(tc.render(MS))) == MS % tc.MODULO_MS
    assert tc.decode(np.full((720, 1280, 3), 255, np.uint8)) is None


@pytest.mark.parametrize("delta_ms", [1, 17, 33, 1000])
def test_blended_refreshes_are_never_misread(delta_ms):
    a, b = tc.render(MS), tc.render(MS + delta_ms)
    blend = ((a.astype(np.uint16) + b) // 2).astype(np.uint8)
    assert tc.decode(blend) in (None, MS % tc.MODULO_MS, (MS + delta_ms) % tc.MODULO_MS)


def test_page_matches_python_layout():
    # The page draws the same dictionaries and checksum as render()/decode().
    assert all(name in tc.markers_json() for name in tc.DICTS)
    assert "(7 * hi + 13 * lo + 101) % 1000" in tc.PAGE


def test_first_frame_time_from_video(tmp_path: Path):
    av = pytest.importorskip("av")
    fps, first_ms, lag_s = 30, MS, 0.015
    path = tmp_path / "GX010001.MP4"
    with av.open(str(path), "w") as c:
        vs = c.add_stream("mpeg4", rate=fps)
        vs.width, vs.height, vs.pix_fmt, vs.bit_rate = 1280, 720, "yuv420p", 8_000_000
        for i in range(12):
            img = tc.render(first_ms + round(i * 1000 / fps)) if i >= 2 else np.full((720, 1280, 3), 40, np.uint8)
            frame = av.VideoFrame.from_ndarray(img, format="bgr24")
            for pkt in vs.encode(frame):
                c.mux(pkt)
        for pkt in vs.encode():
            c.mux(pkt)
    ff = tc.first_frame_time(path, reference_s=first_ms / 1000 - 0.5, display_lag_s=lag_s)
    assert ff is not None and len(ff.estimates) == 5
    # frames 0-1 unreadable; every readable frame maps back to frame 0 (+ display lag), within 1 ms
    assert abs(ff.t_s - (first_ms / 1000 + lag_s)) < 0.002
    assert ff.spread_s < 0.002
