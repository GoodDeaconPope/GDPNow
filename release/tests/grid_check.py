"""End-to-end check of the grid after the --wid change (mpv drawing inside
GridHost cells). Runs gdpnow with answers, then while it plays:
  - mpv windows sit inside the cells and match the cell size
  - no stray top-level mpv / cmd windows appear
  - zoom (panscan) and pan (video-pan-x) are accepted by every mpv
  - resizing the grid window re-tiles the cells and mpv follows
  - cycling switches files
then stops it and checks screenshots + GRWM files.

    python grid_check.py ANSWERS_JSON OUT_DIR [run_seconds=24]
"""
import glob
import json
import os
import subprocess
import sys
import threading
import time

import psutil
import win32con
import win32gui
import win32process

answers, out_dir = sys.argv[1], sys.argv[2]
run_secs = float(sys.argv[3]) if len(sys.argv) > 3 else 24
cfg = json.load(open(answers))
n_cells = int(cfg["cols"]) * int(cfg["rows"])
stop_file = cfg["stop_file"]
if os.path.exists(stop_file):
    os.remove(stop_file)
os.makedirs(out_dir, exist_ok=True)
gdpnow = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "gdpnow.py")
env = dict(os.environ, PYTHONUTF8="1", GDPNOW_OUTPUT_DIR=out_dir)
p = subprocess.Popen([sys.executable, "-u", "-W", "ignore::SyntaxWarning", gdpnow, "--answers", answers],
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env)
log = []
threading.Thread(target=lambda: [log.append(l.decode("utf-8", "replace").rstrip()) for l in p.stdout], daemon=True).start()
results = {}


def ipc(i, cmd):
    try:
        with open(rf"\\.\pipe\mpvsocket{i}", "r+b", buffering=0) as f:
            f.write((json.dumps({"command": cmd, "request_id": 9}) + "\n").encode())
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
                if msg.get("request_id") == 9:
                    return msg
    except OSError as e:
        return {"error": str(e)}


def layout():
    host = win32gui.FindWindow("GridHostWindowClass", None)
    if not host:
        return None
    cells = []
    win32gui.EnumChildWindows(host, lambda h, _: cells.append(h) if win32gui.GetClassName(h) == "GridCellWindowClass" else None, None)
    info = []
    for c in cells:
        cr = win32gui.GetClientRect(c)
        kids = []
        win32gui.EnumChildWindows(c, lambda h, _: kids.append(h) if win32gui.GetClassName(h) == "mpv" else None, None)
        mr = win32gui.GetClientRect(kids[0]) if kids else None
        info.append({"cell": (cr[2], cr[3]), "mpv": (mr[2], mr[3]) if mr else None})
    hr = win32gui.GetClientRect(host)
    return {"host": host, "host_size": (hr[2], hr[3]), "cells": info}


def stray_windows():
    stray = []

    def cb(h, _):
        if not win32gui.IsWindowVisible(h):
            return
        cls = win32gui.GetClassName(h)
        if cls in ("mpv", "ConsoleWindowClass"):
            try:
                owner = psutil.Process(win32process.GetWindowThreadProcessId(h)[1])
                if any(a.pid == p.pid for a in owner.parents()):
                    stray.append(cls)
            except psutil.Error:
                pass
    win32gui.EnumWindows(cb, None)
    return stray


t0 = time.time()
strays = set()
while time.time() - t0 < 5:
    strays.update(stray_windows())
    time.sleep(0.05)
L = layout()
results["cells inside grid window"] = len(L["cells"]) if L else 0
results["mpv drawing in each cell"] = sum(1 for c in L["cells"] if c["mpv"]) if L else 0
results["mpv size == cell size"] = all(c["mpv"] == c["cell"] for c in L["cells"]) if L else False
results["cell sizes"] = [c["cell"] for c in L["cells"]] if L else None

# zoom + pan through mpv's own properties (the +/- and arrow keys use these)
pan_ok = []
for i in range(n_cells):
    ipc(i, ["set_property", "panscan", 0.5])
    ipc(i, ["set_property", "video-pan-x", 0.05])
    a = (ipc(i, ["get_property", "panscan"]) or {}).get("data")
    b = (ipc(i, ["get_property", "video-pan-x"]) or {}).get("data")
    pan_ok.append(a == 0.5 and abs((b or 0) - 0.05) < 1e-6)
    ipc(i, ["set_property", "panscan", 1.0])
    ipc(i, ["set_property", "video-pan-x", 0.0])
results["zoom + pan accepted by every mpv"] = all(pan_ok) and len(pan_ok) == n_cells

# resize the grid window: cells re-tile, mpv follows
if L:
    win32gui.ShowWindow(L["host"], win32con.SW_RESTORE)
    win32gui.SetWindowPos(L["host"], None, 100, 100, 1280, 720, win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE)
    time.sleep(1.5)
    L2 = layout()
    results["after resize: host"] = L2["host_size"]
    results["after resize: cells"] = [c["cell"] for c in L2["cells"]]
    results["after resize: mpv follows cells"] = all(c["mpv"] == c["cell"] for c in L2["cells"])

played = [set() for _ in range(n_cells)]  # every file each cell showed (sampled each second)
while time.time() - t0 < run_secs:
    strays.update(stray_windows())
    for i in range(n_cells):
        path = (ipc(i, ["get_property", "path"]) or {}).get("data")
        if path:
            played[i].add(path)
    time.sleep(1.0)
results["files each cell played (cycling)"] = [len(s) for s in played]
results["stray mpv/cmd windows seen"] = sorted(strays)

open(stop_file, "w").close()
p.wait(timeout=90)
time.sleep(1)
sess = sorted(glob.glob(os.path.join(out_dir, "*", "")), key=os.path.getmtime)
s = sess[-1] if sess else ""
results["screenshots"] = len(glob.glob(os.path.join(s, "scene_*.png")))
results["GRWM files"] = [os.path.basename(f) for f in glob.glob(os.path.join(s, "run_grwm.*"))]
results["log errors"] = [l for l in log if "Traceback" in l or "ERROR_REPORT" in l or "failed" in l.lower()][:5]
results["cycle switches in log"] = sum("Cycle Switch" in l for l in log)
for k, v in results.items():
    print(f"{k}: {v}")
