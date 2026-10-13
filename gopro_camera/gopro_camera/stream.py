"""Live image stream receiver (webcam / preview) using PyAV, plus camera_info loading.

The camera sends H.264 in MPEG-TS over UDP (webcam "TS" protocol and the Wi-Fi preview stream) or
serves RTSP (webcam "RTSP" protocol). This module demuxes it in a dedicated thread and hands out:

- every H.264 access unit as-is (passthrough; cheap to record), and/or
- decoded frames (optionally downscaled and rate-limited).

TIMESTAMPS: frames are stamped with *receive time minus a configured latency offset*. GoPro stream
latency is roughly 1-2 s and varies with mode, resolution and link; it is NOT suitable for tight
sensor fusion. Use the recording_event sync anchors + SD footage for accurate offline alignment.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

READ_TIMEOUT = 2.0  # seconds without data before PyAV gives up a read (and we reopen / notice stop)
# Bound FFmpeg's stream analysis. The HERO13 preview carries HEVC plus audio and an unidentified data
# stream at ~3 Mbit/s while recording; with the default probe (5 MB) opening took ~15 s, with this ~2 s.
UDP_PROBE = {"probesize": "500000", "analyzeduration": "1000000"}

PacketCb = Callable[[bytes, bool, int], None]  # data (Annex-B), keyframe, receive time ns
FrameCb = Callable[[Any, int], None]  # ndarray (H, W, 3) bgr24, receive time ns


def stream_url(mode: str, protocol: str, port: int, camera_ip: str) -> str:
    """URL to receive from. Webcam TS/preview are UDP pushed to the host; RTSP is served by the camera."""
    if mode == "webcam" and protocol.upper() == "RTSP":
        return f"rtsp://{camera_ip}:554/live"
    # overrun_nonfatal/fifo_size: keep demuxing through bursts instead of aborting.
    return f"udp://0.0.0.0:{port}?overrun_nonfatal=1&fifo_size=50000000"


@dataclass
class StreamStats:
    packets: int = 0
    frames_decoded: int = 0
    frames_published: int = 0
    decode_errors: int = 0
    pts_gaps: int = 0  # missing frames inferred from PTS discontinuities
    reconnects: int = 0
    last_packet_mono: float = 0.0


class StreamReceiver:
    def __init__(
        self,
        url: str,
        on_packet: PacketCb | None,
        on_frame: FrameCb | None,
        *,
        max_rate: float = 0.0,
        scale: float = 1.0,
        open_timeout: float = 10.0,
        now_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        self.url = url
        self.on_packet = on_packet
        self.on_frame = on_frame
        self.max_rate = max_rate
        self.scale = scale
        self.open_timeout = open_timeout
        self.now_ns = now_ns
        self.stats = StreamStats()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._container: Any = None
        self.width = 0
        self.height = 0
        self.codec = ""  # FFmpeg codec name of the video stream once open: "h264", "hevc", ...

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def last_packet_age(self) -> float | None:
        if not self.stats.last_packet_mono:
            return None
        return time.monotonic() - self.stats.last_packet_mono

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="gopro-stream", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        # Never touch the container from this thread (PyAV is not thread-safe): the demux thread sees
        # the flag after at most READ_TIMEOUT and closes it itself.
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
        self._thread = None

    def _run(self) -> None:
        import av

        while not self._stop.is_set():
            try:
                options = {"rtsp_transport": "tcp"} if self.url.startswith("rtsp") else dict(UDP_PROBE)
                self._container = av.open(self.url, options=options, timeout=(self.open_timeout, READ_TIMEOUT))
                self._demux(self._container)
            except Exception as e:  # av.error.* / OSError: retry until stopped
                if self._stop.is_set():
                    break
                self.stats.reconnects += 1
                logger.warning("stream %s: %s; reopening", self.url, e)
                self._stop.wait(1.0)
            finally:
                if self._container is not None:
                    try:
                        self._container.close()
                    except Exception:
                        pass
                    self._container = None

    def _demux(self, container: Any) -> None:
        vstream = container.streams.video[0]
        self.codec = vstream.codec_context.name if vstream.codec_context else ""
        vstream.thread_type = "AUTO"
        frame_dur = None
        if vstream.average_rate and vstream.time_base:
            frame_dur = 1.0 / float(vstream.average_rate) / float(vstream.time_base)
        last_pts: int | None = None
        min_period = 1.0 / self.max_rate if self.max_rate > 0 else 0.0
        last_pub = 0.0
        for packet in container.demux(vstream):
            if self._stop.is_set():
                return
            if packet.size == 0:
                continue
            recv = self.now_ns()
            self.stats.packets += 1
            self.stats.last_packet_mono = time.monotonic()
            if packet.pts is not None and last_pts is not None and frame_dur:
                gap = round((packet.pts - last_pts) / frame_dur) - 1
                if 0 < gap < 1000:
                    self.stats.pts_gaps += gap
            if packet.pts is not None:
                last_pts = packet.pts
            if self.on_packet is not None:
                self.on_packet(bytes(packet), bool(packet.is_keyframe), recv)
            if self.on_frame is None:
                continue
            try:
                frames = packet.decode()
            except Exception:
                self.stats.decode_errors += 1
                continue
            for frame in frames:
                self.stats.frames_decoded += 1
                now = time.monotonic()
                if min_period and now - last_pub < min_period:
                    continue
                last_pub = now
                w, h = frame.width, frame.height
                if self.scale != 1.0:
                    w, h = max(2, int(w * self.scale) // 2 * 2), max(2, int(h * self.scale) // 2 * 2)
                self.width, self.height = w, h
                img = frame.reformat(width=w, height=h, format="bgr24").to_ndarray()
                self.on_frame(img, recv)
                self.stats.frames_published += 1


# ---------------------------------------------------------------------------------- camera_info
def resolve_url(url: str) -> Path | None:
    """file:///abs, package://pkg/rel or a plain path -> Path (None if empty)."""
    if not url:
        return None
    if url.startswith("file://"):
        return Path(url[len("file://") :])
    if url.startswith("package://"):
        from ament_index_python.packages import get_package_share_directory

        pkg, _, rel = url[len("package://") :].partition("/")
        return Path(get_package_share_directory(pkg)) / rel
    return Path(os.path.expanduser(url))


