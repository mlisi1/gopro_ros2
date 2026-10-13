string path                 # camera path folder/file, e.g. 100GOPRO/GX010042.MP4
string folder
string filename
int64 created               # camera-reported creation time, seconds since epoch (camera clock)
int64 modified
int64 size_bytes            # 0 if not reported by the media list
bool is_group               # e.g. burst / timelapse group
string group_type
int32 group_size
