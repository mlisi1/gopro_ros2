#!/bin/bash
# Lint + full test suite (unit, fake camera, launch_testing, offline tools). No hardware needed.
# Extra arguments are passed to pytest, e.g.: run_tests -k settings   |   run_tests --hardware -s
set -euo pipefail
cd "${WS}/src/gopro_ros2"

# If the source is bind-mounted (dev service), rebuild first so tests run the current code.
if [ "${REBUILD:-0}" = "1" ]; then
    (cd "${WS}" && colcon build --event-handlers console_direct- >/dev/null && rm -rf build log)
    set +u  # colcon's setup scripts read unset variables
    source "${WS}/install/setup.bash"
    set -u
fi

echo "== ruff check";  ruff check .
echo "== ruff format"; ruff format --check .
echo "== pytest"
# Isolate DDS traffic of the launch test from anything else on the network.
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-42}"
python3 -m pytest -p no:cacheprovider -q gopro_camera/test gopro_tools/test "$@"
