"""Support code for gdpnow.py and the GDPNow app window.

Works in two places:
  - the repo: mpv / ffmpeg / Python from PATH; the video folder and the map
    to use come from settings (the map next to gdpnow.py is the release map);
  - the release zip (GDPNow\\app\\ next to GDPNow\\runtime\\): bundled mpv,
    ffmpeg and Python, settings and reports next to the launcher.
It holds: tool paths, the video folder setting, matching any file name to
the numbered entries of systemFileMap.json, the screenshot folder, the GRWM
encoder choice (NVIDIA NVENC when it works, CPU libx264 otherwise) and the
'DM the pope on Discord' error reports.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading

APP_DIR = os.path.dirname(os.path.abspath(__file__))
# release layout: GDPNow\app\ (this file) next to GDPNow\runtime\
PORTABLE = os.path.isdir(os.path.join(os.path.dirname(APP_DIR), "runtime"))
ROOT = os.path.dirname(APP_DIR) if PORTABLE else APP_DIR
RUNTIME = os.path.join(ROOT, "runtime")


def _tool(bundled, name):
    if PORTABLE and os.path.isfile(bundled):
        return bundled
    # ask for name.exe: Python's PATH search also looks in the current folder,
    # where an unrelated file called just "mpv" must not win
    return (shutil.which(name + ".exe") or shutil.which(name + ".com")
            or (bundled if os.path.isfile(bundled) else name))


MPV_EXE = _tool(os.path.join(RUNTIME, "mpv", "mpv.exe"), "mpv")
FFMPEG_EXE = _tool(os.path.join(RUNTIME, "ffmpeg", "ffmpeg.exe"), "ffmpeg")
if PORTABLE:
    PYTHON_EXE = os.path.join(RUNTIME, "python", "python.exe")
else:  # the Python running us; python.exe (console), not pythonw.exe
    _py = os.path.join(os.path.dirname(sys.executable), "python.exe")
    PYTHON_EXE = _py if os.path.isfile(_py) else sys.executable
DEFAULT_MAP_FILE = os.path.join(APP_DIR, "systemFileMap.json")
# settings, the matching report and error reports: next to the launcher in the
# release; in the user's AppData when running from the repo (keeps it clean)
STATE_DIR = ROOT if PORTABLE else os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "GDPNow")
REPORT_FILE = os.path.join(STATE_DIR, "video_matches.txt")

VIDEO_EXTS = {".mp4", ".mkv", ".m4v", ".mov", ".avi", ".wmv", ".webm",
              ".ts", ".m2ts", ".flv", ".mpg", ".mpeg"}

_WINDIR = os.environ.get("WINDIR") or os.environ.get("SystemRoot") or r"C:\Windows"
FONT_CANDIDATES = (os.path.join(_WINDIR, "Fonts", "arialbd.ttf"),
                   os.path.join(_WINDIR, "Fonts", "arial.ttf"))
# ffmpeg filter syntax: forward slashes, drive colon escaped ("C\:/Windows/...")
FFMPEG_FONT = FONT_CANDIDATES[0].replace("\\", "/").replace(":", "\\:", 1)

_CHOOSE_FLAGS = ("--choose-folder", "--change-folder")
_choose_requested = any(a.lower() in _CHOOSE_FLAGS for a in sys.argv[1:])
sys.argv[1:] = [a for a in sys.argv[1:] if a.lower() not in _CHOOSE_FLAGS]

# --answers <file.json>: the GUI launcher fills in the console questions
_answers = None
if "--answers" in sys.argv[1:]:
    _i = sys.argv.index("--answers")
    try:
        with open(sys.argv[_i + 1], "r", encoding="utf-8") as _f:
            _answers = json.load(_f)
    except (IndexError, OSError, ValueError) as _e:
        print(f"Could not read answers file: {_e}")
        _answers = {}
    del sys.argv[_i:_i + 2]

# prompt text (lower case) -> answers key; first match wins
_PROMPTS = (
    ("select monitor number", "monitor"),
    ("master type", "master_type"),
    ("(columns)", "cols"),
    ("(rows)", "rows"),
    ("to cycle per window", "cycle"),
    ("interval per video switch", "interval"),
    ("total session duration", "minutes"),
)
_asked = {}


def _answering_input(prompt=""):
    """builtins.input replacement used when the GUI launched us."""
    low = str(prompt).lower()
    key = next((k for text, k in _PROMPTS if text in low), None)
    _asked[low] = _asked.get(low, 0) + 1
    if _asked[low] > 3:  # the script rejected the answer and keeps asking
        raise EOFError(f"no valid answer for: {prompt.strip()}")
    value = "" if key is None else str(_answers.get(key, ""))
    print(f"{prompt}{value}", flush=True)
    return value


if _answers is not None:
    import builtins
    builtins.input = _answering_input

_labels = {}          # full path -> label shown on screen ("330")
_matched_map = None   # {full path: metadata}, built by startup()


# ---------------------------------------------------------------- settings
_settings_file = None
_settings_lock = threading.Lock()


def _settings_path():
    """settings.json in STATE_DIR; in the release, %LOCALAPPDATA%\\GDPNow if
    the folder is read-only (e.g. unzipped under Program Files). Decided once."""
    global _settings_file, STATE_DIR, REPORT_FILE, REPORTS_DIR
    with _settings_lock:
        if _settings_file is None:
            try:
                os.makedirs(STATE_DIR, exist_ok=True)
                probe = os.path.join(STATE_DIR, f".write_test_{os.getpid()}")
                with open(probe, "w") as f:
                    f.write("ok")
                os.remove(probe)
            except OSError:
                base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
                STATE_DIR = os.path.join(base, "GDPNow")
                os.makedirs(STATE_DIR, exist_ok=True)
                REPORT_FILE = os.path.join(STATE_DIR, "video_matches.txt")
                REPORTS_DIR = os.path.join(STATE_DIR, "error reports")
            _settings_file = os.path.join(STATE_DIR, "settings.json")
        return _settings_file


def load_settings():
    try:
        with open(_settings_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(data):
    path = _settings_path()
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)  # readers never see a half-written file


# ------------------------------------------------------------ folder picker
def _pick_folder_dialog(title):
    """Windows folder picker (the modern Explorer-style one). Returns a
    path or None if cancelled / unavailable."""
    try:
        return _modern_folder_dialog(title)
    except Exception as e:
        print(f"(Folder window unavailable: {e})")
    try:
        from win32com.shell import shell, shellcon
        pidl, _, _ = shell.SHBrowseForFolder(0, None, title, 0x0001 | 0x0040)
        return shell.SHGetPathFromIDList(pidl).decode("mbcs") if pidl else None
    except Exception:
        return None


def _modern_folder_dialog(title, start=None):
    """Windows' Explorer-style folder picker (IFileOpenDialog with
    FOS_PICKFOLDERS) through raw COM vtable calls: pywin32 does not wrap
    this interface. Returns the folder path, or None if cancelled."""
    import ctypes
    from ctypes import POINTER, byref, c_void_p, wintypes

    ole32 = ctypes.windll.ole32
    ole32.CoInitialize(None)

    class GUID(ctypes.Structure):
        _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD), ("c", wintypes.WORD),
                    ("d", ctypes.c_ubyte * 8)]

    def guid(text):
        g = GUID()
        ole32.CLSIDFromString(ctypes.c_wchar_p(text), byref(g))
        return g

    def call(obj, index, restype, *args_types_values):
        types = [t for t, _ in args_types_values]
        vtable = ctypes.cast(obj, POINTER(POINTER(c_void_p))).contents
        fn = ctypes.WINFUNCTYPE(restype, c_void_p, *types)(vtable[index])
        return fn(obj, *[v for _, v in args_types_values])

    S_OK, CANCELLED = 0, ctypes.c_long(0x800704C7).value
    FOS_PICKFOLDERS, FOS_FORCEFILESYSTEM, FOS_PATHMUSTEXIST = 0x20, 0x40, 0x800
    SIGDN_FILESYSPATH = ctypes.c_long(0x80058000).value
    dlg = c_void_p()
    hr = ole32.CoCreateInstance(byref(guid("{DC1C5A9C-E88A-4dde-A5A1-60F82A20AEF7}")), None, 1,  # FileOpenDialog
                                byref(guid("{42f85136-db7e-439c-85f1-e4075d135fc8}")), byref(dlg))  # IFileDialog
    if hr != S_OK:
        raise OSError(f"CoCreateInstance failed 0x{hr & 0xFFFFFFFF:08X}")
    try:
        opts = wintypes.DWORD()
        call(dlg, 10, ctypes.c_long, (POINTER(wintypes.DWORD), byref(opts)))           # GetOptions
        call(dlg, 9, ctypes.c_long, (wintypes.DWORD,                                     # SetOptions
                                     opts.value | FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST))
        call(dlg, 17, ctypes.c_long, (wintypes.LPCWSTR, title))                          # SetTitle
        if start and os.path.isdir(start):
            item = c_void_p()
            if ctypes.windll.shell32.SHCreateItemFromParsingName(
                    ctypes.c_wchar_p(start), None,
                    byref(guid("{43826d1e-e718-42ee-bc55-a1e261c37bfe}")), byref(item)) == S_OK:  # IShellItem
                call(dlg, 12, ctypes.c_long, (c_void_p, item))                           # SetFolder
                call(item, 2, ctypes.c_ulong)                                            # Release
        hr = call(dlg, 3, ctypes.c_long, (wintypes.HWND, None))                          # Show
        if hr == CANCELLED:
            return None
        if hr != S_OK:
            raise OSError(f"Show failed 0x{hr & 0xFFFFFFFF:08X}")
        item = c_void_p()
        if call(dlg, 20, ctypes.c_long, (POINTER(c_void_p), byref(item))) != S_OK:      # GetResult
            return None
        name = ctypes.c_wchar_p()
        try:
            if call(item, 5, ctypes.c_long, (ctypes.c_long, SIGDN_FILESYSPATH),          # GetDisplayName
                    (POINTER(ctypes.c_wchar_p), byref(name))) != S_OK:
                return None
            return name.value
        finally:
            if name:
                ole32.CoTaskMemFree(name)
            call(item, 2, ctypes.c_ulong)
    finally:
        call(dlg, 2, ctypes.c_ulong)                                                     # Release


def choose_video_dir():
    print("\nPick the folder where your videos are kept (a window will open).")
    path = _pick_folder_dialog("GDPNow: pick the folder that holds your videos")
    while not path or not os.path.isdir(path):
        typed = input("No folder picked. Paste the folder path here and press Enter "
                      "(or just Enter to try the window again): ").strip().strip('"')
        path = typed if typed else _pick_folder_dialog("GDPNow: pick your video folder")
    path = os.path.abspath(path)
    settings = load_settings()
    settings["video_dir"] = path
    save_settings(settings)
    print(f"Saved. Video folder: {path}")
    return path


def map_file():
    """systemFileMap.json in use: the one picked in the app, else the bundled copy."""
    path = load_settings().get("map_file")
    return path if path and os.path.isfile(path) else DEFAULT_MAP_FILE


def load_map(path=None):
    with open(path or map_file(), "r", encoding="utf-8-sig") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("the map file is not a JSON object")
    return data


def get_video_dir():
    path = load_settings().get("video_dir")
    if _answers is not None and (not path or not os.path.isdir(path)):
        raise SystemExit(f"Video folder not found: {path!r}. Pick it in the GDPNow window.")
    if _choose_requested or not path or not os.path.isdir(path):
        if path and not _choose_requested:
            print(f"The saved video folder is missing: {path}")
        path = choose_video_dir()
    return path


# ------------------------------------------------------- name -> map entry
_EPISODE_MARKER = re.compile(
    r"(?:^|[^a-z0-9])(?:e|ep|eps|episode|no|n°|#)[\s._\-#.]*0*(\d{1,3})(?!\d)", re.I)
_D_KEY = re.compile(r"(?:^|[^a-z0-9])d[\s._\-]*0*(\d{1,2})(?!\d)", re.I)
# standalone numbers only: not "x264", "5.1", "18yo", "1080p"
_BARE_NUMBER = re.compile(r"(?<![A-Za-z0-9.])(\d{1,3})(?![A-Za-z0-9])")


def _map_indexes(map_keys):
    exact, by_num, by_d = {}, {}, {}
    for key in map_keys:
        stem = os.path.splitext(key)[0]
        exact[stem.lower()] = key
        if stem.isdigit():
            by_num[int(stem)] = key
        m = re.fullmatch(r"[dD](\d+)", stem)
        if m:
            by_d[int(m.group(1))] = key
    return exact, by_num, by_d


def match_name(stem, indexes):
    """Map one file name (without extension) to a map key, or None.

    Order: exact name ("330", "D01", "400+") -> pure number with any zero
    padding ("75", "075") -> episode marker ("E330", "Ep 75", "#330") ->
    "D7"/"D07" style -> the only 1-3 digit number in the name that is a
    known episode. Names with several candidate numbers stay unmatched
    rather than guessed."""
    exact, by_num, by_d = indexes
    s = stem.strip()
    if s.lower() in exact:
        return exact[s.lower()]
    if s.isdigit():
        return by_num.get(int(s))
    m = _EPISODE_MARKER.search(s)
    if m and int(m.group(1)) in by_num:
        return by_num[int(m.group(1))]
    m = _D_KEY.search(s)
    if m and int(m.group(1)) in by_d:
        return by_d[int(m.group(1))]
    nums = {int(n) for n in _BARE_NUMBER.findall(s)}
    hits = [by_num[n] for n in nums if n in by_num]
    if len(hits) == 1:
        return hits[0]
    return None


def guess_episode(stem):
    """The episode number a file name seems to carry, map or no map
    ("277" for "E277", "D07" for "d7"), or None."""
    s = stem.strip()
    if s.isdigit() and len(s) <= 4:
        return f"{int(s):03d}"
    m = _EPISODE_MARKER.search(s)
    if m:
        return f"{int(m.group(1)):03d}"
    m = _D_KEY.search(s)
    if m:
        return f"D{int(m.group(1)):02d}"
    nums = set(_BARE_NUMBER.findall(s))
    return f"{int(nums.pop()):03d}" if len(nums) == 1 else None


def include_subfolders():
    return bool(load_settings().get("include_subfolders", True))


def scan_videos(video_dir, recursive=None):
    """Video files in video_dir, and in its subfolders unless turned off."""
    if recursive is None:
        recursive = include_subfolders()
    found = []
    for dirpath, dirnames, filenames in os.walk(video_dir):
        dirnames[:] = [d for d in dirnames if recursive and not d.startswith((".", "$"))]
        for name in filenames:
            if os.path.splitext(name)[1].lower() in VIDEO_EXTS:
                found.append(os.path.join(dirpath, name))
    return found


def match_videos(video_dir, map_keys):
    """Scan video_dir and pair files with map keys.
    Returns (chosen {key: path}, dupes {key: [paths]}, unmatched [paths]).
    When an episode is found more than once, the copy closest to the chosen
    folder wins (old copies usually sit in subfolders), then the largest."""
    indexes = _map_indexes(map_keys)
    chosen, dupes, unmatched = {}, {}, []

    def rank(p):
        try:
            size = os.path.getsize(p)
        except OSError:
            size = 0
        return (-p.count(os.sep), size)

    for path in scan_videos(video_dir):
        stem = os.path.splitext(os.path.basename(path))[0]
        key = match_name(stem, indexes)
        if key is None:
            unmatched.append(path)
            continue
        if key in chosen:
            dupes.setdefault(key, [chosen[key]]).append(path)
            if rank(path) > rank(chosen[key]):
                chosen[key] = path
        else:
            chosen[key] = path
    return chosen, dupes, unmatched


def split_unmatched(unmatched):
    """(files whose episode number is not in the map, files with no number)."""
    not_in_map, no_number = [], []
    for p in unmatched:
        g = guess_episode(os.path.splitext(os.path.basename(p))[0])
        (not_in_map if g else no_number).append((p, g))
    return not_in_map, [p for p, _ in no_number]


def build_matched_map(video_dir, system_file_map, write_report=True):
    chosen, dupes, unmatched = match_videos(video_dir, list(system_file_map))
    result = {}
    for key, meta in system_file_map.items():  # keep the map's order
        if key in chosen:
            result[chosen[key]] = meta
            _labels[chosen[key]] = os.path.splitext(key)[0]
    missing = [k for k in system_file_map if k not in chosen]
    if write_report:
        _write_report(video_dir, chosen, dupes, unmatched, missing)
    return result, len(unmatched), len(missing)


def _write_report(video_dir, chosen, dupes, unmatched, missing):
    lines = [f"GDPNow video matching report", f"Video folder: {video_dir}", "",
             f"MATCHED ({len(chosen)}): map entry -> your file"]
    for key in sorted(chosen, key=lambda k: (not k[0].isdigit(), k)):
        lines.append(f"  {os.path.splitext(key)[0]:>6}  ->  {chosen[key]}")
    if dupes:
        lines += ["", "SAME EPISODE FOUND MORE THAN ONCE (used: the one marked *, closest "
                  "to the video folder, then the largest):"]
        for key, paths in dupes.items():
            marked = [("* " if p == chosen[key] else "  ") + p for p in paths]
            lines.append(f"  {os.path.splitext(key)[0]}:")
            lines += [f"      {m}" for m in marked]
    not_in_map, no_number = split_unmatched(unmatched)
    lines += ["", f"EPISODE NOT IN THE MAP ({len(not_in_map)}): add them on the Episode map "
              "page (with their scene times) if they should play"]
    lines += [f"  {g:>6}  <-  {p}" for p, g in sorted(not_in_map, key=lambda x: x[1])]
    lines += ["", f"NO EPISODE NUMBER FOUND ({len(no_number)}): rename these to their "
              "episode number (for example 330.mp4 or 075.mp4) if they should play"]
    lines += [f"  {p}" for p in sorted(no_number)]
    lines += ["", f"MAP ENTRIES WITH NO FILE ({len(missing)}):",
              "  " + ", ".join(os.path.splitext(k)[0] for k in missing)]
    try:
        with open(REPORT_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError as e:
        print(f"(Could not write {REPORT_FILE}: {e})")


def label_for(path):
    """On-screen label for a video: its episode number, not the file name."""
    return _labels.get(path) or os.path.splitext(os.path.basename(path))[0]


def _episode_id(text):
    """'330', '0330', '330.mp4' -> '330'; 'd6', 'D06' -> 'd6'; else lower-case text."""
    t = os.path.splitext(str(text).strip())[0].lower()
    m = re.fullmatch(r"0*(\d+)(\+?)", t)
    if m:
        return (m.group(1) or "0") + m.group(2)
    m = re.fullmatch(r"d0*(\d+)", t)
    return f"d{m.group(1)}" if m else t


def find_arg_key(arg, keys):
    """The video a 'play this first' argument means (gdpnow 257, or episodes
    picked in the app): the episode with that number, else (old behaviour)
    the first file whose name contains the text."""
    want = _episode_id(arg)
    for key in keys:
        if _episode_id(label_for(key)) == want:
            return key
    for key in keys:
        if str(arg) in os.path.basename(key):
            return key
    return None


def arg_matches(arg, path):
    """Kept for compatibility: does this argument point at this video?"""
    return find_arg_key(arg, [path]) is not None


def load_matched_map():
    """Replacement for the original E:\\ map + folder lookup."""
    if _matched_map is None:
        startup()
    return dict(_matched_map or {})


# ---------------------------------------------------------------- startup
def startup():
    global _matched_map
    if _matched_map is not None:
        return
    print("=== GDPNow ===")
    for exe, name in ((MPV_EXE, "mpv"), (FFMPEG_EXE, "ffmpeg")):
        if not os.path.isfile(exe):
            print(f"Missing bundled program: {exe}\nRe-extract the whole zip and try again." if PORTABLE
                  else f"{name} was not found. Install it or add its folder to PATH.")
            if _answers is None:
                input("Press Enter to close.")
            sys.exit(1)
    try:
        system_file_map = load_map()
    except (OSError, ValueError) as e:
        print(f"Cannot read {map_file()}: {e}")
        if _answers is None:
            input("Press Enter to close.")
        sys.exit(1)
    while True:
        video_dir = get_video_dir()
        print(f"Looking for videos in: {video_dir}"
              + (" (subfolders too)..." if include_subfolders() else "..."))
        matched, n_unmatched, n_missing = build_matched_map(video_dir, system_file_map)
        print(f"Matched {len(matched)} of your files to {len(system_file_map)} map entries; "
              f"{n_unmatched} other video files are not in the map or have no episode number. "
              f"Details: {REPORT_FILE}")
        if matched:
            break
        if _answers is not None:
            raise SystemExit("No videos matched. Pick another folder in the GDPNow window.")
        print("\nNone of the videos in that folder could be matched to an episode number.")
        print("Pick another folder, or rename the files to their numbers (e.g. 330.mp4).")
        globals()["_choose_requested"] = True
    _matched_map = matched
    if _answers and _answers.get("stop_file"):
        threading.Thread(target=_watch_stop_file, args=(_answers["stop_file"],),
                         daemon=True, name="StopFileWatcher").start()


def _watch_stop_file(path):
    """The app's Stop button creates this file: same exit path as pressing Q
    (screenshots + GRWM script are saved, then everything closes)."""
    import time
    while not os.path.exists(path):
        time.sleep(0.2)
    try:
        os.remove(path)
    except OSError:
        pass
    print("\n[GDPNow] Stop requested from the app window.", flush=True)
    import __main__
    __main__.force_shutdown_and_exit()


# ---------------------------------------------------------- error reports
SUPPORT_LINE = "DM the pope on Discord"
REPORTS_DIR = os.path.join(STATE_DIR, "error reports")
DISCORD_LIMIT = 1900  # one Discord message is 2000 characters


class _Tee:
    """stdout/stderr wrapper that also keeps the last lines for an error report."""

    def __init__(self, stream, sink):
        self._stream, self._sink = stream, sink

    def write(self, s):
        self._sink.append(s)
        try:
            return self._stream.write(s)
        except Exception:
            return len(s)

    def __getattr__(self, name):
        return getattr(self._stream, name)


_output = []        # everything the player printed (trimmed to the tail)
_session_saved = False


def _output_tail(n=150):
    text = "".join(_output[-4000:])
    return "\n".join(text.splitlines()[-n:])


def _redact(text):
    """Hide the Windows user name (paths under the profile folder)."""
    home = os.path.expanduser("~")
    if home and len(home) > 3:
        text = re.sub(re.escape(home), "%USERPROFILE%", text, flags=re.I)
        text = re.sub(re.escape(home.replace("\\", "/")), "%USERPROFILE%", text, flags=re.I)
    return text


def _system_info():
    lines = []
    try:
        with open(os.path.join(APP_DIR, "VERSION.txt"), encoding="utf-8") as f:
            lines.append("GDPNow: " + f.read().strip())
    except OSError:
        lines.append("GDPNow: (no VERSION.txt)")
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as k:
            def val(n):
                try:
                    return winreg.QueryValueEx(k, n)[0]
                except OSError:
                    return "?"
            lines.append(f"Windows: {val('ProductName')} {val('DisplayVersion')} "
                         f"build {val('CurrentBuild')}.{val('UBR')}")
    except Exception:
        lines.append(f"Windows: {sys.getwindowsversion()}")
    lines.append(f"Python: {sys.version.split()[0]} {'64' if sys.maxsize > 2**32 else '32'}-bit")
    try:
        import gdpnow_app
        lines.append(f"WebView2: {gdpnow_app._webview2_version() or 'not found'}; "
                     f".NET 4.7.2+: {gdpnow_app._dotnet_ok()}")
    except Exception as e:
        lines.append(f"WebView2: (check failed: {e})")
    try:
        import win32api
        gpus, i = [], 0
        while True:
            try:
                d = win32api.EnumDisplayDevices(None, i)
            except Exception:
                break
            if d.DeviceString and d.DeviceString not in gpus:
                gpus.append(d.DeviceString)
            i += 1
        lines.append("Graphics: " + ("; ".join(gpus) or "?"))
    except Exception:
        pass
    try:
        from screeninfo import get_monitors
        lines.append("Screens: " + "; ".join(f"{m.width}x{m.height} at {m.x},{m.y}"
                                             + (" (main)" if m.is_primary else "")
                                             for m in get_monitors()))
    except Exception:
        pass
    lines.append(f"App folder: {ROOT} ({'release' if PORTABLE else 'repo'})")
    lines.append(f"mpv: {MPV_EXE if os.path.isfile(MPV_EXE) else 'NOT FOUND'}; "
                 f"ffmpeg: {FFMPEG_EXE if os.path.isfile(FFMPEG_EXE) else 'NOT FOUND'}")
    s = load_settings()
    lines.append(f"Video folder: {s.get('video_dir')} (subfolders: {s.get('include_subfolders', True)})")
    lines.append(f"Map file: {'bundled' if map_file() == DEFAULT_MAP_FILE else map_file()}")
    if s.get("last_answers"):
        lines.append(f"Last choices: {json.dumps(s['last_answers'])}")
    return lines


def build_report(where, details, log_text=""):
    """(full report, short version that fits in one Discord message)."""
    import datetime
    details = (details or "").strip()
    last = next((l.strip() for l in reversed(details.splitlines()) if l.strip()), "unknown error")
    head = [f"GDPNow error report ({datetime.datetime.now():%Y-%m-%d %H:%M:%S})",
            f"Where: {where}", f"Error: {last}"]
    try:
        info = _system_info()
    except Exception as e:
        info = [f"(system info failed: {e})"]
    full = "\n".join(head + info + ["", "--- details ---", details])
    if log_text.strip():
        full += "\n\n--- last lines of the log ---\n" + log_text.strip()
    full = _redact(full)
    return full, short_report(full)


def short_report(full):
    """The part of a full report that fits in one Discord message: the
    header lines and the end of the details (where the actual error is)."""
    top, _, rest = full.partition("\n\n--- details ---\n")
    details = rest.split("\n\n--- last lines of the log ---\n")[0].strip()
    head = "\n".join(top.splitlines()[:7])
    budget = max(200, DISCORD_LIMIT - len(head) - 12)
    tail = details[-budget:]
    if len(details) > budget:
        tail = "..." + tail[tail.find("\n") + 1:] if "\n" in tail else tail
    return f"{head}\n```\n{tail}\n```"


def save_report(full):
    import datetime
    os.makedirs(REPORTS_DIR, exist_ok=True)
    path = os.path.join(REPORTS_DIR, f"GDPNow error {datetime.datetime.now():%Y-%m-%d %H-%M-%S}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(full + "\n")
    return path


def report_error_native(where, details, log_text=""):
    """Error dialog for when the app window can't show one: save the full
    report, show the short one in a text box for the user to copy."""
    full, short = build_report(where, details, log_text)
    try:
        path = save_report(full)
    except Exception:
        path = None
    intro = (f"Something went wrong ({where}).\r\n"
             "Copy the text below (it is already selected: press Ctrl+C) and paste it into the chat."
             + (f"\r\nThe full report is also saved in the \"error reports\" folder as {os.path.basename(path)}."
                if path else ""))
    if not _error_dialog(intro, short):
        _error_fallback(where, short, path)
    return path


def _error_fallback(where, short, path):
    """Last resort, only if the dialog could not be created at all: a plain
    Windows box with the same report (Ctrl+C copies a box's whole text)."""
    import ctypes
    ctypes.windll.user32.MessageBoxW(
        0, f"Something went wrong ({where}).\n\n{SUPPORT_LINE}. Press Ctrl+C to copy this "
           f"message and paste it into the chat.\n\n{short}",
        "GDPNow - error", 0x10 | 0x10000)


def _error_dialog(intro, report):
    """Native dialog: error icon, 'DM the pope on Discord', the intro, a
    read-only text box holding the report (pre-selected) and an OK button.
    Works without WebView2; the clipboard is never touched for the user."""
    import ctypes
    import win32con
    import win32gui
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # per-monitor v2
    except Exception:
        pass
    shown = []  # filled when the dialog appears: then no fallback may follow
    actctx = _activate_comctl6()
    try:
        ID_ICON, ID_TITLE, ID_INTRO, ID_EDIT = 101, 102, 103, 104
        W, H = 330, 236
        style = (win32con.WS_POPUP | win32con.WS_CAPTION | win32con.WS_SYSMENU | win32con.DS_MODALFRAME
                 | win32con.DS_CENTER | win32con.DS_SETFONT | win32con.WS_VISIBLE)
        child = win32con.WS_CHILD | win32con.WS_VISIBLE
        template = [
            ["GDPNow - error", (0, 0, W, H), style, win32con.WS_EX_TOPMOST, (9, "Segoe UI")],
            ["Static", "", ID_ICON, (10, 10, 20, 20), child | win32con.SS_ICON],
            ["Static", SUPPORT_LINE, ID_TITLE, (40, 10, W - 50, 12), child],
            ["Static", intro.replace("\r\n", "\n"), ID_INTRO, (40, 24, W - 50, 36), child],
            ["Edit", "", ID_EDIT, (10, 64, W - 20, H - 94),
             child | win32con.WS_BORDER | win32con.WS_VSCROLL | win32con.WS_TABSTOP | win32con.ES_MULTILINE
             | win32con.ES_READONLY | win32con.ES_AUTOVSCROLL],
            ["Button", "OK", win32con.IDOK, (W - 70, H - 24, 60, 15),
             child | win32con.WS_TABSTOP | win32con.BS_DEFPUSHBUTTON],
        ]
        fonts = []

        def proc(hwnd, msg, wparam, lparam):
            if msg == win32con.WM_INITDIALOG:
                shown.append(True)
                win32gui.SendMessage(win32gui.GetDlgItem(hwnd, ID_ICON), win32con.STM_SETICON,
                                     win32gui.LoadIcon(0, win32con.IDI_ERROR), 0)
                bold = win32gui.LOGFONT()
                bold.lfFaceName, bold.lfHeight, bold.lfWeight = "Segoe UI", -_px(hwnd, 15), 700
                mono = win32gui.LOGFONT()
                mono.lfFaceName, mono.lfHeight = "Consolas", -_px(hwnd, 12)
                fonts.extend([win32gui.CreateFontIndirect(bold), win32gui.CreateFontIndirect(mono)])
                win32gui.SendMessage(win32gui.GetDlgItem(hwnd, ID_TITLE), win32con.WM_SETFONT, fonts[0], True)
                edit = win32gui.GetDlgItem(hwnd, ID_EDIT)
                win32gui.SendMessage(edit, win32con.WM_SETFONT, fonts[1], True)
                win32gui.SetWindowText(edit, report.replace("\r\n", "\n").replace("\n", "\r\n"))
                win32gui.SetFocus(edit)
                win32gui.SendMessage(edit, win32con.EM_SETSEL, 0, -1)
                try:
                    win32gui.SetForegroundWindow(hwnd)
                except Exception:
                    pass
                return False  # focus was set by hand
            if msg == win32con.WM_COMMAND and (wparam & 0xFFFF) in (win32con.IDOK, win32con.IDCANCEL):
                win32gui.EndDialog(hwnd, wparam & 0xFFFF)
                return True
            return False

        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONHAND)
        except Exception:
            pass
        win32gui.DialogBoxIndirect(0, template, 0, proc)
        for f in fonts:
            win32gui.DeleteObject(f)
    except Exception:
        pass  # if it never appeared, the caller falls back; if it did, we're done
    finally:
        _deactivate_comctl6(actctx)
    return bool(shown)


