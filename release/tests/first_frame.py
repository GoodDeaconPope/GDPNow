"""How long until each grid cell shows video? Samples every 0.5 s: mpv's
time-pos per cell and the brightness of that cell on screen (numbers only).

    python first_frame.py ANSWERS_JSON [seconds=12]
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
answers, secs = sys.argv[1], float(sys.argv[2]) if len(sys.argv) > 2 else 12
cfg = json.load(open(answers))
n = int(cfg["cols"]) * int(cfg["rows"])
stop_file = cfg["stop_file"]
if os.path.exists(stop_file):
    os.remove(stop_file)
gdpnow = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "gdpnow.py")
env = dict(os.environ, PYTHONUTF8="1", GDPNOW_OUTPUT_DIR=os.path.join(os.environ["TEMP"], "gdp_ff_test"))
p = subprocess.Popen([sys.executable, "-u", "-W", "ignore::SyntaxWarning", gdpnow, "--answers", answers],
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env)
threading.Thread(target=lambda: p.stdout.read(), daemon=True).start()


def tpos(i):
    try:
        with open(rf"\\.\pipe\mpvsocket{i}", "r+b", buffering=0) as f:
            f.write(b'{"command":["get_property","time-pos"],"request_id":3}\n')
            buf = b""
            while True:
                ch = f.read(1)
                if not ch:
                    return None
                if ch != b"\n":
                    buf += ch
                    continue
                m = json.loads(buf)
                buf = b""
                if m.get("request_id") == 3:
                    return m.get("data")
    except OSError:
        return None


t0 = time.time()
first_video = [None] * n
first_pos = [None] * n
while time.time() - t0 < secs:
    t = round(time.time() - t0, 1)
    host = win32gui.FindWindow("GridHostWindowClass", None)
    cells = []
    if host:
        win32gui.EnumChildWindows(host, lambda h, _: cells.append(h) if win32gui.GetClassName(h) == "GridCellWindowClass" else None, None)
    for i, c in enumerate(cells[:n]):
        l, tp, r, b = win32gui.GetWindowRect(c)
        try:
            a = np.asarray(ImageGrab.grab(bbox=(l, tp, r, b), all_screens=True).convert("L").resize((96, 54)))
            lit = (a > 16).mean()
        except Exception:
            lit = 0
        if lit > 0.3 and first_video[i] is None:
            first_video[i] = t
        if first_pos[i] is None and (tpos(i) or 0) > 0:
            first_pos[i] = t
    time.sleep(0.5)
open(stop_file, "w").close()
p.wait(timeout=90)
print("seconds until mpv reports playback per cell:", first_pos)
print("seconds until the cell shows a picture on screen:", first_video)
