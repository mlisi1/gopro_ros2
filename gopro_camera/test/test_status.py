from gopro_camera.spec_tables import STATUSES
from gopro_camera.status import (
    ERROR,
    OK,
    WARN,
    DiagThresholds,
    S,
    evaluate,
    raw_numeric,
    status_fields,
)


def test_status_ids_resolved_from_spec_titles():
    assert STATUSES[S.ENCODING] == "Encoding"
    assert STATUSES[S.PRIMARY_STORAGE] == "Primary Storage"


def test_status_fields_defaults_and_values():
    f = status_fields({})
    assert f["battery_percent"] == -1 and f["encoding"] is False and f["sd_status_text"] == "Unknown"
    f = status_fields({S.ENCODING: 1, S.BATTERY_PERCENT: 55, S.PRIMARY_STORAGE: 1, S.CAMERA_NAME: "cam"})
    assert f["encoding"] is True and f["battery_percent"] == 55
    assert f["sd_status_text"] == "SD Card Full" and f["camera_name"] == "cam"


def test_raw_numeric_skips_strings():
    ids, vals = raw_numeric({S.ENCODING: True, S.CAMERA_NAME: "x", S.BATTERY_PERCENT: "42"})
    assert dict(zip(ids, vals, strict=True)) == {S.ENCODING: 1, S.BATTERY_PERCENT: 42}


def _levels(st, **kw):
    args = dict(
        connected=True, status_age=0.5, transport="usb", stream_active=False, stream_frame_age=None, th=DiagThresholds()
    )
    args.update(kw)
    return {i.name: i.level for i in evaluate(st, **args)}


def test_diagnostics_levels():
    assert _levels({S.BATTERY_PERCENT: 80, S.PRIMARY_STORAGE: 0, S.REMAINING_VIDEO_TIME: 99999}) == {
        "link": OK,
        "battery": OK,
        "sd_card": OK,
        "thermal": OK,
    }
    lv = _levels({S.BATTERY_PERCENT: 15, S.PRIMARY_STORAGE: 0, S.REMAINING_VIDEO_TIME: 60, S.OVERHEATING: 1})
    assert lv["battery"] == WARN and lv["sd_card"] == ERROR and lv["thermal"] == ERROR
    lv = _levels({S.PRIMARY_STORAGE: 2}, connected=False)
    assert lv["link"] == ERROR and lv["sd_card"] == ERROR
    lv = _levels({S.BATTERY_PERCENT: 5, S.BATTERY_BARS: 4})  # charging
    assert lv["battery"] == OK
    lv = _levels({}, stream_active=True, stream_frame_age=10.0)
    assert lv["stream"] == ERROR