def _px(hwnd, size_at_96dpi):
    import ctypes
    try:
        return int(size_at_96dpi * ctypes.windll.user32.GetDpiForWindow(hwnd) / 96)
    except Exception:
        return size_at_96dpi


def _activate_comctl6():
    """Modern-looking controls for the dialog (see ui/comctl6.manifest).
    Returns (context handle, cookie), or None (the dialog then just looks older)."""
    import ctypes
    from ctypes import wintypes
    manifest = os.path.join(APP_DIR, "ui", "comctl6.manifest")
    if not os.path.isfile(manifest):
        return None

    class ACTCTXW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.ULONG), ("dwFlags", wintypes.DWORD),
                    ("lpSource", wintypes.LPCWSTR), ("wProcessorArchitecture", wintypes.USHORT),
                    ("wLangId", wintypes.USHORT), ("lpAssemblyDirectory", wintypes.LPCWSTR),
                    ("lpResourceName", wintypes.LPCWSTR), ("lpApplicationName", wintypes.LPCWSTR),
                    ("hModule", wintypes.HMODULE)]

    k32 = ctypes.windll.kernel32
    k32.CreateActCtxW.restype = wintypes.HANDLE
    ctx = ACTCTXW(cbSize=ctypes.sizeof(ACTCTXW), lpSource=manifest)
    handle = k32.CreateActCtxW(ctypes.byref(ctx))
    if not handle or handle == wintypes.HANDLE(-1).value:
        return None
    cookie = ctypes.c_size_t()
    if not k32.ActivateActCtx(wintypes.HANDLE(handle), ctypes.byref(cookie)):
        return None
    try:
        ctypes.windll.comctl32.InitCommonControls()
    except Exception:
        pass
    return handle, cookie.value


