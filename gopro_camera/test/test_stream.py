import socket
import threading
import time
from pathlib import Path

import pytest
from gopro_camera.stream import StreamReceiver, load_calibration, scale_calibration, stream_url

av = pytest.importorskip("av")
np = pytest.importorskip("numpy")


def _free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _send_ts(port: int, seconds: float, stop: threading.Event) -> None:
    """Encode synthetic H.264 into MPEG-TS and push it over UDP, like the camera does."""
    out = av.open(f"udp://127.0.0.1:{port}?pkt_size=1316", mode="w", format="mpegts")
    st = out.add_stream("libx264", rate=30)
    st.width, st.height, st.pix_fmt = 320, 240, "yuv420p"
    st.options = {"tune": "zerolatency", "preset": "ultrafast", "g": "15"}
    n = int(seconds * 30)
    for i in range(n):
        if stop.is_set():
            break
        img = np.full((240, 320, 3), i % 255, dtype=np.uint8)
        for pkt in st.encode(av.VideoFrame.from_ndarray(img, format="rgb24")):
            out.mux(pkt)
        time.sleep(1 / 30)
    for pkt in st.encode():
        out.mux(pkt)
    out.close()


def test_stream_url():
    assert stream_url("webcam", "RTSP", 8554, "1.2.3.4") == "rtsp://1.2.3.4:554/live"
    assert stream_url("webcam", "TS", 8556, "x").startswith("udp://0.0.0.0:8556")
    assert stream_url("preview", "", 8554, "x").startswith("udp://0.0.0.0:8554")


def test_receiver_passthrough_and_decode():
    port = _free_udp_port()
    packets, frames = [], []
    rx = StreamReceiver(
        f"udp://127.0.0.1:{port}?overrun_nonfatal=1",
        lambda d, k, t: packets.append((len(d), k, t)),
        lambda img, t: frames.append(img.shape),
        max_rate=10.0,
        scale=0.5,
    )
    rx.start()
    time.sleep(0.5)
    stop = threading.Event()
    sender = threading.Thread(target=_send_ts, args=(port, 3.0, stop), daemon=True)
    sender.start()
    sender.join(15)
    time.sleep(0.5)
    rx.stop()
    assert len(packets) > 30, rx.stats
    assert any(k for _, k, _ in packets)  # keyframes flagged
    assert frames and frames[0] == (120, 160, 3)  # scaled
    assert len(frames) < len(packets)  # throttled to max_rate
    assert rx.last_packet_age is not None


def test_calibration_load_and_scale(tmp_path: Path):
    y = tmp_path / "cal.yaml"
    y.write_text(
        "image_width: 1920\nimage_height: 1080\ncamera_name: gopro\n"
        "camera_matrix: {rows: 3, cols: 3, data: [900, 0, 960, 0, 900, 540, 0, 0, 1]}\n"
        "distortion_model: equidistant\n"
        "distortion_coefficients: {rows: 1, cols: 4, data: [0.1, 0.01, 0, 0]}\n"
        "projection_matrix: {rows: 3, cols: 4, data: [900, 0, 960, 0, 0, 900, 540, 0, 0, 0, 1, 0]}\n"
    )
    c = load_calibration(f"file://{y}")
    assert c["distortion_model"] == "equidistant" and len(c["D"]) == 4
    half = scale_calibration(c, 960, 540)
    assert half["K"][0] == 450 and half["K"][2] == 480 and half["P"][5] == 450
    assert load_calibration("") is None
