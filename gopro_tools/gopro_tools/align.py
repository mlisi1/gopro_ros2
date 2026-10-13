"""gopro-align: map GoPro video / GPMF time to ROS time using a rosbag2.

Coarse alignment: ``~/recording_event`` anchors recorded in the bag. For a START command, video
t=0 is ``camera_ack + start_offset`` when a sync calibration matches the camera model, transport and
frame rate (``gopro-calibrate-sync``; HERO13/USB: ~ -37 ms, +-~25 ms). Otherwise it is the ROS time
the driver first observed the camera's *encoding* status flip (``encoding_changed``, with
``encoding_changed_uncertainty``; over HTTP this comes ~0.2 s late with ~0.6 s uncertainty, because
the camera holds state queries while it starts recording). Chapters (GX01xxxx, GX02xxxx, ...)
follow back to back.

Fine alignment (optional, ``--imu-topic``): cross-correlate the gyro *magnitude* of the GoPro IMU
(GPMF) with the robot IMU in the bag. Magnitudes are rotation invariant, so no camera-IMU extrinsics
are needed. The search window is +/- (anchor uncertainty + ``--max-lag``).

Outputs per recording: ``<file>_frames_ros.csv`` (frame index, video pts, ROS time) and a summary
JSON; optionally a new bag with /gopro/imu (sensor_msgs/Imu), /gopro/gps (NavSatFix) and every Nth
frame as /gopro/image/compressed (JPEG).
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from gopro_tools import sync_calibration as sc
from gopro_tools.gpmf import Telemetry, extract, video_meta

log = logging.getLogger("gopro-align")

# RecordingEvent constants (gopro_camera_interfaces/msg/RecordingEvent)
START, STOP, HILIGHT, PHOTO, ADOPTED, EXTERNAL_START, EXTERNAL_STOP = range(7)
_NAME_RE = re.compile(r"^G([A-Z])(\d{2})(\d{4})\.MP4$", re.IGNORECASE)


@dataclass
class Recording:
    start_ns: int
    uncertainty: float
    stop_ns: int = 0
    filename: str = ""  # camera path of the first chapter, e.g. 100GOPRO/GX010042.MP4
    hilights_ns: list[int] = field(default_factory=list)
    source: str = "event"  # event | ack | adopted | calibrated
    transport: str = ""
    command_sent_ns: int = 0
    camera_ack_ns: int = 0  # START command acknowledgement (0 for external/adopted recordings)
    encoding_changed_ns: int = 0

    @property
    def file_number(self) -> str | None:
        parsed = parse_gopro_name(self.filename)
        return parsed[2] if parsed else None


def parse_gopro_name(path: str) -> tuple[str, int, str] | None:
    """'100GOPRO/GX020042.MP4' -> ('X', 2, '0042'): encoding letter, chapter, file number."""
    m = _NAME_RE.match(Path(path).name)
    if not m:
        return None
    return m.group(1).upper(), int(m.group(2)), m.group(3)


def chapters_for(number: str, mp4s: list[Path]) -> list[Path]:
    found = []
    for p in mp4s:
        parsed = parse_gopro_name(p.name)
        if parsed and parsed[2] == number:
            found.append((parsed[1], p))
    return [p for _, p in sorted(found)]


# ------------------------------------------------------------------------------------------ bag io
def _ns(t: Any) -> int:
    return int(t.sec) * 1_000_000_000 + int(t.nanosec)


def read_topics(bag: Path, topics: list[str]) -> dict[str, list[tuple[int, Any]]]:
    import rosbag2_py
    from rclpy.serialization import deserialize_message
    from rosidl_runtime_py.utilities import get_message

    reader = rosbag2_py.SequentialReader()
    reader.open(rosbag2_py.StorageOptions(uri=str(bag)), rosbag2_py.ConverterOptions("", ""))
    types = {t.name: t.type for t in reader.get_all_topics_and_types()}
    wanted = [t for t in topics if t in types]
    reader.set_filter(rosbag2_py.StorageFilter(topics=wanted))
    out: dict[str, list[tuple[int, Any]]] = {t: [] for t in topics}
    classes = {t: get_message(types[t]) for t in wanted}
    while reader.has_next():
        topic, data, t = reader.read_next()
        out[topic].append((t, deserialize_message(data, classes[topic])))
    return out


def recordings_from_events(events: list[Any]) -> list[Recording]:
    """Pair start-like and stop-like RecordingEvents into recordings."""
    recs: list[Recording] = []
    cur: Recording | None = None
    for ev in sorted(events, key=lambda e: _ns(e.header.stamp)):
        if ev.event in (START, EXTERNAL_START) and ev.success:
            changed = _ns(ev.encoding_changed)
            if changed:
                cur = Recording(changed, float(ev.encoding_changed_uncertainty))
            else:  # encoding change not observed: fall back to the command acknowledgement
                cur = Recording(_ns(ev.camera_ack) or _ns(ev.command_sent), 1.0, source="ack")
            cur.transport = str(getattr(ev, "transport", ""))
            cur.encoding_changed_ns = changed
            if ev.event == START:
                cur.command_sent_ns, cur.camera_ack_ns = _ns(ev.command_sent), _ns(ev.camera_ack)
        elif ev.event == ADOPTED:
            # Driver started while recording: start = detection - encoding duration (1 s resolution).
            det = _ns(ev.command_sent)
            cur = Recording(det - int(max(ev.encoding_duration, 0)) * 1_000_000_000, 1.5, source="adopted")
        elif ev.event == HILIGHT and cur is not None:
            cur.hilights_ns.append(_ns(ev.camera_ack) or _ns(ev.command_sent))
        elif ev.event in (STOP, EXTERNAL_STOP) and cur is not None:
            # After a stop command the camera reports encoding for ~1-2 s more while it finalises the
            # file, so the footage ends near the ack, not at the observed encoding change.
            if ev.event == STOP:
                cur.stop_ns = _ns(ev.camera_ack) or _ns(ev.encoding_changed)
            else:
                cur.stop_ns = _ns(ev.encoding_changed) or _ns(ev.camera_ack)
            cur.filename = ev.filename
            recs.append(cur)
            cur = None
    return recs


def apply_calibration(rec: Recording, cal: sc.SyncCalibration) -> bool:
    """Anchor a driver-commanded recording at ``camera_ack + start_offset`` (True if applied)."""
    if not rec.camera_ack_ns:
        return False
    start = rec.camera_ack_ns + round(cal.start_offset_s * 1e9)
    if rec.command_sent_ns and start < rec.command_sent_ns - 50_000_000:
        log.warning(
            "%s: calibrated start %.3f s before the command was sent; calibration ignored",
            rec.filename,
            (rec.command_sent_ns - start) / 1e9,
        )
        return False
    rec.start_ns, rec.uncertainty, rec.source = start, cal.uncertainty_s, "calibrated"
    return True


# ------------------------------------------------------------------------------------------ xcorr
def gyro_magnitude_offset(
    t_a: np.ndarray,
    mag_a: np.ndarray,
    t_b: np.ndarray,
    mag_b: np.ndarray,
    max_lag: float,
    rate: float = 200.0,
) -> tuple[float, float]:
    """Return (lag, peak) such that signal_b(t) ~= signal_a(t - lag); times in seconds.

    lag > 0 means events appear later in b than in a.
    """
    lo = max(t_a[0], t_b[0]) - max_lag
    hi = min(t_a[-1], t_b[-1]) + max_lag
    if hi - lo < 2 * max_lag + 1.0:
        raise ValueError("not enough overlapping IMU data to correlate")
    grid = np.arange(lo, hi, 1.0 / rate)
    a = np.interp(grid, t_a, mag_a, left=np.nan, right=np.nan)
    b = np.interp(grid, t_b, mag_b, left=np.nan, right=np.nan)
    max_k = round(max_lag * rate)
    best_k, best_c = 0, -np.inf
    for k in range(-max_k, max_k + 1):
        if k >= 0:
            x, y = a[: len(a) - k], b[k:]
        else:
            x, y = a[-k:], b[: len(b) + k]
        m = ~(np.isnan(x) | np.isnan(y))
        if m.sum() < rate:  # need at least 1 s of overlap
            continue
        xs, ys = x[m] - x[m].mean(), y[m] - y[m].mean()
        denom = np.sqrt((xs * xs).sum() * (ys * ys).sum())
        c = float((xs * ys).sum() / denom) if denom > 0 else -np.inf
        if c > best_c:
            best_k, best_c = k, c
    return best_k / rate, best_c


# ------------------------------------------------------------------------------------------ alignment
@dataclass
class ChapterAlignment:
    mp4: Path
    t0_ns: int  # ROS time of video t=0 for this chapter
    telemetry: Telemetry


def align_recording(
    rec: Recording,
    mp4s: list[Path],
    robot_imu: list[tuple[int, Any]] | None,
    max_lag: float,
    min_peak: float = 0.5,
    calibrations: list[sc.SyncCalibration] | None = None,
) -> tuple[list[ChapterAlignment], dict[str, Any]]:
    number = rec.file_number
    chapters = chapters_for(number, mp4s) if number else []
    if not chapters:
        raise FileNotFoundError(f"no local MP4 for recording {rec.filename or '(unknown file)'}")
    first_tel = extract(chapters[0])
    calibration: dict[str, Any] | None = None
    if calibrations and rec.camera_ack_ns:
        fps, resolution, firmware = video_meta(chapters[0])
        info = sc.RecordingInfo(first_tel.model, rec.transport, fps, resolution, firmware)
        cal = sc.match(calibrations, info)
        if cal is None:
            log.info(
                "%s: no sync calibration for %s/%s %s fps (run gopro-calibrate-sync); using the encoding change",
                rec.filename,
                info.model or "?",
                info.transport or "?",
                f"{fps:g}" if fps else "?",
            )
        elif apply_calibration(rec, cal):
            calibration = {"entry": cal.describe(), "file": cal.source}
    report: dict[str, Any] = {
        "camera_file": rec.filename,
        "anchor_ns": rec.start_ns,
        "anchor_source": rec.source,
        "anchor_uncertainty_s": rec.uncertainty,
        "chapters": [str(c) for c in chapters],
        "sync_calibration": calibration,
    }
    aligned: list[ChapterAlignment] = []
    offset_s = 0.0
    for mp4 in chapters:
        tel = first_tel if mp4 == chapters[0] else extract(mp4)
        aligned.append(ChapterAlignment(mp4, rec.start_ns + int(offset_s * 1e9), tel))
        offset_s += tel.duration
    report["duration_s"] = offset_s
    if robot_imu:
        gt, gm = [], []
        for ch in aligned:
            for s in ch.telemetry.imu:
                gt.append((ch.t0_ns - rec.start_ns) / 1e9 + s.t)
                gm.append(float(np.linalg.norm(s.gyro)))
        rt = np.array([(_ns(m.header.stamp) or t) - rec.start_ns for t, m in robot_imu]) / 1e9
        rm = np.array(
            [np.linalg.norm([m.angular_velocity.x, m.angular_velocity.y, m.angular_velocity.z]) for _, m in robot_imu]
        )
        order = np.argsort(rt)
        try:
            lag, peak = gyro_magnitude_offset(
                np.array(gt), np.array(gm), rt[order], rm[order], max_lag + rec.uncertainty
            )
            report["xcorr_lag_s"], report["xcorr_peak"] = lag, peak
            if peak >= min_peak:
                for ch in aligned:
                    ch.t0_ns += int(lag * 1e9)
                report["refined"] = True
            else:
                report["refined"] = False
                log.warning("IMU correlation peak %.2f < %.2f: keeping event anchor", peak, min_peak)
        except ValueError as e:
            report["refined"] = False
            log.warning("IMU refinement skipped: %s", e)
    report["t0_ns"] = aligned[0].t0_ns
    return aligned, report


def write_frames_csv(ch: ChapterAlignment, out_dir: Path) -> Path:
    p = out_dir / f"{ch.mp4.stem}_frames_ros.csv"
    with open(p, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["frame", "pts_s", "ros_time_ns"])
        for i, t in enumerate(ch.telemetry.frame_times):
            w.writerow([i, f"{t:.6f}", ch.t0_ns + round(t * 1e9)])
    return p


def write_bag(chapters: list[ChapterAlignment], out_bag: Path, frame_stride: int, frame_id: str) -> None:
    import rosbag2_py
    from builtin_interfaces.msg import Time
    from rclpy.serialization import serialize_message
    from sensor_msgs.msg import Imu, NavSatFix, NavSatStatus

    writer = rosbag2_py.SequentialWriter()
    writer.open(rosbag2_py.StorageOptions(uri=str(out_bag)), rosbag2_py.ConverterOptions("", ""))
    for i, (name, typ) in enumerate(
        (
            ("/gopro/imu", "sensor_msgs/msg/Imu"),
            ("/gopro/gps", "sensor_msgs/msg/NavSatFix"),
            ("/gopro/image/compressed", "sensor_msgs/msg/CompressedImage"),
        )
    ):
        writer.create_topic(rosbag2_py.TopicMetadata(id=i, name=name, type=typ, serialization_format="cdr"))

    def stamp(ns: int) -> Time:
        return Time(sec=ns // 1_000_000_000, nanosec=ns % 1_000_000_000)

    for ch in chapters:
        for s in ch.telemetry.imu:
            ns = ch.t0_ns + int(s.t * 1e9)
            m = Imu()
            m.header.stamp, m.header.frame_id = stamp(ns), frame_id
            m.angular_velocity.x, m.angular_velocity.y, m.angular_velocity.z = s.gyro
            m.linear_acceleration.x, m.linear_acceleration.y, m.linear_acceleration.z = s.accel
            m.orientation_covariance[0] = -1.0  # no orientation
            writer.write("/gopro/imu", serialize_message(m), ns)
        for g in ch.telemetry.gps:
            ns = ch.t0_ns + int(g.t * 1e9)
            m = NavSatFix()
            m.header.stamp, m.header.frame_id = stamp(ns), frame_id
            m.latitude, m.longitude, m.altitude = g.lat, g.lon, g.alt
            m.status.status = NavSatStatus.STATUS_FIX if g.fix >= 2 else NavSatStatus.STATUS_NO_FIX
            m.status.service = NavSatStatus.SERVICE_GPS
            m.position_covariance_type = NavSatFix.COVARIANCE_TYPE_UNKNOWN
            writer.write("/gopro/gps", serialize_message(m), ns)
        if frame_stride > 0:
            _write_frames(writer, ch, frame_stride, frame_id, stamp)
    del writer


def _write_frames(writer: Any, ch: ChapterAlignment, stride: int, frame_id: str, stamp: Any) -> None:
    import av
    from rclpy.serialization import serialize_message
    from sensor_msgs.msg import CompressedImage

    enc = None
    with av.open(str(ch.mp4)) as c:
        vs = c.streams.video[0]
        start = vs.start_time or 0
        for i, frame in enumerate(c.decode(vs)):
            if i % stride:
                continue
            if enc is None:
                enc = av.CodecContext.create("mjpeg", "w")
                enc.width, enc.height, enc.pix_fmt = frame.width, frame.height, "yuvj420p"
                enc.time_base = vs.time_base
            pkts = enc.encode(frame.reformat(format="yuvj420p"))
            t = float((frame.pts - start) * vs.time_base) if frame.pts is not None else ch.telemetry.frame_times[i]
            ns = ch.t0_ns + int(t * 1e9)
            for p in pkts:
                m = CompressedImage()
                m.header.stamp, m.header.frame_id = stamp(ns), frame_id
                m.format = "jpeg"
                m.data = bytes(p)
                writer.write("/gopro/image/compressed", serialize_message(m), ns)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bag", type=Path, help="rosbag2 containing the driver's recording_event topic")
    ap.add_argument("mp4", nargs="+", type=Path, help="downloaded GoPro MP4 files (all chapters)")
    ap.add_argument("--event-topic", default="/gopro/gopro_camera/recording_event")
    ap.add_argument("--imu-topic", default="", help="robot sensor_msgs/Imu topic for xcorr refinement")
    ap.add_argument("--max-lag", type=float, default=0.5, help="extra search window [s] for refinement")
    ap.add_argument("--min-peak", type=float, default=0.5, help="min normalised correlation to accept")
    ap.add_argument("--out", type=Path, default=Path("aligned"))
    ap.add_argument("--out-bag", type=Path, default=None, help="write GoPro IMU/GPS (+frames) bag here")
    ap.add_argument("--frame-stride", type=int, default=0, help="write every Nth frame as JPEG (0 = none)")
    ap.add_argument("--frame-id", default="gopro_optical_frame")
    ap.add_argument(
        "--sync-calibration",
        type=Path,
        action="append",
        default=None,
        help="calibration YAML (repeatable); default: packaged defaults + ~/.ros/gopro/sync_calibration.yaml",
    )
    ap.add_argument("--no-sync-calibration", action="store_true", help="anchor on the observed encoding change")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    calibrations = None
    if not args.no_sync_calibration:
        calibrations = sc.load(args.sync_calibration or sc.default_files())

    topics = [args.event_topic] + ([args.imu_topic] if args.imu_topic else [])
    data = read_topics(args.bag, topics)
    recs = recordings_from_events([m for _, m in data[args.event_topic]])
    if not recs:
        raise SystemExit(f"no complete recordings found on {args.event_topic} in {args.bag}")
    args.out.mkdir(parents=True, exist_ok=True)
    reports, all_chapters = [], []
    for rec in recs:
        robot_imu = None
        if args.imu_topic:
            lo, hi = rec.start_ns - 10_000_000_000, (rec.stop_ns or rec.start_ns) + 10_000_000_000
            robot_imu = [(t, m) for t, m in data[args.imu_topic] if lo <= t <= hi]
        try:
            chapters, report = align_recording(
                rec, args.mp4, robot_imu, args.max_lag, args.min_peak, calibrations=calibrations
            )
        except FileNotFoundError as e:
            log.warning("%s", e)
            continue
        report["frames_csv"] = [str(write_frames_csv(ch, args.out)) for ch in chapters]
        reports.append(report)
        all_chapters.extend(chapters)
        log.info(
            "%s -> t0=%d ns (%s, +-%.3fs)%s",
            rec.filename,
            report["t0_ns"],
            rec.source,
            rec.uncertainty,
            f", IMU lag {report['xcorr_lag_s']:+.3f}s peak {report['xcorr_peak']:.2f}"
            if "xcorr_lag_s" in report
            else "",
        )
    (args.out / "alignment.json").write_text(json.dumps(reports, indent=2))
    if args.out_bag and all_chapters:
        write_bag(all_chapters, args.out_bag, args.frame_stride, args.frame_id)
        log.info("wrote %s", args.out_bag)


if __name__ == "__main__":
    main()
