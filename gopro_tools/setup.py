from setuptools import find_packages, setup

package_name = "gopro_tools"

setup(
    name=package_name,
    version="0.1.0",
    packages=[*find_packages(exclude=["test"]), "gopro_tools.data"],
    package_data={"gopro_tools.data": ["*.yaml"]},
    include_package_data=True,
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml", "requirements.txt"]),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="mlisi1",
    maintainer_email="elechim2196@gmail.com",
    description="Offline GoPro mission tools: media pull, GPMF extraction, rosbag alignment, sync calibration.",
    license="MIT",
    tests_require=["pytest"],
    entry_points={
        "console_scripts": [
            "gopro-pull = gopro_tools.pull:main",
            "gopro-gpmf = gopro_tools.gpmf:main",
            "gopro-align = gopro_tools.align:main",
            "gopro-calibrate-sync = gopro_tools.calibrate_sync:main",
        ],
    },
)
