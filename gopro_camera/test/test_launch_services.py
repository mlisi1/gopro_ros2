"""launch_testing: bring up the driver against the fake camera and call every service and action."""

import os
import tempfile
import time
import unittest

import launch
import launch_ros.actions
import launch_testing
import launch_testing.actions
import pytest
import rclpy
from rclpy.action import ActionClient
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import Empty
from std_srvs.srv import Trigger

pytest.importorskip("aiohttp")
pytest.importorskip("open_gopro")

from gopro_camera.testing.mock_camera import MockCamera

from gopro_camera_interfaces.action import DownloadMedia, ProvisionCohn
from gopro_camera_interfaces.msg import HardwareInfo, RecordingEvent, SettingValue, Status
from gopro_camera_interfaces.srv import (
    Capture,
    DeleteMedia,
    GetCapabilities,
    GetCohnStatus,
    GetHardwareInfo,
    GetMediaAsset,
    GetPresets,
    GetSettings,
    ListMedia,
    LoadPreset,
    SetSettings,
    SetWifiAp,
    StartLivestream,
)
from gopro_camera_interfaces.srv import ProvisionCohn as ProvisionCohnSrv

NS = "/cam_test/gopro_camera"
MOCK = MockCamera()
DOWNLOAD_DIR = tempfile.mkdtemp(prefix="gopro_dl_")


@pytest.mark.launch_test
def generate_test_description():
    url = MOCK.start()
    node = launch_ros.actions.LifecycleNode(
        package="gopro_camera",
        executable="gopro_node",
        name="gopro_camera",
        namespace="cam_test",
        parameters=[
            {
                "transport": "usb",
                "usb_base_url": url,
                "media.download_dir": DOWNLOAD_DIR,
                "status.poll_rate": 5.0,
                "status.publish_rate": 5.0,
                "settings.video_resolution": "4K",
                "stream.port": 18554,
            }
        ],
        output="screen",
    )
    return launch.LaunchDescription([node, launch_testing.actions.ReadyToTest()])


