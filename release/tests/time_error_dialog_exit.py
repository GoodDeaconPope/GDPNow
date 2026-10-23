"""How long does the process take to exit after the native error dialog closes?

    python time_error_dialog_exit.py APP_DIR [variant]
variant: full (default) | no-actctx | no-dpi | no-beep
Prints timings; a helper thread presses OK after 1 s.
"""
import ctypes
import sys
import threading
import time

sys.argv, app, variant = ["x"], sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "full")
sys.path.insert(0, app)
import gdpnow_support as s  # noqa: E402

if variant == "no-actctx":
    s._activate_comctl6 = lambda: None
if variant == "no-beep":
    import winsound
    winsound.MessageBeep = lambda *a: None
if variant == "no-dpi":
    ctypes.windll.user32.SetProcessDpiAwarenessContext = lambda *a: 0


def press_ok():
    import win32con
    import win32gui
    for _ in range(100):
        h = win32gui.FindWindow(None, "GDPNow - error")
        if h:
            time.sleep(1)
            win32gui.PostMessage(h, win32con.WM_COMMAND, win32con.IDOK, 0)
            return
        time.sleep(0.05)


threading.Thread(target=press_ok, daemon=True).start()
t0 = time.time()
s._error_dialog("test intro", "test report")
print(f"{variant}: dialog returned after {time.time() - t0:.1f}s", flush=True)
T_END = time.time()
import atexit  # noqa: E402
atexit.register(lambda: print(f"{variant}: atexit reached {time.time() - T_END:.1f}s after the dialog", flush=True))
