"""Open the text version's folder picker, check it is the modern (Explorer-style)
dialog, then drive it: type a folder + OK, and Cancel.

    python test_folder_picker.py PYTHON_EXE APP_DIR
"""
import subprocess
import sys
import time

import win32con
import win32gui
import win32process

py, app = sys.argv[1], sys.argv[2]
CHILD = ("import sys; sys.argv=['x']; sys.path.insert(0, r'%s'); import gdpnow_support as s; "
         "print('RESULT', repr(s._modern_folder_dialog('GDPNow picker test', r'C:\\Windows')))" % app)


def find(pid):
    hits = []
    win32gui.EnumWindows(lambda h, _: hits.append(h) if win32gui.GetWindowText(h) == "GDPNow picker test"
                         and win32process.GetWindowThreadProcessId(h)[1] == pid else None, None)
    return hits[0] if hits else 0


for mode in ("ok", "cancel"):
    child = [py, sys.argv[3]] if len(sys.argv) > 3 else [py, "-c", CHILD]  # optional debug script
    p = subprocess.Popen(child, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    dlg = 0
    for _ in range(100):
        dlg = find(p.pid)
        if dlg:
            break
        time.sleep(0.1)
    time.sleep(2.0)  # let the dialog build its Explorer view
    kids = []
    win32gui.EnumChildWindows(dlg, lambda h, _: kids.append(h), None)
    classes = {win32gui.GetClassName(h) for h in kids}
    print(f"[{mode}] dialog found: {bool(dlg)}, modern Explorer view (DirectUIHWND): {'DirectUIHWND' in classes}")
    buttons = {win32gui.GetDlgCtrlID(h): h for h in kids if win32gui.GetClassName(h) == "Button"}
    if mode == "ok":  # pick the folder the dialog opened in (or its selected item)
        win32gui.SendMessage(buttons.get(win32con.IDOK), win32con.BM_CLICK, 0, 0)
    else:
        win32gui.SendMessage(buttons.get(win32con.IDCANCEL), win32con.BM_CLICK, 0, 0)
    out = p.communicate(timeout=20)[0].strip().splitlines()
    print(f"[{mode}]", " | ".join(out[-3:]) if out else "(no output)")
