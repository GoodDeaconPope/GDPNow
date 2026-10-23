"""How long does one mpv take to show the first frame of a file at a deep
start position, and does the hardware-decoder setting matter?
Runs mpv with no window (--vo=null --ao=null --frames=1) so nothing pops up.

    python mpv_open_cost.py MPV_EXE MAP_JSON VIDEO_DIR [episodes...]
"""
import json
import os
import random
import subprocess
import sys
import time

mpv, map_file, video_dir = sys.argv[1], sys.argv[2], sys.argv[3]
m = json.load(open(map_file, encoding="utf-8-sig"))
eps = sys.argv[4:] or random.sample([k for k in m if "RC" in m[k] and os.path.exists(os.path.join(video_dir, k))], 6)


def first_frame(path, start, hwdec):
    t = time.time()
    r = subprocess.run([mpv, "--no-config", f"--start={start}", path, "--frames=1", "--vo=null", "--ao=null",
                        f"--hwdec={hwdec}", "--really-quiet"], capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    return round(time.time() - t, 2), r.returncode


print(f"{'episode':8} {'start':6} {'cold auto-copy':>15} {'warm auto-copy':>15} {'warm no hwdec':>14} {'warm d3d11va-copy':>18}")
for k in eps:
    k = k if k.endswith(".mp4") else k + ".mp4"
    path, start = os.path.join(video_dir, k), m[k]["RC"]
    cold = first_frame(path, start, "auto-copy")[0]
    warm = first_frame(path, start, "auto-copy")[0]
    nohw = first_frame(path, start, "no")[0]
    d3d = first_frame(path, start, "d3d11va-copy")[0]
    print(f"{k[:-4]:8} {start:6} {cold:15} {warm:15} {nohw:14} {d3d:18}")
