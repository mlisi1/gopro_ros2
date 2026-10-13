"""Single camera: ros2 launch gopro_camera gopro.launch.py config:=<yaml> namespace:=gopro"""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import LifecycleNode
from launch_ros.substitutions import FindPackageShare


def generate_launch_description() -> LaunchDescription:
    default_cfg = PathJoinSubstitution([FindPackageShare("gopro_camera"), "config", "usb.yaml"])
    return LaunchDescription(
        [
            DeclareLaunchArgument("config", default_value=default_cfg, description="parameter YAML"),
            DeclareLaunchArgument("namespace", default_value="gopro", description="one namespace per camera"),
            DeclareLaunchArgument("node_name", default_value="gopro_camera"),
            DeclareLaunchArgument("log_level", default_value="info"),
            LifecycleNode(
                package="gopro_camera",
                executable="gopro_node",
                name=LaunchConfiguration("node_name"),
                namespace=LaunchConfiguration("namespace"),
                parameters=[LaunchConfiguration("config")],
                arguments=["--ros-args", "--log-level", LaunchConfiguration("log_level")],
                output="screen",
                emulate_tty=True,
            ),
        ]
    )
