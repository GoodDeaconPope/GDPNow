"""Open the app and measure its title bar brightness as soon as it appears,
without moving or activating it (that would repaint it and hide the bug).

    python titlebar_color.py PYTHONW_EXE APP_PY
"""
import ctypes
import subprocess
import sys
import time

import numpy as np
import win32gui
import win32process
from PIL import ImageGrab

ctypes.windll.shcore.SetProcessDpiAwareness(2)
p = subprocess.Popen([sys.argv[1], sys.argv[2]])
hwnd = 0
t0 = time.time()
while time.time() - t0 < 30 and not hwnd:
    def cb(h, _):
        global hwnd
        if win32gui.GetWindowText(h) == "GDPNow" and win32gui.IsWindowVisible(h) \
                and win32process.GetWindowThreadProcessId(h)[1] == p.pid:
            hwnd = h
    win32gui.EnumWindows(cb, None)
    time.sleep(0.02)
samples = []
for delay in (0.1, 0.5, 1.5, 3.0):
    time.sleep(max(0, delay - (samples[-1][0] if samples else 0)))
    l, t, r, b = win32gui.GetWindowRect(hwnd)
    strip = ImageGrab.grab(bbox=(l + 60, t + 2, r - 160, t + 26), all_screens=True).convert("L")
    samples.append((delay, round(float(np.asarray(strip).mean()))))
print("title bar brightness after it appears (0=black, 255=white):", samples)
win32gui.PostMessage(hwnd, 0x0010, 0, 0)  # WM_CLOSE
p.wait(timeout=20)
