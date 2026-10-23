"""GDPNow app window.

An HTML interface (ui/index.html) shown in an Edge WebView2 window via
pywebview. It replaces the typed questions: the window collects the
answers, starts gdpnow.py as a child process with --answers, streams its
log, and stops it through a stop file (same exit path as pressing Q).
It also hosts the systemFileMap editor.
"""
import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback

import gdpnow_support as ps

APP_TITLE = "GDPNow"
UI_INDEX = os.path.join(ps.APP_DIR, "ui", "index.html")
ICON_FILE = os.path.join(ps.APP_DIR, "ui", "gdpnow.ico")
PLAYER = os.path.join(ps.APP_DIR, "gdpnow.py")
SCENE_TYPES = ["PC", "RC", "PM", "D", "M", "CSP", "BJ"]  # the player's master types
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_window = None  # kept off the Api object: pywebview exposes Api attributes to JS


def _mmss(sec):
    sec = max(0, int(sec))
    return f"{sec // 60:02d}:{sec % 60:02d}"


class Session:
    """One running player child process and its log."""

    def __init__(self, answers, first_episodes=()):
        self.lines = []
        self.lock = threading.Lock()
        self.session_dir = None
        self.error_report = None
        self.exit_code = None
        self.started = time.time()
        self.answers = answers
        tmp = tempfile.mkdtemp(prefix="gdpnow_")
        self.stop_file = os.path.join(tmp, "stop")
        answers = dict(answers, stop_file=self.stop_file)
        self.answers_file = os.path.join(tmp, "answers.json")
        with open(self.answers_file, "w", encoding="utf-8") as f:
            json.dump(answers, f)
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        self.proc = subprocess.Popen(
            # first_episodes work like `gdpnow 257 330`: those play first
            [ps.PYTHON_EXE, "-u", "-W", "ignore::SyntaxWarning", PLAYER, "--answers", self.answers_file]
            + list(first_episodes),
            cwd=ps.ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, env=env, creationflags=NO_WINDOW)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for raw in iter(self.proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line.startswith("[GDPNow] SESSION_DIR="):
                self.session_dir = line.split("=", 1)[1]
            if line.startswith("[GDPNow] ERROR_REPORT="):
                self.error_report = line.split("=", 1)[1]
            with self.lock:
                self.lines.append(line)
        self.exit_code = self.proc.wait()
        shutil.rmtree(os.path.dirname(self.answers_file), ignore_errors=True)

    @property
    def running(self):
        return self.exit_code is None

    def stop(self):
        if self.running:
            try:
                open(self.stop_file, "w").close()
            except OSError:
                pass


class Api:
    def __init__(self):
        self._session = None
        self._scan = None
        self._scan_lock = threading.Lock()
        self._scan_time = 0.0
        self._preview = None
        self._preview_pipe = rf"\\.\pipe\gdpnow_preview_{os.getpid()}"
        self._durations = {}

    # ------------------------------------------------------------ state
    def state(self):
        settings = ps.load_settings()
        try:
            from screeninfo import get_monitors
            mons = [{"index": i + 1, "x": m.x, "y": m.y, "w": m.width, "h": m.height,
                     "primary": bool(m.is_primary)} for i, m in enumerate(get_monitors())]
        except Exception:
            mons = []
        last = settings.get("last_answers") or {}
        return {
            "video_dir": settings.get("video_dir") if os.path.isdir(settings.get("video_dir") or "") else None,
            "map_file": ps.map_file(),
            "map_is_bundled": ps.map_file() == ps.DEFAULT_MAP_FILE,
            "monitors": mons,
            "types": SCENE_TYPES,
            "last": last,
            "running": bool(self._session and self._session.running),
            # open on a page / episode (used for screenshots and support)
            "start_view": os.environ.get("GDPNOW_START_VIEW"),
            "start_episode": os.environ.get("GDPNOW_START_EPISODE"),
        }

    def pick_video_dir(self):
        import webview
        start = ps.load_settings().get("video_dir") or os.path.expanduser("~")
        res = _window.create_file_dialog(webview.FileDialog.FOLDER, directory=start)
        if not res:
            return None
        path = res[0] if isinstance(res, (list, tuple)) else res
        s = ps.load_settings()
        s["video_dir"] = os.path.abspath(path)
        ps.save_settings(s)
        self._scan = None
        return s["video_dir"]

    def pick_map_file(self):
        import webview
        res = _window.create_file_dialog(webview.FileDialog.OPEN,
                                         file_types=("Map files (*.json)", "All files (*.*)"))
        if not res:
            return None
        path = res[0] if isinstance(res, (list, tuple)) else res
        try:
            ps.load_map(path)
        except (OSError, ValueError) as e:  # the user's pick, not a bug
            return {"error": f"That file is not a GDPNow map: {e}"}
        s = ps.load_settings()
        s["map_file"] = os.path.abspath(path)
        ps.save_settings(s)
        self._scan = None
        return s["map_file"]

    def use_bundled_map(self):
        s = ps.load_settings()
        s.pop("map_file", None)
        ps.save_settings(s)
        self._scan = None
        return ps.DEFAULT_MAP_FILE

    def scan(self, force=False):
        """Match the video folder against the map; counts for the UI.
        One scan at a time: a second caller waits and reuses the result."""
        asked = time.time()
        with self._scan_lock:
            if self._scan and (not force or self._scan_time >= asked):
                return self._scan
            self._scan = self._do_scan()
            self._scan_time = time.time()
            return self._scan

    def _do_scan(self):
        video_dir = ps.load_settings().get("video_dir")
        try:
            system_map = ps.load_map()
        except Exception as e:
            return {"error": f"Cannot read the map file: {e}"}
        out = {"map_total": len(system_map), "matched": 0, "not_in_map": 0, "no_number": 0,
               "type_counts": {t: 0 for t in SCENE_TYPES}, "files": {},
               "subfolders": ps.include_subfolders()}
        if video_dir and os.path.isdir(video_dir):
            chosen, dupes, unmatched = ps.match_videos(video_dir, list(system_map))
            missing = [k for k in system_map if k not in chosen]
            ps._write_report(video_dir, chosen, dupes, unmatched, missing)
            not_in_map, no_number = ps.split_unmatched(unmatched)
            out["matched"] = len(chosen)
            out["not_in_map"] = len(not_in_map)
            out["no_number"] = len(no_number)
            out["orphans"] = [{"label": g, "path": p} for p, g in sorted(not_in_map, key=lambda x: x[1])]
            out["dupes"] = len(dupes)
            out["files"] = chosen
            episodes = []
            for key in chosen:
                meta = system_map.get(key) or {}
                types = [t for t in SCENE_TYPES if t in meta]
                for t in types:
                    out["type_counts"][t] += 1
                episodes.append({"label": os.path.splitext(key)[0], "types": types})
            out["episodes"] = episodes  # what can be picked to play first
        return out

    def set_subfolders(self, on):
        s = ps.load_settings()
        s["include_subfolders"] = bool(on)
        ps.save_settings(s)
        self._scan = None
        return bool(on)

    def open_report(self):
        if os.path.exists(ps.REPORT_FILE):
            os.startfile(ps.REPORT_FILE)

    # ---------------------------------------------------------- session
    def start(self, answers):
        if self._session and self._session.running:
            return {"error": "A session is already running."}
        clean = {
            "monitor": int(answers.get("monitor") or 1),
            "master_type": str(answers.get("master_type") or "RC"),
            "cols": int(answers.get("cols") or 1),
            "rows": int(answers.get("rows") or 1),
            "cycle": int(answers.get("cycle") or 1),
            "interval": int(answers.get("interval") or 15),
            "minutes": int(answers.get("minutes") or 5),
        }
        s = ps.load_settings()
        s["last_answers"] = clean
        ps.save_settings(s)
        # episodes to play first (like `gdpnow 257`): plain episode ids only
        first = [str(e).strip() for e in (answers.get("episodes") or [])
                 if re.fullmatch(r"[A-Za-z]?\d{1,4}\+?", str(e).strip())]
        self._session = Session(clean, first)
        return {"ok": True}

    def poll(self, cursor=0):
        sess = self._session
        if not sess:
            return {"lines": [], "cursor": 0, "running": False}
        with sess.lock:
            lines = sess.lines[cursor:]
            n = len(sess.lines)
        return {"lines": lines, "cursor": n, "running": sess.running,
                "exit_code": sess.exit_code, "session_dir": sess.session_dir,
                "error_report": sess.error_report, "elapsed": time.time() - sess.started}

    # ------------------------------------------------------ error reports
    def _report_dict(self, path, full):
        # the panel shows `short` in a text box for the user to copy; the clipboard is never touched
        return {"path": path, "full": full, "short": ps.short_report(full), "support": ps.SUPPORT_LINE}

    def open_error_report(self, path):
        """A report the player saved (it printed ERROR_REPORT=...)."""
        with open(path, encoding="utf-8") as f:
            return self._report_dict(path, f.read().rstrip("\n"))

    def player_crash_report(self):
        """The player died without writing a report (e.g. Python itself failed)."""
        sess = self._session
        with sess.lock:
            log = "\n".join(sess.lines[-150:])
        details = log[log.rfind("Traceback (most recent call last)"):] if "Traceback" in log else \
            f"The player exited with code {sess.exit_code} before saving anything."
        full, _ = ps.build_report("player (started from the app window)", details, log)
        return self._report_dict(ps.save_report(full), full)

    def page_error_report(self, details):
        """An unexpected error inside the app window's page (JavaScript or an app call)."""
        full, _ = ps.build_report("app window", str(details))
        return self._report_dict(ps.save_report(full), full)


    def stop(self):
        if self._session:
            self._session.stop()
        return True

    def open_path(self, path):
        if path and os.path.exists(path):
            os.startfile(path)

    def make_video(self, session_dir):
        bat = os.path.join(session_dir or "", "run_grwm.bat")
        if os.path.isfile(bat):
            os.startfile(bat)
            return True
        return False

    # ------------------------------------------------------- map editor
    def map_data(self):
        system_map = ps.load_map()
        scan = self._scan or self.scan()
        files = scan.get("files", {})
        return {
            "file": ps.map_file(),
            "is_bundled": ps.map_file() == ps.DEFAULT_MAP_FILE,
            "episodes": [{"key": k, "meta": v, "file": files.get(k)} for k, v in system_map.items()],
            "orphans": scan.get("orphans", []),
        }

    def save_map(self, episodes):
        path = ps.map_file()
        data = {}
        for ep in episodes:
            key = str(ep["key"]).strip()
            if not key:
                continue
            data[key] = ep.get("meta") or {}
        backup = path + ".bak"
        if os.path.exists(path):
            shutil.copy2(path, backup)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(data, indent=4, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
        self._scan = None
        return {"ok": True, "count": len(data), "backup": backup}

    def normalize_key(self, text):
        """'75' -> '075.mp4', 'd6' -> 'D06.mp4', '400+' -> '400+.mp4'."""
        t = os.path.splitext(str(text).strip())[0].strip()
        if re.fullmatch(r"\d{1,3}", t):
            return f"{int(t):03d}.mp4"
        m = re.fullmatch(r"[dD]\s*0*(\d{1,2})", t)
        if m:
            return f"D{int(m.group(1)):02d}.mp4"
        return f"{t}.mp4" if t else None

    def file_duration(self, path):
        if not path:
            return None
        if path in self._durations:
            return self._durations[path]
        try:
            r = subprocess.run([ps.FFMPEG_EXE, "-hide_banner", "-i", path],
                               capture_output=True, timeout=20, creationflags=NO_WINDOW)
            m = re.search(rb"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr)
            dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else None
        except Exception:
            dur = None
        self._durations[path] = dur
        return dur

    def preview(self, path, start_sec=0):
        """Open the episode in a normal mpv window (keyboard + mouse work),
        with the time always on screen, so map times can be read off it."""
        if not path or not os.path.isfile(path):
            return False
        start = max(0, int(start_sec or 0))
        if self._preview and self._preview.poll() is None:
            if self._ipc({"command": {"name": "loadfile", "url": path, "flags": "replace",
                                      "options": f"start={start}"}}) is not None:
                return True
        self._preview = subprocess.Popen(
            [ps.MPV_EXE, path, f"--start={start}", f"--input-ipc-server={self._preview_pipe}",
             "--force-window=yes", "--keep-open=yes", "--osd-level=3",
             "--osd-status-msg=${time-pos/full} / ${duration/full}",
             "--geometry=60%", "--title=GDPNow preview - ${filename}"],
            creationflags=NO_WINDOW)
        return True

    def preview_time(self):
        """Current playback time of the preview window, in seconds."""
        if not self._preview or self._preview.poll() is not None:
            return None
        reply = self._ipc({"command": ["get_property", "time-pos"], "request_id": 7})
        return reply.get("data") if reply else None

    def _ipc(self, obj, timeout=2.0):
        result = {}

        def talk():
            try:
                with open(self._preview_pipe, "r+b", buffering=0) as f:
                    f.write((json.dumps(obj) + "\n").encode())
                    buf = b""
                    while True:
                        ch = f.read(1)
                        if not ch:
                            return
                        if ch != b"\n":
                            buf += ch
                            continue
                        msg = json.loads(buf or b"{}")
                        buf = b""
                        if "error" in msg and "event" not in msg:
                            result["reply"] = msg
                            return
            except (OSError, ValueError):
                return

        t = threading.Thread(target=talk, daemon=True)
        t.start()
        t.join(timeout)
        return result.get("reply")

    # ------------------------------------------------------------- exit
    def set_map_dirty(self, dirty):
        self._map_dirty = bool(dirty)

    def force_close(self):
        self._map_dirty = False
        self._closing_ok = True
        threading.Thread(target=_window.destroy, daemon=True).start()

    def _on_closing(self):
        """Window X: ask in-app first when the map has unsaved edits."""
        if getattr(self, "_map_dirty", False) and not getattr(self, "_closing_ok", False):
            threading.Thread(target=lambda: _window.evaluate_js("window.showUnsavedOnClose()"),
                             daemon=True).start()
            return False
        self._shutdown()

    def _shutdown(self):
        if self._session and self._session.running:
            self._session.stop()
            for _ in range(100):
                if not self._session.running:
                    break
                time.sleep(0.1)
        if self._preview and self._preview.poll() is None:
            self._preview.terminate()


def _apply_window_style(hwnd, repaint=False):
    """Dark title bar + our icon. Best done before the window is first shown;
    afterwards Windows only repaints the title bar when nudged (repaint)."""
    try:
        import win32con
        import win32gui
        on = ctypes.c_int(1)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (new id, then pre-20H1 id)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(on), 4) == 0:
                break
        if os.path.exists(ICON_FILE):
            for size, which in ((256, win32con.ICON_BIG), (32, win32con.ICON_SMALL)):
                icon = win32gui.LoadImage(0, ICON_FILE, win32con.IMAGE_ICON, size, size,
                                          win32con.LR_LOADFROMFILE)
                win32gui.SendMessage(hwnd, win32con.WM_SETICON, which, icon)
        if repaint:  # make Windows redraw the frame with the new colors now
            win32gui.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                                  win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOZORDER
                                  | win32con.SWP_NOACTIVATE | win32con.SWP_FRAMECHANGED)
            active = win32gui.GetForegroundWindow() == hwnd
            win32gui.SendMessage(hwnd, win32con.WM_NCACTIVATE, int(not active), 0)
            win32gui.SendMessage(hwnd, win32con.WM_NCACTIVATE, int(active), 0)
    except Exception:
        pass


def _style_before_show(window):
    """Runs while the window exists but is not on screen yet: the title bar
    is dark from the very first frame."""
    try:
        _apply_window_style(int(window.native.Handle.ToInt64()))
    except Exception:
        pass


def _style_native_window():
    """Safety net after the window is shown (and the old path if before_show
    is unavailable): apply again and force the title bar to repaint."""
    try:
        import win32gui
        hwnd = 0
        for _ in range(50):
            hwnd = win32gui.FindWindow(None, APP_TITLE)
            if hwnd:
                break
            time.sleep(0.1)
        if hwnd:
            _apply_window_style(hwnd, repaint=True)
    except Exception:
        pass


WEBVIEW2_SETUP = os.path.join(ps.RUNTIME, "webview2", "MicrosoftEdgeWebview2Setup.exe")
CONSOLE_BAT = os.path.join(ps.ROOT, "extras", "Console mode (typed questions).bat")
_WV2_CLIENTS = ("{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",  # runtime
                "{2CD8A007-E189-409D-A2C8-9AF4EF3C72AA}",  # beta
                "{0D50BFEC-CD6A-4F9A-964C-C7416E3ACB10}",  # dev
                "{65C35B14-6C1D-4122-AC46-7148CC9D6497}")  # canary


def _dotnet_ok():
    """pythonnet (the bridge to the WebView2 control) needs .NET Framework 4.7.2+."""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full") as k:
            return winreg.QueryValueEx(k, "Release")[0] >= 461808
    except OSError:
        return False


def _webview2_version():
    """Installed Edge WebView2 version (same registry places pywebview checks), or None."""
    import winreg
    if os.environ.get("GDPNOW_FAKE_NO_WEBVIEW2"):
        return None
    for guid in _WV2_CLIENTS:
        for root, sub in ((winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\WOW6432Node\\Microsoft\\EdgeUpdate\\Clients\\"),
                          (winreg.HKEY_CURRENT_USER, "SOFTWARE\\Microsoft\\EdgeUpdate\\Clients\\"),
                          (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\Microsoft\\EdgeUpdate\\Clients\\")):
            try:
                with winreg.OpenKey(root, sub + guid) as k:
                    pv = str(winreg.QueryValueEx(k, "pv")[0])
                if pv and pv != "0.0.0.0" and int(pv.split(".")[0]) >= 86:
                    return pv
            except (OSError, ValueError):
                pass
    return None


def _ask(text, buttons):
    """Native Windows message box (works without WebView2). Returns the MessageBox id."""
    return ctypes.windll.user32.MessageBoxW(0, text, APP_TITLE, buttons | 0x40)  # MB_ICONINFORMATION


def _console_mode():
    if os.path.exists(CONSOLE_BAT):
        os.startfile(CONSOLE_BAT)


def _ensure_webview2():
    """The app window needs Edge WebView2 (in Windows 11 and updated Windows 10).
    Without it pywebview would fall back to Internet Explorer, which cannot run
    this interface, so ask instead: install it, or use the text version."""
    if _webview2_version():
        return True
    if not _dotnet_ok():
        _ask("This copy of Windows is missing updates the GDPNow window needs "
             "(.NET Framework 4.7.2). Running Windows Update fixes it.\n\n"
             "The text version of GDPNow will open now. It plays the same way; "
             "you type your choices instead of clicking.", 0x0)
        _console_mode()
        return False
    choice = _ask("GDPNow's window needs Microsoft Edge WebView2, a free part of Windows "
                  "that this PC does not have yet.\n\n"
                  "Yes: install it now (needs internet, about a minute).\n"
                  "No: use the text version of GDPNow instead (it plays the same way; "
                  "you type your choices).", 0x3)  # MB_YESNOCANCEL
    if choice == 7:  # No
        _console_mode()
        return False
    if choice != 6:  # Cancel / closed
        return False
    if os.path.exists(WEBVIEW2_SETUP):
        try:
            subprocess.run([WEBVIEW2_SETUP, "/install"], timeout=900)
        except Exception:
            pass
    if _webview2_version():
        return True
    _ask("WebView2 could not be installed (is the internet connected?).\n\n"
         "The text version of GDPNow will open now. You can try the window again "
         "later, or install \"Microsoft Edge WebView2 Runtime\" from microsoft.com.", 0x0)
    _console_mode()
    return False


def _unblock_downloaded_dlls():
    """Extract All on a downloaded zip marks every file as from the internet
    (a Zone.Identifier stream), and .NET then refuses to load the window's
    DLLs: "Failed to resolve Python.Runtime.Loader.Initialize". Drop the mark
    from the bundled DLLs (23 files, ~0.01 s)."""
    if not ps.PORTABLE:
        return
    for root, _, files in os.walk(os.path.join(ps.RUNTIME, "python")):
        for f in files:
            if f.lower().endswith(".dll"):
                try:
                    os.remove(os.path.join(root, f) + ":Zone.Identifier")
                except OSError:
                    pass  # not marked, or a read-only folder


def main():
    global _window
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("GDPNow.App")
    except Exception:
        pass
    if not _ensure_webview2():
        return
    if os.environ.get("GDPNOW_TEST_CRASH") == "app":
        raise RuntimeError("test crash requested with GDPNOW_TEST_CRASH=app")
    _unblock_downloaded_dlls()
    import webview
    webview.settings["SHOW_DEFAULT_MENUS"] = False  # no browser right-click menu
    if os.environ.get("GDPNOW_DEBUG_PORT"):
        webview.settings["REMOTE_DEBUGGING_PORT"] = int(os.environ["GDPNOW_DEBUG_PORT"])
    api = Api()
    _window = webview.create_window(APP_TITLE, url=UI_INDEX, js_api=api, width=1240, height=860,
                                    min_size=(1000, 700), background_color="#0B0C10",
                                    text_select=False)
    _window.events.closing += api._on_closing
    _window.events.before_show += _style_before_show
    _window.events.shown += lambda: threading.Thread(target=_style_native_window, daemon=True).start()
    # never let pywebview silently fall back to Internet Explorer (MSHTML)
    webview.start(gui="edgechromium", debug="--debug" in sys.argv)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        details = traceback.format_exc()
        try:
            ps.report_error_native("app window", details)
        except Exception:  # last resort: the report itself failed
            ctypes.windll.user32.MessageBoxW(
                0, f"GDPNow could not start. {ps.SUPPORT_LINE} and send this:\n\n{details[-1500:]}",
                APP_TITLE, 0x10)
