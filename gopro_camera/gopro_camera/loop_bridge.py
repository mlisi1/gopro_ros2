"""asyncio <-> rclpy bridge.

The Open GoPro SDK is asyncio based. Each camera gets one asyncio event loop running in a dedicated
thread. ROS callbacks never perform network I/O themselves: they submit coroutines to that loop with
``asyncio.run_coroutine_threadsafe`` and either

- fire-and-observe (timers, subscriptions): :meth:`AsyncLoopThread.submit`, or
- await the result without blocking an executor thread (async service/action callbacks):
  :meth:`AsyncLoopThread.ros_await`, which returns an awaitable rclpy Future that is completed from
  the asyncio thread and wakes the executor.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

T = TypeVar("T")


class AsyncLoopThread:
    """Owns an asyncio event loop running forever in a daemon thread."""

    def __init__(self, name: str = "gopro-asyncio") -> None:
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()
        self._ready.wait()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.call_soon(self._ready.set)
        self._loop.run_forever()
        # Drain cancelled tasks so they can run their cleanup.
        pending = asyncio.all_tasks(self._loop)
        for task in pending:
            task.cancel()
        if pending:
            self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self._loop.run_until_complete(self._loop.shutdown_asyncgens())
        self._loop.close()

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        return self._loop

    @property
    def is_running(self) -> bool:
        return self._thread.is_alive() and self._loop.is_running()

    def in_loop_thread(self) -> bool:
        return threading.current_thread() is self._thread

    def submit(self, coro: Coroutine[Any, Any, T]) -> concurrent.futures.Future[T]:
        """Schedule a coroutine on the camera loop from any thread."""
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def run_sync(self, coro: Coroutine[Any, Any, T], timeout: float | None = None) -> T:
        """Block the *calling* thread until the coroutine finishes.

        Only for startup/shutdown paths and tests; never call from an rclpy executor callback.
        """
        if self.in_loop_thread():
            raise RuntimeError("run_sync() called from the asyncio thread would deadlock")
        return self.submit(coro).result(timeout)

    def ros_await(self, coro: Coroutine[Any, Any, T], wake: Callable[[], None] | None = None) -> Any:
        """Run ``coro`` on the camera loop and return an awaitable rclpy Future.

        Use inside ``async def`` rclpy callbacks: ``result = await bridge.ros_await(coro, executor.wake)``.
        """
        from rclpy.task import Future as RclpyFuture

        ros_future: RclpyFuture = RclpyFuture()
        cf = self.submit(coro)

        def _done(f: concurrent.futures.Future[T]) -> None:
            if f.cancelled():
                ros_future.cancel()
            elif (exc := f.exception()) is not None:
                ros_future.set_exception(exc)
            else:
                ros_future.set_result(f.result())
            if wake is not None:
                wake()

        cf.add_done_callback(_done)
        return ros_future

    def stop(self, timeout: float = 5.0) -> None:
        if self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout)
