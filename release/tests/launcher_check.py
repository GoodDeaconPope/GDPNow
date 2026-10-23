"""Check GDPNow.exe: (1) from the release folder it opens the app window (no
console window), (2) copied alone it explains that the zip must be extracted.

    python launcher_check.py RELEASE_DIR
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time

import psutil
import win32gui
import win32process

rel = os.path.abspath(sys.argv[1])
exe = os.path.join(rel, "GDPNow.exe")
env = {k: os.environ[k] for k in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA", "APPDATA")}
env["PATH"] = r"C:\Windows\System32;C:\Windows"

# 1. normal start
launcher = subprocess.Popen([exe], env=env)
win, consoles = 0, 0
t0 = time.time()
while time.time() - t0 < 30 and not win:
    def cb(h, _):
        global win, consoles
        if not win32gui.IsWindowVisible(h):
            return
        try:
            proc = psutil.Process(win32process.GetWindowThreadProcessId(h)[1])
            exe_path = proc.exe()
        except psutil.Error:
            return
        if not exe_path.lower().startswith(rel.lower()):
            return
        if win32gui.GetClassName(h) == "ConsoleWindowClass":
            consoles += 1
        elif win32gui.GetWindowText(h) == "GDPNow":
            win = h
    win32gui.EnumWindows(cb, None)
    time.sleep(0.1)
print(f"launcher exit code: {launcher.wait(timeout=10)}; app window opened: {bool(win)} "
      f"after {time.time() - t0:.1f}s; console windows from the release: {consoles}")
if win:
    owner = psutil.Process(win32process.GetWindowThreadProcessId(win)[1])
    print("app window owner:", owner.name(), owner.exe())
    win32gui.PostMessage(win, 0x0010, 0, 0)
    time.sleep(3)

# 2. exe alone (as if double-clicked inside the zip)
tmp = tempfile.mkdtemp()
alone = os.path.join(tmp, "GDPNow.exe")
shutil.copy2(exe, alone)
p = subprocess.Popen([alone])
box = 0
for _ in range(100):
    box = win32gui.FindWindow("#32770", "GDPNow")
    if box:
        break
    time.sleep(0.1)
texts = []
if box:
    win32gui.EnumChildWindows(box, lambda h, _: texts.append(win32gui.GetWindowText(h)), None)
    win32gui.PostMessage(box, 0x0111, 1, 0)  # OK
print("alone -> message:", [t for t in texts if len(t) > 3][:1])
p.wait(timeout=10)
shutil.rmtree(tmp, ignore_errors=True)
