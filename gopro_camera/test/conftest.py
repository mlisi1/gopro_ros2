import pytest
from gopro_camera.camera import CameraConfig, GoProCamera, RecordingEventData
from gopro_camera.transports import TransportConfig


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "hardware: needs a real camera (skipped unless --hardware)")


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--hardware", action="store_true", help="run tests against real GoPro hardware")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--hardware"):
        return
    skip = pytest.mark.skip(reason="hardware test; pass --hardware to run")
    for item in items:
        if "hardware" in item.keywords:
            item.add_marker(skip)


class Recorder:
    def __init__(self) -> None:
        self.events: list[RecordingEventData] = []
        self.links: list[tuple[bool, str]] = []
        self.states = 0

    def on_state(self, camera: GoProCamera) -> None:
        self.states += 1

    def on_recording_event(self, camera: GoProCamera, event: RecordingEventData) -> None:
        self.events.append(event)

    def on_link(self, camera: GoProCamera, connected: bool, reason: str) -> None:
        self.links.append((connected, reason))


@pytest.fixture
def mock_cam():
    aiohttp = pytest.importorskip("aiohttp")  # noqa: F841
    from gopro_camera.testing.mock_camera import MockCamera

    cam = MockCamera()
    cam.start()
    yield cam
    cam.stop()


@pytest.fixture
def make_camera(mock_cam):
    def _make(**overrides) -> tuple[GoProCamera, Recorder]:
        tcfg = TransportConfig(
            transport="usb", usb_base_url=mock_cam.base_url, command_timeout=5.0, connect_timeout=5.0
        )
        cfg = CameraConfig(
            transport=tcfg,
            status_poll_rate=5.0,
            keep_alive_period=0.5,
            link_loss_threshold=2,
            status_timeout=1.0,
            reconnect_backoff_initial=0.2,
            reconnect_backoff_max=0.5,
        )
        for k, v in overrides.items():
            setattr(cfg, k, v)
        rec = Recorder()
        return GoProCamera(cfg, listener=rec), rec

    return _make