def _deactivate_comctl6(actctx):
    """Undo _activate_comctl6. The cookie is a pointer-sized value: ctypes
    needs explicit argument types or it overflows (that once broke the dialog)."""
    if not actctx:
        return
    import ctypes
    from ctypes import wintypes
    handle, cookie = actctx
    k32 = ctypes.windll.kernel32
    try:
        k32.DeactivateActCtx.argtypes = [wintypes.DWORD, ctypes.c_size_t]
        k32.DeactivateActCtx(0, cookie)
        k32.ReleaseActCtx.argtypes = [wintypes.HANDLE]
        k32.ReleaseActCtx(handle)
    except Exception:
        pass


def run_player(main):
    """Run the player with error reporting. Text version: an error box.
    From the app window: the report path is printed for the window to show."""
    global _session_saved
    sys.stdout = _Tee(sys.stdout, _output)
    sys.stderr = _Tee(sys.stderr, _output)
    try:
        startup()
        if os.environ.get("GDPNOW_TEST_CRASH") == "player":
            raise RuntimeError("test crash requested with GDPNOW_TEST_CRASH=player")
        main()
    except SystemExit as e:
        if e.code in (0, None):
            raise
        _player_failed(str(e.code))
        raise
    except BaseException:
        import traceback
        _player_failed(traceback.format_exc())
        raise SystemExit(1)
    log = _output_tail()
    # main() prints its own tracebacks instead of raising
    if "Traceback (most recent call last)" in log or (
            not _session_saved and re.search(r"^\s*Error", log, re.M)):
        _player_failed("The player stopped with an error (see the log below).")


