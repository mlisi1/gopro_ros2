import asyncio
import threading

import pytest
from gopro_camera.loop_bridge import AsyncLoopThread


def test_submit_runs_on_loop_thread():
    bridge = AsyncLoopThread()
    try:

        async def where():
            await asyncio.sleep(0.01)
            return threading.current_thread().name

        assert bridge.run_sync(where(), 5) == "gopro-asyncio"
        assert bridge.is_running
    finally:
        bridge.stop()


def test_ros_await_propagates_result_and_exception():
    rclpy = pytest.importorskip("rclpy")
    from rclpy.executors import SingleThreadedExecutor

    rclpy.init()
    bridge = AsyncLoopThread()
    try:
        node = rclpy.create_node("bridge_test")
        ex = SingleThreadedExecutor()
        ex.add_node(node)
        out = {}

        async def ok():
            await asyncio.sleep(0.05)
            return 42

        async def bad():
            raise ValueError("boom")

        async def caller():
            out["ok"] = await bridge.ros_await(ok(), ex.wake)
            try:
                await bridge.ros_await(bad(), ex.wake)
            except ValueError as e:
                out["err"] = str(e)

        task = ex.create_task(caller())
        ex.spin_until_future_complete(task, timeout_sec=5)
        assert out == {"ok": 42, "err": "boom"}
        node.destroy_node()
    finally:
        bridge.stop()
        rclpy.shutdown()
