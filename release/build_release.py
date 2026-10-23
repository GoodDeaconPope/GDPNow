"""Build the GDPNow release zip (for a stock Windows PC) from this repo.

The release ships the same gdpnow.py you run here, plus gdpnow_support.py,
gdpnow_app.py (the app window), ui\\ and systemFileMap.json, next to a
bundled runtime: embeddable Python 3.12 with its libraries, mpv, ffmpeg and
Microsoft's WebView2 installer.

    python release\\build_release.py [--version 1.0.0]
                                               -> build\\dist\\GDPNow\\ + GDPNow-<version>-windows-x64.zip
    python release\\build_release.py --no-zip
    python release\\build_release.py --out NAME (build into dist\\NAME, e.g. while GDPNow is open)

Downloads it needs (cached in build\\downloads):
  pyembed.zip   python.org python-3.12.10-embed-amd64.zip
  wheels\\       requirements.txt (+ deps; proxy_tools and bottle are sdist-only
                upstream: build them with `pip wheel --no-deps`)
  mpv.7z        zhongfly/mpv-winbuild mpv-x86_64-<date>.7z (non-v3: runs on any x64 CPU)
  MicrosoftEdgeWebview2Setup.exe   https://go.microsoft.com/fwlink/p/?LinkId=2124703
ffmpeg.exe comes from PATH (or GDPNOW_FFMPEG): a static GPL build with NVENC, libx264,
drawtext.
"""
import datetime
import hashlib
import os
import py_compile
import re
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
BUILD = os.environ.get("GDPNOW_BUILD_DIR", os.path.join(REPO, "build"))
# The map you edit day to day (the "working map") can live outside the repo;
# each build copies it over the repo's release map so the zip ships the latest.
WORKING_MAP = os.environ.get("GDPNOW_WORKING_MAP", r"E:\1SystemFile\systemFileMap.json")
DL = os.path.join(BUILD, "downloads")
DIST = os.path.join(BUILD, "dist")
OUT = os.path.join(DIST, sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else "GDPNow")
VERSION = sys.argv[sys.argv.index("--version") + 1] if "--version" in sys.argv else "1.0.0"
SEVENZIP = r"C:\Program Files\7-Zip\7z.exe"
CSC = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), r"Microsoft.NET\Framework64\v4.0.30319\csc.exe")
FFMPEG_SRC = os.environ.get("GDPNOW_FFMPEG") or shutil.which("ffmpeg.exe")
APP_FILES = ("gdpnow.py", "gdpnow_support.py", "gdpnow_app.py", "systemFileMap.json")
TEXT_EXT = (".py", ".json", ".txt", ".bat", ".html", ".css", ".js", ".manifest")


def run(cmd, **kw):
    print("  $", " ".join(cmd))
    subprocess.run(cmd, check=True, **kw)


def privacy_check(root):
    """Fail the build if a shipped text file names this PC's user or their old email."""
    home = os.path.expanduser("~")
    needles = {home.lower(), home.replace("\\", "/").lower(), os.path.basename(home).lower()}
    needles |= {n.strip().lower() for n in os.environ.get("GDPNOW_PRIVATE_WORDS", "").split(",") if n.strip()}
    found = []
    for dirpath, _, files in os.walk(root):
        if os.sep + "runtime" in dirpath:
            continue  # third-party binaries
        for name in files:
            if not name.lower().endswith(TEXT_EXT):
                continue
            p = os.path.join(dirpath, name)
            text = open(p, encoding="utf-8", errors="replace").read().lower()
            for n in needles:
                if n and re.search(r"(?<![a-z])" + re.escape(n) + r"(?![a-z])", text):
                    found.append(f"{os.path.relpath(p, root)}: {n!r}")
    if found:
        raise SystemExit("PRIVACY CHECK FAILED (personal names/paths in shipped files):\n  " + "\n  ".join(found))


def build_launcher(exe_path):
    """GDPNow.exe (launcher\\GDPNow.cs): starts the app with no console window and
    carries the app icon + version. Compiled with the C# compiler in Windows."""
    if not os.path.isfile(CSC):
        raise SystemExit(f"C# compiler not found: {CSC}")
    info = os.path.join(BUILD, "_AssemblyInfo.cs")
    v4 = ".".join((VERSION.split("-")[0].split(".") + ["0", "0", "0"])[:4])
    with open(info, "w", encoding="utf-8") as f:
        f.write("using System.Reflection;\n"
                '[assembly: AssemblyTitle("GDPNow")]\n[assembly: AssemblyProduct("GDPNow")]\n'
                f'[assembly: AssemblyVersion("{v4}")]\n[assembly: AssemblyFileVersion("{v4}")]\n'
                f'[assembly: AssemblyInformationalVersion("{VERSION}")]\n')
    run([CSC, "/nologo", "/target:winexe", "/optimize+", f"/out:{exe_path}",
         f"/win32icon:{os.path.join(REPO, 'ui', 'gdpnow.ico')}", "/r:System.Windows.Forms.dll",
         os.path.join(HERE, "launcher", "GDPNow.cs"), info])
    os.remove(info)


