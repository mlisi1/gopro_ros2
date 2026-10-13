"""gopro-calibrate-sync: measure the start-sync offset (and stream latency) against an on-screen clock.

1. Serves a page that shows the host clock as ArUco markers (``--port``, default 8765). Open it on a
   screen attached to this host, full screen, and point the camera at it (markers filling a good part
   of the frame, no glare). Use the camera settings you record missions with; HindSight must be off.
2. Records ``--n`` short videos through the driver's camera facade (the driver node must not run),
   downloads each one and decodes the clock in its first frames.
3. Reports first frame - camera_ack (and - command_sent) per recording, and writes the calibration
   for this model / transport / frame rate / resolution / firmware to ``--calibration-file``
   (default ~/.ros/gopro/sync_calibration.yaml), which ``gopro-align`` reads. Per-run details go to
   ``--out`` (default ~/gopro_media/calibration/<date>).
4. Optionally (``--stream-latency SECONDS``) starts the webcam stream and measures receive time -
   shown time, i.e. the value for the driver's ``stream.latency_offset`` parameter.

Results are only valid for the configuration measured. The monitor shows each clock value a little
late (display lag); pass ``--display-lag-ms`` if you know it, otherwise its uncertainty is covered
by ``--display-lag-uncertainty-ms``.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import datetime
import http.server
import json
import logging
import math
import statistics
import sys
import threading
import time
from pathlib import Path
from typing import Any

from gopro_tools import sync_calibration as sc
from gopro_tools import timecode as tc
from gopro_tools.gpmf import video_meta

log = logging.getLogger("gopro-calibrate-sync")

LOGGED_SETTINGS = ("video_resolution", "frames_per_second", "video_lens", "hypersmooth", "hindsight")


# ------------------------------------------------------------------------------------------ clock page
def serve_clock(port: int) -> http.server.ThreadingHTTPServer:
    page = tc.PAGE.encode()
    markers = tc.markers_json().encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            is_markers = self.path.startswith("/markers.json")
            body, ctype = (markers, "application/json") if is_markers else (page, "text/html")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: Any) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, name="clock-page", daemon=True).start()
    return server


# ------------------------------------------------------------------------------------------ camera
async def _connect(args: argparse.Namespace) -> Any:
    from gopro_camera.camera import CameraConfig, GoProCamera
    from gopro_camera.transports import TransportConfig

    tcfg = TransportConfig(
        transport=args.transport,
        serial=args.serial or None,
        ble_sidechannel=args.ble,
        usb_base_url=args.usb_base_url or None,
    )
    cam = GoProCamera(CameraConfig(transport=tcfg))
    await cam.connect()
    return cam


def _settings(cam: Any) -> dict[str, str]:
    from gopro_camera.settings_map import SETTING_BY_NAME

    out = {}
    for name in LOGGED_SETTINGS:
        sdef = SETTING_BY_NAME.get(name)
        if sdef is not None and sdef.id in cam.settings:
            out[name] = sdef.label(int(cam.settings[sdef.id]))
    return out


async def record_and_measure(cam: Any, args: argparse.Namespace, out: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for i in range(args.n):
        start = await cam.start_recording()
        await asyncio.sleep(args.duration)
        stop = await cam.stop_recording()
        if not (start.success and stop.success and stop.filename):
            log.error("[%d] recording failed: %s / %s", i, start.message, stop.message)
            continue
        # No turbo transfer: files are small, and toggling it around every download made the camera's
        # HTTP server stop accepting connections for a few seconds (HERO13/USB).
        ok, failed = await cam.download_media([stop.filename], out / "media", turbo=False)
        if failed or not ok:
            log.error("[%d] download of %s failed", i, stop.filename)
            continue
        mp4 = Path(ok[0])
        try:
            ff = await asyncio.to_thread(
                tc.first_frame_time,
                mp4,
                start.command_sent_ns / 1e9,
                args.display_lag_ms / 1e3,
                args.max_frames,
            )
            why = f"no readable clock in the first {args.max_frames} frames"
        except Exception as e:  # corrupt / unsupported file
            ff, why = None, f"cannot decode video ({type(e).__name__}: {e})"
        if ff is None:
            msg = f"[{i}] {mp4.name}: {why}"
            if not rows:
                raise SystemExit(msg + ": is the calibration page visible and in focus?")
            log.error("%s", msg)
            continue
        row = {
            "i": i,
            "file": stop.filename,
            "transport": start.transport,
            "command_sent": start.command_sent_ns / 1e9,
            "camera_ack": start.camera_ack_ns / 1e9,
            "encoding_changed": start.encoding_changed_ns / 1e9,
            "first_frame": ff.t_s,
            "ack_minus_sent_ms": (start.camera_ack_ns - start.command_sent_ns) / 1e6,
            "first_frame_minus_ack_ms": (ff.t_s - start.camera_ack_ns / 1e9) * 1e3,
            "first_frame_minus_sent_ms": (ff.t_s - start.command_sent_ns / 1e9) * 1e3,
            "encoding_changed_minus_first_frame_ms": (start.encoding_changed_ns / 1e9 - ff.t_s) * 1e3,
            "frames_decoded": len(ff.estimates),
            "estimate_spread_ms": ff.spread_s * 1e3,
        }
        row["fps"], row["resolution"], row["firmware"] = video_meta(mp4)
        rows.append(row)
        _write_csv(out / "recordings.csv", rows)  # keep partial results if the run is interrupted
        log.info(
            "[%d] %s: first frame %+.0f ms vs ack, %+.0f ms vs sent (%d frames, spread %.0f ms)",
            i,
            stop.filename,
            row["first_frame_minus_ack_ms"],
            row["first_frame_minus_sent_ms"],
            row["frames_decoded"],
            row["estimate_spread_ms"],
        )
        if not args.keep_media:
            mp4.unlink(missing_ok=True)
        await asyncio.sleep(args.pause)
    return rows


async def measure_stream_latency(cam: Any, args: argparse.Namespace) -> dict[str, float] | None:
    from gopro_camera.stream import StreamReceiver, stream_url

    lat: list[float] = []
    last = [0.0]
    lag = args.display_lag_ms / 1e3

    def on_frame(img: Any, recv_ns: int) -> None:
        now = time.monotonic()
        if now - last[0] < 0.2:  # decode at most 5 frames/s: keep up with the stream
            return
        last[0] = now
        value = tc.decode(img)
        if value is not None:
            shown = tc.unwrap(value, recv_ns // 1_000_000) / 1000 + lag
            lat.append(recv_ns / 1e9 - shown)

    await cam.start_stream("webcam", None, None, args.stream_port, "TS")
    rx = StreamReceiver(stream_url("webcam", "TS", args.stream_port, ""), None, on_frame)
    rx.start()
    try:
        await asyncio.sleep(args.stream_latency)
    finally:
        rx.stop()
        await cam.stop_stream()
    if not lat:
        log.error("stream latency: no readable clock in the stream")
        return None
    s = tc.summarize(lat)
    q = statistics.quantiles(lat, n=10) if len(lat) >= 10 else [s["min"], s["max"]]
    s["p10"], s["p90"] = q[0], q[-1]
    return s


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


async def _wait_for_enter() -> None:
    """Wait for Enter in a daemon thread (a default-executor thread blocked on stdin would hang Ctrl-C)."""
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[None] = loop.create_future()

    def _read() -> None:
        sys.stdin.readline()
        loop.call_soon_threadsafe(lambda: fut.done() or fut.set_result(None))

    threading.Thread(target=_read, name="wait-enter", daemon=True).start()
    await fut


# ------------------------------------------------------------------------------------------ main
async def run(args: argparse.Namespace) -> int:
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    cam = await _connect(args)
    try:
        if not args.no_preset:
            await cam.load_preset(group="video")
        await cam.refresh_state()
        settings = _settings(cam)
        meta = {"model": cam.model_name, "firmware": cam.hardware.get("firmware_version", ""), "settings": settings}
        log.info("camera: %s", json.dumps(meta))
        if settings.get("hindsight", "Off").casefold() != "off":
            raise SystemExit("HindSight is on: the files would start before the command. Turn it off.")
        if not args.yes:
            print(f"\n>>> Open http://localhost:{args.port}/ full screen, point the camera at it, then press Enter.")
            await _wait_for_enter()
        rows = await record_and_measure(cam, args, out) if args.n > 0 else []
        stream = await measure_stream_latency(cam, args) if args.stream_latency > 0 else None
    finally:
        await cam.disconnect()

    summary: dict[str, Any] = {"camera": meta, "display_lag_ms": args.display_lag_ms, "recordings": rows}
    print("\n=== gopro-calibrate-sync summary")
    if rows:
        values = [r["first_frame_minus_ack_ms"] / 1e3 for r in rows]
        keep = tc.inliers(values)
        for r, ok in zip(rows, keep, strict=True):
            r["outlier"] = not ok
        outliers = [r for r in rows if r["outlier"]]
        _write_csv(out / "recordings.csv", rows)
        # Robust: an occasionally late ack (host/HTTP scheduling) must not shift the calibration.
        offs = tc.summarize(v for v, ok in zip(values, keep, strict=True) if ok)
        summary["first_frame_minus_ack_all_s"] = tc.summarize(values)
        summary["first_frame_minus_ack_s"] = offs
        summary["first_frame_minus_sent_s"] = tc.summarize(r["first_frame_minus_sent_ms"] / 1e3 for r in rows)
        fps, resolution, firmware = rows[0]["fps"], rows[0]["resolution"], rows[0]["firmware"]
        unc = math.hypot(2 * offs["sd"], args.display_lag_uncertainty_ms / 1e3)
        cal = sc.SyncCalibration(
            model=meta["model"],
            transport=rows[0]["transport"],
            start_offset_s=round(offs["median"], 4),
            uncertainty_s=round(unc, 4),
            fps=fps,
            resolution=resolution,
            firmware=firmware or meta["firmware"],
            sd_s=round(offs["sd"], 4),
            n=int(offs["n"]),
            display_lag_s=args.display_lag_ms / 1e3,
            settings=settings,
            measured=datetime.date.today().isoformat(),
        )
        summary["calibration"] = cal.to_dict()
        sent = summary["first_frame_minus_sent_s"]
        print(f"camera:       {meta['model']} fw {cal.firmware}, {fps:g} fps {resolution}, settings {settings}")
        print(
            f"first - ack:  median {offs['median'] * 1e3:+.1f} ms, mean {offs['mean'] * 1e3:+.1f} ms, "
            f"sd {offs['sd'] * 1e3:.1f} ms, range [{offs['min'] * 1e3:+.0f}, {offs['max'] * 1e3:+.0f}] ms "
            f"(n={offs['n']}, {len(outliers)} outlier(s) excluded)"
        )
        for r in outliers:
            print(
                f"  outlier: [{r['i']}] {r['first_frame_minus_ack_ms']:+.0f} ms vs ack, "
                f"{r['first_frame_minus_sent_ms']:+.0f} ms vs sent (ack {r['ack_minus_sent_ms']:.0f} ms after sent)"
            )
        print(f"first - sent: mean {sent['mean'] * 1e3:+.1f} ms, sd {sent['sd'] * 1e3:.1f} ms")
        if offs["n"] < 3:
            print(f"calibration NOT saved: only {offs['n']} usable recording(s)")
        elif args.dry_run:
            print(f"calibration (dry run, not saved): {cal.describe()}")
        else:
            sc.save(Path(args.calibration_file), cal)
            print(f"calibration saved to {args.calibration_file}: {cal.describe()}")
    elif args.n > 0:
        print("no usable recordings")
    if stream is not None:
        summary["stream_latency_s"] = stream
        print(
            f"stream:       latency median {stream['median']:.3f} s (p10 {stream['p10']:.3f}, p90 {stream['p90']:.3f}, "
            f"n={stream['n']}) -> stream.latency_offset: {stream['median']:.2f}"
        )
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    print(f"details:      {out}")
    return 0 if rows or stream else 1


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--transport", default="usb", help="usb | cohn | ble | wifi_ap (needs HTTP for downloads)")
    ap.add_argument("--serial", default="")
    ap.add_argument("--ble", action="store_true", help="also open the BLE side channel")
    ap.add_argument("--usb-base-url", default="", help="camera HTTP base URL override (tests / NAT)")
    ap.add_argument("--n", type=int, default=10, help="number of recordings (0 = only stream latency)")
    ap.add_argument("--duration", type=float, default=2.0, help="seconds per recording")
    ap.add_argument("--pause", type=float, default=2.0, help="seconds between recordings")
    ap.add_argument("--port", type=int, default=8765, help="port of the clock page")
    ap.add_argument("--display-lag-ms", type=float, default=0.0, help="known monitor lag, added to shown times")
    ap.add_argument("--display-lag-uncertainty-ms", type=float, default=20.0)
    ap.add_argument("--max-frames", type=int, default=30, help="frames searched per file for a readable clock")
    ap.add_argument("--stream-latency", type=float, default=0.0, help="also measure webcam latency for N seconds")
    ap.add_argument("--stream-port", type=int, default=8554)
    ap.add_argument("--calibration-file", default=str(sc.USER_FILE))
    ap.add_argument("--out", default=f"~/gopro_media/calibration/{datetime.datetime.now():%Y%m%d_%H%M%S}")
    ap.add_argument("--keep-media", action="store_true", help="keep the downloaded MP4s")
    ap.add_argument("--no-preset", action="store_true", help="do not load the video preset group first")
    ap.add_argument("--dry-run", action="store_true", help="do not write the calibration file")
    ap.add_argument("--yes", action="store_true", help="do not wait for Enter before recording")
    args = ap.parse_args(argv)
    # Our progress at INFO; the SDK logs every HTTP request at INFO, so only its warnings.
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    for name in ("gopro-calibrate-sync", "gopro_camera"):
        logging.getLogger(name).setLevel(logging.INFO)
    server = serve_clock(args.port)
    log.info("clock page: http://localhost:%d/", args.port)
    try:
        code = asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\ninterrupted; partial results (if any) are in", args.out)
        code = 130
    finally:
        server.shutdown()
    raise SystemExit(code)


if __name__ == "__main__":
    main()
