from glob import glob

from setuptools import find_packages, setup

package_name = "gopro_camera"

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml", "requirements.txt"]),
        ("share/" + package_name + "/launch", glob("launch/*.py")),
        ("share/" + package_name + "/config", glob("config/*.yaml")),
        ("share/" + package_name + "/udev", glob("udev/*")),
        ("share/" + package_name + "/scripts", glob("scripts/*.py")),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="mlisi1",
    maintainer_email="elechim2196@gmail.com",
    description="ROS 2 driver for GoPro cameras (Open GoPro API): USB, Wi-Fi AP, COHN and BLE.",
    license="MIT",
    tests_require=["pytest"],
    entry_points={
        "console_scripts": [
            "gopro_node = gopro_camera.node:main",
            "mock_camera = gopro_camera.testing.mock_camera:main",
        ],
    },
)
