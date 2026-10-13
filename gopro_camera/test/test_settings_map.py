import pytest
from gopro_camera.errors import InvalidSetting
from gopro_camera.spec_tables import SETTINGS

from gopro_camera import settings_map as smap


def test_every_spec_setting_has_unique_param_name():
    names = [d.param_name for d in smap.SETTING_DEFS.values()]
    assert len(names) == len(set(names)) == len(SETTINGS)
    assert all(n.startswith("settings.") for n in names)


def test_lookup_by_id_and_name():
    res = smap.lookup("settings.video_resolution")
    assert smap.lookup(res.id) is res
    assert smap.lookup(str(res.id)) is res
    with pytest.raises(InvalidSetting):
        smap.lookup("settings.not_a_setting")


@pytest.mark.parametrize(
    ("name", "value", "label"),
    [
        ("video_resolution", "4K", "4K"),
        ("video_resolution", "4k", "4K"),
        ("video_resolution", 1080, "1080"),  # int that is not an option id but a label
        ("frames_per_second", "60", "60.0"),
        ("hypersmooth", "auto boost", "Auto Boost"),
        ("gps", True, "On"),
        ("gps", False, "Off"),
    ],
)
def test_parse_option_labels(name, value, label):
    sdef = smap.SETTING_BY_NAME[name]
    assert sdef.label(smap.parse_option(sdef, value)) == label


def test_parse_option_id_and_errors():
    sdef = smap.SETTING_BY_NAME["video_resolution"]
    assert smap.parse_option(sdef, 1) == 1
    with pytest.raises(InvalidSetting, match="Valid"):
        smap.parse_option(sdef, "potato")


def test_model_specific_options():
    sdef = smap.SETTING_BY_NAME["video_resolution"]
    eight_k = smap.parse_option(sdef, "8K")
    smap.validate_option(sdef, eight_k, "MAX 2")
    with pytest.raises(InvalidSetting) as exc:
        smap.validate_option(sdef, eight_k, "HERO13 Black")
    assert exc.value.supported_options
    smap.validate_option(sdef, eight_k, "Some Future Camera")  # unknown model: let camera decide


def test_runtime_options_and_range():
    sdef = smap.SETTING_BY_NAME["video_resolution"]
    with pytest.raises(InvalidSetting, match="current preset"):
        smap.validate_option(sdef, 1, "HERO13 Black", runtime_options=[9])
    lcd = smap.SETTING_BY_NAME["lcd_brightness"]
    smap.validate_option(lcd, 50, "HERO13 Black")
    with pytest.raises(InvalidSetting):
        smap.validate_option(lcd, 5, "HERO13 Black")


def test_firmware_compare():
    assert smap.min_firmware_for("HERO13 Black") == "v01.10.00"
    assert smap.min_firmware_for("Mission 1") == "v01.10.00"  # case-insensitive vs spec table
    assert smap.firmware_at_least("H24.01.02.02.00", "v01.10.00")
    assert not smap.firmware_at_least("H21.01.01.05.00", "v01.10.00")
    assert smap.min_firmware_for("Unknown Cam") is None
