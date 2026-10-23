"""List every top-level window that appears while gdpnow starts (any program)
and every process gdpnow itself starts, to find stray (console) windows.

    python new_windows.py ANSWERS_JSON [seconds=10]
"""
import json
import os
import subprocess
import sys
import threading
import time

import psutil
import win32gui
import win32process

answers, secs = sys.argv[1], float(sys.argv[2]) if len(sys.argv) > 2 else 10
stop_file = json.load(open(answers))["stop_file"]
if os.path.exists(stop_file):
    os.remove(stop_file)
gdpnow = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "gdpnow.py")


def visible_windows():
    found = []
    win32gui.EnumWindows(lambda h, _: found.append(h) if win32gui.IsWindowVisible(h) else None, None)
    return found


before = set(visible_windows())
env = dict(os.environ, PYTHONUTF8="1", GDPNOW_OUTPUT_DIR=os.path.join(os.environ["TEMP"], "gdp_startup_test"))
p = subprocess.Popen([sys.executable, "-u", "-W", "ignore::SyntaxWarning", gdpnow, "--answers", answers],
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env)
threading.Thread(target=lambda: p.stdout.read(), daemon=True).start()  # keep the pipe drained
me = psutil.Process(p.pid)
t0 = time.time()
seen, children = {}, {}
while time.time() - t0 < secs:
    for h in visible_windows():
        if h in before or h in seen:
            continue
        try:
            pid = win32process.GetWindowThreadProcessId(h)[1]
            name = psutil.Process(pid).name()
        except Exception:
            pid, name = 0, "?"
        l, t, r, b = win32gui.GetWindowRect(h)
        seen[h] = (round(time.time() - t0, 2), win32gui.GetClassName(h), win32gui.GetWindowText(h)[:40],
                   f"{r - l}x{b - t}", f"{name}({pid})")
    try:
        for c in me.children(recursive=True):
            if c.pid not in children:
                try:
                    parent = psutil.Process(c.ppid())
                    pinfo = f"{parent.name()}({c.ppid()})"
                    cmdline = " ".join(c.cmdline())[:160]
                except psutil.Error:
                    pinfo, cmdline = "?", "?"
                children[c.pid] = (round(time.time() - t0, 2), c.name(), pinfo, cmdline)
    except psutil.Error:
        pass
    time.sleep(0.03)
open(stop_file, "w").close()
p.wait(timeout=60)
print("processes gdpnow started:", sorted(set(c[1] for c in children.values())), f"({len(children)} total)")
shown = set()
for pid, (t, name, parent, cmdline) in sorted(children.items(), key=lambda x: x[1][0]):
    key = (name, parent.split("(")[0], cmdline[:60])
    if key in shown:
        continue
    shown.add(key)
    print(f"  t={t:5}s {name:16} parent={parent:22} cmd={cmdline}")
if "--summary" in sys.argv:
    seen = {}
for h, info in sorted(seen.items(), key=lambda x: x[1][0]):
    print(f"  t={info[0]:5}s  class={info[1]!r:28} title={info[2]!r:30} size={info[3]:10} owner={info[4]}")
consoles = [i for i in seen.values() if i[1] in ("ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS")]
print(f"{len(seen)} new top-level windows, {len(consoles)} of them console windows")
