"""Smoke test of a built GDPNow.app on a Mac (run by the macOS workflow).
Uses generated test-pattern videos only. Checks, in order:
  bundled Python + libraries, mpv, ffmpeg encoders, the GPU decoder measurement,
  a 2x2 session from the player (cells play, rotation switches, E and Q
  arrive through mac_keys.lua, screenshots + run_grwm.command saved),
  the session video (run_grwm.py), the app window starting from the bundle,
  and the quarantine clean-up.
Screenshots of the screen go to OUT for a look.

    python3 mac_smoke.py /path/GDPNow.app OUT
"""
import glob
import json
import os
import shutil
import socket
import subprocess
import sys
import time

bundle, out = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2])
RES = os.path.join(bundle, "Contents", "Resources")
APP = os.path.join(RES, "app")
PY = os.path.join(RES, "runtime", "python", "bin", "python3")
MPV = os.path.join(RES, "runtime", "mpv", "mpv.app", "Contents", "MacOS", "mpv")
FF = os.path.join(RES, "runtime", "ffmpeg", "ffmpeg")
STATE = os.path.expanduser("~/Library/Application Support/GDPNow")
SOCK_DIR = f"/tmp/gdpnow-{os.getuid()}"
os.makedirs(out, exist_ok=True)
results = []


def check(name, ok, info=""):
    results.append((name, bool(ok), str(info)))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}  {info}", flush=True)


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def pyc(code):
    return sh([PY, "-W", "ignore", "-c", f"import sys; sys.path.insert(0, {APP!r})\n" + code], cwd=APP)


def ipc(path, cmd, timeout=3.0):
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(path)
        s.sendall((json.dumps({"command": cmd, "request_id": 5}) + "\n").encode())
        buf = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                return None
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                msg = json.loads(line)
                if msg.get("request_id") == 5:
                    s.close()
                    return msg.get("data")
    except (OSError, ValueError):
        return None


def screenshot(name):
    sh(["screencapture", "-x", os.path.join(out, name)])


# 1. bundled Python and libraries
r = pyc("import webview, pynput.keyboard, PIL, screeninfo, AppKit\n"
        "import gdpnow_support as ps\n"
        "print(ps.PORTABLE, ps.MPV_EXE, ps.FFMPEG_EXE, ps.PYTHON_EXE)\n"
        "from screeninfo import get_monitors; print(get_monitors())")
check("bundled python imports webview, pynput, PIL, screeninfo, AppKit", r.returncode == 0, (r.stdout + r.stderr).strip()[-600:])
check("support module finds the bundled tools", "True" in r.stdout and MPV in r.stdout and FF in r.stdout)

# 2. mpv and ffmpeg
r = sh([MPV, "--version"])
check("mpv runs", r.returncode == 0, r.stdout.splitlines()[0] if r.stdout else r.stderr[-300:])
r = sh([FF, "-hide_banner", "-encoders"])
check("ffmpeg has libx264 and h264_videotoolbox", "libx264" in r.stdout and "h264_videotoolbox" in r.stdout)

# 3. GPU decoder measurement (VideoToolbox)
r = pyc("import gdpnow_support as ps; print(round(ps.measure_gpu_decode(), 2), ps._gpu_name())")
check("GPU decoder measurement runs", r.returncode == 0, r.stdout.strip() or r.stderr[-300:])