def _player_failed(details):
    log = _output_tail()
    if "Traceback (most recent call last)" in log and "Traceback" not in details:
        details = log[log.rfind("Traceback (most recent call last)"):]
    where = "player (started from the app window)" if _answers is not None else "player (text version)"
    if _answers is not None:
        full, _ = build_report(where, details, log)
        try:
            path = save_report(full)
            print(f"[GDPNow] ERROR_REPORT={path}", flush=True)
        except Exception as e:
            print(f"[GDPNow] could not save the error report: {e}", flush=True)
    else:
        print(f"\n*** Something went wrong. {SUPPORT_LINE} and send the error report. ***", flush=True)
        report_error_native(where, details, log)


# ------------------------------------------------------------- mpv / GRWM
def mpv_loadfile_cmd(path, start_mmss):
    """JSON IPC loadfile with named arguments: survives spaces/quotes in
    paths and works on old (0.35) and new (0.38+, extra 'index' arg) mpv."""
    return json.dumps({"command": {"name": "loadfile", "url": path,
                                   "flags": "replace",
                                   "options": f"start={start_mmss}"}})


# Grid cells that decode on the GPU; the others decode on the CPU. The GPU's
# video decoder is shared by every cell and how much it carries differs per
# card, so it is measured once per card. An RTX 4070 Ti decodes about seven
# 4K (4096x2304) H.264 videos in real time: with all 16 cells of a 4x4 on it,
# every cell played at half speed while the CPU sat at 16 %; 6 GPU cells +
# 10 CPU cells played without dropped frames (measured). The test clip here
# measures that card at 6.3 (real 4K episodes: 6.8), so cells = whole number.
_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _gpu_name():
    """The primary display's graphics card, e.g. "NVIDIA GeForce RTX 4070 Ti"."""
    try:
        import win32api
        i = 0
        while True:
            d = win32api.EnumDisplayDevices(None, i)
            if d.StateFlags & 4:  # DISPLAY_DEVICE_PRIMARY_DEVICE
                return d.DeviceString
            i += 1
    except Exception:
        return "unknown"


