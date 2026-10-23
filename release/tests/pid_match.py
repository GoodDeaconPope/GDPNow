"""Why does GridHost's PID search miss our own mpv windows? Start gdpnow,
then compare the PIDs it launched with the PIDs that own the mpv windows.

    python pid_match.py ANSWERS_JSON
"""
import json
import os
import re
import subprocess
import sys
import threading
import time

import win32gui
import win32process

answers = sys.argv[1]
stop_file = json.load(open(answers))["stop_file"]
if os.path.exists(stop_file):
    os.remove(stop_file)
gdpnow = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "gdpnow.py")
env = dict(os.environ, PYTHONUTF8="1", GDPNOW_OUTPUT_DIR=os.path.join(os.environ["TEMP"], "gdp_startup_test"))
p = subprocess.Popen([sys.executable, "-u", "-W", "ignore::SyntaxWarning", gdpnow, "--answers", answers],
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env)
lines = []
threading.Thread(target=lambda: [lines.append(l.decode("utf-8", "replace").rstrip()) for l in p.stdout], daemon=True).start()
samples = []
t0 = time.time()
while time.time() - t0 < 8:
    wins = []

    def cb(h, _):
        if win32gui.GetClassName(h) == "mpv":
            pid = win32process.GetWindowThreadProcessId(h)[1]
            wins.append((pid, bool(win32gui.IsWindowVisible(h)), win32gui.GetParent(h)))
    win32gui.EnumWindows(cb, None)
    samples.append((round(time.time() - t0, 1), wins))
    time.sleep(0.5)
open(stop_file, "w").close()
p.wait(timeout=60)
time.sleep(0.5)
launched = [int(m.group(1)) for l in lines for m in [re.search(r"Started MPV instance with PID: (\d+)", l)] if m]
print("gdpnow launched mpv PIDs:", launched)
for t, wins in samples:
    print(f"t={t:4}s top-level mpv windows (pid, visible, parent): {wins}")
print("\n".join(l[:120] for l in lines if "GridHost" in l and "attempt" not in l)[:900])
attempts = [l for l in lines if "PID search attempt" in l]
print("search attempts:", len(attempts), attempts[-1:] if attempts else "")