def build():
    if not FFMPEG_SRC or not os.path.isfile(FFMPEG_SRC):
        raise SystemExit("ffmpeg.exe not found on PATH; set GDPNOW_FFMPEG")
    if os.path.exists(OUT):
        shutil.rmtree(OUT)
    app = os.path.join(OUT, "app")
    py = os.path.join(OUT, "runtime", "python")
    mpv = os.path.join(OUT, "runtime", "mpv")
    ff = os.path.join(OUT, "runtime", "ffmpeg")
    for d in (app, py, mpv, ff):
        os.makedirs(d)

    release_map = os.path.join(REPO, "systemFileMap.json")
    if os.path.isfile(WORKING_MAP) and open(WORKING_MAP, "rb").read() != open(release_map, "rb").read():
        import json
        json.load(open(WORKING_MAP, encoding="utf-8-sig"))  # refuse a broken working map
        shutil.copy2(WORKING_MAP, release_map)
        print(f"release map refreshed from the working map {WORKING_MAP} (commit it)")

    print("app: gdpnow.py, support module, app window, ui, map")
    for name in APP_FILES:
        shutil.copy2(os.path.join(REPO, name), app)
    shutil.copytree(os.path.join(REPO, "ui"), os.path.join(app, "ui"))
    for name in ("gdpnow.py", "gdpnow_support.py", "gdpnow_app.py"):
        py_compile.compile(os.path.join(app, name), doraise=True, cfile=os.path.join(BUILD, "_pycheck.pyc"))
    sha = lambda p: hashlib.sha256(open(p, "rb").read()).hexdigest()[:10]
    try:
        commit = subprocess.run(["git", "-C", REPO, "rev-parse", "--short", "HEAD"],
                                capture_output=True, text=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", REPO, "status", "--porcelain", "--", *APP_FILES, "ui"],
                               capture_output=True, text=True).stdout.strip()
    except OSError:
        commit, dirty = "?", ""
    with open(os.path.join(app, "VERSION.txt"), "w", encoding="utf-8") as f:
        f.write(f"GDPNow {VERSION}, build {datetime.datetime.now():%Y-%m-%d %H:%M}, commit {commit}{'+changes' if dirty else ''}, "
                f"gdpnow.py {sha(os.path.join(REPO, 'gdpnow.py'))}, map {sha(os.path.join(REPO, 'systemFileMap.json'))}\n")

    print("runtime: embeddable Python 3.12 + libraries")
    with zipfile.ZipFile(os.path.join(DL, "pyembed.zip")) as z:
        z.extractall(py)
    pth = [p for p in os.listdir(py) if p.endswith("._pth")][0]
    with open(os.path.join(py, pth), "w", encoding="ascii", newline="\r\n") as f:
        f.write("python312.zip\n.\nLib\\site-packages\n..\\..\\app\nimport site\n")
    run([sys.executable, "-m", "pip", "install", "--quiet", "--no-index",
         "--find-links", os.path.join(DL, "wheels"), "--target",
         os.path.join(py, "Lib", "site-packages"), "--no-compile",
         "-r", os.path.join(REPO, "requirements.txt")])

    print("runtime: mpv")
    tmp = os.path.join(BUILD, "_mpv_extract")
    shutil.rmtree(tmp, ignore_errors=True)
    run([SEVENZIP, "x", "-y", "-bso0", f"-o{tmp}", os.path.join(DL, "mpv.7z")])
    for name in os.listdir(tmp):
        if name.lower().endswith((".exe", ".com", ".dll")):
            shutil.copy2(os.path.join(tmp, name), mpv)
    shutil.copytree(os.path.join(tmp, "mpv"), os.path.join(mpv, "mpv"))  # fonts.conf
    shutil.rmtree(tmp)
    os.makedirs(os.path.join(mpv, "portable_config"))
    with open(os.path.join(mpv, "portable_config", "mpv.conf"), "w") as f:
        f.write("# Empty on purpose: this folder keeps the bundled mpv from loading any\n"
                "# mpv settings already installed on this PC (%APPDATA%\\mpv).\n")

    print("runtime: ffmpeg")
    shutil.copy2(FFMPEG_SRC, os.path.join(ff, "ffmpeg.exe"))

    print("runtime: WebView2 installer (only used on PCs that lack it)")
    os.makedirs(os.path.join(OUT, "runtime", "webview2"))
    shutil.copy2(os.path.join(DL, "MicrosoftEdgeWebview2Setup.exe"), os.path.join(OUT, "runtime", "webview2"))

    print("launchers + readme")
    build_launcher(os.path.join(OUT, "GDPNow.exe"))
    extras = os.path.join(OUT, "extras")
    os.makedirs(extras)
    with open(os.path.join(extras, "Console mode (typed questions).bat"), "w",
              encoding="ascii", newline="\r\n") as f:
        f.write("@echo off\ntitle GDPNow\ncd /d \"%~dp0..\"\nset PYTHONUTF8=1\n"
                "\"%~dp0..\\runtime\\python\\python.exe\" -W ignore::SyntaxWarning \"%~dp0..\\app\\gdpnow.py\" %*\n"
                "echo.\npause\n")
    shutil.copy2(os.path.join(HERE, "README.txt"), OUT)
    shutil.copy2(os.path.join(HERE, "LICENSES.txt"), os.path.join(OUT, "runtime"))
    privacy_check(OUT)

    if "--no-zip" in sys.argv:
        print(f"done (no zip): {OUT}")
        return
    print("zip")
    zpath = os.path.join(DIST, f"GDPNow-{VERSION}-windows-x64.zip")
    if os.path.exists(zpath):
        os.remove(zpath)
    skip = {"settings.json", "video_matches.txt", "app_error.log"}
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for dirpath, dirnames, files in os.walk(OUT):
            dirnames[:] = [d for d in dirnames if d not in ("__pycache__", "error reports")]
            for name in files:
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, OUT)
                if rel in skip or (name.endswith((".bak", ".tmp")) and rel.startswith("app")):
                    continue
                z.write(full, os.path.join("GDPNow", rel))  # always unzips as GDPNow\
    print(f"done: {zpath} ({os.path.getsize(zpath) / 1e6:.0f} MB)")


if __name__ == "__main__":
    build()
