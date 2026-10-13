"""Several cameras, one lifecycle node + namespace each, described by config/multi_cam.yaml."""

from pathlib import Path

import yaml
from launch import LaunchContext, LaunchDescription
from launch.actions import DeclareLaunchArgument, OpaqueFunction
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import LifecycleNode
from launch_ros.substitutions import FindPackageShare


def _nodes(context: LaunchContext) -> list[LifecycleNode]:
    cfg_path = Path(LaunchConfiguration("config").perform(context))
    spec = yaml.safe_load(cfg_path.read_text())
    nodes = []
    for cam in spec.get("cameras", []):
        params = []
        if cam.get("params"):
            p = Path(cam["params"])
            params.append(str(p if p.is_absolute() else cfg_path.parent / p))
        if cam.get("overrides"):
            params.append(dict(cam["overrides"]))
        nodes.append(
            LifecycleNode(
                package="gopro_camera",
                executable="gopro_node",
                name="gopro_camera",
                namespace=cam["namespace"],
                parameters=params,
                output="screen",
                emulate_tty=True,
            )
        )
    return nodes


def generate_launch_description() -> LaunchDescription:
    default_cfg = PathJoinSubstitution([FindPackageShare("gopro_camera"), "config", "multi_cam.yaml"])
    return LaunchDescription(
        [
            DeclareLaunchArgument("config", default_value=default_cfg),
            OpaqueFunction(function=_nodes),
        ]
    )
