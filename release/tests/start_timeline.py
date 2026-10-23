"""Where does the time go between pressing Start and all cells playing?
Starts the player the way the app does and timestamps its log lines, the
grid window, and the first picture in each cell (numbers only).

    python start_timeline.py PYTHON_EXE GDPNOW_PY ANSWERS_JSON [seconds=20]
"""
import ctypes
import json
import os
import subprocess
import sys
import threading
import time

import numpy as np
import win32gui
from PIL import ImageGrab

ctypes.windll.shcore.SetProcessDpiAwareness(2)
py, player, answers = sys.argv[1], sys.argv[2], sys.argv[3]
secs = float(sys.argv[4]) if len(sys.argv) > 4 else 20
cfg = json.load(open(answers))
n = int(cfg["cols"]) * int(cfg["rows"])
if os.path.exists(cfg["stop_file"]):
    os.remove(cfg["stop_file"])
env = dict(os.environ, PYTHONUTF8="1", GDPNOW_OUTPUT_DIR=os.path.join(os.environ["TEMP"], "gdp_timeline"))
t0 = time.time()
p = subprocess.Popen([py, "-u", "-W", "ignore::SyntaxWarning", player, "--answers", answers],
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env,
                     creationflags=subprocess.CREATE_NO_WINDOW)
log = []
threading.Thread(target=lambda: [log.append((time.time() - t0, l.decode("utf-8", "replace").rstrip()))
                                 for l in p.stdout], daemon=True).start()
def mpv_prop(i, prop):
    try:
        with open(rf"\\.\pipe\mpvsocket{i}", "r+b", buffering=0) as f:
            f.write((json.dumps({"command": ["get_property", prop], "request_id": 5}) + "\n").encode())
            buf = b""
            while True:
                ch = f.read(1)
                if not ch:
                    return None
                if ch != b"\n":
                    buf += ch
                    continue
                msg = json.loads(buf)
                buf = b""
                if msg.get("request_id") == 5:
                    return msg.get("data")
    except OSError:
        return None


events, lit, seen_dark = {}, [None] * n, [False] * n
vo_up, playing = [None] * n, [None] * n
while time.time() - t0 < secs and (None in lit or None in vo_up):
    t = time.time() - t0
    for i in range(n):  # mpv's own view: video output configured / clock running
        if vo_up[i] is None and mpv_prop(i, "vo-configured"):
            vo_up[i] = t
        if playing[i] is None and (mpv_prop(i, "playback-time") or 0) > 0.05:
            playing[i] = t
    host = win32gui.FindWindow("GridHostWindowClass", None)
    if host and win32gui.IsWindowVisible(host):
        events.setdefault("grid window visible", t)
        cells = []
        win32gui.EnumChildWindows(host, lambda h, _: cells.append(h) if win32gui.GetClassName(h) == "GridCellWindowClass" else None, None)
        for i, c in enumerate(cells[:n]):
            if lit[i] is None:
                l, tp, r, b = win32gui.GetWindowRect(c)
                a = np.asarray(ImageGrab.grab(bbox=(l, tp, r, b), all_screens=True).convert("L").resize((64, 36)))
                bright = (a > 16).mean()
                if bright < 0.05:
                    seen_dark[i] = True  # the black cell, before mpv draws into it
                elif seen_dark[i] and bright > 0.3:
                    lit[i] = t
    time.sleep(0.05)
for i in range(n):
    if vo_up[i] is not None:
        events[f"mpv {i + 1}: video output ready"] = vo_up[i]
    if playing[i] is not None:
        events[f"mpv {i + 1}: playback clock running"] = playing[i]
open(cfg["stop_file"], "w").close()
p.wait(timeout=90)
marks = [("=== GDPNow ===", "player started (Python + imports)"), ("Matched", "videos matched"),
         ("Selected Monitor", "questions answered"), ("cells ready", "grid + cells created"),
         ("Started MPV instance", "mpv started"), ("Successfully connected to pipe", "mpv control pipe connected"),
         ("Initialization Summary", "all mpv started"), ("Cycle Thread: Execution loop", "rotation running")]
print(f"{'seconds':>8}  event")
rows = [(t, label) for t, label in [(next((t for t, l in log if m in l), None), lab) for m, lab in marks] if t is not None]
rows += [(t, k) for k, t in events.items()]
rows += [(t, f"cell {i + 1} shows a picture") for i, t in enumerate(lit) if t is not None]
for t, label in sorted(rows):
    print(f"{t:8.2f}  {label}")
starts = [t for t, l in log if "Started MPV instance" in l]
pipes = [t for t, l in log if "Successfully connected to pipe" in l]
print("mpv start times:", [round(x, 2) for x in starts])
print("pipe connected times:", [round(x, 2) for x in pipes])
if None in lit:
    print("cells never lit:", [i + 1 for i, x in enumerate(lit) if x is None])