def measure_gpu_decode(parallel=4, loops=4):
    """How many 4K H.264 videos (30 fps) the GPU decodes in real time, the way
    the grid's mpv does it (D3D11, frames copied back); 0.0 if it cannot.
    Several at once because some cards have more than one decoder."""
    import tempfile
    import time
    tmp = tempfile.mkdtemp(prefix="gdpnow_gpu_")
    clip = os.path.join(tmp, "test4k.mp4")
    try:
        subprocess.run([FFMPEG_EXE, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=4096x2304:r=30:d=2",
                        "-vf", "noise=alls=8:allf=t", "-c:v", "libx264", "-preset", "veryfast", "-b:v", "24M",
                        "-maxrate", "24M", "-bufsize", "12M", "-pix_fmt", "yuv420p", clip],
                       capture_output=True, timeout=120, creationflags=_NOWIN)
        if not os.path.isfile(clip):
            return 0.0
        cmd = [FFMPEG_EXE, "-nostdin", "-v", "verbose", "-hwaccel", "d3d11va", "-stream_loop", str(loops - 1),
               "-i", clip, "-f", "null", "-"]
        logs = [os.path.join(tmp, f"dec{k}.log") for k in range(parallel)]
        outs = [open(log, "wb") for log in logs]
        t0 = time.time()
        procs = [subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=out, creationflags=_NOWIN) for out in outs]
        for p in procs:
            p.wait(timeout=300)
        wall = time.time() - t0
        for out in outs:
            out.close()
        for p, log in zip(procs, logs):
            # ffmpeg quietly decodes on the CPU when the GPU cannot
            with open(log, "rb") as f:
                if p.returncode or b"pix_fmt: d3d11" not in f.read():
                    return 0.0
        return parallel * loops * 60 / wall / 30
    except Exception:
        return 0.0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def gpu_decode_cells(grid_size):
    value = os.environ.get("GDPNOW_GPU_DECODE_CELLS") or load_settings().get("gpu_decode_cells")
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        pass
    if grid_size <= 4:
        return grid_size  # small grids: every cell on the GPU, as always
    name = _gpu_name()
    cal = load_settings().get("gpu_decode")
    if isinstance(cal, dict) and cal.get("gpu") == name and isinstance(cal.get("cells"), int):
        return cal["cells"]
    print("Measuring this PC's video decoder (once per graphics card, a few seconds)...")
    streams = measure_gpu_decode()
    cells = int(streams)
    print(f"  {name}: about {streams:.1f} 4K videos at once; {cells} grid cells decode on the GPU, the rest on the CPU")
    if streams > 0:  # a failed measurement is tried again next session
        settings = load_settings()
        settings["gpu_decode"] = {"gpu": name, "streams_4k": round(streams, 1), "cells": cells}
        try:
            save_settings(settings)
        except OSError:
            pass
    return cells


