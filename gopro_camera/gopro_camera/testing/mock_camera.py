"""Fake Open GoPro HTTP camera (aiohttp) for tests and demos without hardware.

Implements the subset of the official HTTP API the driver uses, with realistic state transitions:
idle -> (shutter start) busy -> encoding -> (shutter stop) idle + new media file; settings rejected
while encoding; hilight only while encoding; webcam state machine; sleep; going offline.

Run standalone: ``ros2 run gopro_camera mock_camera --port 8080`` then start the driver with
``transport: usb`` and ``usb_base_url: http://127.0.0.1:8080/``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from gopro_camera.settings_map import SETTING_BY_NAME, SETTING_DEFS, options_for_model
from gopro_camera.status import S


def _preset_title_id(name: str) -> int:
    try:
        from open_gopro.models import proto

        return int(proto.EnumPresetTitle.Value(name))
    except Exception:
        return 0


def _group_id(name: str) -> int:
    from open_gopro.models import proto

    return int(proto.EnumPresetGroup.Value(name))


@dataclass
class MockState:
    model_name: str = "HERO13 Black"
    model_number: int = 65
    serial: str = "C3501324500123"
    firmware: str = "H24.01.02.02.00"
    statuses: dict[int, Any] = field(default_factory=dict)
    settings: dict[int, Any] = field(default_factory=dict)
    files: dict[str, bytes] = field(default_factory=dict)
    file_times: dict[str, int] = field(default_factory=dict)
    hilights: list[int] = field(default_factory=list)
    camera_dt: tuple[str, str, int, int] = ("2024_1_1", "0_0_0", 0, 0)
    webcam_status: int = 0  # spec: 0 off, 1 idle, 2 high power preview, 3 low power preview
    preview_on: bool = False
    turbo: bool = False
    control: int = 0
    wired_usb: bool = False
    asleep: bool = False
    next_index: int = 1
    # Timing as measured on a HERO13 over USB: the shutter-start reply comes once recording runs
    # (~470 ms; the first frame ~37 ms before the reply), state queries are held while the command is
    # processed, busy stays 1 for the whole recording, and encoding stays 1 for ~1-2 s after a stop
    # while the file is finalised. Defaults are shortened to keep tests fast.
    busy_time: float = 0.3  # shutter start -> encoding
    shutter_reply_delay: float = 0.0  # encoding -> shutter-start reply
    stop_finalize_time: float = 0.2  # stop reply -> encoding 0
    hold_state_during_shutter: bool = True
    # media transfer behaviour (offload tests)
    video_template: bytes = b""  # content of new videos (e.g. a real MP4); empty = filler bytes
    download_rate: float = 0.0  # bytes/s for /videos/DCIM downloads; 0 = unthrottled
    serve_wrong_bytes: bool = False  # serve a file shorter than the media list says (corrupt copy)
    sd_reads_while_encoding: int = 0  # download chunks pushed to the socket while encoding
    downloads_started_while_encoding: int = 0  # download requests received while encoding (offload: 0)
    shutter_in_flight: bool = False
    finalizing: bool = False
    request_log: list[str] = field(default_factory=list)
    presets_loaded: list[int] = field(default_factory=list)


class MockCamera:
    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self.host = host
        self.port = port
        self.state = MockState()
        self._init_state()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._runner: Any = None
        self._site: Any = None
        self._tasks: set[asyncio.Task[Any]] = set()

    # ------------------------------------------------------------------ state
    def _init_state(self) -> None:
        st = self.state
        st.statuses = {
            S.BATTERY_PRESENT: 1,
            S.BATTERY_BARS: 3,
            S.BATTERY_PERCENT: 87,
            S.OVERHEATING: 0,
            S.COLD: 0,
            S.BUSY: 0,
            S.ENCODING: 0,
            S.READY: 1,
            S.VIDEO_ENCODING_DURATION: 0,
            S.PRIMARY_STORAGE: 0,
            S.REMAINING_VIDEO_TIME: 7200,
            S.REMAINING_PHOTOS: 3000,
            S.SD_REMAINING: 60_000_000,
            S.PHOTOS: 0,
            S.VIDEOS: 1,
            S.GPS_LOCK: 0,
            S.PRESET_GROUP: _group_id("PRESET_GROUP_ID_VIDEO"),
            S.PRESET: 0,
            S.PRESET_MODIFIED: 0,
            S.HINDSIGHT: 0,
            S.ACTIVE_HILIGHTS: 0,
            S.USB_CONNECTED: 1,
            S.USB_CONTROLLED: 0,
            S.CAMERA_CONTROL: 0,
            S.PREVIEW_STREAM: 0,
            S.AP_MODE: 0,
        }
        for name, opt in (("video_resolution", 1), ("frames_per_second", 8), ("hypersmooth", 4), ("gps", 1)):
            st.settings[SETTING_BY_NAME[name].id] = opt
        st.settings[SETTING_BY_NAME["lcd_brightness"].id] = 50
        self.add_file("100GOPRO/GX010001.MP4", b"\x00" * 300_000, created=1_700_000_000)

    def add_file(self, path: str, data: bytes, created: int | None = None) -> None:
        self.state.files[path] = data
        self.state.file_times[path] = created if created is not None else self.camera_now()

    def camera_now(self) -> int:
        import calendar
        import datetime

        return calendar.timegm(datetime.datetime.now().timetuple())

    def _spawn(self, coro: Any) -> None:
        t = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def _start_encoding(self) -> None:
        st = self.state
        st.statuses[S.BUSY] = 1
        await asyncio.sleep(st.busy_time)
        st.statuses[S.ENCODING] = 1  # busy stays 1 while encoding (HERO13)
        st.statuses[S.READY] = 0
        group = st.statuses[S.PRESET_GROUP]
        if group == _group_id("PRESET_GROUP_ID_PHOTO"):
            await asyncio.sleep(0.3)
            self._finish_capture(".JPG")
            return
        t0 = time.monotonic()
        while st.statuses[S.ENCODING]:
            st.statuses[S.VIDEO_ENCODING_DURATION] = int(time.monotonic() - t0)
            await asyncio.sleep(0.2)

    async def _finalize(self, ext: str) -> None:
        st = self.state
        st.finalizing = True
        try:
            await asyncio.sleep(st.stop_finalize_time)
            self._finish_capture(ext)
        finally:
            st.finalizing = False

    async def _start_encoding_task(self) -> None:
        """Start encoding in the background; return once the camera reports encoding (or a photo is done)."""
        st = self.state
        st.statuses[S.BUSY] = 1
        self._spawn(self._start_encoding())
        while not st.statuses[S.ENCODING] and st.statuses[S.BUSY]:
            await asyncio.sleep(0.005)

    def _finish_capture(self, ext: str) -> None:
        st = self.state
        st.statuses[S.ENCODING] = 0
        st.statuses[S.BUSY] = 0
        st.statuses[S.READY] = 1
        st.statuses[S.VIDEO_ENCODING_DURATION] = 0
        st.next_index += 1
        name = f"100GOPRO/GX01{st.next_index:04d}{ext}" if ext == ".MP4" else f"100GOPRO/GOPR{st.next_index:04d}{ext}"
        data = st.video_template if ext == ".MP4" and st.video_template else b"\x01" * 200_000
        self.add_file(name, data)
        st.statuses[S.VIDEOS if ext == ".MP4" else S.PHOTOS] += 1
        st.last_captured = name  # type: ignore[attr-defined]

    def external_shutter(self, on: bool) -> None:
        """Simulate the camera button (thread-safe)."""
        assert self._loop is not None

        def _do() -> None:
            if on and not self.state.statuses[S.ENCODING]:
                self._spawn(self._start_encoding())
            elif not on and self.state.statuses[S.ENCODING] and not self.state.finalizing:
                self._spawn(self._finalize(".MP4"))

        self._loop.call_soon_threadsafe(_do)

    # ------------------------------------------------------------------ http
    def _app(self) -> Any:
        from aiohttp import web

        st = self.state
        routes = web.RouteTableDef()

        def ok(data: Any = None, status: int = 200) -> web.Response:
            return web.json_response({} if data is None else data, status=status)

        @web.middleware
        async def mw(request: web.Request, handler: Any) -> web.StreamResponse:
            st.request_log.append(request.path_qs)
            if st.asleep:
                raise web.HTTPServiceUnavailable()
            return await handler(request)

        @routes.get("/gopro/version")
        async def version(_r: web.Request) -> web.Response:
            return ok({"version": "2.0"})

        @routes.get("/gopro/camera/control/wired_usb")
        async def wired(r: web.Request) -> web.Response:
            st.wired_usb = r.query.get("p") == "1"
            st.statuses[S.USB_CONTROLLED] = int(st.wired_usb)
            return ok()

        @routes.get("/gopro/camera/analytics/set_client_info")
        async def client(_r: web.Request) -> web.Response:
            return ok()

        @routes.get("/gopro/camera/control/set_ui_controller")
        async def control(r: web.Request) -> web.Response:
            st.control = int(r.query.get("p", 0))
            st.statuses[S.CAMERA_CONTROL] = st.control
            return ok()

        @routes.get("/gopro/camera/keep_alive")
        async def keep_alive(_r: web.Request) -> web.Response:
            return ok()

        @routes.get("/gopro/camera/info")
        async def info(_r: web.Request) -> web.Response:
            return ok(
                {
                    "model_number": st.model_number,
                    "model_name": st.model_name,
                    "firmware_version": st.firmware,
                    "serial_number": st.serial,
                    "ap_mac_addr": "0441693db8a2",
                    "ap_ssid": "GP24500123",
                }
            )

        @routes.get("/gopro/camera/state")
        async def state(_r: web.Request) -> web.Response:
            while st.hold_state_during_shutter and st.shutter_in_flight:
                await asyncio.sleep(0.005)
            return ok(
                {
                    "status": {str(k): v for k, v in st.statuses.items()},
                    "settings": {str(k): v for k, v in st.settings.items()},
                }
            )

        @routes.get("/gopro/camera/shutter/{mode}")
        async def shutter(r: web.Request) -> web.Response:
            mode = r.match_info["mode"]
            if st.webcam_status in (2, 3):
                return ok({"error": "webcam active"}, status=500)
            if mode == "start":
                if st.statuses[S.ENCODING] or st.statuses[S.BUSY]:
                    return ok({}, status=500)
                st.shutter_in_flight = True
                try:
                    await self._start_encoding_task()
                    await asyncio.sleep(st.shutter_reply_delay)
                finally:
                    st.shutter_in_flight = False
            elif st.statuses[S.ENCODING] and not st.finalizing:
                self._spawn(self._finalize(".MP4"))
            return ok()

        @routes.get("/gopro/media/hilight/moment")
        async def hilight(_r: web.Request) -> web.Response:
            if not st.statuses[S.ENCODING]:
                return ok({}, status=500)
            st.hilights.append(st.statuses[S.VIDEO_ENCODING_DURATION])
            st.statuses[S.ACTIVE_HILIGHTS] += 1
            return ok()

        @routes.get("/gopro/camera/setting")
        async def setting(r: web.Request) -> web.Response:
            sid = int(r.query["setting"])
            if "option" not in r.query:
                return ok({"option": st.settings.get(sid, 0)})
            opt = int(r.query["option"])
            if sid not in SETTING_DEFS:
                return ok({"error": 2, "setting_id": sid, "option_id": opt}, status=403)
            if st.statuses[S.ENCODING]:
                return ok({"error": 1, "setting_id": sid, "option_id": opt}, status=403)
            sdef = SETTING_DEFS[sid]
            allowed = options_for_model(sdef, st.model_name)
            if sdef.is_enumerated and opt not in allowed:
                return ok(
                    {
                        "error": 3,
                        "setting_id": sid,
                        "option_id": opt,
                        "supported_options": [{"display_name": v, "id": k} for k, v in allowed.items()],
                    },
                    status=403,
                )
            st.settings[sid] = opt
            st.statuses[S.PRESET_MODIFIED] = 1
            return ok()

        @routes.get("/gopro/camera/presets/get")
        async def presets(_r: web.Request) -> web.Response:
            def p(pid: int, title: str, settings: list[tuple[int, int]]) -> dict[str, Any]:
                return {
                    "id": pid,
                    "titleId": _preset_title_id(title),
                    "titleNumber": 0,
                    "userDefined": False,
                    "isFixed": True,
                    "isModified": False,
                    "settingArray": [{"id": s, "value": v, "isCaption": False} for s, v in settings],
                }

            res = SETTING_BY_NAME["video_resolution"].id
            return ok(
                {
                    "presetGroupArray": [
                        {
                            "id": _group_id("PRESET_GROUP_ID_VIDEO"),
                            "canAddPreset": True,
                            "presetArray": [
                                p(0, "PRESET_TITLE_STANDARD", [(res, 1)]),
                                p(1, "PRESET_TITLE_ACTIVITY", []),
                            ],
                        },
                        {
                            "id": _group_id("PRESET_GROUP_ID_PHOTO"),
                            "canAddPreset": True,
                            "presetArray": [p(65536, "PRESET_TITLE_PHOTO", [])],
                        },
                        {
                            "id": _group_id("PRESET_GROUP_ID_TIMELAPSE"),
                            "canAddPreset": True,
                            "presetArray": [p(131072, "PRESET_TITLE_TIME_LAPSE", [])],
                        },
                    ]
                }
            )

        def _group_of(pid: int) -> int:
            if pid >= 131072:
                return _group_id("PRESET_GROUP_ID_TIMELAPSE")
            if pid >= 65536:
                return _group_id("PRESET_GROUP_ID_PHOTO")
            return _group_id("PRESET_GROUP_ID_VIDEO")

        @routes.get("/gopro/camera/presets/load")
        async def load_preset(r: web.Request) -> web.Response:
            if st.statuses[S.ENCODING]:
                return ok({}, status=500)
            pid = int(r.query["id"])
            st.statuses[S.PRESET] = pid
            st.statuses[S.PRESET_GROUP] = _group_of(pid)
            st.presets_loaded.append(pid)
            return ok()

        @routes.get("/gopro/camera/presets/set_group")
        async def load_group(r: web.Request) -> web.Response:
            gid = int(r.query["id"])
            st.statuses[S.PRESET_GROUP] = gid
            st.statuses[S.PRESET] = {1000: 0, 1001: 65536, 1002: 131072}.get(gid, 0)
            return ok()

        @routes.get("/gopro/camera/set_date_time")
        async def set_dt(r: web.Request) -> web.Response:
            st.camera_dt = (r.query["date"], r.query["time"], int(r.query.get("tzone", 0)), int(r.query.get("dst", 0)))
            return ok()

        @routes.get("/gopro/camera/get_date_time")
        async def get_dt(_r: web.Request) -> web.Response:
            import datetime

            now = datetime.datetime.now()
            return ok(
                {
                    "date": f"{now.year}_{now.month}_{now.day}",
                    "time": f"{now.hour}_{now.minute}_{now.second}",
                    "tzone": st.camera_dt[2],
                    "dst": st.camera_dt[3],
                }
            )

        @routes.get("/gp/gpControl/command/system/sleep")
        async def sleep(_r: web.Request) -> web.Response:
            st.asleep = True
            return ok()

        @routes.get("/gopro/media/list")
        async def media_list(_r: web.Request) -> web.Response:
            dirs: dict[str, list[dict[str, str]]] = {}
            for path, data in sorted(st.files.items()):
                d, n = path.split("/", 1)
                cre = str(st.file_times[path])
                dirs.setdefault(d, []).append({"n": n, "cre": cre, "mod": cre, "s": str(len(data))})
            return ok({"id": "1", "media": [{"d": d, "fs": fs} for d, fs in dirs.items()]})

        @routes.get("/gopro/media/last_captured")
        async def last(_r: web.Request) -> web.Response:
            name = getattr(st, "last_captured", None) or sorted(st.files)[-1]
            folder, file = name.split("/", 1)
            return ok({"folder": folder, "file": file})

        @routes.get("/gopro/media/info")
        async def media_info(r: web.Request) -> web.Response:
            path = r.query["path"]
            if path not in st.files:
                return ok({}, status=404)
            return ok({"s": str(len(st.files[path])), "cre": str(st.file_times[path]), "hc": str(len(st.hilights))})

        @routes.get("/gopro/media/delete/file")
        async def delete(r: web.Request) -> web.Response:
            path = r.query["path"]
            if path not in st.files:
                return ok({}, status=500)
            del st.files[path]
            return ok()

        @routes.get("/gopro/media/turbo_transfer")
        async def turbo(r: web.Request) -> web.Response:
            st.turbo = r.query.get("p") == "1"
            return ok()

        @routes.get("/videos/DCIM/{folder}/{file}")
        async def download(r: web.Request) -> web.StreamResponse:
            path = f"{r.match_info['folder']}/{r.match_info['file']}"
            if path not in st.files:
                raise web.HTTPNotFound()
            if st.statuses[S.ENCODING]:
                st.downloads_started_while_encoding += 1
            data = st.files[path][:-10] if st.serve_wrong_bytes else st.files[path]
            resp = web.StreamResponse(headers={"Content-Type": "application/octet-stream"})
            resp.content_length = len(data)
            await resp.prepare(r)
            chunk = 32 * 1024
            for i in range(0, len(data), chunk):
                if st.statuses[S.ENCODING]:
                    st.sd_reads_while_encoding += 1
                part = data[i : i + chunk]
                await resp.write(part)
                if st.download_rate > 0:
                    await asyncio.sleep(len(part) / st.download_rate)
            await resp.write_eof()
            return resp

        for kind in ("gpmf", "thumbnail", "screennail", "telemetry"):

            async def asset(r: web.Request, _k: str = kind) -> web.Response:
                if r.query.get("path") not in st.files:
                    raise web.HTTPNotFound()
                return web.Response(body=f"{_k}-data".encode(), content_type="application/octet-stream")

            routes.get(f"/gopro/media/{kind}")(asset)

        @routes.get("/gopro/webcam/status")
        async def wc_status(_r: web.Request) -> web.Response:
            return ok({"status": st.webcam_status, "error": 0})

        @routes.get("/gopro/webcam/start")
        async def wc_start(_r: web.Request) -> web.Response:
            if st.statuses[S.ENCODING]:
                return ok({"status": st.webcam_status, "error": 4})
            st.webcam_status = 2
            return ok({"status": st.webcam_status, "error": 0})

        @routes.get("/gopro/webcam/stop")
        async def wc_stop(_r: web.Request) -> web.Response:
            st.webcam_status = 1
            return ok({"status": st.webcam_status, "error": 0})

        @routes.get("/gopro/webcam/exit")
        async def wc_exit(_r: web.Request) -> web.Response:
            st.webcam_status = 0
            return ok({"status": st.webcam_status, "error": 0})

        @routes.get("/gopro/camera/stream/{mode}")
        async def preview(r: web.Request) -> web.Response:
            st.preview_on = r.match_info["mode"] == "start"
            st.statuses[S.PREVIEW_STREAM] = int(st.preview_on)
            return ok()

        app = web.Application(middlewares=[mw])
        app.add_routes(routes)
        return app

    # ------------------------------------------------------------------ lifecycle
    async def _serve(self) -> None:
        from aiohttp import web

        self._runner = web.AppRunner(self._app())
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()
        self.port = self._site._server.sockets[0].getsockname()[1]

    def start(self) -> str:
        ready = threading.Event()

        def _run() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._serve())
            ready.set()
            self._loop.run_forever()
            self._loop.run_until_complete(self._runner.cleanup())
            self._loop.close()

        self._thread = threading.Thread(target=_run, name="mock-gopro", daemon=True)
        self._thread.start()
        ready.wait(10)
        return self.base_url

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    def set_offline(self, offline: bool) -> None:
        """Stop/restart the listening socket (simulates unplug / camera off)."""
        assert self._loop is not None

        async def _toggle() -> None:
            from aiohttp import web

            if offline and self._site is not None:
                await self._site.stop()
                self._site = None
            elif not offline and self._site is None:
                self._site = web.TCPSite(self._runner, self.host, self.port)
                await self._site.start()

        asyncio.run_coroutine_threadsafe(_toggle(), self._loop).result(5)

    def stop(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(5)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Fake Open GoPro HTTP camera")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args, _ = parser.parse_known_args(argv)
    cam = MockCamera(args.host, args.port)
    url = cam.start()
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("mock_camera").info("mock GoPro listening on %s", url)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        cam.stop()


if __name__ == "__main__":
    main()
