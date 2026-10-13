# Sync anchor between rosbags and SD footage.
uint8 START = 0
uint8 STOP = 1
uint8 HILIGHT = 2
uint8 PHOTO = 3
uint8 ADOPTED = 4            # driver (re)connected and found the camera already encoding
uint8 EXTERNAL_START = 5     # encoding started without a driver command (button, BLE remote, ...)
uint8 EXTERNAL_STOP = 6      # encoding stopped without a driver command (button, SD full, overheat, ...)

std_msgs/Header header       # header.stamp == command_sent (or detection time for ADOPTED/EXTERNAL_*)
uint8 event
string camera_serial
string transport             # transport that executed the command

builtin_interfaces/Time command_sent      # ROS time right before the command was written
builtin_interfaces/Time camera_ack        # ROS time the camera acknowledged the command.
                                          # START: the camera replies once recording runs; the first video frame is at
                                          # camera_ack + a stable, configuration-dependent offset (HERO13/USB: ~-37 ms),
                                          # measured with gopro-calibrate-sync and applied by gopro-align.
builtin_interfaces/Time encoding_changed  # ROS time the camera-reported encoding state change was first observed (zero if not observed).
                                          # STOP: the camera keeps reporting encoding for ~1-2 s while it finalises the file,
                                          # so this is the end of finalisation; the footage ends near camera_ack.
float64 encoding_changed_uncertainty      # seconds; upper bound on detection delay (time since the previous status sample,
                                          # capped at the time since command_sent; ~BLE latency with push status)

bool success
string message
string filename              # camera path, e.g. 100GOPRO/GX010042.MP4 (filled when known)
int32 encoding_duration      # seconds, camera-reported (STOP / EXTERNAL_STOP)
