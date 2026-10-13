import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from gopro_tools.align import (
    ADOPTED,
    HILIGHT,
    START,
    STOP,
    chapters_for,
    gyro_magnitude_offset,
    parse_gopro_name,
    recordings_from_events,
)

SAMPLE = os.environ.get("GOPRO_SAMPLE_MP4", "")


def _t(ns: int) -> SimpleNamespace:
    return SimpleNamespace(sec=ns // 1_000_000_000, nanosec=ns % 1_000_000_000)


def _ev(kind, sent, changed=0, unc=0.1, filename="", duration=-1, success=True):
    return SimpleNamespace(
        event=kind,
        header=SimpleNamespace(stamp=_t(sent)),
        command_sent=_t(sent),
        camera_ack=_t(sent + 5_000_000),
        encoding_changed=_t(changed),
        encoding_changed_uncertainty=unc,
        filename=filename,
        encoding_duration=duration,
        success=success,
    )


def test_parse_names_and_chapters(tmp_path: Path):
    assert parse_gopro_name("100GOPRO/GX020042.MP4") == ("X", 2, "0042")
    assert parse_gopro_name("GH010007.mp4") == ("H", 1, "0007")
    assert parse_gopro_name("GOPR0001.JPG") is None
    files = [tmp_path / n for n in ("GX020042.MP4", "GX010042.MP4", "GX010043.MP4")]
    assert [p.name for p in chapters_for("0042", files)] == ["GX010042.MP4", "GX020042.MP4"]


def test_recordings_from_events():
    s = 1_000_000_000_000
    evs = [
        _ev(START, s, changed=s + 400_000_000, unc=0.2),
        _ev(HILIGHT, s + 5_000_000_000),
        _ev(STOP, s + 10_000_000_000, changed=s + 10_300_000_000, filename="100GOPRO/GX010042.MP4"),
        _ev(ADOPTED, s + 20_000_000_000, duration=7),
        _ev(STOP, s + 30_000_000_000, filename="100GOPRO/GX010043.MP4"),
    ]
    recs = recordings_from_events(evs)
    assert len(recs) == 2
    assert recs[0].start_ns == s + 400_000_000 and recs[0].uncertainty == 0.2
    assert recs[0].file_number == "0042" and len(recs[0].hilights_ns) == 1
    assert recs[1].source == "adopted" and recs[1].start_ns == s + 13_000_000_000


def test_xcorr_recovers_known_lag():
    rng = np.random.default_rng(0)
    t = np.arange(0, 60, 1 / 200)
    sig = np.convolve(np.abs(rng.normal(size=t.size)), np.ones(20) / 20, mode="same")
    lag_true = 0.237
    t_b = np.arange(0, 60, 1 / 100)
    b = np.interp(t_b - lag_true, t, sig)
    lag, peak = gyro_magnitude_offset(t, sig, t_b, b, max_lag=1.0)
    assert abs(lag - lag_true) < 0.01 and peak > 0.95


@pytest.mark.skipif(not SAMPLE, reason="set GOPRO_SAMPLE_MP4 to a GoPro MP4 (e.g. gpmf-parser samples/hero8.mp4)")
def test_align_end_to_end_with_bag(tmp_path: Path):
    pytest.importorskip("telemetry_parser")
    import rosbag2_py
    from gopro_tools.gpmf import extract
    from rclpy.serialization import serialize_message
    from sensor_msgs.msg import Imu

    from gopro_camera_interfaces.msg import RecordingEvent
    from gopro_tools import align

    mp4 = tmp_path / "GX010042.MP4"
    shutil.copy(SAMPLE, mp4)
    tel = extract(mp4)
    t0 = 1_700_000_000_000_000_000
    true_lag = 0.3  # robot IMU sees motion 0.3 s after the event anchor would predict

    bag = tmp_path / "bag"
    w = rosbag2_py.SequentialWriter()
    w.open(rosbag2_py.StorageOptions(uri=str(bag)), rosbag2_py.ConverterOptions("", ""))
    topic = "/gopro/gopro_camera/recording_event"
    w.create_topic(
        rosbag2_py.TopicMetadata(
            id=0, name=topic, type="gopro_camera_interfaces/msg/RecordingEvent", serialization_format="cdr"
        )
    )
    w.create_topic(rosbag2_py.TopicMetadata(id=1, name="/imu", type="sensor_msgs/msg/Imu", serialization_format="cdr"))

    def ev(kind, ns, filename=""):
        m = RecordingEvent(event=kind, success=True, filename=filename, encoding_changed_uncertainty=0.05)
        m.header.stamp.sec, m.header.stamp.nanosec = ns // 10**9, ns % 10**9
        m.encoding_changed = m.header.stamp
        m.command_sent = m.header.stamp
        return m

    w.write(topic, serialize_message(ev(RecordingEvent.START, t0)), t0)
    for s in tel.imu:
        ns = t0 + int((s.t + true_lag) * 1e9)
        m = Imu()
        m.header.stamp.sec, m.header.stamp.nanosec = ns // 10**9, ns % 10**9
        m.angular_velocity.x, m.angular_velocity.y, m.angular_velocity.z = s.gyro[2], s.gyro[0], -s.gyro[1]
        w.write("/imu", serialize_message(m), ns)
    end = t0 + int(tel.duration * 1e9)
    w.write(topic, serialize_message(ev(RecordingEvent.STOP, end, "100GOPRO/GX010042.MP4")), end)
    del w

    out = tmp_path / "out"
    align.main([str(bag), str(mp4), "--imu-topic", "/imu", "--out", str(out), "--out-bag", str(tmp_path / "gopro_bag")])
    import json

    report = json.loads((out / "alignment.json").read_text())[0]
    assert report["refined"] and abs(report["xcorr_lag_s"] - true_lag) < 0.02
    rows = (out / "GX010042_frames_ros.csv").read_text().splitlines()
    assert len(rows) == len(tel.frame_times) + 1
    first_ns = int(rows[1].split(",")[2])
    assert abs(first_ns - (t0 + true_lag * 1e9)) < 0.02e9
    assert (tmp_path / "gopro_bag").exists()