class TestServices(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rclpy.init()
        cls.node = rclpy.create_node("gopro_test_client")

    @classmethod
    def tearDownClass(cls):
        cls.node.destroy_node()
        rclpy.shutdown()
        MOCK.stop()

    def call(self, srv_type, name, request=None, timeout=30.0):
        cli = self.node.create_client(srv_type, f"{NS}/{name}")
        self.assertTrue(cli.wait_for_service(timeout_sec=30.0), f"service {name} not available")
        fut = cli.call_async(request or srv_type.Request())
        rclpy.spin_until_future_complete(self.node, fut, timeout_sec=timeout)
        self.assertTrue(fut.done(), f"service {name} timed out")
        return fut.result()

    def wait_for_msg(self, msg_type, topic, qos=10, timeout=20.0, predicate=lambda m: True):
        got = []
        sub = self.node.create_subscription(msg_type, topic, lambda m: got.append(m) if predicate(m) else None, qos)
        end = time.time() + timeout
        while time.time() < end and not got:
            rclpy.spin_once(self.node, timeout_sec=0.1)
        self.node.destroy_subscription(sub)
        return got[0] if got else None

    def wait_until_active(self):
        st = self.wait_for_msg(Status, f"{NS}/status", timeout=40.0, predicate=lambda m: m.connected)
        self.assertIsNotNone(st, "driver never published a connected status")
        return st

    def run_action(self, action_type, name, goal, timeout=60.0):
        client = ActionClient(self.node, action_type, f"{NS}/{name}")
        self.assertTrue(client.wait_for_server(timeout_sec=20.0))
        feedback = []
        send = client.send_goal_async(goal, feedback_callback=lambda f: feedback.append(f.feedback))
        rclpy.spin_until_future_complete(self.node, send, timeout_sec=10.0)
        handle = send.result()
        self.assertTrue(handle.accepted)
        res = handle.get_result_async()
        rclpy.spin_until_future_complete(self.node, res, timeout_sec=timeout)
        self.assertTrue(res.done())
        return res.result().result, feedback

    def test_all_services(self):
        st = self.wait_until_active()
        self.assertEqual(st.transport, "usb")
        self.assertEqual(MOCK.state.settings[2], 1, "settings.video_resolution=4K applied on configure")

        latched = QoSProfile(
            depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL, reliability=ReliabilityPolicy.RELIABLE
        )
        hw = self.wait_for_msg(HardwareInfo, f"{NS}/hardware_info", qos=latched)
        self.assertIsNotNone(hw)
        self.assertEqual(hw.model_name, "HERO13 Black")
        self.assertTrue(hw.firmware_supported)

        r = self.call(GetHardwareInfo, "get_hardware_info")
        self.assertTrue(r.success)
        self.assertEqual(r.info.serial_number, MOCK.state.serial)

        r = self.call(GetCapabilities, "get_capabilities")
        self.assertTrue(r.success)
        self.assertIn("record", r.features)
        self.assertTrue(any(s.name == "video_resolution" for s in r.settings))

        r = self.call(GetPresets, "get_presets")
        self.assertTrue(r.success, r.message)
        self.assertEqual({g.name for g in r.groups}, {"video", "photo", "timelapse"})

        r = self.call(LoadPreset, "load_preset", LoadPreset.Request(preset_id=1))
        self.assertTrue(r.success, r.message)
        self.assertEqual(r.active_preset_id, 1)

        req = SetSettings.Request(settings=[SettingValue(name="video_resolution", option_label="1080")])
        r = self.call(SetSettings, "set_settings", req)
        self.assertTrue(r.success, r.message)
        self.assertEqual(MOCK.state.settings[2], 9)
        req = SetSettings.Request(settings=[SettingValue(name="video_resolution", option_label="8K")])
        r = self.call(SetSettings, "set_settings", req)
        self.assertFalse(r.success)

        r = self.call(GetSettings, "get_settings")
        self.assertTrue(any(s.name == "video_resolution" and s.option_label == "1080" for s in r.settings))

        r = self.call(Trigger, "sync_datetime")
        self.assertTrue(r.success, r.message)

        # --- recording with sync anchors
        events = []
        events_qos = QoSProfile(depth=100, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        sub = self.node.create_subscription(RecordingEvent, f"{NS}/recording_event", events.append, events_qos)
        r = self.call(Capture, "start_recording")
        self.assertTrue(r.success, r.message)
        self.assertGreater(r.event.encoding_changed.sec, 0)
        r = self.call(Capture, "hilight")
        self.assertTrue(r.success, r.message)
        r = self.call(Capture, "start_recording")
        self.assertFalse(r.success)
        self.assertIn("already encoding", r.message)
        r = self.call(Capture, "stop_recording")
        self.assertTrue(r.success, r.message)
        self.assertTrue(r.event.filename.startswith("100GOPRO/"))
        end = time.time() + 5
        while time.time() < end and len(events) < 3:
            rclpy.spin_once(self.node, timeout_sec=0.1)
        self.node.destroy_subscription(sub)
        self.assertEqual(
            [e.event for e in events][:3], [RecordingEvent.START, RecordingEvent.HILIGHT, RecordingEvent.STOP]
        )

        r = self.call(Capture, "take_photo")
        self.assertFalse(r.success)  # video preset active
        r = self.call(LoadPreset, "load_preset", LoadPreset.Request(group="photo"))
        self.assertTrue(r.success, r.message)
        r = self.call(Capture, "take_photo")
        self.assertTrue(r.success, r.message)

        # --- media
        r = self.call(ListMedia, "list_media", ListMedia.Request(since_mission_start=True))
        self.assertTrue(r.success, r.message)
        new_files = [i.path for i in r.items]
        self.assertEqual(len(new_files), 2, new_files)  # the video and the photo from this test
        r = self.call(GetMediaAsset, "get_media_asset", GetMediaAsset.Request(path=new_files[0], kind="thumbnail"))
        self.assertTrue(r.success, r.message)
        self.assertTrue(os.path.exists(r.local_file))
        r = self.call(GetMediaAsset, "get_media_asset", GetMediaAsset.Request(path=new_files[0], kind="info"))
        self.assertTrue(r.success and r.info_json)

        result, feedback = self.run_action(
            DownloadMedia, "download_media", DownloadMedia.Goal(since_mission_start=True)
        )
        self.assertTrue(result.success, result.message)
        self.assertEqual(len(result.downloaded), 2)
        self.assertTrue(feedback)
        self.assertTrue(all(os.path.getsize(p) > 0 for p in result.downloaded))

        r = self.call(DeleteMedia, "delete_media", DeleteMedia.Request(paths=[new_files[0]], confirm=False))
        self.assertFalse(r.success)
        r = self.call(DeleteMedia, "delete_media", DeleteMedia.Request(paths=[new_files[0]], confirm=True))
        self.assertTrue(r.success, r.message)
        self.assertNotIn(new_files[0], MOCK.state.files)

        # --- stream (camera side; no frames arrive from the fake camera)
        self.call(LoadPreset, "load_preset", LoadPreset.Request(group="video"))
        r = self.call(Trigger, "start_stream")
        self.assertTrue(r.success, r.message)
        self.assertEqual(MOCK.state.webcam_status, 2)
        r = self.call(Trigger, "stop_stream")
        self.assertTrue(r.success, r.message)
        self.assertEqual(MOCK.state.webcam_status, 0)

        # --- BLE-only features fail cleanly without BLE
        for srv_type, name, req in [
            (SetWifiAp, "set_wifi_ap", SetWifiAp.Request(enable=True)),
            (ProvisionCohnSrv, "provision_cohn", ProvisionCohnSrv.Request(ssid="x", password="y")),
            (StartLivestream, "start_livestream", StartLivestream.Request(url="rtmp://127.0.0.1/live")),
            (Trigger, "stop_livestream", None),
            (Trigger, "power_down", None),
        ]:
            r = self.call(srv_type, name, req)
            self.assertFalse(r.success, name)
            self.assertIn("BLE", r.message, name)
        r = self.call(GetCohnStatus, "get_cohn_status")
        self.assertTrue(r.success)
        self.assertFalse(r.status.supported)
        result, _ = self.run_action(ProvisionCohn, "provision_cohn", ProvisionCohn.Goal(ssid="x", password="y"))
        self.assertFalse(result.success)

        # --- topic trigger for hilights (only valid while recording)
        self.call(Capture, "start_recording")
        pub = self.node.create_publisher(Empty, f"{NS}/hilight_trigger", 10)
        before = len(MOCK.state.hilights)
        end = time.time() + 5
        while time.time() < end and len(MOCK.state.hilights) == before:
            pub.publish(Empty())
            rclpy.spin_once(self.node, timeout_sec=0.3)
        self.assertGreater(len(MOCK.state.hilights), before)
        r = self.call(Trigger, "sleep")
        self.assertFalse(r.success)  # refused while recording
        self.call(Capture, "stop_recording")

        r = self.call(Trigger, "sleep")
        self.assertTrue(r.success, r.message)
        self.assertTrue(MOCK.state.asleep)
        MOCK.state.asleep = False
        r = self.call(Trigger, "wake")
        self.assertTrue(r.success, r.message)


@launch_testing.post_shutdown_test()
class TestShutdown(unittest.TestCase):
    def test_exit_code(self, proc_info):
        launch_testing.asserts.assertExitCodes(proc_info, allowable_exit_codes=[0, -2, -15])
