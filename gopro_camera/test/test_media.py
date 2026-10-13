import datetime
from pathlib import Path

import pytest
from gopro_camera.media import filter_since, local_path_for, parse_media_list, to_camera_epoch

SAMPLE = {
    "id": "1",
    "media": [
        {
            "d": "100GOPRO",
            "fs": [
                {"n": "GX010002.MP4", "cre": "200", "mod": "201", "s": "1234"},
                {"n": "G0010001.JPG", "cre": "100", "mod": "100", "g": "1", "s": "30", "t": "b"},
            ],
        }
    ],
}


def test_parse_media_list_single_and_group():
    items = parse_media_list(SAMPLE)
    assert [i.path for i in items] == ["100GOPRO/G0010001.JPG", "100GOPRO/GX010002.MP4"]
    group, video = items
    assert group.is_group and group.group_size == 30 and group.size_bytes == 0 and group.group_type == "b"
    assert not video.is_group and video.size_bytes == 1234


def test_filter_since():
    items = parse_media_list(SAMPLE)
    assert [i.filename for i in filter_since(items, 150)] == ["GX010002.MP4"]
    assert len(filter_since(items, None)) == 2


def test_camera_epoch_is_local_wall_clock():
    aware = datetime.datetime(2024, 6, 1, 12, 0, 0, tzinfo=datetime.UTC)
    utc_epoch = int(aware.timestamp())
    local = aware.astimezone()
    offset = int(local.utcoffset().total_seconds())
    assert to_camera_epoch(aware) == utc_epoch + offset
    assert to_camera_epoch(aware, camera_clock_is_local=False) == utc_epoch


def test_local_path_rejects_traversal(tmp_path: Path):
    assert local_path_for(tmp_path, "100GOPRO/GX01.MP4") == tmp_path / "100GOPRO" / "GX01.MP4"
    with pytest.raises(ValueError):
        local_path_for(tmp_path, "../etc/passwd")
