"""gopro-pull: download a mission's media from the camera.

Select files by camera time window (``--since/--until``, ISO 8601 host-local time) or by the
recordings referenced in a rosbag's ``recording_event`` topic (``--bag``). All chapters of a
recording are fetched.

Two ways to reach the camera:
- direct (default): opens the camera with the driver's transport layer (USB or COHN). Do not use
  this while the driver node is running on the same camera.
- ``--ros``: sends a ``DownloadMedia`` goal to a running driver node (``--node``).
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import logging
import sys
from pathlib import Path

log = logging.getLogger("gopro-pull")


def _parse_time(text: str | None) -> datetime.datetime | None:
    if not text:
        return None
    dt = datetime.datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.astimezone()


def files_from_bag(bag: Path, topic: str) -> list[str]:
    """Camera paths referenced by stop events; chapters are expanded later against the media list."""
    from gopro_tools.align import read_topics, recordings_from_events

    events = [m for _, m in read_topics(bag, [topic])[topic]]
    recs = recordings_from_events(events)
    names = [r.filename for r in recs if r.filename]
    names += [e.filename for e in events if e.filename and e.event == 3]  # photos
    return sorted(set(names))


def expand_chapters(wanted: list[str], available: list[str]) -> list[str]:
    from gopro_tools.align import parse_gopro_name

    numbers = {p[2] for w in wanted if (p := parse_gopro_name(w))}
    out = set(wanted) & set(available)
    for a in available:
        parsed = parse_gopro_name(a)
        if parsed and parsed[2] in numbers:
            out.add(a)
    return sorted(out)


async def pull_direct(args: argparse.Namespace) -> int:
    from gopro_camera.camera import CameraConfig, GoProCamera
    from gopro_camera.media import to_camera_epoch
    from gopro_camera.transports import TransportConfig

    tcfg = TransportConfig(
        transport=args.transport,
        serial=args.serial or None,
        usb_base_url=args.usb_base_url or None,
        cohn_credentials_path=args.cohn_credentials,
    )
    # Passive: do not claim control, sync time or reconnect - another process may own the camera later.
    cfg = CameraConfig(transport=tcfg, claim_control=False, sync_datetime_on_connect=False, reconnect=False)
    cam = GoProCamera(cfg)
    await cam.connect()
    try:
        entries = await cam.list_media()
        since, until = _parse_time(args.since), _parse_time(args.until)
        lo = to_camera_epoch(since) if since else None
        hi = to_camera_epoch(until) if until else None
        selected = [e.path for e in entries if (lo is None or e.created >= lo) and (hi is None or e.created <= hi)]
        if args.bag:
            selected = expand_chapters(files_from_bag(args.bag, args.event_topic), [e.path for e in entries])
        if not selected:
            log.warning("nothing to download")
            return 0
        log.info("downloading %d file(s) to %s", len(selected), args.dest)
        last = [-1]

        def progress(i: int, n: int, path: str, done: int, total: int) -> None:
            pct = int(100 * done / total) if total else 0
            if pct // 10 != last[0] or done == total:
                last[0] = pct // 10
                log.info("[%d/%d] %s %d%%", i + 1, n, path, pct)

        ok, failed = await cam.download_media(
            selected, args.dest, skip_existing=not args.overwrite, turbo=not args.no_turbo, progress=progress
        )
        log.info("downloaded %d, failed %d", len(ok), len(failed))
        return 1 if failed else 0
    finally:
        await cam.disconnect(release_control=False)


def pull_ros(args: argparse.Namespace) -> int:
    import rclpy
    from rclpy.action import ActionClient

    from gopro_camera_interfaces.action import DownloadMedia

    rclpy.init()
    node = rclpy.create_node("gopro_pull")
    try:
        client = ActionClient(node, DownloadMedia, f"{args.node}/download_media")
        if not client.wait_for_server(timeout_sec=10.0):
            log.error("no DownloadMedia action server at %s/download_media", args.node)
            return 2
        goal = DownloadMedia.Goal(destination=str(args.dest), skip_existing=not args.overwrite, no_turbo=args.no_turbo)
        if args.bag:
            goal.paths = files_from_bag(args.bag, args.event_topic)
        elif args.since:
            since = _parse_time(args.since)
            assert since is not None
            ns = int(since.timestamp() * 1e9)
            goal.since.sec, goal.since.nanosec = ns // 1_000_000_000, ns % 1_000_000_000
        else:
            goal.since_mission_start = True

        def fb(msg: object) -> None:
            f = msg.feedback  # type: ignore[attr-defined]
            log.info("[%d/%d] %s %.0f%%", f.file_index + 1, f.file_count, f.current_file, 100 * f.overall_progress)

        send = client.send_goal_async(goal, feedback_callback=fb)
        rclpy.spin_until_future_complete(node, send)
        handle = send.result()
        if not handle.accepted:
            log.error("goal rejected")
            return 2
        res = handle.get_result_async()
        rclpy.spin_until_future_complete(node, res)
        result = res.result().result
        log.info("%s", result.message)
        return 0 if result.success else 1
    finally:
        node.destroy_node()
        rclpy.shutdown()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dest", type=Path, default=Path("gopro_media"))
    ap.add_argument("--since", help="ISO time (host local if no zone), e.g. 2025-06-01T09:00")
    ap.add_argument("--until", help="ISO time")
    ap.add_argument("--bag", type=Path, help="rosbag2 with the driver's recording_event topic")
    ap.add_argument("--event-topic", default="/gopro/gopro_camera/recording_event")
    ap.add_argument("--overwrite", action="store_true", help="re-download files that already exist")
    ap.add_argument("--no-turbo", action="store_true")
    ap.add_argument("--ros", action="store_true", help="use a running driver node's DownloadMedia action")
    ap.add_argument("--node", default="/gopro/gopro_camera", help="driver node name for --ros")
    ap.add_argument("--transport", default="usb", choices=["usb", "cohn", "wifi_ap"])
    ap.add_argument("--serial", default="")
    ap.add_argument("--usb-base-url", default="")
    ap.add_argument("--cohn-credentials", default="~/.ros/gopro/cohn_credentials.json")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    rc = pull_ros(args) if args.ros else asyncio.run(pull_direct(args))
    sys.exit(rc)


if __name__ == "__main__":
    main()
