from pathlib import Path
from types import SimpleNamespace

from gopro_tools.align import START, STOP, Recording, apply_calibration, recordings_from_events

from gopro_tools import sync_calibration as sc

HERO13 = sc.RecordingInfo("HERO13 Black", "usb", 29.97, "5312x2988", "H24.01.02.04.00")


def _cal(**kw) -> sc.SyncCalibration:
    base = {"model": "HERO13 Black", "transport": "usb", "start_offset_s": -0.03, "uncertainty_s": 0.02}
    return sc.SyncCalibration(**{**base, **kw})


def test_packaged_default_matches_measured_configuration():
    cals = sc.load([sc.packaged_file()])
    cal = sc.match(cals, HERO13)
    assert cal is not None and cal.start_offset_s < 0 and cal.uncertainty_s < 0.05
    # other frame rate / transport / model: the default does not apply
    assert sc.match(cals, sc.RecordingInfo("HERO13 Black", "usb", 59.94)) is None
    assert sc.match(cals, sc.RecordingInfo("HERO13 Black", "ble", 29.97)) is None
    assert sc.match(cals, sc.RecordingInfo("HERO12 Black", "usb", 29.97)) is None


def test_match_prefers_specific_then_later_entries():
    generic = _cal(start_offset_s=-0.010)
    by_fps = _cal(fps=29.97, start_offset_s=-0.020)
    by_fw = _cal(fps=29.97, firmware="H24.01.02.04.00", start_offset_s=-0.030)
    assert sc.match([generic], HERO13) is generic
    assert sc.match([by_fw, by_fps, generic], HERO13) is by_fw
    newer = _cal(fps=29.97, firmware="H24.01.02.04.00", start_offset_s=-0.040)
    assert sc.match([by_fw, newer], HERO13) is newer  # user file is loaded last
    assert sc.match([_cal(resolution="3840x2160")], HERO13) is None
    assert sc.match([_cal(transport="USB ")], HERO13) is not None  # case/space-insensitive


def test_save_replaces_same_configuration(tmp_path: Path):
    f = tmp_path / "sub" / "cal.yaml"
    sc.save(f, _cal(fps=29.97, start_offset_s=-0.01, settings={"hypersmooth": "Low"}))
    sc.save(f, _cal(fps=59.94, start_offset_s=-0.02))
    sc.save(f, _cal(fps=29.97, start_offset_s=-0.03))
    cals = sc.load([f])
    assert sorted(c.start_offset_s for c in cals) == [-0.03, -0.02]
    assert all(c.source == str(f) for c in cals)


def _t(ns: int) -> SimpleNamespace:
    return SimpleNamespace(sec=ns // 1_000_000_000, nanosec=ns % 1_000_000_000)


def _ev(kind, sent, ack, changed, filename=""):
    return SimpleNamespace(
        event=kind,
        header=SimpleNamespace(stamp=_t(sent)),
        transport="usb",
        command_sent=_t(sent),
        camera_ack=_t(ack),
        encoding_changed=_t(changed),
        encoding_changed_uncertainty=0.6,
        filename=filename,
        encoding_duration=-1,
        success=True,
    )


def test_calibrated_anchor_uses_ack_and_stop_uses_ack():
    s = 1_000_000_000_000
    # Values of a real HERO13/USB start: ack +473 ms, encoding seen +626 ms; stop ack +40 ms, finalised +1.6 s
    evs = [
        _ev(START, s, s + 473_000_000, s + 626_000_000),
        _ev(STOP, s + 10_000_000_000, s + 10_040_000_000, s + 11_600_000_000, "100GOPRO/GX010042.MP4"),
    ]
    rec = recordings_from_events(evs)[0]
    assert rec.start_ns == s + 626_000_000 and rec.source == "event"
    assert rec.stop_ns == s + 10_040_000_000
    assert rec.transport == "usb" and rec.camera_ack_ns == s + 473_000_000
    assert apply_calibration(rec, _cal(start_offset_s=-0.037, uncertainty_s=0.024))
    assert rec.start_ns == s + 436_000_000 and rec.source == "calibrated" and rec.uncertainty == 0.024


def test_calibration_not_applied_without_ack_or_when_implausible():
    rec = Recording(start_ns=5_000_000_000, uncertainty=0.6)
    assert not apply_calibration(rec, _cal())  # external start: no command ack
    rec = Recording(start_ns=5_000_000_000, uncertainty=0.6, command_sent_ns=4_900_000_000, camera_ack_ns=4_950_000_000)
    assert not apply_calibration(rec, _cal(start_offset_s=-0.5))  # would precede the command
    assert rec.start_ns == 5_000_000_000 and rec.source == "event"