def screenshots_dir():
    if os.environ.get("GDPNOW_OUTPUT_DIR"):
        return os.environ["GDPNOW_OUTPUT_DIR"]
    try:
        from win32com.shell import shell, shellcon
        pictures = shell.SHGetKnownFolderPath(shellcon.FOLDERID_Pictures, 0, None)
    except Exception:
        pictures = os.path.join(os.path.expanduser("~"), "Pictures")
    return os.path.join(pictures, "gdpnow_nuts")


_NVENC_FULL = ["-c:v", "h264_nvenc", "-preset", "p1", "-tune", "hq", "-rc", "vbr",
               "-cq", "15", "-b:v", "30M", "-maxrate", "60M", "-bufsize", "120M",
               "-bf", "3", "-b_ref_mode", "middle", "-temporal-aq", "1",
               "-spatial-aq", "1", "-rc-lookahead", "20"]
# Older NVIDIA cards (GTX 10xx and earlier) reject b_ref_mode / temporal-aq.
_NVENC_BASIC = ["-c:v", "h264_nvenc", "-preset", "p1", "-rc", "vbr", "-cq", "15",
                "-b:v", "30M", "-maxrate", "60M", "-bufsize", "120M", "-bf", "3",
                "-spatial-aq", "1"]
_X264 = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"]
_encoder_cache = None


