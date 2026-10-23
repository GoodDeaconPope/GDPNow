"""Measure what shows on screen while gdpnow starts: how long separate mpv
windows sit on the desktop before the grid window takes them in.

    python startup_windows.py ANSWERS_JSON [seconds=14]
Starts E:\\GDPNow\\gdpnow.py with those answers (stop_file inside is used to
end it), samples top-level windows every 50 ms and reports.
"""
import json
import os
import subprocess
import sys
import time

import win32con
import win32gui
import win32process

answers, secs = sys.argv[1], float(sys.argv[2]) if len(sys.argv) > 2 else 14
stop_file = json.load(open(answers))["stop_file"]
if os.path.exists(stop_file):
    os.remove(stop_file)
gdpnow = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "gdpnow.py")
env = dict(os.environ, PYTHONUTF8="1", GDPNOW_OUTPUT_DIR=os.path.join(os.environ["TEMP"], "gdp_startup_test"))
p = subprocess.Popen([sys.executable, "-u", "-W", "ignore::SyntaxWarning", gdpnow, "--answers", answers],
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env)

t0 = time.time()
first_loose = last_loose = None
max_loose = 0
host_seen = None
while time.time() - t0 < secs:
    loose = 0
    mpv_pids = set()

    def cb(h, _):
        global loose, host_seen
        cls = win32gui.GetClassName(h)
        if cls == "GridHostWindowClass" and win32gui.IsWindowVisible(h) and host_seen is None:
            host_seen = time.time() - t0
        if cls == "mpv" and win32gui.IsWindowVisible(h) and not win32gui.IsIconic(h):
            loose += 1  # an mpv window sitting on the desktop on its own
    win32gui.EnumWindows(cb, None)
    if loose:
        first_loose = first_loose if first_loose is not None else time.time() - t0
        last_loose = time.time() - t0
        max_loose = max(max_loose, loose)
    time.sleep(0.05)

# after startup: are the mpv windows inside the grid host, shown, not minimized?
inside = []
host = win32gui.FindWindow("GridHostWindowClass", None)
if host:
    win32gui.EnumChildWindows(host, lambda h, _: inside.append((win32gui.GetClassName(h), win32gui.IsIconic(h),
                                                                 win32gui.IsWindowVisible(h))), None)
open(stop_file, "w").close()
out = p.communicate(timeout=60)[0].decode("utf-8", "replace")
loose_time = (last_loose - first_loose + 0.05) if first_loose is not None else 0
print(f"loose mpv windows on the desktop: max {max_loose} at once, visible for {loose_time:.2f}s"
      + (f" (from {first_loose:.2f}s to {last_loose:.2f}s)" if first_loose is not None else ""))
print(f"grid window visible at {host_seen:.2f}s" if host_seen is not None else "grid window never seen")
mpv_inside = [c for c in inside if c[0] == "mpv"]
print(f"mpv windows inside the grid: {len(mpv_inside)}, minimized: {sum(c[1] for c in mpv_inside)}, "
      f"visible: {sum(c[2] for c in mpv_inside)}")
print("errors in log:", [l for l in out.splitlines() if "Traceback" in l or "ERROR_REPORT" in l][:3])
print("grid log:", [l[:110] for l in out.splitlines() if "GridHost" in l and "PID search attempt" not in l][:6])
print("last search:", [l for l in out.splitlines() if "PID search attempt" in l][-1:])
