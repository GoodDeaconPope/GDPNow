"""Test helper: screenshot a GDPNow app window (the app UI only).

    python winshot.py shot OUT.png [PID]     newest 'GDPNow' window, or the one owned by PID
    python winshot.py close PID              close that process's GDPNow window
"""
import ctypes
import sys
import time

import win32con
import win32gui
import win32process
from PIL import ImageGrab

ctypes.windll.shcore.SetProcessDpiAwareness(2)


def find(pid=None):
    hits = []

    def cb(h, _):
        if win32gui.IsWindowVisible(h) and win32gui.GetWindowText(h) == "GDPNow":
            if pid is None or win32process.GetWindowThreadProcessId(h)[1] == pid:
                hits.append(h)
    win32gui.EnumWindows(cb, None)
    return hits[0] if hits else 0


cmd = sys.argv[1]
pid = int(sys.argv[3]) if cmd == "shot" and len(sys.argv) > 3 else (int(sys.argv[2]) if cmd == "close" else None)
hwnd = 0
for _ in range(100):
    hwnd = find(pid)
    if hwnd:
        break
    time.sleep(0.2)
if not hwnd:
    sys.exit("window not found")
if cmd == "close":
    win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
    sys.exit(0)
win32gui.ShowWindow(hwnd, 9)
try:
    win32gui.SetForegroundWindow(hwnd)
except Exception:
    pass
time.sleep(0.8)
l, t, r, b = win32gui.GetWindowRect(hwnd)
ImageGrab.grab(bbox=(l + 8, t, r - 8, b - 8), all_screens=True).save(sys.argv[2])
print("saved", (l, t, r, b))
