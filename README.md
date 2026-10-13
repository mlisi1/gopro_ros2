# gopro_ros2

ROS 2 (Jazzy) driver that exposes a modern GoPro (primary target **HERO13 Black**; also HERO12/11/10, MAX 2, ...) as a full ROS 2 citizen, built on GoPro's official **Open GoPro** API and Python SDK (`open_gopro`).

It records high-quality footage to the SD card while ROS gets control, typed status, a live preview stream and **sync anchors** for aligning the footage with rosbags offline.

> **Disclaimer.** This project is not affiliated with, endorsed by or sponsored by GoPro, Inc. "GoPro", "HERO" and "Open GoPro" are trademarks of GoPro, Inc. The driver uses the public Open GoPro API and the `open_gopro` SDK as a declared dependency and ships no GoPro source code.

## Packages

| Package | Type | Contents |
|---|---|---|
| `gopro_camera_interfaces` | ament_cmake | `Status`, `RecordingEvent`, `HardwareInfo`, settings/preset/media messages, services, `DownloadMedia` / `ProvisionCohn` actions |
| `gopro_camera` | ament_python | lifecycle driver node, transports (USB, Wi-Fi AP, COHN, BLE), stream pipeline, fake camera for tests |
| `gopro_tools` | ament_python | `gopro-pull`, `gopro-gpmf`, `gopro-align`, `gopro-calibrate-sync` offline tools |

## Install

The SDK and the GPMF parser are pip-only, so use a virtualenv that can still see the ROS packages:

```bash
python3 -m venv --system-site-packages ~/gopro_venv
source ~/gopro_venv/bin/activate
pip install -r gopro_camera/requirements.txt -r gopro_tools/requirements.txt   # open_gopro, telemetry-parser, av
pip install aiohttp                                                            # only for tests / mock camera

cd ~/ros2_ws/src && git clone <this repo> gopro_ros2 && cd ..
rosdep install --from-paths src --ignore-src -y
source /opt/ros/jazzy/setup.bash
colcon build              # not --symlink-install: entry points must use `#!/usr/bin/env python3`
source install/setup.bash
```

Entry points use `#!/usr/bin/env python3`, so **activate the venv before `ros2 run` / `ros2 launch`**. `foxglove_msgs` is optional: without it, `~/image_h264` is published as `sensor_msgs/CompressedImage` with `format: h264`.

### Try it without a camera

```bash
ros2 run gopro_camera mock_camera --port 8080 &
ros2 launch gopro_camera gopro.launch.py config:=$(ros2 pkg prefix gopro_camera)/share/gopro_camera/config/mock.yaml
ros2 service call /gopro/gopro_camera/start_recording gopro_camera_interfaces/srv/Capture
```

## Transports

Select with the `transport` parameter: `usb | wifi_ap | cohn | ble | auto` (`auto` tries `auto_order`, default `usb, cohn, ble`).

| Transport | Control / settings | Status | Record | Stream | Media | Notes |
|---|---|---|---|---|---|---|
| `usb` | HTTP | HTTP poll (or BLE push with side channel) | yes | webcam | yes (fast) | powers the camera; camera IP `172.2X.1YZ.51` from serial |
| `wifi_ap` | HTTP (+BLE) | BLE push | yes | preview (webcam only on HERO12+) | yes | host joins camera AP via nmcli; slow, short range |
| `cohn` | HTTPS + basic auth | HTTP poll (or BLE push) | yes | webcam (per model) | yes | camera joins the robot network; provision once |
| `ble` | BLE | BLE push | yes | no | no | low bandwidth; ideal side channel |

**Hybrid mode** (`ble_sidechannel: true`): a BLE link next to USB/COHN for push status, wake-from-sleep, Wi-Fi AP control, COHN provisioning and RTMP livestream. HTTP does the heavy work; every command reports the transport that executed it (`RecordingEvent.transport`). If HTTP fails with a transport error, commands fall back to BLE where possible.

### USB setup

1. Plug the camera in. The driver enables *wired USB control* itself; the camera then appears as a CDC-NCM network interface and the host receives `172.2X.1YZ.5x/24` by DHCP.
2. Optional udev rule (keeps NetworkManager managing the link and tags it):
   ```bash
   sudo cp $(ros2 pkg prefix gopro_camera)/share/gopro_camera/udev/99-gopro-ncm.rules /etc/udev/rules.d/
   sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=net
   ```
