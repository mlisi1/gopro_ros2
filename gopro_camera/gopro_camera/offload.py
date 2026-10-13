"""Automatic SD -> local transfer of finished recordings (``media.offload``: off | copy | move).

The SD card holds the master copy until a verified local copy exists. Rules:

1. **Never while the camera records.** Transfers run only when the camera has been idle (connected,
   fresh status, not encoding, not busy, no capture command in flight) for ``settle_s``: a HERO13
   keeps finishing a file for a few seconds after it reports encoding 0, and reading it then stalls
   the camera's HTTP server for seconds (measured over USB: up to ~4 s of unanswered requests; none
   with a 10 s settle). ``start_recording`` /
   ``take_photo`` / ``start_livestream`` first stop a running transfer (:meth:`Offloader.hold`); a
   start from the camera button is seen by the next status sample and stops it within a poll period.
   Reading the SD while it writes high-bitrate video could trip the camera's SD-speed error.
2. **Atomic local files.** Data goes to ``<file>.part``, is fsynced, then renamed; a file with its
   final name is always complete (see ``HttpChannel.download``).
3. **Verify** before anything is considered done: local size == SD size and, for videos, the MP4
   opens and has a video track with a positive duration. A bad copy is removed and retried; the SD
   file is untouched.
4. **move deletes only what it verified.** The journal records ``verified`` (fsynced) *before* the
   SD file is deleted; after a restart a ``verified`` entry is re-verified before deletion. Only files
   this offloader queued itself (recordings finished while the driver ran) are ever deleted, and only
   while ``media.offload`` is ``move``. A crash or kill at any point before the delete leaves the SD
   file in place.
5. **Journal** ``<dest>/.gopro_offload_<serial>.json`` (atomic writes): pending work resumes after a
   restart; failures are retried with backoff.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import json
import logging
import os
import re
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from gopro_camera.errors import GoProError
from gopro_camera.media import local_path_for
from gopro_camera.status import S, as_flag

logger = logging.getLogger(__name__)

MODES = ("off", "copy", "move")
DONE_STATES = ("copied", "moved")
_VIDEO_RE = re.compile(r"^G([A-Z])(\d{2})(\d{4})\.MP4$", re.IGNORECASE)  # GX010042.MP4: letter, chapter, number


@dataclass
class OffloadConfig:
    mode: str = "off"
    dest: Path = field(default_factory=lambda: Path("~/gopro_media").expanduser())
    min_free_bytes: int = 5_000_000_000  # keep this much free on the local disk
    retry_backoff: tuple[float, ...] = (10.0, 30.0, 60.0, 300.0)
    max_attempts: int = 5  # per file and run; failed entries are retried again after a restart
    space_retry: float = 300.0
    hold_timeout: float = 10.0  # max wait for a transfer to stop before a capture command proceeds
    idle_poll: float = 0.5
    max_status_age: float = 5.0  # do not trust an older "not encoding"
    settle_s: float = 10.0  # camera idle this long before reading the SD (file finalisation, see above)


@dataclass
class Entry:
    path: str  # camera path, e.g. 100GOPRO/GX010042.MP4
    mode: str  # mode when queued: a "copy" entry is never deleted from the SD
    state: str = "queued"  # queued | copying | copied | verified | moved | failed
    size: int = 0
    local: str = ""
    attempts: int = 0
    error: str = ""
    updated: str = ""
    chapters_checked: bool = False
    next_try: float = field(default=0.0, metadata={"transient": True})


class Offloader:
    def __init__(self, camera: Any, cfg: OffloadConfig, serial: str = "") -> None:
        self.cam = camera
        self.cfg = cfg
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", serial or "camera")
        self.journal_path = Path(cfg.dest).expanduser() / f".gopro_offload_{safe}.json"
        self.entries: dict[str, Entry] = {}
        self.current = ""
        self.last_error = ""
        self._hold = False
        self._wake: asyncio.Event | None = None
        self._processing: asyncio.Event | None = None  # set while a file is being handled
        self._sd_active = False  # the SD card is being read (download) or written (delete) right now
        self._idle_since: float | None = None  # monotonic time the camera was first seen idle
        self._task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ public (camera loop)
    @property
    def mode(self) -> str:
        return self.cfg.mode

    def start(self) -> None:
        self._ensure_sync()
        self._load()
        self._task = asyncio.create_task(self._run(), name="gopro-offload")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    def enqueue(self, camera_path: str) -> None:
        """A recording/photo finished: queue its file (chapters are added when it is processed)."""
        if self.cfg.mode not in ("copy", "move") or not camera_path:
            return
        e = self.entries.get(camera_path)
        if e is not None and e.state in DONE_STATES:
            return
        if e is None:
            self.entries[camera_path] = Entry(camera_path, self.cfg.mode)
        else:  # retry a failed one; never downgrade copy -> move silently
            e.state, e.attempts, e.next_try = "queued", 0, 0.0
        self._save()
        self._kick()

    async def hold(self) -> None:
        """Stop any SD access before a capture command; returns once the SD card is no longer in use."""
        self._ensure_sync()
        self._hold = True
        try:
            await asyncio.wait_for(self._wait_sd_idle(), self.cfg.hold_timeout)
        except TimeoutError:
            logger.warning("offload: SD transfer did not stop within %.0fs; capturing anyway", self.cfg.hold_timeout)

    def release(self) -> None:
        self._hold = False
        self._kick()

    def wake(self) -> None:
        """Re-check for work now (e.g. after a mode change). Camera loop only."""
        self._kick()

    def summary(self) -> dict[str, Any]:
        states = [e.state for e in self.entries.values()]
        return {
            "mode": self.cfg.mode,
            "pending": sum(s not in (*DONE_STATES, "failed") for s in states),
            "failed": states.count("failed"),
            "done": sum(s in DONE_STATES for s in states),
            "current": self.current,
            "last_error": self.last_error,
            "dest": str(self.cfg.dest),
        }

    # ------------------------------------------------------------------ loop
    def _ensure_sync(self) -> None:
        if self._wake is None:
            self._wake = asyncio.Event()
            self._processing = asyncio.Event()

    def _kick(self) -> None:
        if self._wake is not None:
            self._wake.set()

    async def _wait_sd_idle(self) -> None:
        while self._sd_active:
            await asyncio.sleep(0.02)

    def _next(self) -> Entry | None:
        now = time.monotonic()
        todo = [e for e in self.entries.values() if e.state in ("queued", "copying", "verified") and e.next_try <= now]
        return todo[0] if todo else None

    async def _run(self) -> None:
        assert self._wake is not None and self._processing is not None
        while True:
            entry = self._next() if self.cfg.mode in ("copy", "move") else None
            if entry is None:
                self._wake.clear()
                waits = [e.next_try - time.monotonic() for e in self.entries.values() if e.next_try]
                timeout = max(min([w for w in waits if w > 0], default=60.0), 0.1)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout)
                continue
            if not self._settled():
                await asyncio.sleep(self.cfg.idle_poll)
                continue
            self._processing.set()
            self.current = entry.path
            try:
                await self._process(entry)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # never let one file stop the loop
                logger.exception("offload of %s failed unexpectedly", entry.path)
                self._fail(entry, f"{type(e).__name__}: {e}")
            finally:
                self._processing.clear()
                self.current = ""

    def _idle(self) -> bool:
        cam = self.cam
        st = cam.status_snapshot
        return bool(
            not self._hold
            and cam.connected
            and cam.http is not None
            and cam.http.is_connected
            and cam.status_age <= self.cfg.max_status_age
            and not as_flag(st.get(S.ENCODING))
            and not as_flag(st.get(S.BUSY))
            and not cam.capture_pending
        )

    def _settled(self) -> bool:
        """Idle continuously for settle_s (the camera finishes writing a file after encoding ends)."""
        if not self._idle():
            self._idle_since = None
            return False
        if self._idle_since is None:
            self._idle_since = time.monotonic()
        return time.monotonic() - self._idle_since >= self.cfg.settle_s

    def _should_stop(self) -> bool:
        """Download thread: stop at the next chunk if a capture is coming or the camera records."""
        st = self.cam.status_snapshot
        return self._hold or as_flag(st.get(S.ENCODING)) or self.cam.capture_pending

    # ------------------------------------------------------------------ one file
    async def _process(self, e: Entry) -> None:
        sizes = {m.path: m.size_bytes for m in await self.cam.list_media()}
        if not e.chapters_checked:
            self._add_chapters(e, sizes)
        dest = Path(self.cfg.dest).expanduser()
        local = local_path_for(dest, e.path)
        if e.path not in sizes:
            if e.state == "verified" and local.exists() and e.size and local.stat().st_size == e.size:
                self._set(e, "moved", "")  # deleted before the journal was updated (crash in between)
            else:
                self._fail(e, "not on the SD card", retry=False)
            return
        e.size = sizes[e.path]
        if e.state != "verified":
            if not await self._copy(e, dest, local):
                return
        ok, why = await asyncio.to_thread(verify_copy, local, e.size)
        if not ok:
            local.unlink(missing_ok=True)
            e.state = "queued"  # copy again (also for a "verified" entry whose local copy went bad)
            self._fail(e, f"verification failed ({why}); local copy removed, SD file kept")
            return
        e.local = str(local)
        if e.mode != "move" or self.cfg.mode != "move":
            self._set(e, "copied", "")
            logger.info("offload: %s copied to %s (kept on SD)", e.path, local)
            return
        self._set(e, "verified", "")  # persisted before the delete
        if not self._idle():
            return  # delete when idle again (the entry stays "verified")
        self._sd_active = True
        try:
            deleted, _ = await self.cam.delete_media([e.path], confirm=True)
        finally:
            self._sd_active = False
        if e.path in deleted:
            self._set(e, "moved", "")
            logger.info("offload: %s moved to %s (deleted from SD after verification)", e.path, local)
        else:
            self._fail(e, "verified, but deleting it from the SD failed; will retry")

    async def _copy(self, e: Entry, dest: Path, local: Path) -> bool:
        if not (local.exists() and local.stat().st_size == e.size):
            dest.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(dest).free
            if free - e.size < self.cfg.min_free_bytes:
                e.next_try = time.monotonic() + self.cfg.space_retry
                self.last_error = (
                    f"low disk space for {e.path}: {free / 1e9:.1f} GB free, need {e.size / 1e9:.1f} GB "
                    f"+ {self.cfg.min_free_bytes / 1e9:.1f} GB reserve"
                )
                e.error = self.last_error
                self._save()
                logger.warning("offload: %s", self.last_error)
                return False
        self._set(e, "copying", "")
        self._sd_active = True
        try:
            downloaded, failed = await self.cam.download_media(
                [e.path], dest, skip_existing=True, turbo=False, cancelled=self._should_stop
            )
        except GoProError as err:
            self._fail(e, str(err))
            return False
        finally:
            self._sd_active = False
        if failed:
            self._fail(e, "download failed")
            return False
        if not downloaded:  # paused: a capture started; not an attempt
            self._set(e, "queued", "paused for a capture")
            return False
        return True

    def _add_chapters(self, e: Entry, sizes: dict[str, int]) -> None:
        m = _VIDEO_RE.match(Path(e.path).name)
        if m:
            letter, number = m.group(1).upper(), m.group(3)
            for p in sorted(sizes):
                pm = _VIDEO_RE.match(Path(p).name)
                if pm and pm.group(1).upper() == letter and pm.group(3) == number and p not in self.entries:
                    self.entries[p] = Entry(p, e.mode, chapters_checked=True)
        e.chapters_checked = True
        self._save()

    # ------------------------------------------------------------------ state / journal
    def _set(self, e: Entry, state: str, error: str) -> None:
        e.state, e.error = state, error
        e.updated = datetime.datetime.now().isoformat(timespec="seconds")
        self._save()

    def _fail(self, e: Entry, error: str, retry: bool = True) -> None:
        e.attempts += 1
        self.last_error = f"{e.path}: {error}"
        logger.error("offload: %s", self.last_error)
        if retry and e.attempts < self.cfg.max_attempts:
            backoff = self.cfg.retry_backoff[min(e.attempts - 1, len(self.cfg.retry_backoff) - 1)]
            e.next_try = time.monotonic() + backoff
            keep = e.state if e.state == "verified" else "queued"
            self._set(e, keep, error)
        else:
            self._set(e, "failed", error)

    def _load(self) -> None:
        if not self.journal_path.exists():
            return
        try:
            data = json.loads(self.journal_path.read_text())
        except (OSError, ValueError) as err:
            logger.error("offload journal %s unreadable (%s); starting a new one", self.journal_path, err)
            return
        names = {f.name for f in Entry.__dataclass_fields__.values()}
        for raw in data.get("files", []):
            e = Entry(**{k: v for k, v in raw.items() if k in names and k != "next_try"})
            if e.state in ("failed", "copying"):  # a new run retries; an existing complete copy is reused
                e.state, e.attempts = "queued", 0
            self.entries[e.path] = e
        pending = sum(e.state not in DONE_STATES for e in self.entries.values())
        if pending:
            logger.info("offload: resuming %d pending file(s) from %s", pending, self.journal_path)

    def _save(self) -> None:
        path = self.journal_path
        path.parent.mkdir(parents=True, exist_ok=True)
        files = [{k: v for k, v in asdict(e).items() if k != "next_try"} for e in self.entries.values()]
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump({"version": 1, "files": files}, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)


def verify_copy(local: Path, expected_size: int) -> tuple[bool, str]:
    """Local copy complete? Size must match the SD; an MP4 must open and contain video with a duration."""
    try:
        size = local.stat().st_size
    except OSError as e:
        return False, f"missing ({e})"
    if size != expected_size:
        return False, f"size {size} != {expected_size} on SD"
    if local.suffix.upper() != ".MP4":
        return True, ""
    try:
        import av

        with av.open(str(local)) as c:
            vs = c.streams.video[0]
            dur = float(vs.duration * vs.time_base) if vs.duration else float(c.duration or 0) / 1e6
    except Exception as e:  # av.error.*, IndexError (no video stream)
        return False, f"not a readable MP4 ({type(e).__name__}: {e})"
    if dur <= 0:
        return False, "MP4 has no duration"
    return True, ""
