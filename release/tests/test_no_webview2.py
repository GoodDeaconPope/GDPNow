"""Simulate a PC without WebView2: start the app with GDPNOW_FAKE_NO_WEBVIEW2=1,
read the dialog, press No, check that the text version starts, then close it.

    python test_no_webview2.py DIST_GDPNOW_DIR
"""
import json
import os
import subprocess
import sys
import time

import win32con
import win32gui
import win32process

dist = os.path.abspath(sys.argv[1])
env = dict(os.environ, GDPNOW_FAKE_NO_WEBVIEW2="1")
app = subprocess.Popen([os.path.join(dist, "runtime", "python", "pythonw.exe"),
                        os.path.join(dist, "app", "gdpnow_app.py")], env=env)


def dialogs(pid):
    found = []

    def cb(h, _):
        if win32gui.GetClassName(h) == "#32770" and win32process.GetWindowThreadProcessId(h)[1] == pid:
            found.append(h)
    win32gui.EnumWindows(cb, None)
    return found


dlg = 0
for _ in range(100):
    d = dialogs(app.pid)
    if d:
        dlg = d[0]
        break
    time.sleep(0.1)
texts = []
win32gui.EnumChildWindows(dlg, lambda h, _: texts.append((win32gui.GetClassName(h), win32gui.GetWindowText(h))), None)
print("dialog:", json.dumps([t for c, t in texts if t], ensure_ascii=False))
win32gui.PostMessage(dlg, win32con.WM_COMMAND, 7, 0)  # IDNO
app.wait(timeout=20)
print("app exit code:", app.returncode)

# the text version: a cmd.exe running the console bat from this dist
time.sleep(3)
ps = ("Get-CimInstance Win32_Process | ? { $_.CommandLine -like '*" + dist.replace("'", "''")
      + "*' -and ($_.Name -eq 'cmd.exe' -or $_.Name -eq 'python.exe') } | "
      "Select ProcessId,ParentProcessId,Name,CommandLine | ConvertTo-Json")
out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True).stdout
procs = json.loads(out) if out.strip() else []
procs = procs if isinstance(procs, list) else [procs]
procs = [p for p in procs if p["ProcessId"] != os.getpid()
         and ("Console mode" in (p["CommandLine"] or "") or "runtime\\python" in (p["CommandLine"] or ""))]
for p in procs:
    print("started:", p["Name"], (p["CommandLine"] or "")[:140])
for p in procs:  # close only what this test started
    subprocess.run(["taskkill", "/PID", str(p["ProcessId"]), "/T", "/F"], capture_output=True)
print("text version started:", any(p["Name"] == "python.exe" and "gdpnow.py" in (p["CommandLine"] or "") for p in procs))
