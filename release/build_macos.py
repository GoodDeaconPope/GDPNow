"""Build GDPNow.app (macOS) and its zip. Runs ON a Mac (the GitHub Actions
workflow .github/workflows/macos.yml runs it for Apple Silicon and Intel).

    python3 release/build_macos.py --version 1.0.4 [--arch arm64|x86_64]
        -> build/mac/GDPNow.app + build/mac/GDPNow-<version>-macos-<arch>.zip

The app is the same code as the Windows release (gdpnow.py, gdpnow_support.py,
gdpnow_app.py, ui/, systemFileMap.json, plus mac_keys.lua) inside
GDPNow.app/Contents/Resources/app, next to a bundled runtime:
  python   python-build-standalone CPython 3.12 + requirements.txt from PyPI
  mpv      the mpv project's own macOS build (mpv.app)
  ffmpeg   Martin Riedl's static macOS build (libx264, VideoToolbox, drawtext)
Every download is checked against the SHA-256 below. The bundle is signed
ad hoc (no Apple Developer ID), so macOS asks once before opening it.
"""
import datetime
import hashlib
import os
import platform
import plistlib
import py_compile
import shutil
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
BUILD = os.path.join(REPO, "build", "mac")
DL = os.path.join(REPO, "build", "downloads-mac")
VERSION = sys.argv[sys.argv.index("--version") + 1] if "--version" in sys.argv else "1.0.0"
ARCH = sys.argv[sys.argv.index("--arch") + 1] if "--arch" in sys.argv else platform.machine()
APP_FILES = ("gdpnow.py", "gdpnow_support.py", "gdpnow_app.py", "mac_keys.lua", "systemFileMap.json")

PBS = "https://github.com/astral-sh/python-build-standalone/releases/download/20261021/"
DOWNLOADS = {
    "arm64": {
        "python": (PBS + "cpython-3.12.15+20261021-aarch64-apple-darwin-install_only_stripped.tar.gz",
                   "7b8d00b751db21cea87dc551a8f8165147c5fbc5251d5537d7e5497dde00a219"),
        # built on macOS 14: runs on macOS 14 (Sonoma) and newer
        "mpv": ("https://github.com/mpv-player/mpv/releases/download/v0.41.0/mpv-v0.41.0-macos-14-arm.zip",
                "5c96f9b21355fc0a11d2e2161ad65f33031070e9fb3f6bd9865fb459b94587e6"),
        "ffmpeg": ("https://ffmpeg.martin-riedl.de/download/macos/arm64/1789931890_9.0.2/ffmpeg.zip",
                   "c8ed4c4e6978a03c485edbfe4e0a5dc2380f8a30bba5150531b31b094492d924"),
        "min_macos": "14.0",
    },
    "x86_64": {
        "python": (PBS + "cpython-3.12.15+20261021-x86_64-apple-darwin-install_only_stripped.tar.gz",
                   "f12e0504397683d6be0745ffd4c39ef45a455cec169adf35c628840d06133773"),
        # mpv's only Intel build is made on macOS 15
        "mpv": ("https://github.com/mpv-player/mpv/releases/download/v0.41.0/mpv-v0.41.0-macos-15-intel.zip",
                "41003617ab4f7784394b5ddea7ce51b3e0838e8cfc8166ad1a378b2eda3b583c"),
        "ffmpeg": ("https://ffmpeg.martin-riedl.de/download/macos/amd64/1789931006_9.0.2/ffmpeg.zip",
                   "7c6b4125b191cbf773832dc51f424cf2b6bb7da43007d1e066f95909e47cacd4"),
        "min_macos": "15.0",
    },
}

LAUNCHER = """#!/bin/bash
# GDPNow: open the app window with the bundled Python
RES="$(cd "$(dirname "$0")/../Resources" && pwd)"
export PYTHONUTF8=1
exec "$RES/runtime/python/bin/python3" -W ignore::SyntaxWarning "$RES/app/gdpnow_app.py" "$@"
"""


def run(cmd, **kw):
    print("  $", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, **kw)


def fetch(url, sha256):
    os.makedirs(DL, exist_ok=True)
    path = os.path.join(DL, f"{sha256[:12]}-{url.rsplit('/', 1)[1]}")
    if not os.path.exists(path):
        print(f"  download {url}", flush=True)
        req = urllib.request.Request(url, headers={"User-Agent": "GDPNow-build"})
        with urllib.request.urlopen(req) as r, open(path + ".part", "wb") as f:
            shutil.copyfileobj(r, f)
        os.replace(path + ".part", path)
    got = hashlib.sha256(open(path, "rb").read()).hexdigest()
    if got != sha256:
        os.remove(path)
        raise SystemExit(f"checksum mismatch for {url}: {got}")
    return path


def make_icns(res):
    """GDPNow.icns from ui/logo.png (256 px)."""
    iconset = os.path.join(BUILD, "GDPNow.iconset")
    shutil.rmtree(iconset, ignore_errors=True)
    os.makedirs(iconset)
    logo = os.path.join(REPO, "ui", "logo.png")
    for size, name in ((16, "16x16"), (32, "16x16@2x"), (32, "32x32"), (64, "32x32@2x"),
                       (128, "128x128"), (256, "128x128@2x"), (256, "256x256")):
        run(["sips", "-z", str(size), str(size), logo, "--out", os.path.join(iconset, f"icon_{name}.png")],
            stdout=subprocess.DEVNULL)
    run(["iconutil", "-c", "icns", iconset, "-o", os.path.join(res, "GDPNow.icns")])
    shutil.rmtree(iconset)


