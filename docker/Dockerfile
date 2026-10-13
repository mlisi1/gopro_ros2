# gopro_ros2 development / test image (ROS 2 Jazzy, Ubuntu 24.04).
#
# System dependencies come from the packages' package.xml via rosdep; the pip-only dependencies
# (open_gopro SDK, telemetry-parser) go into a venv created with --system-site-packages so ROS
# Python packages stay visible. The driver's entry points use `#!/usr/bin/env python3`, so the venv
# on PATH is what runs them.
ARG ROS_DISTRO=jazzy
FROM ros:${ROS_DISTRO}-ros-base

ARG ROS_DISTRO
# 1 = download GoPro's public HERO8 sample MP4 (gpmf-parser repo) for the end-to-end align test
ARG FETCH_SAMPLE=1
# 1 = install nmcli for the wifi_ap transport (needs the host's NetworkManager over D-Bus)
ARG WITH_WIFI_AP=0
# Containers run as this user, so files written to bind mounts (COHN credentials, media, calibration,
# caches in a mounted source tree) belong to the host user. Pass your ids if they are not 1000:
#   HOST_UID=$(id -u) HOST_GID=$(id -g) docker compose -f docker/compose.yaml build
ARG USER_UID=1000
ARG USER_GID=1000
ARG USERNAME=gopro

SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ROS_DISTRO=${ROS_DISTRO} \
    VENV=/opt/gopro_venv \
    WS=/ws

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3-venv python3-pip python3-colcon-common-extensions python3-pytest \
        iproute2 curl ca-certificates bluez \
    && if [ "${WITH_WIFI_AP}" = "1" ]; then apt-get install -y --no-install-recommends network-manager; fi \
    && rm -rf /var/lib/apt/lists/*

# --- system/ROS dependencies from package.xml (copied alone for layer caching)
WORKDIR ${WS}
COPY gopro_camera_interfaces/package.xml src/gopro_ros2/gopro_camera_interfaces/package.xml
COPY gopro_camera/package.xml src/gopro_ros2/gopro_camera/package.xml
COPY gopro_tools/package.xml src/gopro_ros2/gopro_tools/package.xml
RUN apt-get update \
    && (rosdep update --rosdistro "${ROS_DISTRO}" || rosdep update --rosdistro "${ROS_DISTRO}") \
    && rosdep install --from-paths src --ignore-src -y --rosdistro "${ROS_DISTRO}" \
    && rm -rf /var/lib/apt/lists/*

# --- pip-only dependencies in a venv that still sees the ROS python packages
COPY gopro_camera/requirements.txt /tmp/req/gopro_camera.txt
COPY gopro_tools/requirements.txt /tmp/req/gopro_tools.txt
RUN python3 -m venv --system-site-packages "${VENV}" \
    && "${VENV}/bin/pip" install --no-cache-dir \
        -r /tmp/req/gopro_camera.txt -r /tmp/req/gopro_tools.txt ruff

RUN if [ "${FETCH_SAMPLE}" = "1" ]; then \
        mkdir -p /opt/samples && curl -fsSL -o /opt/samples/hero8.mp4 \
        https://raw.githubusercontent.com/gopro/gpmf-parser/main/samples/hero8.mp4; \
    fi
ENV GOPRO_SAMPLE_MP4=/opt/samples/hero8.mp4

# --- unprivileged user with the host's uid/gid (the base image's "ubuntu" user may hold uid 1000)
RUN if id -u ubuntu >/dev/null 2>&1; then userdel -r ubuntu 2>/dev/null || userdel ubuntu; fi \
    && if ! getent group "${USER_GID}" >/dev/null; then groupadd -g "${USER_GID}" "${USERNAME}"; fi \
    && useradd -m -s /bin/bash -u "${USER_UID}" -g "${USER_GID}" "${USERNAME}" \
    && mkdir -p "/home/${USERNAME}/.ros/gopro" "/home/${USERNAME}/gopro_media" \
    && chown -R "${USER_UID}:${USER_GID}" "/home/${USERNAME}" "${WS}"
ENV HOME=/home/${USERNAME}

COPY docker/entrypoint.sh /entrypoint.sh
COPY docker/run_tests.sh /usr/local/bin/run_tests
RUN chmod +x /entrypoint.sh /usr/local/bin/run_tests

# --- build the workspace as that user (not --symlink-install: entry points need the env-python shebang)
USER ${USER_UID}:${USER_GID}
COPY --chown=${USER_UID}:${USER_GID} . src/gopro_ros2
RUN source "/opt/ros/${ROS_DISTRO}/setup.bash" \
    && export PATH="${VENV}/bin:${PATH}" \
    && colcon build --event-handlers console_direct- \
    && rm -rf build log
ENTRYPOINT ["/entrypoint.sh"]
CMD ["bash"]