def load_calibration(url: str) -> dict[str, Any] | None:
    """Load a ROS camera_calibration YAML (image_width/height, camera_matrix, distortion_model, ...).

    GoPro lenses are fisheye; calibrate with the ``equidistant`` model (4 coefficients).
    """
    path = resolve_url(url)
    if path is None:
        return None
    import yaml

    data = yaml.safe_load(path.read_text())

    def mat(key: str) -> list[float]:
        v = data.get(key) or {}
        return [float(x) for x in (v.get("data") if isinstance(v, dict) else v) or []]

    return {
        "width": int(data["image_width"]),
        "height": int(data["image_height"]),
        "camera_name": str(data.get("camera_name", "")),
        "distortion_model": str(data.get("distortion_model", "equidistant")),
        "K": mat("camera_matrix"),
        "D": mat("distortion_coefficients"),
        "R": mat("rectification_matrix") or [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
        "P": mat("projection_matrix"),
    }


def scale_calibration(calib: dict[str, Any], width: int, height: int) -> dict[str, Any]:
    """Rescale intrinsics when the published image size differs from the calibrated size."""
    if not width or not height or (width, height) == (calib["width"], calib["height"]):
        return calib
    sx, sy = width / calib["width"], height / calib["height"]
    out = dict(calib, width=width, height=height)
    k = list(calib["K"])
    if len(k) == 9:
        k[0] *= sx
        k[2] *= sx
        k[4] *= sy
        k[5] *= sy
    out["K"] = k
    p = list(calib["P"])
    if len(p) == 12:
        p[0] *= sx
        p[2] *= sx
        p[5] *= sy
        p[6] *= sy
    out["P"] = p
    return out