def build():
    if sys.platform != "darwin":
        raise SystemExit("build_macos.py runs on macOS (see .github/workflows/macos.yml)")
    dl = DOWNLOADS[ARCH]
    app_bundle = os.path.join(BUILD, "GDPNow.app")
    shutil.rmtree(app_bundle, ignore_errors=True)
    contents = os.path.join(app_bundle, "Contents")
    res = os.path.join(contents, "Resources")
    app = os.path.join(res, "app")
    runtime = os.path.join(res, "runtime")
    for d in (os.path.join(contents, "MacOS"), app, os.path.join(runtime, "mpv"), os.path.join(runtime, "ffmpeg")):
        os.makedirs(d)

    print("app: gdpnow.py, support module, app window, ui, map", flush=True)
    for name in APP_FILES:
        shutil.copy2(os.path.join(REPO, name), app)
    shutil.copytree(os.path.join(REPO, "ui"), os.path.join(app, "ui"),
                    ignore=shutil.ignore_patterns("*.ico", "*.manifest"))  # Windows-only files
    for name in ("gdpnow.py", "gdpnow_support.py", "gdpnow_app.py"):
        py_compile.compile(os.path.join(app, name), doraise=True, cfile=os.path.join(BUILD, "_pycheck.pyc"))
    sha = lambda p: hashlib.sha256(open(p, "rb").read()).hexdigest()[:10]
    commit = subprocess.run(["git", "-C", REPO, "rev-parse", "--short", "HEAD"],
                            capture_output=True, text=True).stdout.strip() or "?"
    with open(os.path.join(app, "VERSION.txt"), "w", encoding="utf-8") as f:
        f.write(f"GDPNow {VERSION} (macOS {ARCH}), build {datetime.datetime.now():%Y-%m-%d %H:%M}, commit {commit}, "
                f"gdpnow.py {sha(os.path.join(REPO, 'gdpnow.py'))}, map {sha(os.path.join(REPO, 'systemFileMap.json'))}\n")

    print("runtime: Python 3.12 + libraries", flush=True)
    run(["tar", "-xzf", fetch(*dl["python"]), "-C", runtime])  # -> runtime/python
    py = os.path.join(runtime, "python", "bin", "python3")
    run([py, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--no-compile",
         "-r", os.path.join(REPO, "requirements.txt")])
    freeze = subprocess.run([py, "-m", "pip", "freeze"], capture_output=True, text=True).stdout
    print(freeze, flush=True)

    print("runtime: mpv", flush=True)
    tmp = os.path.join(BUILD, "_mpv")
    shutil.rmtree(tmp, ignore_errors=True)
    run(["ditto", "-x", "-k", fetch(*dl["mpv"]), tmp])
    # the zip holds mpv.tar.gz (keeps mpv.app's symlinks and permissions)
    for name in os.listdir(tmp):
        if name.endswith((".tar.gz", ".tgz")):
            run(["tar", "-xzf", os.path.join(tmp, name), "-C", tmp])
    found = [os.path.join(r, d) for r, ds, _ in os.walk(tmp) for d in ds if d == "mpv.app"]
    if not found:
        raise SystemExit("mpv.app not found in the mpv download")
    run(["ditto", found[0], os.path.join(runtime, "mpv", "mpv.app")])
    shutil.rmtree(tmp)

    print("runtime: ffmpeg", flush=True)
    run(["ditto", "-x", "-k", fetch(*dl["ffmpeg"]), os.path.join(runtime, "ffmpeg")])
    os.chmod(os.path.join(runtime, "ffmpeg", "ffmpeg"), 0o755)

    print("bundle: launcher, Info.plist, icon, signature", flush=True)
    launcher = os.path.join(contents, "MacOS", "GDPNow")
    with open(launcher, "w", encoding="utf-8", newline="\n") as f:
        f.write(LAUNCHER)
    os.chmod(launcher, 0o755)
    make_icns(res)
    short = VERSION.split("-")[0]
    with open(os.path.join(contents, "Info.plist"), "wb") as f:
        plistlib.dump({
            "CFBundleName": "GDPNow", "CFBundleDisplayName": "GDPNow",
            "CFBundleIdentifier": "io.github.gooddeaconpope.gdpnow",
            "CFBundleExecutable": "GDPNow", "CFBundleIconFile": "GDPNow",
            "CFBundlePackageType": "APPL", "CFBundleShortVersionString": short, "CFBundleVersion": short,
            "LSMinimumSystemVersion": dl["min_macos"], "NSHighResolutionCapable": True,
            "LSApplicationCategoryType": "public.app-category.entertainment",
        }, f)
    run(["codesign", "--force", "--deep", "--sign", "-", app_bundle])

    zip_path = os.path.join(BUILD, f"GDPNow-{VERSION}-macos-{ARCH}.zip")
    if os.path.exists(zip_path):
        os.remove(zip_path)
    run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", app_bundle, zip_path])
    print(f"done: {zip_path} ({os.path.getsize(zip_path) // 2**20} MB)", flush=True)


if __name__ == "__main__":
    build()