3. Leave `serial` empty for a single camera (detected from the NCM address, then mDNS), or set it (last 3+ digits) / `usb_interface` with several cameras.

### BLE setup (BlueZ)

- `sudo apt install bluez` and make sure `bluetoothd` runs; the user needs D-Bus access to BlueZ (normally true for desktop users; on headless robots add the user to the `bluetooth` group).
- **Pair once**: put the camera in pairing mode (see GoPro's pairing instructions for your model) and start the driver; the SDK finds it by its advertised name `GoPro XXXX` (`ble_name`, or the last 4 digits of `serial`) and completes pairing.
- A camera asleep still advertises: connecting over BLE wakes it. This is how the side channel recovers a sleeping camera.

### COHN provisioning walkthrough

COHN makes the camera a client of the robot's access point. Provisioning needs BLE once:

1. Start the driver with any transport plus BLE (e.g. `transport: usb`, `ble_sidechannel: true`).
2. Provision (scans for the SSID, joins it, creates the certificate, stores credentials):
   ```bash
   ros2 action send_goal --feedback /gopro/gopro_camera/provision_cohn \
     gopro_camera_interfaces/action/ProvisionCohn "{ssid: robot_ap, password: '********'}"
   ```
   Credentials (IP, user, password, certificate) go to `cohn_credentials_path` (default `~/.ros/gopro/cohn_credentials.json`, directory `0700`, file `0600`).
3. Switch to `config/cohn.yaml` (`transport: cohn`, `serial` set). Use a DHCP reservation for the camera; with `ble_sidechannel` the IP is refreshed from the camera's COHN status.
4. `~/get_cohn_status` reports provisioning/network state.

### Wi-Fi AP

`transport: wifi_ap` and `wifi_ap.interface: wlan0`. The SDK reads SSID/password over BLE and joins with `nmcli` (needs an en_US locale and permission to manage Wi-Fi; `wifi_ap.sudo_password` exists but a polkit rule is preferable).

## ROS interface

Names are relative to the node (`~/x` = `/<namespace>/<node>/x`, e.g. `/gopro/gopro_camera/status`). One node and namespace per camera.

**Published**
- `~/status` (`gopro_camera_interfaces/Status`) - battery, encoding/busy/ready, SD state and remaining time, thermal, GPS lock, preset, lens/accessories, network/USB, active transport, plus every raw numeric status.
- `/diagnostics` - link, battery, SD card, thermal, stream stall.
- `~/recording_event` (`RecordingEvent`, reliable, transient-local depth 100) - START / STOP / HILIGHT / PHOTO / ADOPTED / EXTERNAL_START / EXTERNAL_STOP with `command_sent`, `camera_ack`, `encoding_changed` (+ uncertainty) and the camera `filename`. **This is the sync anchor between bags and SD footage.**
- `~/hardware_info` (transient-local) - model, serial, firmware, minimum supported firmware and whether it is met.
- `~/image_raw` (`sensor_msgs/Image`, bgr8), `~/image_h264` (H.264 passthrough), `~/camera_info`.

**Subscribed**: `~/hilight_trigger` (`std_msgs/Empty`) - tag the footage from any node (only while recording).

**Services**: `start_recording`, `stop_recording`, `take_photo`, `hilight` (`Capture`, returns the RecordingEvent); `load_preset`, `get_presets`; `set_settings` (batch, validated, rollback on failure), `get_settings`, `get_capabilities`, `get_hardware_info`; `sync_datetime`, `sleep`, `power_down`, `wake`; `set_wifi_ap`, `provision_cohn`, `get_cohn_status`; `start_stream`, `stop_stream`; `start_livestream`, `stop_livestream`; `list_media`, `delete_media` (needs `confirm: true`), `get_media_asset` (thumbnail / screennail / gpmf / telemetry / info).

**Actions**: `~/download_media` (files or "since mission start"/"since time", progress feedback, cancelable, turbo transfer when idle), `~/provision_cohn` (stage feedback).

### Parameters

See `ros2 param describe` or `gopro_camera/node.py` (`PARAMS`). Main groups: connection (`transport`, `serial`, `ble_name`, `ble_sidechannel`, `usb_interface`, `cohn_credentials_path`, `wifi_ap.*`), `on_connect.*` (`sync_datetime`, `load_preset`, `apply_settings`), `status.*`, `keep_alive_period` (3 s per spec), `reconnect.*`, `stream.*`, `camera_info_url`, `media.*`, `diagnostics.*`.

**Camera settings** are exposed as `settings.<name>` for every setting in the official spec (`video_resolution`, `frames_per_second`, `video_lens`, `hypersmooth`, `hindsight`, `video_bit_rate`, `gps`, `led`, `anti_flicker`, `max_lens_mod`, `auto_power_down`, `control_mode`, `profiles`, ...). Values are spec labels (`"4K"`, `"60"`, `"Linear"`, `"On"`) or option IDs. They are applied on `configure` and can be changed live; changes are validated against the camera model's options (from the spec), the camera's runtime capabilities (BLE / HTTP 403 answers) and **rejected while the camera is encoding** with the reason. Unset parameters are left untouched. `ros2 param describe /gopro/gopro_camera settings.video_resolution` lists the options.

All setting/status IDs and options come from `gopro_camera/spec_tables.py`, generated from the official OpenAPI spec:
```bash
python3 gopro_camera/scripts/generate_spec_tables.py   # downloads https://gopro.github.io/OpenGoPro/http/openapi.json
```

### Lifecycle

`autostart: true` (default) configures and activates at startup, retrying every `autostart_retry_period` until the camera is reachable. Otherwise drive it with `ros2 lifecycle set`.
- **configure**: connect, claim external control, hardware info, date/time sync, load preset, apply `settings.*`.
- **activate**: publish status/images; start the stream if `stream.enabled`.
- **deactivate**: stop the stream; an ongoing recording continues unless `stop_recording_on_deactivate`.
- **cleanup/shutdown**: release control and disconnect (`sleep_on_shutdown` optional; never while recording).
- **link loss**: diagnostics error, reconnect with exponential backoff; after reconnecting, a running recording is detected and *adopted* (ADOPTED event).

**Recording is never stopped by the driver on its own**: not on crash, restart, deactivate, sleep requests or stream start (the SDK's stream helpers stop the shutter first; the driver refuses instead).

## Time synchronisation model

- On connect the camera clock is set from the host clock with timezone/DST (`sync_datetime`, ~1 s resolution); the service reports the camera-host offset.
- Each START/STOP records: `command_sent` (ROS time before sending), `camera_ack` (reply received), and `encoding_changed` - the first time the driver saw the *encoding* status flip, with `encoding_changed_uncertainty` (time since the previous status sample, capped at the time since the command was sent; ~0 with BLE push; with HTTP polling the driver polls at 10 Hz from just before the shutter command until the change is confirmed).
- Measured on a HERO13 Black over USB (5.3K30, firmware H24.01.02.04.00): the camera answers shutter-start only once recording runs (~470 ms) and **holds state queries meanwhile**, so over HTTP `encoding_changed` arrives ~0.2 s after the first frame with ~0.6 s uncertainty. The ack, however, is a stable reference: **first frame ≈ `camera_ack` - 37 ms (sd ~7 ms)**. After shutter-stop the ack comes in ~40 ms but *encoding* stays on for 1-2 s while the file is finalised, so for STOP `encoding_changed` is the end of finalisation, not of the footage; `busy` stays 1 for the whole recording.
- `gopro-align` maps video `t=0` to `camera_ack + start_offset` when a **sync calibration** matches (model, transport, frame rate; see below), otherwise to `encoding_changed`; then it optionally refines by cross-correlating the GoPro gyro (GPMF) with a robot IMU, searching within the anchor's uncertainty.

### Sync calibration (`gopro-calibrate-sync`)

The start offset depends on the camera configuration (model, firmware, frame rate, resolution, possibly HyperSmooth) and transport, so measure it for the configuration you record missions with. `gopro_tools` ships one default (the HERO13/USB/5.3K30 measurement above); results you measure go to `~/.ros/gopro/sync_calibration.yaml` and take precedence.

1. Stop the driver node. Set the camera as for a mission; **HindSight off** (it would prepend footage from before the command). Higher frame rates resolve the start more finely.
2. Run the tool. In Docker, use the `driver` service (host network; `~/.ros/gopro` and `~/gopro_media` are mounted, so the calibration and the per-run details in `~/gopro_media/calibration/<date>/` land on the host): `docker compose -f docker/compose.yaml run --rm driver ros2 run gopro_tools gopro-calibrate-sync --stream-latency 20`. Natively:
   ```bash
   ros2 run gopro_tools gopro-calibrate-sync --n 10 --stream-latency 20
   ```
3. Open `http://localhost:8765/` on a screen of the same host, full screen, and point the camera at it (the three markers large in the frame, no glare); press Enter.

The page shows the host clock as three ArUco markers (seconds, milliseconds, checksum), redrawn every display refresh. The tool records `--n` short videos, decodes the clock in the first frames of each, and reports *first frame - ack* per recording, then saves mean and uncertainty (2 sd combined with `--display-lag-uncertainty-ms`, default 20 ms: the monitor shows each value slightly late; pass `--display-lag-ms` if you know your display's lag). `--stream-latency N` also measures the webcam stream latency for N seconds and prints the value for `stream.latency_offset`. Details per run go to `~/gopro_media/calibration/<date>/` (`recordings.csv` with outliers marked, `summary.json`). The offset is the median over recordings; an occasional late ack (> 4 robust sd from the median) is excluded and listed.

## Live stream

Webcam mode (USB/COHN; over Wi-Fi only on HERO12+ per the spec) or preview (Wi-Fi AP). H.264 in MPEG-TS over UDP (or RTSP) is received with PyAV:
- `~/image_h264`: access units as received, no decoding, cheap to bag.
- `~/image_raw`: decoded, optionally downscaled (`stream.scale`) and rate-limited (`stream.max_rate`).
- `~/camera_info`: from `camera_info_url`, rescaled to the published size.

Frames are stamped with **receive time - `stream.latency_offset`**. GoPro stream latency is roughly 1-2 s and varies with mode/resolution/link: **the stream is not suitable for tight sensor fusion.** Use it for monitoring; use SD footage + `recording_event` + `gopro-align` for data.

**Stream latency.** Measure it for your configuration with `gopro-calibrate-sync --n 0 --stream-latency 30` (see *Sync calibration*): it decodes the on-screen clock in the received frames and prints the median receive-minus-shown time; set `stream.latency_offset` to it. `test_hardware.py::test_stream_60s_latency_and_drops` measures frame rate, dropped frames (PTS gaps) and jitter.

### Calibration

GoPro lenses are fisheye: calibrate the exact stream (or recording) mode you use, at its resolution and lens (e.g. *Linear* gives near-pinhole images; *Wide*/*SuperView* need the fisheye model).
```bash
ros2 run camera_calibration cameracalibrator --size 8x6 --square 0.04 \
  --ros-args -r image:=/gopro/gopro_camera/image_raw -r camera:=/gopro/gopro_camera
```
Set the *Camera type* slider to **1 (fisheye)** for the `equidistant` model, save, and point `camera_info_url` at the YAML (`file:///path/gopro_1080_wide.yaml`). Kalibr or OpenCV's `cv2.fisheye` work too; the driver reads the standard ROS calibration YAML.

## Offline tools (`gopro_tools`)

```bash
# Download the files a mission recorded (from a bag's recording events), or by time window
ros2 run gopro_tools gopro-pull --bag mission_bag/ --dest media/              # direct over USB (driver stopped)
ros2 run gopro_tools gopro-pull --ros --node /gopro/gopro_camera --dest media/ # through the running driver
ros2 run gopro_tools gopro-pull --since 2026-10-07T09:00 --until 2026-10-07T15:00

# GPMF telemetry -> CSV/JSON (IMU in rad/s & m/s^2 in the camera frame, GPS, frame PTS)
ros2 run gopro_tools gopro-gpmf media/100GOPRO/GX010042.MP4 --out telemetry/

# Align with the bag; refine with the robot IMU; write a bag with GoPro IMU/GPS and every 30th frame
ros2 run gopro_tools gopro-align mission_bag/ media/100GOPRO/GX01*.MP4 media/100GOPRO/GX02*.MP4 \
  --imu-topic /imu/data --out aligned/ --out-bag gopro_aligned_bag --frame-stride 30

# Measure the start-sync offset (and stream latency) for the current camera configuration
ros2 run gopro_tools gopro-calibrate-sync --n 10 --stream-latency 20
```
`gopro-align` reads the packaged calibrations plus `~/.ros/gopro/sync_calibration.yaml` (`--sync-calibration FILE` to choose, `--no-sync-calibration` to anchor on `encoding_changed`); `alignment.json` reports which anchor and calibration each recording used.
GPMF parsing uses [`telemetry-parser`](https://github.com/AdrianEddy/telemetry-parser) (MIT/Apache-2.0, x86_64 and aarch64 wheels); GoPro's `gpmf-parser` bindings (`py-gpmf-parser`) were considered but ship x86_64 wheels only.

## Testing

```bash
source ~/gopro_venv/bin/activate && source install/setup.bash
python3 -m pytest gopro_camera/test gopro_tools/test           # unit + mock-camera + launch_testing (no hardware)
GOPRO_SAMPLE_MP4=hero8.mp4 python3 -m pytest gopro_tools/test  # + end-to-end align on a real GoPro file
GOPRO_TRANSPORT=usb python3 -m pytest gopro_camera/test/test_hardware.py --hardware -s   # real camera
```
The fake camera (`gopro_camera/testing/mock_camera.py`, aiohttp) implements the HTTP endpoints the driver uses with realistic state transitions (busy → encoding → idle, rejections while encoding, webcam state machine, sleep, going offline). `test_launch_services.py` brings the node up against it and calls every service and action. Hardware tests cover connect, settings round-trip, 10 s recording with hilight + download, 60 s stream and a soak test that records to SD for `GOPRO_SOAK_S` seconds (opt-in: skipped unless the variable is set).

### Docker

`docker/` has an image (ROS 2 Jazzy, rosdep deps, venv with the pip-only deps, workspace built) and a compose file with three services. Run from the repository root:

```bash
mkdir -p ~/.ros/gopro ~/gopro_media                        # once: mounted by the driver service (see below)
docker compose -f docker/compose.yaml build                # uid/gid other than 1000: HOST_UID=$(id -u) HOST_GID=$(id -g) docker compose ... build
docker compose -f docker/compose.yaml run --rm test        # ruff + full test suite, no hardware, isolated network
docker compose -f docker/compose.yaml run --rm dev         # shell; source bind-mounted, `run_tests` rebuilds first
docker compose -f docker/compose.yaml run --rm test run_tests -k settings     # extra args go to pytest
GOPRO_CONFIG=usb.yaml docker compose -f docker/compose.yaml up driver         # real camera
```

The `driver` service uses host networking (the USB NCM link, COHN and the UDP stream are host interfaces) and `ipc: host` so DDS shared memory works with ROS nodes on the host. BLE goes through the host's BlueZ via the mounted system D-Bus socket. COHN credentials and sync calibrations are kept in `~/.ros/gopro`, downloads and calibration runs in `~/gopro_media` on the host. Containers run as an unprivileged user with your uid/gid (build args `USER_UID`/`USER_GID`, from `HOST_UID`/`HOST_GID`, default 1000), so nothing they write is root-owned; the two host directories must exist (Docker would create missing ones as root, so the compose file refuses instead). Parameter files are read from `docker/config/` (`GOPRO_CONFIG` picks one). Run the hardware tests against a real camera with `docker compose -f docker/compose.yaml run --rm driver run_tests --hardware -s`. Build args: `FETCH_SAMPLE` (download GoPro's public HERO8 sample MP4 for the end-to-end align test, default 1) and `WITH_WIFI_AP` (install `nmcli` for the `wifi_ap` transport, default 0).

## Known limitations

- **Not tested on real hardware yet**: transports were exercised against the fake HTTP camera; BLE, Wi-Fi AP and COHN paths rely on the SDK and need the hardware tests.
- The SDK's HTTP layer is blocking (`requests`); the driver runs it in worker threads. When the camera is unreachable, the SDK retries for up to ~10 s per request, so link-loss detection takes about `reconnect.link_loss_threshold × status.timeout`.
- `open_gopro` 0.22 does not wrap every endpoint of the current spec (e.g. hilight-while-recording and sleep over HTTP, settings newer than the SDK); the driver sends those through the SDK's HTTP transport. Settings unknown to the SDK cannot be set over BLE.
- HTTP has no per-setting capability query; runtime option validation uses BLE capabilities when BLE is connected, otherwise the spec's per-model table plus the camera's 403 answers.
- Webcam and recording are mutually exclusive; preview/webcam start is refused while recording.
- Camera media timestamps are local wall-clock time without zone (`media.camera_clock_is_local`).
- No OTA updates, no hardware trigger/PPS (non-goals).

## License

MIT, see [LICENSE](LICENSE).

