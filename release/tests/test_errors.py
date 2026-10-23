"""Crash the app on purpose and check the 'DM the pope on Discord' error dialog.

    python test_errors.py DIST_GDPNOW_DIR app|console [SHOT.png]

app      crash in the app window before it opens (native dialog)
console  crash in the text-version player (native dialog)
Reads every text in the dialog (including the report text box), optionally
screenshots it, presses OK, and checks the process exits. Never touches the clipboard.
"""
import ctypes
import glob
import os
import subprocess
import sys
import time

import win32con
import win32gui
import win32process
from PIL import ImageGrab

ctypes.windll.shcore.SetProcessDpiAwareness(2)
dist, mode = os.path.abspath(sys.argv[1]), sys.argv[2]
shot = sys.argv[3] if len(sys.argv) > 3 else None
py = os.path.join(dist, "runtime", "python", "pythonw.exe" if mode == "app" else "python.exe")
script = os.path.join(dist, "app", "gdpnow_app.py" if mode == "app" else "gdpnow.py")
before = set(glob.glob(os.path.join(dist, "error reports", "*.txt")))
env = dict(os.environ, GDPNOW_TEST_CRASH="app" if mode == "app" else "player")
proc = subprocess.Popen([py, "-W", "ignore::SyntaxWarning", script], env=env, cwd=dist,
                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        creationflags=subprocess.CREATE_NO_WINDOW)


def dialogs(pid):
    found = []

    def cb(h, _):
        if win32gui.GetClassName(h) == "#32770" and win32process.GetWindowThreadProcessId(h)[1] == pid:
            found.append(h)
    win32gui.EnumWindows(cb, None)
    return found


dlg = 0
for _ in range(300):
    d = dialogs(proc.pid)
    if d:
        dlg = d[0]
        break
    time.sleep(0.1)
if not dlg:
    print("NO ERROR DIALOG APPEARED")
else:
    time.sleep(0.6)
    print("TITLE:", win32gui.GetWindowText(dlg))
    kids = []
    win32gui.EnumChildWindows(dlg, lambda h, _: kids.append((win32gui.GetClassName(h), win32gui.GetWindowText(h))), None)
    for cls, text in kids:
        if cls == "Edit":
            print(f"[text box, {len(text)} chars, fits one Discord message: {len(text) <= 2000}]")
            print("    " + text.replace("\r\n", "\n").replace("\n", "\n    ")[:900])
        elif text:
            print(f"[{cls}] {text}")
    if shot:
        ImageGrab.grab(bbox=win32gui.GetWindowRect(dlg), all_screens=True).save(shot)
        print("screenshot:", shot)
    win32gui.PostMessage(dlg, win32con.WM_COMMAND, win32con.IDOK, 0)
t0 = time.time()
out = proc.communicate(timeout=120)[0].decode("utf-8", "replace")
print(f"exit code {proc.returncode}, exited {time.time() - t0:.1f}s after OK")
new = sorted(set(glob.glob(os.path.join(dist, "error reports", "*.txt"))) - before)
print("report file saved:", [os.path.basename(p) for p in new])
if new:
    rep = open(new[-1], encoding="utf-8").read()
    print("user name visible in the report file:", os.path.expanduser("~").lower() in rep.lower())
