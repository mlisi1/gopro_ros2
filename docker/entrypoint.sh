#!/bin/bash
# Source ROS + the workspace and put the venv (open_gopro, telemetry-parser) first on PATH.
set -e
source "/opt/ros/${ROS_DISTRO}/setup.bash"
if [ -f "${WS}/install/setup.bash" ]; then
    source "${WS}/install/setup.bash"
fi
export PATH="${VENV}/bin:${PATH}"
exec "$@"
