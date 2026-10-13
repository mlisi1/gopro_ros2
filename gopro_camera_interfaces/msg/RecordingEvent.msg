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
builtin_interfaces/Time camera_ack        # ROS time the camera acknowledged the command
builtin_interfaces/Time encoding_changed  # ROS time the camera-reported encoding state change was first observed (zero if not observed)
float64 encoding_changed_uncertainty      # seconds; upper bound on detection delay (poll period, or BLE push latency)

bool success
string message
string filename              # camera path, e.g. 100GOPRO/GX010042.MP4 (filled when known)
int32 encoding_duration      # seconds, camera-reported (STOP / EXTERNAL_STOP)
