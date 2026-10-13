# Typed camera status. Integer fields use -1 for "unknown / not reported by this camera".
# Values mirror the Open GoPro status IDs (see gopro_camera/spec_tables.py, generated from the official spec).
std_msgs/Header header

# --- link ---
bool connected                      # primary transport usable
string transport                    # active primary transport: usb | wifi_ap | cohn | ble
bool http_connected
bool ble_connected                  # BLE link (primary or side channel)
string status_source                # "ble_push" or "http_poll"
float64 status_age                  # seconds since the last successful status update

# --- power ---
int8 battery_present                # status 1
int8 battery_bars                   # status 2 (4 = charging)
int16 battery_percent               # status 70

# --- capture state ---
bool busy                           # status 8
bool encoding                       # status 10
bool ready                          # status 82
int32 video_encoding_duration       # status 13, seconds
bool hindsight_active               # status 106
int32 active_hilights               # status 58
int32 photos_on_sd                  # status 38
int32 videos_on_sd                  # status 39

# --- storage ---
int8 sd_status                      # status 33 (Primary Storage)
string sd_status_text
int64 sd_remaining_kb               # status 54
int32 remaining_video_time          # status 35, seconds
int32 remaining_photos              # status 34
int8 sd_write_speed_error           # status 111
int32 sd_errors                     # status 112
int64 sd_capacity_kb                # status 117

# --- thermal ---
int8 overheating                    # status 6
int8 cold                           # status 85

# --- gps ---
int8 gps_lock                       # status 68

# --- preset ---
int32 preset_id                     # status 97
int32 preset_group                  # status 96
int8 preset_modified                # status 98

# --- accessories ---
int8 lens_type                      # status 105
int8 media_mod_state                # status 102
int8 microphone_accessory           # status 74

# --- network / control ---
int8 ap_mode                        # status 69
int8 wireless_enabled               # status 17
int8 usb_connected                  # status 115
int8 usb_controlled                 # status 116
int8 camera_control                 # status 114
int8 preview_stream_active          # status 32
int8 ota_state                      # status 41
string camera_name                  # status 122

# --- stream / livestream (driver side) ---
bool stream_active
string stream_mode
bool livestream_active

# --- every numeric status the camera reported, for forward compatibility ---
int32[] raw_status_ids
int64[] raw_status_values
