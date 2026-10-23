"""Force the native error dialog to fail and check the fallback: the report opens
in Notepad (copyable) plus a plain box. Closes both afterwards.

    python test_error_fallback.py APP_DIR
"""
import sys
import threading
import time

import win32con
import win32gui

sys.argv, app = ["x"], sys.argv[1]
sys.path.insert(0, app)
import gdpnow_support as s  # noqa: E402

s._error_dialog = lambda intro, report: False  # pretend the dialog could not be shown
seen = {}


def watcher():
    for _ in range(200):
        np = []
        win32gui.EnumWindows(lambda h, _: np.append(h) if "copy this into Discord" in win32gui.GetWindowText(h) else None, None)
        box = win32gui.FindWindow(None, "GDPNow - error")
        if np and box:
            seen["notepad"] = win32gui.GetWindowText(np[0])
            kids = []
            win32gui.EnumChildWindows(box, lambda h, _: kids.append(win32gui.GetWindowText(h)), None)
            seen["box"] = [k for k in kids if k and k != "OK"]
            time.sleep(0.5)
            win32gui.PostMessage(box, win32con.WM_COMMAND, win32con.IDOK, 0)
            win32gui.PostMessage(np[0], win32con.WM_CLOSE, 0, 0)
            return
        time.sleep(0.1)


threading.Thread(target=watcher, daemon=True).start()
s.report_error_native("test", "RuntimeError: fallback test")
time.sleep(0.5)
print("notepad window:", seen.get("notepad"))
print("box text:", seen.get("box"))
note = s.os.path.join(s.REPORTS_DIR, "copy this into Discord.txt")
print("notepad file starts with:", open(note, encoding="utf-8").read().splitlines()[:2])