def _encoder_works(opts):
    cmd = [FFMPEG_EXE, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
           "-i", "color=c=black:s=1920x1080:r=30:d=0.5"] + opts + [
           "-pix_fmt", "yuv420p", "-f", "null", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode == 0
    except Exception:
        return False


def video_encoder():
    """('nvenc'|'cpu', ffmpeg video options) for the GRWM video."""
    global _encoder_cache
    if _encoder_cache is None:
        if _encoder_works(_NVENC_FULL):
            _encoder_cache = ("nvenc", _NVENC_FULL)
        elif _encoder_works(_NVENC_BASIC):
            _encoder_cache = ("nvenc", _NVENC_BASIC)
        else:
            _encoder_cache = ("cpu", _X264)
        kind = "NVIDIA GPU (NVENC)" if _encoder_cache[0] == "nvenc" else "CPU (libx264, no NVIDIA GPU found)"
        print(f"[GRWM] Video encoder: {kind}")
    return _encoder_cache


def video_enc_opts_literal():
    return repr(video_encoder()[1])


def hwaccel_name():
    # cuda = NVIDIA decoder; auto = whatever the PC has (Intel/AMD/none)
    return "cuda" if video_encoder()[0] == "nvenc" else "auto"


def python_exe_for_bat():
    """Bundled python path for run_grwm.bat (the .bat is ASCII; use the
    8.3 short path if the install folder has non-ASCII characters)."""
    p = PYTHON_EXE
    if not p.isascii():
        try:
            import win32api
            p = win32api.GetShortPathName(p)
        except Exception:
            pass
    return p


_opened_sessions = set()


def session_finished(session_dir):
    """Open the session folder (screenshots + run_grwm.bat) in Explorer."""
    global _session_saved
    if not session_dir or not os.path.isdir(session_dir) or session_dir in _opened_sessions:
        return
    _opened_sessions.add(session_dir)
    _session_saved = True
    print(f"\nSession saved to: {session_dir}")
    print("Double-click run_grwm.bat in that folder to build the grid video (grwm.mp4).")
    print(f"[GDPNow] SESSION_DIR={session_dir}", flush=True)
    if _answers is None and load_settings().get("open_folder_after_session", PORTABLE):
        try:
            os.startfile(session_dir)
        except OSError:
            pass