# 4. test videos + map + settings
vids = os.path.join(out, "videos")
os.makedirs(vids, exist_ok=True)
system_map = {}
for n in range(101, 109):  # 2x2 grid x 2 episodes per cell
    path = os.path.join(vids, f"Episode {n} test.mp4")
    if not os.path.exists(path):
        sh([FF, "-v", "error", "-f", "lavfi", "-i", f"testsrc2=s=1280x720:r=30:d=40",
            "-f", "lavfi", "-i", f"sine=frequency={200 + n}:d=40", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", path])
    system_map[f"{n}.mp4"] = {"RC": "00:02", "ERC": "00:35", "CSP": "00:20"}
map_file = os.path.join(vids, "systemFileMap.json")
json.dump(system_map, open(map_file, "w"), indent=1)
os.makedirs(STATE, exist_ok=True)
json.dump({"video_dir": vids, "map_file": map_file}, open(os.path.join(STATE, "settings.json"), "w"))
check("test videos made", len(glob.glob(os.path.join(vids, "*.mp4"))) == 8)

# 5. a 2x2 session from the player
stop_file = os.path.join(out, "stop")
answers = os.path.join(out, "answers.json")
json.dump({"monitor": 1, "master_type": "RC", "cols": 2, "rows": 2, "cycle": 2, "interval": 6, "minutes": 1,
           "stop_file": stop_file}, open(answers, "w"))
session_out = os.path.join(out, "session")
log_path = os.path.join(out, "player.log")
env = dict(os.environ, PYTHONUTF8="1", GDPNOW_OUTPUT_DIR=session_out)
t0 = time.time()
player = subprocess.Popen([PY, "-u", "-W", "ignore", os.path.join(APP, "gdpnow.py"), "--answers", answers],
                          cwd=RES, stdout=open(log_path, "w"), stderr=subprocess.STDOUT, env=env)
socks = [os.path.join(SOCK_DIR, f"mpvsocket{i}") for i in range(4)]
playing = {}
while time.time() - t0 < 90 and len(playing) < 4 and player.poll() is None:
    for i, s in enumerate(socks):
        if i not in playing and (ipc(s, ["get_property", "playback-time"]) or 0) > 0.2:
            playing[i] = round(time.time() - t0, 1)
    time.sleep(0.3)
check("all 4 cells play", len(playing) == 4, f"started at {playing}")
names_before = [ipc(s, ["get_property", "filename"]) for s in socks]
geoms = [ipc(s, ["get_property", "geometry"]) for s in socks]
check("cell geometry set", all(geoms), geoms)


def cell_spread(png):
    """Colour spread of each grid quarter on a screen capture: the test
    pattern gives ~100, a flat colour (blank or undrawn cell) ~0."""
    r = sh([PY, "-c", "import sys\nfrom PIL import Image, ImageStat\n"
            "im = Image.open(sys.argv[1]).convert('RGB'); w, h = im.size\n"
            "top, bottom = int(h * 0.05), int(h * 0.85)\n"  # skip the menu bar and the Dock
            "boxes = [(0, top, w // 2, h // 2), (w // 2, top, w, h // 2), (0, h // 2, w // 2, bottom), (w // 2, h // 2, w, bottom)]\n"
            "print([round(sum(ImageStat.Stat(im.crop(b)).stddev) / 3, 1) for b in boxes])", png])
    return json.loads(r.stdout.strip() or "[0, 0, 0, 0]")


# cells must show the test pattern on screen (a slow test Mac may draw a cell's
# first frame late, so two captures 8 s apart); episodes rotate every 6 s
time.sleep(2)
screenshot("grid.png")
names_seen = [names_before]
for _ in range(8):
    time.sleep(1)
    names_seen.append([ipc(s, ["get_property", "filename"]) for s in socks])
screenshot("grid2.png")
spreads = [cell_spread(os.path.join(out, "grid.png")), cell_spread(os.path.join(out, "grid2.png"))]
best = [max(a, b) for a, b in zip(*spreads)]
check("cells show video on screen", min(best) > 15, f"colour spread per cell, two captures: {spreads}")
check("rotation switched episodes", any(n != names_before for n in names_seen[1:]),
      f"{names_before} -> {names_seen[-1]}")
before = open(log_path).read()
ipc(socks[0], ["keypress", "e"])
time.sleep(3)
after = open(log_path).read()[len(before):]
check("E pressed in a cell reaches the player (CSP)", "CSP" in after, after.strip()[-300:])
ipc(socks[1], ["keypress", "q"])
try:
    player.wait(timeout=40)
except subprocess.TimeoutExpired:
    player.kill()
log = open(log_path).read()
check("Q pressed in a cell stops the player", player.returncode is not None and "IMMEDIATE SHUTDOWN" in log)
check("no traceback in the player log", "Traceback" not in log, log[log.find("Traceback"):][:800] if "Traceback" in log else "")
sessions = [d for d in glob.glob(os.path.join(session_out, "*")) if os.path.isdir(d)]
shots = glob.glob(os.path.join(sessions[0], "scene_*.png")) if sessions else []
runner = os.path.join(sessions[0], "run_grwm.command") if sessions else ""
check("session screenshots saved", len(shots) >= 1, f"{len(shots)} in {sessions[:1]}")
check("run_grwm.command written and executable", runner and os.access(runner, os.X_OK))
if shots:
    shutil.copy2(sorted(shots)[-1], os.path.join(out, "session_screenshot.png"))
time.sleep(1)
check("no grid mpv left running", not sh(["pgrep", "-f", "mpvsocket"]).stdout.strip())

# 6. the session video
if sessions:
    r = sh([PY, os.path.join(sessions[0], "run_grwm.py")], cwd=sessions[0], timeout=600)
    video = os.path.join(sessions[0], "grwm.mp4")
    check("run_grwm.py builds grwm.mp4", os.path.isfile(video) and os.path.getsize(video) > 10000,
          (r.stdout + r.stderr).strip()[-400:])

# 7. the app window from the bundle (through LaunchServices, like a double-click)
reports = set(glob.glob(os.path.join(STATE, "error reports", "*")))
sh(["open", "-n", bundle])
time.sleep(12)
pid = sh(["pgrep", "-f", "gdpnow_app.py"]).stdout.split()
screenshot("app.png")
new_reports = set(glob.glob(os.path.join(STATE, "error reports", "*"))) - reports
check("app window process runs", bool(pid), pid)
check("no error report from the app", not new_reports,
      open(next(iter(new_reports))).read()[-800:] if new_reports else "")
for p in pid:
    sh(["kill", p])

# 8. quarantine: a downloaded copy gets the mark; the app clears it
qdir = os.path.join(out, "quarantine")
shutil.rmtree(qdir, ignore_errors=True)
os.makedirs(qdir)
qapp = os.path.join(qdir, "GDPNow.app")
sh(["ditto", bundle, qapp])
sh(["xattr", "-rw", "com.apple.quarantine", f"0081;{int(time.time()):x};Safari;", qapp])
qres = os.path.join(qapp, "Contents", "Resources")
try:
    r = sh([os.path.join(qres, "runtime", "python", "bin", "python3"), "-c",
            f"import sys; sys.path.insert(0, {os.path.join(qres, 'app')!r}); import gdpnow_app; "
            "print(gdpnow_app._mac_check_location())"], timeout=60)
except subprocess.TimeoutExpired:
    r = subprocess.CompletedProcess([], 1, "", "timed out (blocked by Gatekeeper?)")
left = sh(["xattr", "-p", "com.apple.quarantine", os.path.join(qres, "runtime", "mpv", "mpv.app", "Contents", "MacOS", "mpv")])
check("app clears the download mark from its tools", r.stdout.strip().endswith("True") and left.returncode != 0,
      (r.stdout + r.stderr).strip()[-300:])
shutil.rmtree(qdir, ignore_errors=True)

failed = [n for n, ok, _ in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed", flush=True)
json.dump([{"check": n, "ok": ok, "info": i} for n, ok, i in results], open(os.path.join(out, "results.json"), "w"), indent=1)
shutil.rmtree(vids, ignore_errors=True)
sys.exit(1 if failed else 0)
