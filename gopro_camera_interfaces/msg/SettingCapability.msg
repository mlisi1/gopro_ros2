int32 id
string name                 # ROS parameter suffix: settings.<name>
string title
int32[] options             # options the official spec lists for this camera model
string[] option_labels
int32[] runtime_options     # options the camera itself reports as currently valid (empty if not reported)
int32 range_min -1          # for non-enumerated settings
int32 range_max -1
int32 current -1            # -1 if not reported in current preset/mode
bool settable_over_ble
