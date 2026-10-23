try:
    import tkinter as tk  # unused
except ImportError:
    tk = None
import gdpnow_support as _ps
import shlex
import shutil
import subprocess
import sys as _sys
import tempfile
import time
import math
import random
from PIL import Image, ImageDraw, ImageFont

# Reconfigure stdout/stderr to UTF-8 so prints containing non-ASCII chars
# (e.g. U+A789 "꞉" in session IDs) don't crash on legacy cp1252 consoles.
# line_buffering=True forces a flush on every '\n' — critical when running
# under PowerShell, which presents stdout as a block-buffered pipe rather
# than a terminal. Without this, prints from background threads (CSP swap,
# panscan, NutCounter) can sit in the buffer for tens of seconds before
# becoming visible, giving the false impression that the session hung.
try:
    _sys.stdout.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)
    _sys.stderr.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)
except Exception:
    pass
from PIL import ImageGrab
# import screenGeo ...
import sys
# import msvcrt ...
import json
import datetime
import win32pipe, win32file, pywintypes, win32event

import glob
from pynput.keyboard import Key, Listener
import os
import threading
import win32api
import win32con
from screeninfo import get_monitors
import queue
import win32process
import win32gui
import win32con
import ctypes
from ctypes import wintypes
import win32api

GWL_WNDPROC = -4

# Store original WndProc per child hwnd
_CHILD_OLD_PROCS = {}

# For the host window / grid
host_hwnd_global = None
host_hwnds_for_layout = []
host_rows_for_layout = 1
host_cols_for_layout = 1
_GRID_HOST_STATE = {}

GWL_WNDPROC = -4

_CHILD_WNDPROC_TYPE = ctypes.WINFUNCTYPE(
    ctypes.c_long,
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
)

_CHILD_OLD_PROCS = {}
_CHILD_CB_PROCS = {}

# --- Functions Copied from Snippet 2 ---
child_wndprocs = {}


class MpvIpcClient:
    """
    Simple mpv IPC client:
    - Connects to \\.\pipe\mpvsocket{N}
    - Starts a reader thread that drains JSON messages from mpv
    - Provides send() and send_json() methods

    ========================================================================
    TODO: REWRITE WITH FILE_FLAG_OVERLAPPED (owner: GoodDeaconPope, priority: medium)
    ========================================================================

    WHY THIS NEEDS A REWRITE
    ------------------------
    This class opens the pipe handle SYNCHRONOUSLY (no
    FILE_FLAG_OVERLAPPED). Per Microsoft's CreateFile docs:

        "If this flag is not specified, then I/O operations are
         serialized, even if the calls to the read and write functions
         specify an OVERLAPPED structure."

    Consequence: when the reader thread is parked in a blocking
    ReadFile() waiting for MPV to send something, any WriteFile() call
    from another thread on the SAME handle is queued behind the pending
    ReadFile at the I/O Manager level. Windows serializes the ops —
    "one I/O at a time on a sync handle" — regardless of which Python
    thread initiated the call.

    In practice: if MPV is quiet (no command replies, no events), the
    reader sits blocked forever, and every subsequent send() deadlocks
    waiting its turn. This happened real-world when cycled_persons=1
    (no cycle thread firing loadfile events) and the user pressed E to
    trigger CSP swap — trigger_csp_swap() hung at the first
    sockets[i].send(cmd) call. Repeated E presses just queued more
    stuck sends.

    The current workaround in gdpnow is _send_fresh() at
    ~line 1561, which opens a brand-new short-lived pipe handle per
    command, WriteFiles, closes. Each fresh handle has its own
    independent I/O queue so it doesn't contend with the persistent
    reader thread. That's fine for fire-and-forget commands (CSP swap,
    panscan, loadfile) but it's a band-aid — it does not allow reading
    replies, which is what this class is supposed to enable.

    HOW TO REWRITE
    --------------
    The correct solution is async / overlapped I/O:

    1. Open the handle with FILE_FLAG_OVERLAPPED in connect():
         self.handle = win32file.CreateFile(
             self.pipe_name,
             win32file.GENERIC_READ | win32file.GENERIC_WRITE,
             win32file.FILE_SHARE_READ | win32file.FILE_SHARE_WRITE,
             None,
             win32file.OPEN_EXISTING,
             win32con.FILE_FLAG_OVERLAPPED,   # <-- THE KEY CHANGE
             None,
         )

    2. Every ReadFile / WriteFile call now takes a PyOVERLAPPED struct
       with an hEvent. The call returns immediately; you wait on the
       event with WaitForSingleObject or WaitForMultipleObjects.

       Example read:
         overlapped = pywintypes.OVERLAPPED()
         overlapped.hEvent = win32event.CreateEvent(None, True, False, None)
         try:
             rc, _ = win32file.ReadFile(self.handle, buf, overlapped)
             # rc == ERROR_IO_PENDING is normal; the op is in flight
         except pywintypes.error as e:
             if e.winerror != winerror.ERROR_IO_PENDING:
                 raise
         wait_rc = win32event.WaitForSingleObject(overlapped.hEvent, timeout_ms)
         if wait_rc == win32event.WAIT_TIMEOUT:
             win32file.CancelIoEx(self.handle, overlapped)
             ...
         nbytes = win32file.GetOverlappedResult(self.handle, overlapped, False)

       Same pattern for WriteFile. Need to clean up the hEvent handle
       in every path (try/finally on CloseHandle).

    3. _reader_loop() becomes a wait-driven loop:
         - Issue an overlapped ReadFile into a persistent buffer
         - WaitForSingleObject on the read event + a stop event
         - On stop-event signal, CancelIoEx and exit cleanly
         - On read completion, parse newline-delimited JSON, enqueue

    4. send() (and send_json()) becomes:
         - Issue an overlapped WriteFile
         - Wait with a timeout (e.g. 2s)
         - On timeout, CancelIoEx and raise a TimeoutError
         - This no longer needs self.lock because overlapped writes
           don't contend with overlapped reads. You may still want a
           lock to serialize multiple write callers for JSON framing,
           but it can be a much narrower critical section.

    5. Add a send_and_wait_reply(request_id, timeout) helper that
       sends a command with a request_id, then drains messages from
       self._messages until one matching that id appears. This is what
       enables querying properties like time-pos / filename and
       eliminates the need for _send_fresh() entirely.

    FILES TO TOUCH
    --------------
    - This class definition (connect, _reader_loop, send, send_json,
      close). Adds ~80-120 lines of overlapped I/O ceremony.
    - Remove _send_fresh(), _pipe_name_for() helpers once MpvIpcClient
      is reliable again. Update trigger_csp_swap() and adjust_panscan()
      to go through sockets[i].send() again.
    - Cycle_videos already uses sockets[i].send via its inner helper —
      no code change needed there, but it benefits from the fix
      automatically.
    - Connect path in play_videos calls `create_socket(i)` which
      should work unchanged; that helper just instantiates and calls
      connect().

    TESTING
    -------
    - Start a session with cycled_persons=1 (NO cycling) and immediately
      press E. With the old sync-handle code, this would deadlock. With
      overlapped, it should swap instantly.
    - Spam E and +/- rapidly. Should never hang the listener and every
      send should complete within ~2s or report timeout cleanly.
    - Kill an MPV process externally (Task Manager) and try to send to
      its pipe. Should error out within a few seconds, not hang.
    - Verify reader thread drains events during a long cycle_videos
      run — no leaked memory, no stuck threads at session end.
    - Add a smoke test that exercises send_and_wait_reply to query
      time-pos on cell 0 and compare against the wall-clock estimate
      NutCounter uses for labels. Good sanity check.

    REFERENCES
    ----------
    - MS docs on CreateFile / FILE_FLAG_OVERLAPPED:
      https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew
    - MS docs on named pipes + I/O serialization behavior:
      https://learn.microsoft.com/en-us/windows/win32/ipc/synchronous-and-overlapped-input-and-output
    - pywintypes.OVERLAPPED / win32file usage examples in the pywin32
      test suite (look for test_win32file_overlapped).
    - Working overlapped IPC reference impl: mpv's own client.c in the
      libmpv source (it uses Windows overlapped I/O on the pipe).
    ========================================================================
    """

    def __init__(self, pipe_name):
        self.pipe_name = pipe_name
        self.handle = None
        self.lock = threading.Lock()
        self.running = False
        self.reader_thread = None
        self._buffer = b""
        self._messages = queue.Queue()

    def connect(self, timeout=10.0, alive=None):
        """Connect to the named pipe and start the reader thread. Stops
        waiting early once alive() (the mpv process) returns False."""
        deadline = time.time() + timeout
        last_err = None

        while time.time() < deadline:
            try:
                self.handle = win32file.CreateFile(
                    self.pipe_name,
                    win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                    win32file.FILE_SHARE_READ | win32file.FILE_SHARE_WRITE,
                    None,
                    win32file.OPEN_EXISTING,
                    0,
                    None
                )
                break
            except pywintypes.error as e:
                last_err = e
                if alive is not None and not alive():
                    break
                time.sleep(0.05)

        if self.handle is None:
            raise RuntimeError(f"Failed to connect to {self.pipe_name}: {last_err}")

        self.running = True
        self.reader_thread = threading.Thread(
            target=self._reader_loop,
            name=f"MpvReader-{self.pipe_name}",
            daemon=True,
        )
        self.reader_thread.start()

    def _reader_loop(self):
        """Continuously read newline-delimited JSON from mpv and enqueue it."""
        try:
            while self.running:
                try:
                    hr, data = win32file.ReadFile(self.handle, 4096, None)
                except pywintypes.error:
                    # mpv closed the pipe or some error occurred
                    break

                if not data:
                    # EOF
                    break

                self._buffer += data

                while b"\n" in self._buffer:
                    line, self._buffer = self._buffer.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        decoded = line.decode("utf-8", errors="ignore")
                        msg = json.loads(decoded)
                        self._messages.put(msg)
                    except Exception:
                        # Non-JSON / random noise, ignore
                        pass
        finally:
            self.running = False
            try:
                if self.handle is not None:
                    win32file.CloseHandle(self.handle)
            except Exception:
                pass
            self.handle = None

    def send(self, command_str: str):
        """Send a raw mpv command string (plain text mode)."""
        if self.handle is None:
            raise RuntimeError(f"IPC not connected: {self.pipe_name}")

        if not command_str.endswith("\n"):
            command_str += "\n"

        data = command_str.encode("utf-8")
        with self.lock:
            win32file.WriteFile(self.handle, data)

    def send_json(self, obj):
        """Send a JSON mpv command object."""
        line = json.dumps(obj) + "\n"
        self.send(line)

    def get_message(self, block=False, timeout=None):
        """Get next JSON message from mpv (if you ever care about replies)."""
        try:
            return self._messages.get(block=block, timeout=timeout)
        except queue.Empty:
            return None

    def close(self):
        """Stop reader and close the pipe."""
        self.running = False
        try:
            if self.handle is not None:
                win32file.CloseHandle(self.handle)
        except Exception:
            pass
        self.handle = None



# ---------- GridHost helpers (outer resizable window) ----------
def _enum_windows_for_pids(pid_list, grid_size, max_wait=20.0, interval=0.05):
    """
    Find up to grid_size mpv top-level windows and return them IN THE SAME
    ORDER as pid_list.

    The ordering matters because _tile_children() places child_hwnds[i] at
    grid position (i % cols, i // cols), and NutCounter draws labels for
    socket_idx at the same grid position. If the returned list was in
    EnumWindows Z-order (which is random relative to PID order), socket 0's
    video could end up visually in grid position 1 while its label gets
    drawn at grid position 0 — wrong tile.

    Algorithm:
    1. Map pid -> hwnd as we find matching windows.
    2. Build the return list by iterating pid_list and picking up each
       corresponding hwnd, so order matches the caller's expectation.
    3. Fallback by class/title only appends unknown extras at the end if
       we couldn't fill by PID.
    """
    import time
    import win32gui
    import win32process

    wanted_pids = set(pid_list)
    pid_to_hwnd = {}  # pid -> hwnd (first match wins)

    def pid_callback(hwnd, extra):
        if len(pid_to_hwnd) >= grid_size:
            return
        if not win32gui.IsWindowVisible(hwnd):
            return
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:
            return
        if pid in wanted_pids and pid not in pid_to_hwnd:
            pid_to_hwnd[pid] = hwnd

    deadline = time.time() + max_wait
    attempt = 0

    # --- Search by PID (only windows of the mpv processes we started) ---
    last_found = -1
    while time.time() < deadline and len(pid_to_hwnd) < grid_size:
        attempt += 1
        win32gui.EnumWindows(pid_callback, None)
        if len(pid_to_hwnd) != last_found:
            last_found = len(pid_to_hwnd)
            print(f"GridHost: PID search attempt {attempt}, found {last_found}/{grid_size}")
        if len(pid_to_hwnd) >= grid_size:
            break
        time.sleep(interval)

    # --- Build ordered list matching pid_list order ---
    ordered_hwnds = []
    for pid in pid_list:
        if pid in pid_to_hwnd:
            ordered_hwnds.append(pid_to_hwnd[pid])

    print(f"GridHost: Ordered HWNDs (matching pid_list): {ordered_hwnds}")

    # No fallback by window class/title: that could grab mpv windows the
    # user has open for other things. Missing cells just stay out of the grid.
    if len(ordered_hwnds) < grid_size:
        print(f"GridHost: WARNING only {len(ordered_hwnds)}/{grid_size} mpv windows appeared in time.")

    # Final trim (safety)
    if len(ordered_hwnds) > grid_size:
        ordered_hwnds = ordered_hwnds[:grid_size]

    return ordered_hwnds

def _create_drag_overlay_for_host(host_hwnd):
    """
    Transparent child overlay inside GridHost:

    - Fills the client area.
    - Single click:
        * If on resize border → start resize of host.
        * Else → start drag of host (caption move).
    - Double click → toggle maximize / restore of host.
    - WM_SETCURSOR → sets correct resize/arrow cursor.
    """
    import win32gui
    import win32con
    import win32api
    import ctypes

    class_name = "GridHostDragOverlay"

    # Track last click to detect double-click
    state = {
        "last_down_time": 0,
        "last_down_pos": (0, 0),
    }

    # Get system double-click time once
    try:
        _dbl_time_ms = ctypes.windll.user32.GetDoubleClickTime()
        if _dbl_time_ms <= 0:
            _dbl_time_ms = 500
    except Exception:
        _dbl_time_ms = 500  # fallback

    def overlay_wndproc(hwnd, msg, wparam, lparam):
        try:
            if msg == win32con.WM_LBUTTONDOWN:
                # ----- Double-click detection -----
                now = win32api.GetTickCount()
                pt = win32gui.GetCursorPos()

                last_time = state["last_down_time"]
                last_pos = state["last_down_pos"]

                dx = pt[0] - last_pos[0]
                dy = pt[1] - last_pos[1]
                dist2 = dx * dx + dy * dy

                # Is this a double-click? (same spot, within system dbl-click time)
                is_double = (
                    last_time != 0
                    and (now - last_time) <= _dbl_time_ms
                    and dist2 <= 16  # within ~4px radius
                )

                if is_double:
                    # Reset so a third click doesn't immediately count as another double
                    state["last_down_time"] = 0
                    state["last_down_pos"] = (0, 0)

                    # Toggle maximize/restore on host
                    placement = win32gui.GetWindowPlacement(host_hwnd)
                    show_cmd = placement[1]
                    if show_cmd == win32con.SW_SHOWMAXIMIZED:
                        win32gui.ShowWindow(host_hwnd, win32con.SW_RESTORE)
                    else:
                        win32gui.ShowWindow(host_hwnd, win32con.SW_MAXIMIZE)
                    return 0
                else:
                    # Record this as the "first click" of a possible double-click
                    state["last_down_time"] = now
                    state["last_down_pos"] = pt

                # ----- Single-click: drag or resize host -----
                lparam_screen = win32api.MAKELONG(pt[0], pt[1])

                # Ask host how it classifies this point
                hit = win32gui.SendMessage(
                    host_hwnd,
                    win32con.WM_NCHITTEST,
                    0,
                    lparam_screen,
                )

                resize_hits = (
                    win32con.HTLEFT,
                    win32con.HTRIGHT,
                    win32con.HTTOP,
                    win32con.HTBOTTOM,
                    win32con.HTTOPLEFT,
                    win32con.HTTOPRIGHT,
                    win32con.HTBOTTOMLEFT,
                    win32con.HTBOTTOMRIGHT,
                )

                if hit in resize_hits:
                    # Start native resize of host
                    try:
                        win32api.ReleaseCapture()
                    except Exception:
                        pass
                    win32gui.SendMessage(
                        host_hwnd,
                        win32con.WM_NCLBUTTONDOWN,
                        hit,
                        lparam_screen,
                    )
                    return 0

                # Otherwise, start host drag (caption move)
                try:
                    win32api.ReleaseCapture()
                except Exception:
                    pass
                try:
                    win32gui.SetForegroundWindow(host_hwnd)
                except Exception:
                    pass

                win32gui.SendMessage(
                    host_hwnd,
                    win32con.WM_NCLBUTTONDOWN,
                    win32con.HTCAPTION,
                    lparam_screen,
                )
                return 0

            elif msg == win32con.WM_SETCURSOR:
                # Cursor shapes (resize vs arrow) based on host hit-test
                pt = win32gui.GetCursorPos()
                hit = win32gui.SendMessage(
                    host_hwnd,
                    win32con.WM_NCHITTEST,
                    0,
                    win32api.MAKELONG(pt[0], pt[1]),
                )

                if hit in (win32con.HTLEFT, win32con.HTRIGHT):
                    cur = win32gui.LoadCursor(0, win32con.IDC_SIZEWE)
                elif hit in (win32con.HTTOP, win32con.HTBOTTOM):
                    cur = win32gui.LoadCursor(0, win32con.IDC_SIZENS)
                elif hit in (win32con.HTTOPLEFT, win32con.HTBOTTOMRIGHT):
                    cur = win32gui.LoadCursor(0, win32con.IDC_SIZENWSE)
                elif hit in (win32con.HTTOPRIGHT, win32con.HTBOTTOMLEFT):
                    cur = win32gui.LoadCursor(0, win32con.IDC_SIZENESW)
                else:
                    cur = win32gui.LoadCursor(0, win32con.IDC_ARROW)

                win32gui.SetCursor(cur)
                return 1  # handled

            elif msg == win32con.WM_ERASEBKGND:
                # Keep it visually transparent
                return 1

        except Exception as e:
            print(f"GridHost overlay WndProc error: {e}")

        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    # --- Register overlay class once ---
    if not hasattr(_create_drag_overlay_for_host, "_atom"):
        wc = win32gui.WNDCLASS()
        wc.hInstance = win32gui.GetModuleHandle(None)
        wc.lpszClassName = class_name
        wc.lpfnWndProc = overlay_wndproc
        wc.hbrBackground = 0
        wc.hCursor = win32gui.LoadCursor(0, win32con.IDC_ARROW)
        _create_drag_overlay_for_host._atom = win32gui.RegisterClass(wc)

    # --- Create overlay as child of host, filling client area ---
    client_rect = win32gui.GetClientRect(host_hwnd)
    w = client_rect[2] - client_rect[0]
    h = client_rect[3] - client_rect[1]

    hwnd = win32gui.CreateWindowEx(
        win32con.WS_EX_LAYERED,
        class_name,
        None,
        win32con.WS_CHILD | win32con.WS_VISIBLE,
        0,
        0,
        w,
        h,
        host_hwnd,
        0,
        win32gui.GetModuleHandle(None),
        None,
    )
    if not hwnd:
        raise RuntimeError("GridHost: failed to create drag overlay")

    win32gui.SetLayeredWindowAttributes(
        hwnd,
        0,
        1,  # alpha 1/255; invisible but hit-testable
        win32con.LWA_ALPHA,
    )

    win32gui.SetWindowPos(
        hwnd,
        win32con.HWND_TOP,
        0,
        0,
        w,
        h,
        win32con.SWP_NOACTIVATE,
    )

    print(f"GridHost: Created drag overlay hwnd={hwnd}")
    return hwnd







def _tile_children(host_hwnd, child_hwnds, config):
    """
    Tile mpv *child* windows inside GridHost's client area, using full space.

    - Coordinates are in host client coordinates (because mpv HWNDs are WS_CHILD).
    - Last row/col stretch to consume any leftover pixels, so no gaps.
    """
    import win32gui, win32con

    rect = win32gui.GetClientRect(host_hwnd)
    host_w = rect[2] - rect[0]
    host_h = rect[3] - rect[1]

    if host_w <= 0 or host_h <= 0:
        return

    cols = max(1, config.column_grid)
    rows = max(1, config.row_grid)

    base_cell_w = host_w // cols
    base_cell_h = host_h // rows

    for idx, hwnd in enumerate(child_hwnds):
        if not win32gui.IsWindow(hwnd):
            continue

        col = idx % cols
        row = idx // cols

        # normal cell origin
        x = col * base_cell_w
        y = row * base_cell_h

        # last column/row consume remaining space so sum = host_w/host_h
        if col == cols - 1:
            w = host_w - x
        else:
            w = base_cell_w

        if row == rows - 1:
            h = host_h - y
        else:
            h = base_cell_h

        if w <= 0 or h <= 0:
            continue

        win32gui.SetWindowPos(
            hwnd,
            None,
            x,
            y,
            w,
            h,
            win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE,
        )




def _create_grid_host_window(config):
    """
    Create the outer 'GridHost' window:

    - Visually borderless.
    - Actually WS_OVERLAPPEDWINDOW so Snap/maximize/minimize work.
    - Hit-testing gives resize edges + drag anywhere else.
    """
    import win32gui
    import win32con
    import win32api

    class_name = "GridHostWindowClass"

    def wndproc(hwnd, msg, wparam, lparam):
        try:
            # Remove normal non-client (caption/borders)
            if msg == win32con.WM_NCCALCSIZE and wparam:
                return 0

            elif msg == win32con.WM_NCPAINT:
                return 0

            elif msg == win32con.WM_SIZE:
                info = _GRID_HOST_STATE.get(hwnd)
                if info:
                    try:
                        _tile_children(hwnd, info["child_hwnds"], info["config"])
                    except Exception as e:
                        print(f"GridHost: WM_SIZE tiling failed: {e}")

                    overlay = info.get("overlay_hwnd")
                    if overlay:
                        rect = win32gui.GetClientRect(hwnd)
                        w = rect[2] - rect[0]
                        h = rect[3] - rect[1]
                        win32gui.SetWindowPos(
                            overlay,
                            win32con.HWND_TOP,
                            0,
                            0,
                            w,
                            h,
                            win32con.SWP_NOACTIVATE,
                        )
                return 0

            elif msg == win32con.WM_NCHITTEST:
                x = win32api.LOWORD(lparam)
                y = win32api.HIWORD(lparam)

                try:
                    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                except win32gui.error:
                    return win32con.HTCLIENT

                border = 8

                on_left   = x < left   + border
                on_right  = x > right  - border
                on_top    = y < top    + border
                on_bottom = y > bottom - border

                # Corners
                if on_left and on_top:
                    return win32con.HTTOPLEFT
                if on_right and on_top:
                    return win32con.HTTOPRIGHT
                if on_left and on_bottom:
                    return win32con.HTBOTTOMLEFT
                if on_right and on_bottom:
                    return win32con.HTBOTTOMRIGHT

                # Sides
                if on_left:
                    return win32con.HTLEFT
                if on_right:
                    return win32con.HTRIGHT
                if on_bottom:
                    return win32con.HTBOTTOM

                # Top edge: behave like caption (drag)
                if on_top:
                    return win32con.HTCAPTION

                # Anywhere else: drag window (caption)
                return win32con.HTCAPTION

            elif msg == win32con.WM_DESTROY:
                win32gui.PostQuitMessage(0)
                return 0

        except Exception as e:
            print(f"GridHost: WndProc error: {e}")

        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    # Register once
    if not hasattr(_create_grid_host_window, "_atom"):
        wc = win32gui.WNDCLASS()
        wc.hInstance = win32gui.GetModuleHandle(None)
        wc.lpszClassName = class_name
        wc.lpfnWndProc = wndproc
        wc.hbrBackground = 0
        _create_grid_host_window._atom = win32gui.RegisterClass(wc)

    mon_w, mon_h = config.input_geometry
    mon_x, mon_y = config.monitor_offset

    # Overlapped window → Snap/maximize/minimize work.
    style = win32con.WS_OVERLAPPEDWINDOW
    ex_style = win32con.WS_EX_APPWINDOW

    hwnd = win32gui.CreateWindowEx(
        ex_style,
        class_name,
        "GridHost",
        style,
        mon_x,
        mon_y,
        mon_w,
        mon_h,
        0,
        0,
        win32gui.GetModuleHandle(None),
        None,
    )
    if not hwnd:
        raise RuntimeError("GridHost: CreateWindowEx failed")

    # 🔥 Force Windows to recompute the non-client area *now*
    win32gui.SetWindowPos(
        hwnd,
        None,
        0,
        0,
        0,
        0,
        win32con.SWP_NOMOVE
        | win32con.SWP_NOSIZE
        | win32con.SWP_NOZORDER
        | win32con.SWP_NOACTIVATE
        | win32con.SWP_FRAMECHANGED,
    )

    print(f"GridHost: Created outer window hwnd={hwnd}")
    return hwnd






def _create_grid_cells(host_hwnd, count):
    """One black child window per grid cell; each mpv draws inside its cell
    (--wid), so mpv never opens a window of its own."""
    import win32api
    import win32con
    import win32gui

    class_name = "GridCellWindowClass"
    if not hasattr(_create_grid_cells, "_atom"):
        wc = win32gui.WNDCLASS()
        wc.hInstance = win32gui.GetModuleHandle(None)
        wc.lpszClassName = class_name
        wc.lpfnWndProc = lambda h, m, w, l: win32gui.DefWindowProc(h, m, w, l)
        wc.hbrBackground = win32gui.GetStockObject(win32con.BLACK_BRUSH)
        wc.hCursor = win32api.LoadCursor(0, win32con.IDC_ARROW)
        _create_grid_cells._atom = win32gui.RegisterClass(wc)
    cells = []
    for _ in range(count):
        cells.append(win32gui.CreateWindowEx(
            0, class_name, "", win32con.WS_CHILD | win32con.WS_VISIBLE | win32con.WS_CLIPCHILDREN,
            0, 0, 10, 10, host_hwnd, 0, win32gui.GetModuleHandle(None), None))
    return cells


def _start_grid_host_with_cells(config, timeout=5.0):
    """Create and show the GridHost window with one cell per video on its own
    thread (which then runs the window's message loop). Returns the cell
    HWNDs to hand to mpv as --wid, or None (then mpv opens its own windows
    and GridHost adopts them afterwards, the old way)."""
    grid_size = config.column_grid * config.row_grid
    if grid_size <= 0:
        return None
    ready = threading.Event()
    result = {}

    def run():
        import win32gui, win32con
        try:
            host_hwnd = _create_grid_host_window(config)
            cells = _create_grid_cells(host_hwnd, grid_size)
            _attach_children_to_host(host_hwnd, cells, config)
            win32gui.ShowWindow(host_hwnd, win32con.SW_SHOWNORMAL)
            win32gui.UpdateWindow(host_hwnd)
            result["cells"] = cells
            print(f"GridHost: {grid_size} cells ready, mpv will draw inside them.")
        except Exception as e:
            print(f"GridHost: could not create the grid window first ({e}); using separate mpv windows.")
        finally:
            ready.set()
        if "cells" in result:
            win32gui.PumpMessages()
            print("GridHost: Message loop exited.")

    threading.Thread(target=run, name="GridHostThread", daemon=True).start()
    ready.wait(timeout)
    return result.get("cells")


def _attach_children_to_host(host_hwnd, child_hwnds, config):
    """
    Attach mpv windows as WS_CHILD inside GridHost, strip borders,
    then place the transparent drag overlay on top.
    """
    import win32gui
    import win32con

    GWL_STYLE   = -16
    GWL_EXSTYLE = -20

    WS_CHILD      = win32con.WS_CHILD
    WS_VISIBLE    = win32con.WS_VISIBLE
    WS_CAPTION    = win32con.WS_CAPTION
    WS_THICKFRAME = win32con.WS_THICKFRAME
    WS_DISABLED   = win32con.WS_DISABLED

    # Ensure host is enabled
    try:
        host_style = win32gui.GetWindowLong(host_hwnd, GWL_STYLE)
        host_style &= ~WS_DISABLED
        win32gui.SetWindowLong(host_hwnd, GWL_STYLE, host_style)
    except Exception as e:
        print(f"GridHost: failed to ensure host enabled: {e}")

    for hwnd in child_hwnds:
        try:
            if not win32gui.IsWindow(hwnd):
                continue

            # Reparent mpv into host
            win32gui.SetParent(hwnd, host_hwnd)
            # mpv started minimized (no loose windows during startup):
            # restore it now, inside the host, which is not shown yet.
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)

            style = win32gui.GetWindowLong(hwnd, GWL_STYLE)
            style &= ~(WS_CAPTION | WS_THICKFRAME)
            style |= (WS_CHILD | WS_VISIBLE)
            win32gui.SetWindowLong(hwnd, GWL_STYLE, style)

            ex_style = win32gui.GetWindowLong(hwnd, GWL_EXSTYLE)
            ex_style &= ~win32con.WS_EX_TRANSPARENT
            win32gui.SetWindowLong(hwnd, GWL_EXSTYLE, ex_style)

            win32gui.SetWindowPos(
                hwnd,
                None,
                0,
                0,
                0,
                0,
                win32con.SWP_NOSIZE
                | win32con.SWP_NOMOVE
                | win32con.SWP_NOZORDER
                | win32con.SWP_NOACTIVATE
                | win32con.SWP_FRAMECHANGED,
            )

        except Exception as e:
            print(f"GridHost: SetParent/style failed for {hwnd}: {e}")

    # Create drag overlay *after* mpv children so it sits above them
    overlay_hwnd = _create_drag_overlay_for_host(host_hwnd)

    _GRID_HOST_STATE[host_hwnd] = {
        "config": config,
        "child_hwnds": list(child_hwnds),
        "overlay_hwnd": overlay_hwnd,
    }

    try:
        _tile_children(host_hwnd, child_hwnds, config)
    except Exception as e:
        print(f"GridHost: _tile_children failed: {e}")


def _grid_host_thread(config, mpv_pids):
    """
    Background thread:
    - Finds mpv HWNDs
    - Creates GridHost
    - Attaches mpv windows
    - Runs a Win32 message loop
    """
    import win32gui, win32con

    try:
        grid_size = config.column_grid * config.row_grid
        if grid_size <= 0:
            print("GridHost: grid_size <= 0, nothing to host.")
            return

        # mpv only creates its window once the video opens, which can take a
        # while on a slow disk: wait up to 30 s for OUR windows.
        child_hwnds = _enum_windows_for_pids(mpv_pids, grid_size=grid_size, max_wait=30.0, interval=0.1)
        if not child_hwnds:
            print("GridHost: Could not find any MPV HWNDs.")
            return

        print(f"GridHost: Starting with {len(child_hwnds)} child windows, grid={config.column_grid}x{config.row_grid}")

        host_hwnd = _create_grid_host_window(config)
        _attach_children_to_host(host_hwnd, child_hwnds, config)

        win32gui.ShowWindow(host_hwnd, win32con.SW_SHOWNORMAL)
        win32gui.UpdateWindow(host_hwnd)

        print("GridHost: Entering message loop.")
        win32gui.PumpMessages()
        print("GridHost: Message loop exited.")
    except Exception as e:
        print(f"GridHost: failed to start host: {e}")



def get_all_monitors():
    """Get information about all connected monitors."""
    monitors = get_monitors()
    return [{
        'name': f'Monitor {i+1}',
        'x': monitor.x,
        'y': monitor.y,
        'width': monitor.width,
        'height': monitor.height,
        'is_primary': monitor.is_primary
    } for i, monitor in enumerate(monitors)]

def select_monitor():
    """Allow user to select a monitor. Auto-selects if only one is found."""
    monitors = get_all_monitors()

    # Handle case where no monitors are detected
    if not monitors:
        print("Error: No monitors detected. Exiting.")
        return None

    # Auto-select if only one monitor is found
    if len(monitors) == 1:
        selected = monitors[0]
        primary_str = " (Primary)" if selected['is_primary'] else ""
        print("Auto-selecting the only available monitor:")
        print(f"-> {selected['name']}{primary_str} | Resolution: {selected['width']}x{selected['height']} | Position: ({selected['x']}, {selected['y']})")
        # Return width, height, x_offset, y_offset
        return [selected['width'], selected['height'], selected['x'], selected['y']]

    # If multiple monitors are found, prompt the user for a selection
    print("\nAvailable Monitors:")
    for i, monitor in enumerate(monitors):
        primary_str = " (Primary)" if monitor['is_primary'] else ""
        print(f"{i+1}. {monitor['name']}{primary_str}")
        print(f"   Resolution: {monitor['width']}x{monitor['height']}")
        print(f"   Position: ({monitor['x']}, {monitor['y']})")

    while True:
        try:
            choice = int(input("\nSelect monitor number (1-{}): ".format(len(monitors))))
            if 1 <= choice <= len(monitors):
                selected = monitors[choice-1]
                # Return width, height, x_offset, y_offset
                return [selected['width'], selected['height'], selected['x'], selected['y']]
        except ValueError:
            pass
        print("Invalid selection. Please try again.")

class VideoPlayerConfig:
    def __init__(self, master_type, column_grid, row_grid, cycled_persons, interval_cycle, total_time,
                 input_geometry, pixel_drop, crop_height, crop_width, production, test_mode, monitor_offset):
        self.master_type = master_type
        self.column_grid = column_grid
        self.row_grid = row_grid
        self.cycled_persons = cycled_persons
        self.interval_cycle = interval_cycle if interval_cycle > 0 else 15 # Ensure interval is positive
        self.total_time = total_time if total_time > 0 else 300 # Ensure total_time is positive
        self.input_geometry = input_geometry
        self.pixel_drop = pixel_drop
        self.crop_height = crop_height
        self.crop_width = crop_width
        self.production = production
        self.test_mode = test_mode
        self.monitor_offset = monitor_offset # [x, y] offset

        # Performance settings - taken from snippet 2, adjustable
        self.use_interpolation = False  # Set to False to reduce GPU load
        self.optimize_performance = True  # Set to True to enable basic optimizations
        self.extra_optimization = True  # Set to True for additional optimizations
        self.lower_resolution = False  # GPU handles 4K->cell downscale natively
        self.gpu_decode_cells = _ps.gpu_decode_cells(column_grid * row_grid)

        # Maps - ensure interval_cycle is valid before using it here
        self.pixel_drop_map = {
            "PC": 0, "BJ": 0, "RC": 0, "PM": 0, "D": 100, "M": 130, "CSP": 200
        }
        self.default_duration_map = {
            "PC": 60, "BJ": 60, "M": 360, "RC": 360, "PM": 60, "D": 360, "PD": 60,
            "CP": self.interval_cycle, "CSP": self.interval_cycle, "CS": self.interval_cycle,
            "B": 180, "BP": 60, "BEP": 180
        }
        # Update default duration map again in case interval_cycle was corrected
        for key in ["CP", "CSP", "CS"]:
            self.default_duration_map[key] = self.interval_cycle


def create_mpv_command(video_file, start_time, geometry, config, socket_index, wid=None):
    """Creates the MPV command string. With wid (a GridHost cell window),
    mpv draws inside that cell instead of opening its own window."""
    # Base parameters.
    # --input-default-bindings=no + --input-vo-keyboard=no disable ALL of
    # MPV's built-in keyboard handling. This stops MPV from reacting to
    # keys that our pynput listener also uses (e.g. + and - triggering
    # MPV's default panscan bindings, or q quitting a single MPV cell
    # mid-session). Keyboard control now flows exclusively through the
    # pynput global listener -> IPC pipe path we already own. Works with
    # GridHost parent window focus too, because it disables the VO
    # keyboard input driver entirely regardless of which window has focus.
    # Grids of 9+ cells time video by the audio clock: display-resample redraws
    # every cell on every screen refresh, which on a 4x4 kept the GPU at ~77 %
    # and dropped frames; with audio sync 0 dropped, GPU ~50 % (measured)
    sync = "audio" if config.column_grid * config.row_grid >= 9 else "display-resample"
    pc_parameters = (
        f"--no-border --video-sync={sync} --keepaspect "
        f"--no-keepaspect-window --panscan=1 "
        f"--input-default-bindings=no --input-vo-keyboard=no "
        # No personal mpv scripts in the grid (keys are off there anyway): a
        # user script that shells out flashes a cmd window per call per cell.
        f"--load-scripts=no "
        # Start each scene at the nearest keyframe instead of decoding forward
        # to the exact second: these files have a keyframe every ~1 s, so a
        # scene starts at most ~1 s early, and a 2x2 grid shows all its
        # pictures in ~1-2 s instead of ~3 s (measured). Switches get faster too.
        f"--hr-seek=no "
    )

    # Only add interpolation if enabled
    if config.use_interpolation:
        pc_parameters += "--interpolation "

    # Performance optimization parameters (only if enabled)
    performance_params = ""
    scale_param = ""
    if config.optimize_performance:
        # The first cells decode on the GPU, the rest on the CPU: one GPU video
        # decoder serves every cell (see _ps.gpu_decode_cells)
        hwdec = "auto-copy" if socket_index < config.gpu_decode_cells else "no"
        performance_params = f"--scale=bilinear --cscale=bilinear --dscale=bilinear --hwdec={hwdec} --vd-lavc-threads=4 "

        # Add additional optimizations if enabled
        if config.extra_optimization:
            # CPU decoding in big grids skips H.264 deblocking (not visible at cell size)
            skip = "all --vd-lavc-fast" if config.column_grid * config.row_grid >= 9 else "nonkey"
            performance_params += f"--vo=gpu --cache=yes --demuxer-max-bytes=100M --vd-lavc-skiploopfilter={skip} "

        # Add resolution scaling if enabled and valid dimensions
        # Skip for 1x1 grid: scale filter forces exact dimensions which destroys
        # aspect ratio on loadfile switches (prevents panscan from working)
        if config.lower_resolution and config.column_grid > 0 and config.row_grid > 0 and (config.column_grid * config.row_grid) > 1:
             # Use floor to avoid potential issues with slightly oversized windows
            scale_width = math.floor(config.input_geometry[0] / config.column_grid)
            scale_height = math.floor(config.input_geometry[1] / config.row_grid)
            # Ensure dimensions are at least 1 pixel
            scale_width = max(1, scale_width)
            scale_height = max(1, scale_height)
            scale_param = f"--vf-add=scale={scale_width}:{scale_height}"
        else:
            scale_param = ""
    else:
        performance_params = ""
        scale_param = ""

    # Cropping parameter (ensure crop factors are valid)
    crop_w_factor = max(0.0, min(1.0, config.crop_width))
    crop_h_factor = max(0.0, min(1.0, config.crop_height))
    lavfi = f"crop={crop_w_factor}*in_w:{crop_h_factor}*in_h:y=0" # Simplified y=0 assumption
    ffmpeg_params = f"--vf=lavfi=[{lavfi}] {scale_param}" # Combine crop and scale

    # Test mode parameters
    if config.test_mode:
        pc_parameters += "--no-video --no-audio "

    # Type-specific parameters
    if config.master_type == "PC":
        pc_parameters += "--brightness=6 --gamma=7 "

    # For single video mode, add fullscreen flag
    if config.column_grid == 1 and config.row_grid == 1 and not wid:
        pc_parameters += "--fs "

    # IPC socket parameter using the provided index
    socket_params = f"--input-ipc-server=mpvsocket{socket_index} "

    # Assemble the command
    if os.environ.get("GDPNOW_MPV_LOG_DIR"):  # diagnostics: one timestamped log per mpv
        pc_parameters += f'--log-file="{os.path.join(os.environ["GDPNOW_MPV_LOG_DIR"], f"mpv{socket_index}.log")}" '
    if os.environ.get("GDPNOW_MPV_EXTRA"):  # diagnostics: try extra mpv options
        pc_parameters += os.environ["GDPNOW_MPV_EXTRA"] + " "
    place = f"--wid={int(wid)}" if wid else f"--geometry={geometry}"
    command = f"{place} {pc_parameters} {performance_params} {socket_params} {ffmpeg_params}"
    # print(f"Generated Command (Socket {socket_index}): {command}") # Debug print
    # Use shlex for proper quoting, especially for filenames with spaces
    # Ensure quotes around video_file are handled correctly by shlex if needed, but usually better handled by Popen directly if command is passed as list
    # Passing the whole string to Popen with shell=True is generally discouraged for security.
    # shlex.split works well here.
    try:
        split_command = [_ps.MPV_EXE, f"--start={start_time}", video_file] + shlex.split(command)
        # print(f"Shlex Split Command: {split_command}") # Debug print
        return split_command
    except ValueError as e:
         print(f"Error splitting command with shlex: {e}")
         print(f"Command was: {command}")
         # Fallback or re-raise
         return None # Indicate error


def start_mpv_instance(command_list):
    """Starts an MPV instance with the given command list."""
    if command_list is None:
        print("Error: Cannot start MPV with invalid command.")
        return None
    try:
        # Use DETACHED_PROCESS and CREATE_NEW_PROCESS_GROUP to prevent MPV from capturing Ctrl+C
        # Note: This might make manual termination harder if needed.
        # Adjust flags as necessary for your environment.
        # creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        # Simpler approach without special flags often works fine:
        creationflags = 0
        process = subprocess.Popen(command_list, creationflags=creationflags)
        print(f"Started MPV instance with PID: {process.pid}")
        return process
    except FileNotFoundError:
        print("Error: 'mpv' command not found. Ensure MPV is installed and in your system's PATH.")
        return None
    except Exception as e:
        print(f"Error starting MPV process: {e}")
        print(f"Command was: {command_list}")
        return None


def create_socket(index, retries=100, delay=0.05, process=None, timeout=None):
    """Create an MpvIpcClient for the given socket index and connect it.

    Returns an MpvIpcClient instance instead of a raw handle. With process,
    waits up to timeout (10 s) while that mpv runs: on a busy PC (cells
    already decoding from a slow drive) mpv's pipe took up to ~5 s (measured).
    """
    pipe_name = fr'\\.\pipe\mpvsocket{index}'
    print(f"Attempting to connect to pipe: {pipe_name}")
    client = MpvIpcClient(pipe_name)
    if timeout is None:
        timeout = 10.0 if process is not None else retries * delay
    try:
        client.connect(timeout=timeout,
                       alive=(lambda: process.poll() is None) if process is not None else None)
    except RuntimeError as e:
        print(f"Error connecting to pipe {pipe_name}: {e}")
        raise FileNotFoundError(f"Failed to connect to socket within {timeout:.0f} s: {pipe_name}")
    print(f"Successfully connected to pipe: {pipe_name}")
    return client




def calculate_geometry(index, config):
    """Calculates the window geometry string for an MPV instance."""
    input_width, input_height = config.input_geometry[:2]
    if config.column_grid <= 0 or config.row_grid <= 0:
        print("Error: column_grid and row_grid must be positive.")
        # Return a default/fallback geometry
        return f"640x360+{config.monitor_offset[0]}+{config.monitor_offset[1]}"

    # Use floor division or ceil depending on desired behavior for non-exact fits
    # math.ceil ensures windows cover the whole area, potentially overlapping slightly
    # math.floor prevents overlap but might leave small gaps
    # Using ceil seems more common for tiling window managers
    output_width = math.ceil(input_width / config.column_grid)
    output_height = math.ceil(input_height / config.row_grid)

    col = index % config.column_grid
    row = index // config.column_grid

    # Calculate top-left corner position, adding monitor offset
    width_position = output_width * col + config.monitor_offset[0]
    height_position = output_height * row + config.monitor_offset[1]

    # Ensure positions are integers
    width_position = int(width_position)
    height_position = int(height_position)
    output_width = int(output_width)
    output_height = int(output_height)


    return f"{output_width}x{output_height}+{width_position}+{height_position}"

def prepare_video_list():
    """
    Loads video map from E:\1SystemFile\, checks for videos in Current Dir AND E:\1SystemFile\,
    handles command line args, shuffles, returns list with correct paths.
    """
    
    # Portable release: systemFileMap.json ships in app\, the video folder
    # is the user's own, files are matched to map entries by number.
    system_file_map = _ps.load_matched_map()

    if not system_file_map:
        print("Warning: No matching video files found in '.' or 'E:\\1SystemFile'.")

    # Handle command-line arguments
    args_file_map = {}
    remaining_map = system_file_map.copy() 

    for arg in sys.argv[1:]:
        # Episode number first (257 = 257.mp4, not 1257 or 2570), then the old
        # "file name contains the text" match.
        found_key = _ps.find_arg_key(arg, list(remaining_map.keys()))

        if found_key:
            args_file_map[found_key] = remaining_map.pop(found_key)
            print(f"Prioritizing video '{os.path.basename(found_key)}' based on argument '{arg}'.")
        else:
            print(f"No video matches '{arg}' (not in your folder, or not in the map).")

    # Shuffle the remaining
    shuffled_items = list(remaining_map.items())
    random.shuffle(shuffled_items)
    shuffled_system_file_map = dict(shuffled_items)

    # Create final list
    final_list = list(args_file_map.items()) + list(shuffled_system_file_map.items())

    print(f"Prepared video list: {len(final_list)} videos ({len(args_file_map)} prioritized, {len(shuffled_system_file_map)} shuffled).")
    return final_list


def get_duration_of_session(time1_str, time2_str):
    """
    Duration in seconds between two 'MM:SS' strings.
    Allows minutes >= 60 (e.g. '60:59').
    """
    t1 = mmss_to_seconds(time1_str)
    t2 = mmss_to_seconds(time2_str)
    return max(0, t1 - t2)

# --- End of Functions Copied from Snippet 2 ---


# Global variables (ensure these are defined before use)
sockets = [] # List to hold socket handles
processes = [] # List to hold process objects
interrupt_event = threading.Event() # Event to signal interruption/shutdown
person_socket_map = {} # Dictionary mapping socket index to list of assigned videos

# Per-cell playback state, used by NutCounter to label screenshots.
# Keyed by socket index (which maps to grid cell position).
# Each entry: {"filename": str, "started_wall": float, "started_pos_sec": float,
#              "metadata": dict}
cell_state = {}
cell_state_lock = threading.Lock()

# Full session history: every (wall_time, socket_idx, filename, pos_sec, metadata)
# tuple, appended in order. Used by GRWM script generator to reconstruct what
# was watched.
cell_history = []

# Set when E key is pressed; cycle thread checks this and stops switching.
e_locked = threading.Event()

# Snapshot of the live crop factors + output dimensions, captured at
# play_videos() start so GRWM can match what was actually on screen
# during the session (including portrait orientation after monitor rot).
session_crop_width = None
session_crop_height = None
session_output_w = None
session_output_h = None

# Live session panscan tracking. Starts at 1.0 to match MPV's --panscan=1
# flag in create_mpv_command. adjust_panscan() updates this by delta and
# clamps to [0, 1]. GRWM reads this at generation time to pick its
# fill/letterbox strategy.
#
# TODO (1:1 time-varying panscan): panscan_history lets us know WHEN the
# user changed panscan during the session. A future GRWM pass could split
# each scene at panscan change wall-times and apply a different scale/crop
# factor per sub-scene. Requires ffprobing each source to learn its aspect
# ratio, then lerping the scale factor between decrease-fit and
# increase-fill based on the panscan value. For now GRWM uses the FINAL
# session_panscan value for the entire replay and logs a warning if the
# value was changed during the session.
session_panscan = 1.0
session_panscan_history = []  # list of (wall_time, value)

# +/- panscan adjustment step (MPV panscan property range is [0.0, 1.0])
PANSCAN_STEP = 0.05

# Live session video-pan-x / video-pan-y tracking. MPV's --video-pan-x and
# --video-pan-y properties pan the rendered video within the cell by a
# fraction of the output window size, range [-3.0, 3.0]. We only use a
# small sub-range because values past the visible clipped region just push
# the video into empty margin (black). adjust_video_pan_x/y() update these
# and append to history so GRWM can eventually reconstruct pan state.
session_video_pan_x = 0.0
session_video_pan_y = 0.0
session_video_pan_history = []  # list of (wall_time, x, y)

# arrow-key video-pan adjustment step (fraction of output cell width/height)
# This is the *per-tap* step when the key is briefly pressed and released
# (single discrete movement). Held-down arrow keys use a separate smooth
# animation thread (see _pan_animator below) with a smaller per-tick delta
# so motion looks fluid instead of jerky jumps. Tuned down repeatedly
# because each tap was moving the image too much — the user wants
# subtle framing tweaks, not full-scene jumps.
VIDEO_PAN_STEP = 0.008

# Smooth-pan animation constants. While any arrow key is held, a single
# background thread ticks at PAN_TICK_HZ and emits PAN_TICK_STEP-sized
# deltas for each held direction. Rate math: 33Hz * 0.003 = ~10%/sec
# held pan, so the full [-1, 1] range takes ~20s of sustained hold —
# slow enough for precise framing, you'll reach for hold-down rather
# than rapid tapping if you need to move far.
PAN_TICK_HZ = 33
PAN_TICK_STEP = 0.003

# Held-state + animator thread globals (see _pan_key_down / _pan_animator)
_pan_held = {("x", +1): False, ("x", -1): False, ("y", +1): False, ("y", -1): False}
_pan_thread = None
_pan_thread_lock = threading.Lock()

# Arrow-key video-pan gating is DYNAMIC based on the current GridHost
# client aspect, not a static session-level flag, because the user can
# resize the GridHost at runtime and make pan go from useless to useful
# (or vice versa). Arrow keys only do something visible when the video
# overflows the cell in at least one direction, which requires
# post-crop aspect != current cell aspect.
#
# _pan_ref_cell_aspect is the cell aspect at which post-crop aspect
# matches exactly (the "pan is a no-op" aspect). Set in main() at
# config-finalize time:
#   - Landscape override active (row<col, calc_w=1.0 forced):
#     post-crop aspect = source_aspect / crop_h, which we CANNOT compute
#     without ffprobing the source. Pan is ALWAYS meaningful in that
#     case because calc_w=1.0 never matches cell aspect exactly (except
#     by coincidence). Sentinel value: None -> "always meaningful".
#   - Otherwise: the prefit calc_w formula makes post-crop aspect equal
#     to the INITIAL cell aspect. Store that initial aspect and compare
#     at press time against the current (post-resize) cell aspect. A
#     resize that changes the aspect means pan now has range.
_pan_ref_cell_aspect = None
# Throttle for the "pan disabled" log line so OS key-repeat doesn't spam.
_pan_disabled_log_ts = 0.0


def _update_cell_state(socket_idx, filename, started_pos_sec, metadata=None):
    """Record that socket_idx just started playing `filename` from
    `started_pos_sec` seconds into the source. Updates the live cell_state
    snapshot (used by NutCounter for label overlay) AND appends to the
    session history (used by GRWM for scene reconstruction)."""
    entry_metadata = dict(metadata) if metadata else {}
    now = time.time()
    with cell_state_lock:
        cell_state[socket_idx] = {
            "filename": filename,
            "started_wall": now,
            "started_pos_sec": float(started_pos_sec),
            "metadata": entry_metadata,
        }
        cell_history.append({
            "wall": now,
            "socket_idx": socket_idx,
            "filename": filename,
            "pos_sec": float(started_pos_sec),
            "metadata": entry_metadata,
        })


def _snapshot_cell_state():
    """Return a shallow copy of cell_state for consumers that iterate it."""
    with cell_state_lock:
        return dict(cell_state)


def _snapshot_cell_history():
    with cell_state_lock:
        return list(cell_history)


class NutCounter:
    """
    Periodic screen-region capture with guaranteed final-count output.

    - Captures the GridHost window's CLIENT area every `capture_interval`
      seconds. Client area == the actual video grid, excluding title bar.
      Resolved via win32 FindWindow at each capture, so resizing/moving
      the container is handled automatically.
    - If the host window can't be found (e.g. not created yet, destroyed,
      minimized), falls back to the static `fallback_bbox` passed to start().
    - On finalize(), selects exactly `target_count` captures evenly spaced
      across the session and copies them to the final output dir.
    - If buffer has fewer than `target_count` captures at finalize time,
      takes rapid emergency fill captures before MPVs are killed.
    - finalize() is idempotent.
    """

    DEFAULT_OUTPUT_DIR = _ps.screenshots_dir()
    HOST_WINDOW_CLASS = "GridHostWindowClass"

    @staticmethod
    def _make_session_id():
        """Human-readable session ID for the folder name.
        Format: "4꞉21 AM, April 8th 2026"

        NTFS forbids ASCII ':' in path components (it's reserved for
        Alternate Data Streams), so the hour/minute separator uses
        U+A789 MODIFIER LETTER COLON '꞉' which is in Unicode
        specifically for displaying colons where ':' is unavailable.
        It renders nearly identically to ':' in most fonts."""
        now = datetime.datetime.now()
        hour_12 = now.hour % 12 or 12
        ampm = "AM" if now.hour < 12 else "PM"
        time_str = f"{hour_12}\uA789{now.minute:02d} {ampm}"
        day = now.day
        if 10 <= day % 100 <= 20:
            suffix = "th"
        else:
            suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
        return f"{time_str}, {now.strftime('%B')} {day}{suffix} {now.year}"

    def __init__(self, target_count=10, capture_interval=1.0, output_dir=None):
        self.target_count = target_count
        self.capture_interval = capture_interval
        self.fallback_bbox = None
        self.column_grid = 1
        self.row_grid = 1
        self.captures = []  # list of (timestamp, tmp_file_path)
        self.tmp_dir = None
        self.final_dir = None
        self.session_id = self._make_session_id()
        self.stop_event = threading.Event()
        self.thread = None
        self.finalized = False
        self.lock = threading.Lock()
        self.base_final_dir = output_dir or self.DEFAULT_OUTPUT_DIR
        self._fallback_warned = False
        self._host_seen = False
        self._host_first_seen_at = 0.0  # monotonic time of first successful find
        self._last_host_bbox = None  # last known good host rect
        self._skipped_pre_host = 0   # counter for early skipped captures
        self._font_cache = {}  # size -> ImageFont
        # After GridHost first appears, wait this long before the first
        # real capture. MPV windows exist (GridHost wraps them) but they
        # might still be rendering their first frames — capturing during
        # that window yields a black cell that ends up as the first
        # screenshot in the final selection.
        self._startup_settle_sec = 2.0

    def start(self, fallback_bbox, column_grid=1, row_grid=1):
        """fallback_bbox = (x1, y1, x2, y2) screen coords, used only if the
        GridHost window can't be found via FindWindow at capture time.
        column_grid/row_grid determine how labels are positioned per cell."""
        if self.thread is not None:
            return  # already running
        self.fallback_bbox = tuple(int(v) for v in fallback_bbox)
        self.column_grid = max(1, int(column_grid))
        self.row_grid = max(1, int(row_grid))
        # Regenerate session_id at actual session start (not import time).
        self.session_id = self._make_session_id()
        # tmp dir uses a filesystem-safe slug since %TEMP% tools dislike spaces/commas
        tmp_slug = self.session_id.replace(", ", "_").replace(" ", "_").replace("\uA789", "-")
        self.tmp_dir = os.path.join(tempfile.gettempdir(), f"gdpnow_nuts_{tmp_slug}")
        self.final_dir = os.path.join(self.base_final_dir, self.session_id)
        try:
            os.makedirs(self.tmp_dir, exist_ok=True)
            os.makedirs(self.final_dir, exist_ok=True)
        except OSError as e:
            print(f"[NutCounter] Could not create dirs: {e}")
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._capture_loop, name="NutCounter", daemon=True)
        self.thread.start()
        print(f"[NutCounter] Started. class={self.HOST_WINDOW_CLASS} grid={self.column_grid}x{self.row_grid} fallback_bbox={self.fallback_bbox} interval={self.capture_interval}s tmp={self.tmp_dir}")

    def _get_font(self, size):
        """Load Arial at the requested size, falling back to PIL default."""
        if size in self._font_cache:
            return self._font_cache[size]
        font = None
        for candidate in _ps.FONT_CANDIDATES:
            try:
                font = ImageFont.truetype(candidate, size)
                break
            except OSError:
                continue
        if font is None:
            try:
                font = ImageFont.load_default()
            except Exception:
                font = None
        self._font_cache[size] = font
        return font

    def _annotate(self, img):
        """Overlay per-cell file number + timestamp labels onto the captured
        image. Reads the global cell_state snapshot at call time. Renders
        the text onto a transparent RGBA overlay and alpha-composites it
        onto the captured image so the labels look subtle instead of
        dominating the frame."""
        try:
            state = _snapshot_cell_state()
            if not state:
                return img
            # Ensure RGBA so we can alpha-composite the label overlay.
            if img.mode != "RGBA":
                img = img.convert("RGBA")
            overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
            draw = ImageDraw.Draw(overlay)
            w, h = img.size
            cell_w = w / self.column_grid
            cell_h = h / self.row_grid
            # "Subtle" sizing: smaller font, tighter padding.
            font_size = max(10, min(24, int(cell_h * 0.04)))
            font = self._get_font(font_size)
            now = time.time()
            pad = max(4, font_size // 3)
            # Semi-transparent colors — readable but not dominating.
            TEXT_RGBA = (255, 255, 255, 180)
            STROKE_RGBA = (0, 0, 0, 140)
            for socket_idx, info in state.items():
                col = socket_idx % self.column_grid
                row = socket_idx // self.column_grid
                if row >= self.row_grid:
                    continue
                # Compute current playback position = start_pos + wall elapsed.
                elapsed = now - info["started_wall"]
                pos_sec = max(0.0, info["started_pos_sec"] + elapsed)
                mm = int(pos_sec) // 60
                ss = int(pos_sec) % 60
                stem = _ps.label_for(info["filename"])
                label = f"{stem}  {mm:d}:{ss:02d}"
                x = col * cell_w + pad
                y = row * cell_h + pad
                try:
                    draw.text(
                        (x, y),
                        label,
                        fill=TEXT_RGBA,
                        font=font,
                        stroke_width=1,
                        stroke_fill=STROKE_RGBA,
                    )
                except TypeError:
                    # Pillow < 8.0 doesn't support stroke_*; fake it with 4-way shadow.
                    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                        draw.text((x + dx, y + dy), label, fill=STROKE_RGBA, font=font)
                    draw.text((x, y), label, fill=TEXT_RGBA, font=font)
            img = Image.alpha_composite(img, overlay)
        except Exception as e:
            print(f"[NutCounter] Annotate error: {e}")
        return img

    def _get_host_bbox(self):
        """Return the current client-rect of GridHost in screen coords,
        or None if not found/usable."""
        try:
            hwnd = win32gui.FindWindow(self.HOST_WINDOW_CLASS, None)
            if not hwnd:
                return None
            if not win32gui.IsWindowVisible(hwnd):
                return None
            if win32gui.IsIconic(hwnd):  # minimized
                return None
            left, top, right, bottom = win32gui.GetClientRect(hwnd)
            tl = win32gui.ClientToScreen(hwnd, (left, top))
            br = win32gui.ClientToScreen(hwnd, (right, bottom))
            if br[0] - tl[0] < 10 or br[1] - tl[1] < 10:
                return None  # degenerate rect
            return (tl[0], tl[1], br[0], br[1])
        except Exception:
            return None

    def _capture_loop(self):
        # First capture immediately so even a 0s session has something.
        self._capture_one()
        while not self.stop_event.is_set():
            if self.stop_event.wait(timeout=self.capture_interval):
                break
            self._capture_one()

    def _capture_one(self):
        if self.tmp_dir is None:
            return
        bbox = self._get_host_bbox()
        if bbox is not None:
            if not self._host_seen:
                print(f"[NutCounter] Host window found after skipping {self._skipped_pre_host} pre-host captures: {bbox}")
                self._host_seen = True
                self._host_first_seen_at = time.monotonic()
            self._last_host_bbox = bbox
            # Startup settle: after host first appears, skip captures
            # until MPV has had a moment to render its first frames. The
            # GridHost window exists before MPV children finish their
            # initial loadfile sequence, so capturing immediately yields
            # black cells. Wait _startup_settle_sec after first sight.
            elapsed_since_host = time.monotonic() - self._host_first_seen_at
            if elapsed_since_host < self._startup_settle_sec:
                self._skipped_pre_host += 1
                return
        else:
            if not self._host_seen:
                # Host never appeared yet — skip this capture entirely so
                # we don't record the terminal / desktop background during
                # the MPV launch + GridHost creation window.
                self._skipped_pre_host += 1
                return
            # Host has been seen before but is momentarily un-findable
            # (maybe minimized during a transition). Use last known rect.
            bbox = self._last_host_bbox
            if bbox is None:
                return
        try:
            img = ImageGrab.grab(bbox=bbox, all_screens=True)
            img = self._annotate(img)
            with self.lock:
                idx = len(self.captures)
            ts = time.time()
            path = os.path.join(self.tmp_dir, f"cap_{idx:05d}.png")
            img.save(path, "PNG", compress_level=1)
            with self.lock:
                self.captures.append((ts, path))
        except Exception as e:
            print(f"[NutCounter] Capture error: {e}")

    def finalize(self):
        """Stop the capture thread, fill to target if short, save final PNGs.
        Idempotent — calling twice is a no-op."""
        with self.lock:
            if self.finalized:
                return
            self.finalized = True

        if self.thread is None:
            # Never started. Nothing to do.
            return

        buf_len = len(self.captures)
        print(f"[NutCounter] Finalizing. {buf_len} captures in buffer.")
        self.stop_event.set()
        try:
            self.thread.join(timeout=2.0)
        except Exception:
            pass

        # Emergency fill: session too short to hit target naturally.
        # Screen is still alive at this point (caller must not have killed MPVs yet).
        emergency_budget = 50  # max extra attempts
        while len(self.captures) < self.target_count and emergency_budget > 0:
            prev = len(self.captures)
            self._capture_one()
            if len(self.captures) == prev:
                break  # capture itself failed, stop trying
            emergency_budget -= 1
            time.sleep(0.05)

        if not self.captures:
            print("[NutCounter] WARNING: no captures collected. Nothing to save.")
            return

        # Select evenly spaced indices.
        n = len(self.captures)
        target = min(self.target_count, n)
        if target == 1:
            selected_indices = [0]
        elif n <= self.target_count:
            selected_indices = list(range(n))
        else:
            selected_indices = [round(i * (n - 1) / (self.target_count - 1)) for i in range(self.target_count)]

        # Save with descriptive filenames.
        session_start_ts = self.captures[0][0]
        saved = 0
        total = len(selected_indices)
        for scene_num, cap_idx in enumerate(selected_indices, start=1):
            ts, src_path = self.captures[cap_idx]
            elapsed = ts - session_start_ts
            wall = datetime.datetime.fromtimestamp(ts).strftime("%H%M%S")
            dest_name = f"scene_{scene_num:02d}_of_{total:02d}_t{elapsed:07.1f}s_{wall}.png"
            dest_path = os.path.join(self.final_dir, dest_name)
            try:
                shutil.copy2(src_path, dest_path)
                saved += 1
            except Exception as e:
                print(f"[NutCounter] Save failed for {dest_name}: {e}")

        print(f"[NutCounter] Saved {saved}/{total} screenshots to {self.final_dir}")

        # Clean up tmp dir (best-effort).
        try:
            for _, p in self.captures:
                try: os.remove(p)
                except OSError: pass
            os.rmdir(self.tmp_dir)
        except Exception:
            pass


nut_counter = NutCounter()


# ============================================================
# E key / CSP swap + GRWM script generator
# ============================================================

_SOURCE_ROOT_DEFAULT = _ps.ROOT


def _resolve_source_path(filename):
    """Resolve a bare filename (e.g. '321.mp4') to an absolute path under
    the source directory. Returns the original if it's already absolute."""
    if os.path.isabs(filename):
        return filename
    return os.path.join(_SOURCE_ROOT_DEFAULT, filename)


CSP_PRE_ROLL_SEC = 2  # start playback this many seconds before the CSP marker


def _send_fresh(pipe_name, cmd_str, timeout=5.0, retries=1):
    """Send a single command to an MPV IPC pipe via a fresh, short-lived
    handle. Bypasses MpvIpcClient entirely so its persistent reader thread
    can't interfere with the write.

    Runs the CreateFile/WriteFile/CloseHandle sequence on an inner daemon
    thread and joins with a timeout. If the inner thread blocks (broken
    pipe, MPV frozen, buffer full, whatever), the caller gets "timeout"
    and can retry up to `retries` times with fresh handles.

    Default timeout bumped from 2.0 -> 5.0 because 2.0 was occasionally
    dropping CSP swap commands on one cell when MPV was briefly slow to
    accept the connection, leaving that cell's cell_history without its
    CSP entry and making the GRWM replay show only one cell swapping.

    Returns: 'ok' | 'timeout' | 'error: <msg>' | 'retry-ok'
    """
    if not cmd_str.endswith("\n"):
        cmd_str += "\n"
    data = cmd_str.encode("utf-8")

    def _do_one_attempt():
        result = {"status": None}
        def _inner():
            handle = None
            try:
                handle = win32file.CreateFile(
                    pipe_name,
                    win32file.GENERIC_WRITE,
                    0,
                    None,
                    win32file.OPEN_EXISTING,
                    0,
                    None,
                )
                win32file.WriteFile(handle, data)
                result["status"] = "ok"
            except pywintypes.error as e:
                result["status"] = f"error: {e}"
            except Exception as e:
                result["status"] = f"error: {e}"
            finally:
                if handle is not None:
                    try:
                        win32file.CloseHandle(handle)
                    except Exception:
                        pass
        t = threading.Thread(target=_inner, daemon=True, name="PipeSend")
        t_start = time.monotonic()
        t.start()
        t.join(timeout)
        elapsed = time.monotonic() - t_start
        if t.is_alive():
            return "timeout", elapsed
        return (result["status"] or "error: unknown"), elapsed

    status, elapsed = _do_one_attempt()
    if status == "ok":
        return status
    if status.startswith("error:"):
        # Hard errors (no such pipe, etc) — no point retrying.
        return status
    # Retry loop only on timeout.
    attempt = 1
    while status == "timeout" and attempt <= retries:
        print(f"[send] {pipe_name}: timeout after {elapsed:.2f}s, retry {attempt}/{retries}", flush=True)
        status, elapsed = _do_one_attempt()
        if status == "ok":
            return "retry-ok"
        attempt += 1
    return status


def _pipe_name_for(socket_idx):
    return rf"\\.\pipe\mpvsocket{socket_idx}"


def adjust_panscan(delta):
    """Set panscan to (current + delta) on every active cell via a fresh
    pipe handle per cell, using 'no-osd set panscan <abs>' (NOT 'add
    panscan <delta>'). The set-absolute pattern is idempotent: if any cell
    missed a previous IPC write — or if MPV reset the property during an
    intervening loadfile — the next press re-synchronizes all cells to the
    same absolute value. 'add' would instead let each cell drift from its
    own possibly-stale local state, producing the visible per-cell size
    asymmetry that we hit in practice.

    The 'no-osd' prefix suppresses MPV's OSD overlay text that would
    otherwise flash on every cell on each press.

    Also updates the module-level session_panscan tracker and appends to
    session_panscan_history so GRWM knows the final panscan state."""
    global sockets, session_panscan, session_panscan_history
    # Track the new absolute value on our side, clamped to MPV's [0, 1].
    new_value = max(0.0, min(1.0, session_panscan + delta))
    if new_value == session_panscan:
        # Already at the clamp wall; don't spam cells with no-op writes.
        return
    session_panscan = new_value
    session_panscan_history.append((time.time(), new_value))
    sent = 0
    failed = 0
    cmd = f"no-osd set panscan {new_value:.4f}"
    for i, client in enumerate(sockets):
        if client is None:
            continue
        status = _send_fresh(_pipe_name_for(i), cmd, timeout=5.0, retries=1)
        if status in ("ok", "retry-ok"):
            sent += 1
        else:
            failed += 1
            print(f"[panscan] socket {i}: {status}", flush=True)
    sign = "+" if delta > 0 else ""
    print(f"[panscan] {sign}{delta:.2f} -> {sent} ok, {failed} failed | session_panscan = {session_panscan:.2f}", flush=True)


def _adjust_video_pan(axis, delta, quiet=False):
    """Set video-pan-<axis> to (current + delta) on every active cell via a
    fresh pipe handle per cell. Uses 'no-osd set' (NOT 'add') for the same
    reason adjust_panscan does: 'add' relies on MPV's local state matching
    session_video_pan_x/y which can drift after an IPC failure or an
    intervening loadfile that resets the property. 'set' is idempotent —
    every keypress re-synchronizes every cell to the same absolute value,
    which guarantees no asymmetric drift between cells.

    `axis` must be 'x' or 'y'. We clamp to [-1.0, 1.0] rather than MPV's
    native [-3.0, 3.0] because anything beyond 1.0 pushes the video into
    empty cell margin (black bars).

    `quiet=True` suppresses the per-call log line — used by the smooth-pan
    animator thread which ticks at PAN_TICK_HZ and would otherwise spam the
    console with 30+ lines per second of sustained hold.
    """
    global sockets, session_video_pan_x, session_video_pan_y, session_video_pan_history
    if axis not in ("x", "y"):
        print(f"[video-pan] invalid axis {axis!r}", flush=True)
        return
    if axis == "x":
        new_value = max(-1.0, min(1.0, session_video_pan_x + delta))
        if new_value == session_video_pan_x:
            return
        session_video_pan_x = new_value
    else:
        new_value = max(-1.0, min(1.0, session_video_pan_y + delta))
        if new_value == session_video_pan_y:
            return
        session_video_pan_y = new_value
    session_video_pan_history.append(
        (time.time(), session_video_pan_x, session_video_pan_y)
    )
    sent = 0
    failed = 0
    cmd = f"no-osd set video-pan-{axis} {new_value:.4f}"
    for i, client in enumerate(sockets):
        if client is None:
            continue
        status = _send_fresh(_pipe_name_for(i), cmd, timeout=5.0, retries=1)
        if status in ("ok", "retry-ok"):
            sent += 1
        else:
            failed += 1
            if not quiet:
                print(f"[video-pan-{axis}] socket {i}: {status}", flush=True)
    if not quiet:
        sign = "+" if delta > 0 else ""
        print(
            f"[video-pan-{axis}] {sign}{delta:.2f} -> {sent} ok, {failed} failed | "
            f"session = ({session_video_pan_x:+.2f}, {session_video_pan_y:+.2f})",
            flush=True,
        )


def _reapply_view_state_to_socket(socket_idx):
    """Re-send the current panscan + video-pan-x/y state to a single cell.
    Called after every loadfile so the newly-loaded video inherits the
    user's current view adjustments even if MPV reset those properties
    across the file load.

    Uses _send_fresh() with a short timeout to match the load path; all
    three writes are fire-and-forget (no per-cell failure logging) because
    a missed write will be corrected on the next user input anyway (the
    set-based adjust_panscan / _adjust_video_pan functions are idempotent).
    """
    try:
        pipe = _pipe_name_for(socket_idx)
        # Only bother re-sending non-default values. Default panscan is 1.0
        # (we launch MPV with --panscan=1), default video-pan-x/y is 0.0.
        if session_panscan != 1.0:
            _send_fresh(pipe, f"no-osd set panscan {session_panscan:.4f}",
                        timeout=2.0, retries=0)
        if session_video_pan_x != 0.0:
            _send_fresh(pipe, f"no-osd set video-pan-x {session_video_pan_x:.4f}",
                        timeout=2.0, retries=0)
        if session_video_pan_y != 0.0:
            _send_fresh(pipe, f"no-osd set video-pan-y {session_video_pan_y:.4f}",
                        timeout=2.0, retries=0)
    except Exception as e:
        print(f"[reapply-view] socket {socket_idx}: {e}", flush=True)


def reset_video_pan():
    """Snap video-pan-x and video-pan-y back to 0 on every active cell.
    Bound to the '0' key as an ergonomic 'recenter' control — manually
    walking back to center with small arrow-key deltas is tedious and
    imprecise, this gives a single-keypress reset.

    Does NOT touch panscan. If you also want to reset zoom, press '0'
    then adjust panscan separately."""
    global sockets, session_video_pan_x, session_video_pan_y, session_video_pan_history
    if session_video_pan_x == 0.0 and session_video_pan_y == 0.0:
        print("[video-pan] already at (0, 0), reset no-op", flush=True)
        return
    session_video_pan_x = 0.0
    session_video_pan_y = 0.0
    session_video_pan_history.append((time.time(), 0.0, 0.0))
    sent_x = 0
    sent_y = 0
    for i, client in enumerate(sockets):
        if client is None:
            continue
        pipe = _pipe_name_for(i)
        sx = _send_fresh(pipe, "no-osd set video-pan-x 0.0000", timeout=5.0, retries=1)
        sy = _send_fresh(pipe, "no-osd set video-pan-y 0.0000", timeout=5.0, retries=1)
        if sx in ("ok", "retry-ok"):
            sent_x += 1
        if sy in ("ok", "retry-ok"):
            sent_y += 1
    print(f"[video-pan] reset to (0, 0) -> {sent_x}/{sent_y} cells ok", flush=True)


def adjust_video_pan_x(delta):
    """Arrow-left/right handler: pan the rendered video horizontally within
    each cell by `delta` fraction of cell width. Positive = pan right (see
    more of the LEFT side of the source), negative = pan left (see more of
    the RIGHT side of the source). Only has visible effect when the video
    is clipped on the sides, i.e. when fill_scale > fit_scale and panscan is
    non-zero, which requires the source aspect to differ from the cell
    aspect (see main()'s landscape crop_width=1.0 override)."""
    _adjust_video_pan("x", delta)


def adjust_video_pan_y(delta):
    """Arrow-up/down handler: pan the rendered video vertically within each
    cell by `delta` fraction of cell height. Only visible when the video is
    clipped top/bottom."""
    _adjust_video_pan("y", delta)


def _pan_animator():
    """Background thread: while any arrow key is held, emit small pan deltas
    at PAN_TICK_HZ so motion looks smooth instead of discrete-step jerky.

    Exits when all arrow keys are released. Only one instance of this
    thread runs at a time (enforced by _pan_thread_lock in _pan_key_down).

    Held state is read under _pan_thread_lock to stay consistent with
    _pan_key_down / _pan_key_up, but the IPC writes themselves happen
    OUTSIDE the lock so a slow _send_fresh doesn't block keydown/keyup
    events (which are on pynput's listener thread)."""
    global _pan_thread
    period = 1.0 / PAN_TICK_HZ
    try:
        while True:
            with _pan_thread_lock:
                if not any(_pan_held.values()):
                    _pan_thread = None
                    return
                # net direction = +sign if held, minus opposite sign if held
                dx = (
                    (PAN_TICK_STEP if _pan_held[("x", +1)] else 0.0)
                    - (PAN_TICK_STEP if _pan_held[("x", -1)] else 0.0)
                )
                dy = (
                    (PAN_TICK_STEP if _pan_held[("y", +1)] else 0.0)
                    - (PAN_TICK_STEP if _pan_held[("y", -1)] else 0.0)
                )
            if dx != 0.0:
                _adjust_video_pan("x", dx, quiet=True)
            if dy != 0.0:
                _adjust_video_pan("y", dy, quiet=True)
            time.sleep(period)
    except Exception as e:
        print(f"[pan-animator] error: {e}", flush=True)
        with _pan_thread_lock:
            _pan_thread = None


def _pan_key_down(axis, sign):
    """Arrow keydown: mark the (axis, sign) direction held and start the
    animator thread if not already running. Re-entrant: OS key-repeat fires
    on_press multiple times while the key is physically held, and those
    repeated calls become no-ops here (held flag already True)."""
    global _pan_thread
    with _pan_thread_lock:
        if _pan_held.get((axis, sign), False):
            return  # OS key-repeat, already tracked
        _pan_held[(axis, sign)] = True
        if _pan_thread is None or not _pan_thread.is_alive():
            t = threading.Thread(
                target=_pan_animator, daemon=True, name="PanAnimator"
            )
            _pan_thread = t
            t.start()


def _pan_key_up(axis, sign):
    """Arrow keyup: clear the (axis, sign) held flag. The animator thread
    will notice on its next tick and exit when all directions are clear."""
    with _pan_thread_lock:
        _pan_held[(axis, sign)] = False


def _arrow_pan_meaningful():
    """Return True if arrow keys would visibly pan the video given the
    CURRENT GridHost client area. Called at every keydown so a mid-session
    resize toggles the gate automatically.

    Regime 1: _pan_ref_cell_aspect is None (landscape override applied)
       Always True — calc_w=1.0 means post-crop aspect is source-derived
       and never matches cell aspect exactly.

    Regime 2: _pan_ref_cell_aspect is a float (prefit formula applied)
       Read the current host client rect, compute current cell aspect,
       compare to the reference. If they differ by more than 2% the user
       has resized non-proportionally and pan is now useful. If they
       match, pan is useless (post-crop is cell-sized -> pan pushes into
       black margin) and we block.

    Regime 3: no reference set yet OR no GridHost yet -> False, block.
    """
    if _pan_ref_cell_aspect is None:
        return True  # landscape sentinel: always on
    try:
        import win32gui
        if not _GRID_HOST_STATE:
            return False
        host_hwnd = next(iter(_GRID_HOST_STATE))
        state = _GRID_HOST_STATE[host_hwnd]
        cfg = state.get("config")
        if cfg is None:
            return False
        rect = win32gui.GetClientRect(host_hwnd)
        host_w = rect[2] - rect[0]
        host_h = rect[3] - rect[1]
        cols = max(1, int(cfg.column_grid))
        rows = max(1, int(cfg.row_grid))
        if host_w <= 0 or host_h <= 0:
            return False
        curr_aspect = (host_w / cols) / (host_h / rows)
        # 2% threshold: tighter than this and cells are within 10 pixels
        # of matching which is essentially "no visible pan range".
        return abs(curr_aspect - _pan_ref_cell_aspect) > 0.02
    except Exception as e:
        print(f"[pan-check] {e}", flush=True)
        return False


def _csp_start_sec_for(socket_idx, filename):
    """Look up CSP start time (with pre-roll) for a given (socket, video)
    from person_socket_map's metadata. Returns the start-second the
    'loadfile start=...' command should use, or None if the video has no
    CSP marker. Used both by the one-shot trigger_csp_swap and by the
    cycle thread's CSP-mode rotation."""
    try:
        for entry in person_socket_map.get(socket_idx, []):
            if filename in entry:
                meta = entry[filename] or {}
                csp_str = meta.get("CSP")
                if not csp_str:
                    return None
                csp_sec = mmss_to_seconds(csp_str)
                return max(0, csp_sec - CSP_PRE_ROLL_SEC)
    except Exception:
        return None
    return None


def trigger_csp_swap():
    """E key handler: for every active cell, send an immediate loadfile to
    that cell's CURRENT video's CSP position (with a small pre-roll), then
    engage e_locked. Once e_locked is set, the cycle thread switches into
    CSP-rotation mode instead of halting — on each subsequent cycle tick it
    advances through the cell's remaining videos (round-robin), loading
    each video's CSP scene in turn. Cells whose current/next videos lack a
    CSP marker are simply skipped on that tick.

    This way, a cell that was originally cycling between persons A/B/C
    continues to cycle after E, but always landing on the CSP moment of
    whichever person is up. Before this change, E froze each cell on its
    ONE current video's CSP and never moved off it.

    Uses _send_fresh() (fresh short-lived pipe handles with a 2s timeout)
    instead of the persistent MpvIpcClient, so a stuck WriteFile on the
    shared reader/writer handle can't hang the CSP swap. Runs on a
    background daemon thread spawned by on_press."""
    global sockets
    print("\n[E] CSP SWAP TRIGGERED ============================", flush=True)
    snap = _snapshot_cell_state()
    print(f"[E] snapshot has {len(snap)} cells", flush=True)
    if not snap:
        print("[E] No cell state — nothing to swap.", flush=True)
        return

    live_slot_count = len(sockets)
    live_slots = [s is not None for s in sockets]
    print(f"[E] live socket slots: {live_slot_count} ({sum(live_slots)} non-None)", flush=True)

    swapped = 0
    skipped = 0
    for socket_idx in sorted(snap.keys()):
        info = snap[socket_idx]
        print(f"[E] -> cell {socket_idx}: {info.get('filename', '?')}", flush=True)

        if socket_idx >= live_slot_count:
            print(f"  Socket {socket_idx}: index out of range (slots={live_slot_count}), skipping.", flush=True)
            skipped += 1
            continue
        if not live_slots[socket_idx]:
            print(f"  Socket {socket_idx}: slot is None, skipping.", flush=True)
            skipped += 1
            continue

        filename = info["filename"]
        meta = info.get("metadata") or {}
        csp_str = meta.get("CSP")
        print(f"  Socket {socket_idx}: meta keys={list(meta.keys())}, CSP={csp_str!r}", flush=True)
        start_sec = _csp_start_sec_for(socket_idx, filename)
        if start_sec is None:
            print(f"  Socket {socket_idx}: no CSP in metadata for {filename}, skipping.", flush=True)
            skipped += 1
            continue
        start_str = seconds_to_mmss(start_sec)
        cmd = _ps.mpv_loadfile_cmd(filename, start_str)
        pipe_name = _pipe_name_for(socket_idx)

        print(f"  Socket {socket_idx}: sending via {pipe_name}: {cmd}", flush=True)
        t0 = time.monotonic()
        status = _send_fresh(pipe_name, cmd, timeout=5.0, retries=1)
        elapsed = time.monotonic() - t0
        print(f"  Socket {socket_idx}: send status = {status} ({elapsed:.2f}s)", flush=True)

        if status in ("ok", "retry-ok"):
            _update_cell_state(socket_idx, filename, start_sec, meta)
            # Re-apply panscan + video-pan state to the freshly loaded
            # video so the CSP swap inherits the current view adjustments.
            _reapply_view_state_to_socket(socket_idx)
            print(f"  Socket {socket_idx}: OK -> CSP @ {csp_str} (start {start_str}, -{CSP_PRE_ROLL_SEC}s)", flush=True)
            swapped += 1
        else:
            skipped += 1

    e_locked.set()
    print(f"[E] Swapped {swapped}, skipped {skipped}. CSP-cycle mode ENGAGED.", flush=True)
    print("[E] Cycle thread will continue rotating through each cell's videos,", flush=True)
    print("[E] loading CSP scenes on every tick. Press Q or ESC to finalize.", flush=True)


def generate_grwm_script(session_dir):
    """Write GRWM grid-replay artifacts to session_dir.

    Recreates the actual grid the user was watching via a 3-stage
    pipeline (inspired by PMVCreator's tile/stack pattern, adapted for
    our continuous panscan + NVENC use case):

      Stage 1: pre-process each sub-scene to a tile mp4
               One ffmpeg per scene (1 input, 1 NVDEC session active at
               a time). Applies session crop + panscan + drawtext. No
               parallelism contention.

      Stage 2: per-cell concat demuxer stream-copy
               ffmpeg -f concat -i cell_N.ffconcat -c copy cell_N.mp4
               No re-encode. Fast.

      Stage 3: xstack the cell mp4s into the final grid
               Only N inputs (= grid cells, usually 2), trivial filter
               chain. tpad applied per-cell if any cell is shorter than
               the full session duration.

    This architecture solves the NVDEC contention problem: the previous
    single-pass approach opened ALL sub-scene inputs simultaneously
    (up to 32 parallel 4K h264 decoders), overloading the GPU.

    Outputs:
      run_grwm.py              — Python runner that orchestrates the 3 stages
      run_grwm.bat             — thin wrapper: 'python run_grwm.py'
      grwm_sequential.ffconcat — fallback simple-stitch concat file
      tiles/                   — stage 1 intermediate tile mp4s (created at runtime)
      cell_N.mp4               — stage 2 per-cell concatenated output (runtime)
      grwm.mp4                 — stage 3 final grid output

    User runs run_grwm.bat (or run_grwm.py directly) to produce grwm.mp4.
    """
    if not session_dir:
        print("[GRWM] No session dir, skipping.")
        return
    history = _snapshot_cell_history()
    if not history:
        print("[GRWM] No history, skipping.")
        return
    try:
        os.makedirs(session_dir, exist_ok=True)
    except OSError as e:
        print(f"[GRWM] Could not ensure session dir: {e}")
        return

    # ---- Resolve grid dimensions ----
    cg = max(1, int(getattr(nut_counter, "column_grid", 1)))
    rg = max(1, int(getattr(nut_counter, "row_grid", 1)))
    grid_size = cg * rg

    # ---- Time anchors ----
    session_start = min(e["wall"] for e in history)
    session_end = time.time()
    total_duration = max(1.0, session_end - session_start)

    # ---- Build merged panscan timeline for time-varying replay ----
    # panscan_events is sorted (wall, value) — the value is active FROM
    # that wall time onward until the next event. Start with a synthetic
    # (session_start, 1.0) anchor so scenes before any +/- press get
    # the default fill behavior.
    panscan_events = [(session_start, 1.0)]
    for entry in session_panscan_history:
        w, v = entry[0], entry[1]
        if w >= session_start:
            panscan_events.append((w, v))
    panscan_events.sort(key=lambda x: x[0])

    def _panscan_at(wall_time):
        """Return the panscan value active at the given wall time."""
        active = 1.0
        for w, v in panscan_events:
            if w <= wall_time:
                active = v
            else:
                break
        return active

    def _fill_class(panscan_value):
        """Classify a panscan value for merge-optimization purposes.
        Quantize to 0.1 increments so adjacent sub-scenes with nearly
        identical panscan values (e.g. 0.87 and 0.85) get merged into
        one ffmpeg input, which drops total input count dramatically
        when the user rapidly presses +/-.

        Granularity of 0.1 means at most 11 distinct classes across the
        [0.0, 1.0] range, so even a user who hammers panscan through
        the full range produces at most ~11 splits per scene instead
        of one per keypress.

        Visual difference between 0.1-class neighbors (say 0.3 vs 0.35)
        is a tiny zoom step — imperceptible at typical cell sizes.
        Continuous interpolation within the same 0.1 bucket is lost,
        which is the trade-off for performance."""
        return round(panscan_value * 10) / 10

    def _split_scene_by_panscan(filename, in_sec, wall_start, wall_end):
        """Split a scene into sub-scenes at panscan change points, then
        merge adjacent sub-scenes in the same fill class so we don't
        spawn a separate ffmpeg -i per tiny panscan tweak that doesn't
        actually change the output. Without this merge, a user who
        rapidly spams +/- during a scene can produce 70+ sub-scenes per
        cell even though they all render identically — that was the
        "142 scenes lags my computer" incident.

        Returns list of {filename, in_sec, duration, panscan}."""
        scene_duration = wall_end - wall_start
        if scene_duration <= 0:
            return []
        current_panscan = _panscan_at(wall_start)
        # Changes strictly inside the window (exclusive of boundaries)
        changes_in_window = [
            (w, v) for w, v in panscan_events
            if wall_start < w < wall_end
        ]
        if not changes_in_window:
            return [{
                "filename": filename,
                "in_sec": in_sec,
                "duration": scene_duration,
                "panscan": current_panscan,
            }]
        # 1. Raw split at every panscan change.
        raw_subs = []
        sub_wall = wall_start
        sub_in = in_sec
        sub_panscan = current_panscan
        for change_wall, new_value in changes_in_window:
            sub_duration = change_wall - sub_wall
            if sub_duration > 0.01:
                raw_subs.append({
                    "filename": filename,
                    "in_sec": sub_in,
                    "duration": sub_duration,
                    "panscan": sub_panscan,
                })
                sub_in += sub_duration
            sub_wall = change_wall
            sub_panscan = new_value
        final_duration = wall_end - sub_wall
        if final_duration > 0.01:
            raw_subs.append({
                "filename": filename,
                "in_sec": sub_in,
                "duration": final_duration,
                "panscan": sub_panscan,
            })
        # 2. Merge adjacent sub-scenes in the same fill class.
        if len(raw_subs) <= 1:
            return raw_subs
        merged = [dict(raw_subs[0])]
        for s in raw_subs[1:]:
            prev = merged[-1]
            if _fill_class(prev["panscan"]) == _fill_class(s["panscan"]):
                # Same filter output — just extend the previous sub-scene's
                # duration. in_sec stays the same (we're reading contiguous
                # source content). The exact panscan value is irrelevant
                # because the filter quantizes it anyway.
                prev["duration"] += s["duration"]
            else:
                merged.append(dict(s))
        return merged

    # ---- Group history by cell, compute per-scene durations ----
    by_socket = {}
    for entry in history:
        by_socket.setdefault(entry["socket_idx"], []).append(entry)

    cell_scenes = {}
    total_panscan_splits = 0
    for socket_idx in range(grid_size):
        entries = by_socket.get(socket_idx, [])
        entries.sort(key=lambda e: e["wall"])
        scenes = []
        for i, e in enumerate(entries):
            wall_start = e["wall"]
            if i + 1 < len(entries):
                wall_end = entries[i + 1]["wall"]
            else:
                wall_end = session_end
            raw_duration = max(0.5, wall_end - wall_start)
            wall_end = wall_start + raw_duration
            # Split this scene at any panscan change points inside its window.
            subs = _split_scene_by_panscan(e["filename"], e["pos_sec"], wall_start, wall_end)
            if len(subs) > 1:
                total_panscan_splits += len(subs) - 1
            scenes.extend(subs)
        cell_scenes[socket_idx] = scenes
    if total_panscan_splits > 0:
        print(f"[GRWM] Time-varying panscan: split {total_panscan_splits} extra sub-scenes across {grid_size} cells")

    # ---- Minimum sub-scene duration pass ----
    # Rapid +/- keypresses can produce sub-scenes as short as 30ms (less
    # than one frame at 29.97 fps). ffmpeg's concat filter then has frame
    # gaps at the boundaries, which xstack composites as visible
    # stutter / frame rate loss on one cell. Collapse sub-scenes below
    # MIN_SCENE_DUR into the previous scene (keeping the previous's
    # panscan value since the short one is imperceptible anyway).
    MIN_SCENE_DUR = 0.5  # seconds — ~15 frames at 30fps, visually meaningful
    total_collapsed = 0
    for socket_idx in range(grid_size):
        scenes = cell_scenes[socket_idx]
        if len(scenes) <= 1:
            continue
        merged = [dict(scenes[0])]
        for s in scenes[1:]:
            if s["duration"] < MIN_SCENE_DUR:
                # Absorb into previous scene (keep previous's panscan,
                # extend its duration by the short scene's duration).
                merged[-1]["duration"] += s["duration"]
                total_collapsed += 1
            else:
                merged.append(dict(s))
        cell_scenes[socket_idx] = merged
    if total_collapsed > 0:
        print(f"[GRWM] Collapsed {total_collapsed} sub-scenes shorter than {MIN_SCENE_DUR}s across {grid_size} cells")

    # ---- Output dimensions ----
    # Match the live session resolution (respects portrait/rotated setups).
    # Falls back to 1920x1080 only if the session globals were never set.
    if session_output_w and session_output_h:
        out_w = session_output_w - (session_output_w % 2)
        out_h = session_output_h - (session_output_h % 2)
    else:
        out_w, out_h = 1920, 1080
    cell_w = (out_w // cg) - ((out_w // cg) % 2)  # even
    cell_h = (out_h // rg) - ((out_h // rg) % 2)  # even
    print(f"[GRWM] Output: {out_w}x{out_h}, cell: {cell_w}x{cell_h}")

    # ---- Crop factors matching the live session ----
    # play_videos snapshots config.crop_width/crop_height into these globals
    # at session start. Applies the same top-aligned crop the live MPV was
    # showing (y=0, horizontally centered) so GRWM matches what the user
    # actually watched.
    cw = session_crop_width if (session_crop_width and session_crop_width > 0.0) else 1.0
    ch = session_crop_height if (session_crop_height and session_crop_height > 0.0) else 1.0
    cw = max(0.0, min(1.0, cw))
    ch = max(0.0, min(1.0, ch))
    if cw < 1.0 or ch < 1.0:
        # Named params + default centered x + explicit y=0 (top-aligned)
        crop_filter = f"crop=w=iw*{cw:.4f}:h=ih*{ch:.4f}:y=0,"
        print(f"[GRWM] Using session crop: w*{cw:.4f}, h*{ch:.4f}, y=0 (top-aligned)")
    else:
        crop_filter = ""
        print("[GRWM] No crop (session_crop_width/height not set or both == 1.0)")

    # ---- Panscan strategy summary ----
    panscan_final = max(0.0, min(1.0, float(session_panscan)))
    print(f"[GRWM] Panscan final={panscan_final:.2f}, timeline events={len(panscan_events)}")

    # ---- Video-pan-x/y final values for replay ----
    # v1: apply FINAL session pan values to every scene (same pattern as the
    # panscan fallback). Time-varying pan replay would require sub-scene
    # splitting at video_pan_x/y change points like the panscan path does,
    # which is a TODO. For now any pan adjustments made during the session
    # appear as their final-state values across the entire replay.
    pan_x_final = max(-1.0, min(1.0, float(session_video_pan_x)))
    pan_y_final = max(-1.0, min(1.0, float(session_video_pan_y)))
    if pan_x_final != 0.0 or pan_y_final != 0.0:
        print(f"[GRWM] Video pan final: x={pan_x_final:+.2f}, y={pan_y_final:+.2f} "
              f"(applied to all scenes; time-varying replay is TODO)")
    if len(session_video_pan_history) > 1:
        print(f"[GRWM] WARNING: {len(session_video_pan_history)} pan events recorded "
              f"but GRWM v1 only replays the FINAL value.")
    if len(panscan_events) > 1:
        # Log each quantized-class change (merge granularity) so the
        # user can see what GRWM actually captured. Continuous panscan
        # means every event is visually distinct, but the merge buckets
        # to 0.1 increments — only class changes produce new sub-scenes.
        prev_class = None
        for w, v in panscan_events:
            klass = round(v * 10) / 10
            if klass != prev_class:
                rel_t = w - session_start
                print(f"[GRWM]   t={rel_t:+6.2f}s panscan={v:.2f} (bucket {klass:.1f})")
                prev_class = klass

    def _panscan_filter_chain(panscan_value):
        """Continuous panscan scale+pad+crop chain for a given panscan in
        [0, 1]. Math (MPV-compatible):
          fit_scale  = min(cell_w/iw, cell_h/ih)   # video fits inside cell
          fill_scale = max(cell_w/iw, cell_h/ih)   # video fills cell
          factor     = fit_scale + p * (fill_scale - fit_scale)

        The scene filter chain prefixes this with a SAR normalization
        step (scale=trunc(ih*dar):ih,setsar=1) so downstream filters
        see square pixels. Then this function's chain:

          1. scale=w=-2:h='ih*factor'
             Compute height from the panscan factor expression, let
             ffmpeg derive width from it with -2 (round to even,
             preserves source DAR). Specifying BOTH dimensions via
             expression produces SAR drift that pad rejects ('Padded
             dimensions cannot be smaller than input dimensions'),
             but -2-derived width keeps SAR within ffmpeg's tolerance.
          2. pad=max(iw, cw):max(ih, ch):centered:black
             Grows shorter dimensions to at least cell size.
          3. crop=cw:ch:centered
             Shrinks overflowing dimensions back to cell size.

        This was a multi-hour debugging rabbit hole. Explained on the
        ComputerBase German ffmpeg forum: scale with both w and h as
        expressions computes each independently and can produce pixel
        DAR that doesn't match source DAR, which scale compensates for
        by stamping a non-1:1 SAR on the output. pad then uses the
        display-adjusted dimensions (iw * sar_num/sar_den) in its
        'output must be >= input' check and rejects valid targets.
        Using -2 for one dimension lets ffmpeg derive it from the
        other with even-rounding, producing dimensions that preserve
        source DAR exactly (SAR stays near 1:1).
        """
        p = max(0.0, min(1.0, float(panscan_value)))
        fit_expr  = f"min({cell_w}/iw,{cell_h}/ih)"
        fill_expr = f"max({cell_w}/iw,{cell_h}/ih)"
        factor_expr = f"({fit_expr}+{p:.3f}*({fill_expr}-{fit_expr}))"
        sh_expr = f"ih*{factor_expr}"
        # Trailing setsar=1 forces all scene streams to exactly SAR 1:1
        # so concat across scenes accepts them — without it, different
        # panscan scenes produce slightly different SAR values (like
        # 687463:687492 vs 144160:144157, both ≈1.0 but not equal) and
        # concat rejects with 'parameters do not match'.
        return (
            f"scale=w=-2:h='{sh_expr}',"
            f"pad=w='max(iw\\,{cell_w})':h='max(ih\\,{cell_h})':"
            f"x='(ow-iw)/2':y='(oh-ih)/2':color=black,"
            f"crop={cell_w}:{cell_h}:(iw-{cell_w})/2:(ih-{cell_h})/2,"
            f"setsar=1"
        )

    # ---- Drawtext font setup for per-scene filename labels ----
    # Filter-complex string escaping: the fontfile path has a colon
    # which is ffmpeg's filter-option separator, so we wrap it in single
    # quotes and escape the colon with a backslash.
    label_font_size = max(18, min(42, int(cell_h * 0.04)))
    label_font_path = _ps.FFMPEG_FONT
    label_pad = max(8, label_font_size // 3)

    # ---- Build SCENES list for the multi-pass runner ----
    # Architecture: 3 stages to avoid N-parallel-NVDEC contention.
    #  Stage 1: one ffmpeg per sub-scene -> small tile mp4 (cell-sized,
    #           pre-cropped, pre-scaled, pre-drawtext'd). 1 NVDEC active
    #           at a time, no parallelism stress.
    #  Stage 2: per-cell concat demuxer stream-copies the tiles into a
    #           single cell_N.mp4 (no re-encode, very fast).
    #  Stage 3: simple xstack of the 2 cell mp4s into the final grid
    #           (only 2 NVDEC sessions, trivial filter chain).
    # Inspired by PMVCreator's tile/stack pattern but adapted: keep
    # continuous panscan per-scene, use input-side -ss (not trim filter),
    # use NVENC throughout.
    SCENES = []  # list of (cell_idx, scene_idx, src_path, start, dur, panscan, pan_x, pan_y, stem)
    cell_scene_total = {}  # cell_idx -> sum of scene durations
    for socket_idx in range(grid_size):
        scenes = cell_scenes.get(socket_idx, [])
        cell_scene_total[socket_idx] = sum(s["duration"] for s in scenes)
        for scene_idx, scene in enumerate(scenes):
            stem = _ps.label_for(scene["filename"])
            SCENES.append((
                socket_idx,
                scene_idx,
                _resolve_source_path(scene["filename"]),
                scene["in_sec"],
                scene["duration"],
                scene.get("panscan", panscan_final),
                scene.get("video_pan_x", pan_x_final),
                scene.get("video_pan_y", pan_y_final),
                stem,
            ))

    # ---- Per-cell tail padding for stage 3 tpad equalization ----
    cell_tail_pad = {}
    for socket_idx in range(grid_size):
        pad_needed = max(0.0, total_duration - cell_scene_total.get(socket_idx, 0.0))
        cell_tail_pad[socket_idx] = pad_needed
        if pad_needed > 0.05:
            print(f"[GRWM] cell {socket_idx}: will tpad {pad_needed:.2f}s at stage 3 "
                  f"(scenes={cell_scene_total[socket_idx]:.2f}s, session={total_duration:.2f}s)")

    # ---- Write the multi-pass Python runner ----
    runner_path = os.path.join(session_dir, "run_grwm.py")
    output_video = os.path.join(session_dir, "grwm.mp4")

    # Escape font path backslashes for ffmpeg filter (fontfile='C\:/...')
    ff_font_path = _ps.FFMPEG_FONT

    try:
        with open(runner_path, "w", encoding="utf-8", newline="\n") as f:
            f.write("# GRWM multi-pass grid replay runner (auto-generated)\n")
            f.write(f"# Session: {os.path.basename(session_dir)}\n")
            f.write(f"# Grid: {cg}x{rg}, {len(SCENES)} scenes, {total_duration:.1f}s total\n")
            f.write("#\n")
            f.write("# Stage 1: pre-process each sub-scene to a tile mp4 (1 input each,\n")
            f.write("#          NVDEC handles it alone, no contention with other scenes).\n")
            f.write("# Stage 2: per-cell concat demuxer stream-copies tiles into one\n")
            f.write("#          cell_N.mp4 per cell (no re-encode).\n")
            f.write("# Stage 3: xstack the cell files into the final grid (only N inputs,\n")
            f.write("#          simple filter chain). tpad applied per-cell if any cell\n")
            f.write("#          runs shorter than session total.\n")
            f.write("import subprocess, sys, os\n\n")
            f.write(f"FFMPEG = {_ps.FFMPEG_EXE!r} if os.path.exists({_ps.FFMPEG_EXE!r}) else 'ffmpeg'\n")
            f.write(f"SESSION_DIR = {session_dir!r}\n")
            f.write(f"OUTPUT      = {output_video!r}\n")
            f.write(f"TILES_DIR   = os.path.join(SESSION_DIR, 'tiles')\n")
            f.write(f"CELL_W, CELL_H = {cell_w}, {cell_h}\n")
            f.write(f"OUT_W, OUT_H   = {out_w}, {out_h}\n")
            f.write(f"COLS, ROWS     = {cg}, {rg}\n")
            f.write(f"FPS            = '30000/1001'\n")
            f.write(f"CW, CH         = {cw:.4f}, {ch:.4f}  # session crop factors\n")
            f.write(f"FONT_PATH      = {ff_font_path!r}\n")
            f.write(f"FONT_SIZE      = {label_font_size}\n")
            f.write(f"LABEL_PAD      = {label_pad}\n")
            f.write(f"TOTAL_DURATION = {total_duration:.3f}\n\n")
            f.write("# Per-cell tail pad (seconds) — applied at stage 3 via tpad.\n")
            f.write(f"CELL_TAIL_PAD  = {cell_tail_pad!r}\n\n")
            f.write("# (cell_idx, scene_idx, src_path, start_sec, dur_sec, panscan, pan_x, pan_y, stem)\n")
            f.write("SCENES = [\n")
            for (c, i, src, start, dur, pan, px, py, stem) in SCENES:
                f.write(
                    f"    ({c}, {i}, {src!r}, {start:.3f}, {dur:.3f}, "
                    f"{pan:.3f}, {px:.3f}, {py:.3f}, {stem!r}),\n"
                )
            f.write("]\n\n")
            f.write("ENC_OPTS = " + _ps.video_enc_opts_literal() + " + [\n")
            f.write("    '-pix_fmt', 'yuv420p',\n")
            f.write("    '-profile:v', 'high',\n")
            f.write("    '-level', '5.1',\n")
            f.write("    '-r', FPS,\n")
            f.write("    '-fps_mode', 'cfr',\n")
            f.write("    # Audio: AAC 192k 48kHz. Consistent across tiles so\n")
            f.write("    # stage 2 concat demuxer stream-copy works. amix at\n")
            f.write("    # stage 3 mixes all cells into the final output.\n")
            f.write("    '-c:a', 'aac',\n")
            f.write("    '-b:a', '192k',\n")
            f.write("    '-ar', '48000',\n")
            f.write("    '-ac', '2',\n")
            f.write("]\n\n")
            f.write("def build_scene_filter(panscan, pan_x, pan_y, stem):\n")
            f.write("    '''Build the per-scene vf chain for stage 1.\n")
            f.write("\n")
            f.write("    pan_x/pan_y are in [-1, 1] (same as live MPV video-pan-x/y) and\n")
            f.write("    shift the crop position within the padded intermediate by\n")
            f.write("    pan * CELL dimension. Positive pan_x = image shifts right in the\n")
            f.write("    cell (equivalent to cropping from a point further LEFT in source).\n")
            f.write("    The crop x/y exprs are clamped to [0, iw-CELL_W] / [0, ih-CELL_H]\n")
            f.write("    so pan values that exceed available headroom in fit-mode don't\n")
            f.write("    trigger an out-of-frame crop error (they just stop panning).\n")
            f.write("    '''\n")
            f.write("    p = max(0.0, min(1.0, panscan))\n")
            f.write("    px = max(-1.0, min(1.0, pan_x))\n")
            f.write("    py = max(-1.0, min(1.0, pan_y))\n")
            f.write("    fit  = f'min({CELL_W}/iw,{CELL_H}/ih)'\n")
            f.write("    fill = f'max({CELL_W}/iw,{CELL_H}/ih)'\n")
            f.write("    factor = f'({fit}+{p:.3f}*({fill}-{fit}))'\n")
            f.write("    crop_pre = f'crop=w=iw*{CW:.4f}:h=ih*{CH:.4f}:y=0,' if (CW < 1.0 or CH < 1.0) else ''\n")
            f.write("    # Pan offset within the padded intermediate. iw/ih here refer to\n")
            f.write("    # the POST-pad dimensions (pad ensures iw >= CELL_W and ih >= CELL_H).\n")
            f.write("    # Default centered crop: x=(iw-CELL_W)/2, y=(ih-CELL_H)/2.\n")
            f.write("    # Pan shifts it by px*CELL_W (negative x = reveal source LEFT side).\n")
            f.write("    px_off = -px * CELL_W\n")
            f.write("    py_off = -py * CELL_H\n")
            f.write("    x_expr = f'max(0\\\\,min(iw-{CELL_W}\\\\,(iw-{CELL_W})/2+{px_off:+.3f}))'\n")
            f.write("    y_expr = f'max(0\\\\,min(ih-{CELL_H}\\\\,(ih-{CELL_H})/2+{py_off:+.3f}))'\n")
            f.write("    return (\n")
            f.write("        f\"scale=trunc(ih*dar):ih,setsar=1,\"\n")
            f.write("        f\"{crop_pre}\"\n")
            f.write("        f\"setpts=PTS-STARTPTS,\"\n")
            # Force the panscan scale's h to an EVEN INTEGER via
            # trunc(expr/2)*2. Without this, the expression result is a
            # float internally and pad's downstream config-time dimension
            # check rejects with 'Padded dimensions cannot be smaller than
            # input dimensions' on certain panscan values where the
            # fractional h confuses the upstream link metadata. Setting
            # SAR to 1:1 alone is NOT enough — verified by isolating the
            # repro on 321.mp4 at panscan=0.85 in /tmp this turn. The
            # trunc-to-even wrapper makes ALL panscan values [0,1] work.
            f.write("        f\"scale=w=-2:h='trunc(ih*{factor}/2)*2',\"\n")
            f.write("        f\"setsar=1,\"\n")
            f.write("        f\"pad=w='max(iw\\\\,{CELL_W})':h='max(ih\\\\,{CELL_H})':x='(ow-iw)/2':y='(oh-ih)/2':color=black,\"\n")
            f.write("        f\"crop={CELL_W}:{CELL_H}:'{x_expr}':'{y_expr}',\"\n")
            f.write("        f\"setsar=1,\"\n")
            f.write("        f\"drawtext=text='{stem}':fontfile='{FONT_PATH}':fontsize={FONT_SIZE}:fontcolor=white@0.7:bordercolor=black@0.55:borderw=2:x={LABEL_PAD}:y={LABEL_PAD}\"\n")
            f.write("    )\n\n")
            f.write("def run_cmd(tag, cmd):\n")
            f.write("    r = subprocess.run(cmd)\n")
            f.write("    if r.returncode != 0:\n")
            f.write("        print(f'ERROR in {tag}: ffmpeg exit {r.returncode}', file=sys.stderr)\n")
            f.write("        sys.exit(r.returncode)\n\n")
            f.write("# ========== Stage 1: Pre-process each sub-scene to a tile mp4 ==========\n")
            f.write("os.makedirs(TILES_DIR, exist_ok=True)\n")
            f.write("tile_paths = {}  # (cell, idx) -> path\n")
            f.write("total = len(SCENES)\n")
            f.write("for n, (cell, idx, src, start, dur, panscan, pan_x, pan_y, stem) in enumerate(SCENES, 1):\n")
            f.write("    tile_path = os.path.join(TILES_DIR, f'tile_c{cell}_s{idx:04d}.mp4')\n")
            f.write("    vf = build_scene_filter(panscan, pan_x, pan_y, stem)\n")
            f.write("    cmd = [\n")
            f.write("        FFMPEG, '-y', '-hide_banner', '-loglevel', 'warning',\n")
            # Limit decode threads to 1 so NVDEC doesn't exceed 32 decode
            # surfaces on 4K H.264 (16 default threads + ref frames = 33).
            # Stage 1 is 1-input-at-a-time so single-thread decode is fine.
            f.write("        '-threads', '1',\n")
            f.write(f"        '-hwaccel', {_ps.hwaccel_name()!r},\n")
            f.write("        '-ss', f'{start:.3f}',\n")
            f.write("        '-t', f'{dur:.3f}',\n")
            f.write("        '-i', src,\n")
            f.write("        '-vf', vf,\n")
            f.write("    ] + ENC_OPTS + [tile_path]\n")
            f.write("    print(f'[Stage1 {n}/{total}] cell={cell} scene={idx} dur={dur:.2f}s panscan={panscan:.2f} pan=({pan_x:+.2f},{pan_y:+.2f})')\n")
            f.write("    run_cmd(f'Stage1 cell{cell} scene{idx}', cmd)\n")
            f.write("    tile_paths[(cell, idx)] = tile_path\n\n")
            f.write("# ========== Stage 2: Per-cell concat demuxer (stream copy) ==========\n")
            f.write("cell_files = []\n")
            f.write("for cell in range(COLS * ROWS):\n")
            f.write("    cell_scenes_sorted = sorted(\n")
            f.write("        [t for t in SCENES if t[0] == cell],\n")
            f.write("        key=lambda x: x[1],\n")
            f.write("    )\n")
            f.write("    if not cell_scenes_sorted:\n")
            f.write("        continue\n")
            f.write("    concat_list = os.path.join(SESSION_DIR, f'cell_{cell}.ffconcat')\n")
            f.write("    cell_file   = os.path.join(SESSION_DIR, f'cell_{cell}.mp4')\n")
            f.write("    with open(concat_list, 'w', encoding='utf-8') as cf:\n")
            f.write("        cf.write('ffconcat version 1.0\\n')\n")
            f.write("        for (c, i, *_) in cell_scenes_sorted:\n")
            f.write("            path = tile_paths[(c, i)].replace('\\\\', '/').replace(\"'\", \"'\\\\''\")\n")
            f.write("            cf.write(f\"file '{path}'\\n\")\n")
            f.write("    print(f'[Stage2] Concat cell {cell} ({len(cell_scenes_sorted)} tiles)')\n")
            f.write("    run_cmd(\n")
            f.write("        f'Stage2 cell {cell}',\n")
            f.write("        [\n")
            f.write("            FFMPEG, '-y', '-hide_banner', '-loglevel', 'warning',\n")
            f.write("            '-f', 'concat', '-safe', '0', '-i', concat_list,\n")
            f.write("            '-c', 'copy', cell_file,\n")
            f.write("        ],\n")
            f.write("    )\n")
            f.write("    cell_files.append((cell, cell_file))\n\n")
            f.write("# ========== Stage 3: xstack cell files into final grid ==========\n")
            f.write("stack_cmd = [FFMPEG, '-y', '-hide_banner', '-loglevel', 'info']\n")
            f.write("for _, cf in cell_files:\n")
            f.write("    stack_cmd += ['-i', cf]\n")
            f.write("# Build per-cell tpad chain if any cell is short.\n")
            f.write("cell_inputs = []\n")
            f.write("pre_chains = []\n")
            f.write("for input_idx, (cell, _) in enumerate(cell_files):\n")
            f.write("    pad_needed = CELL_TAIL_PAD.get(cell, 0.0)\n")
            f.write("    if pad_needed > 0.05:\n")
            f.write("        lbl = f'[p{input_idx}]'\n")
            f.write("        pre_chains.append(f'[{input_idx}:v]tpad=stop_duration={pad_needed:.3f}:stop_mode=add:color=black{lbl}')\n")
            f.write("        cell_inputs.append(lbl)\n")
            f.write("    else:\n")
            f.write("        cell_inputs.append(f'[{input_idx}:v]')\n")
            f.write("layout_parts = []\n")
            f.write("for i, _ in enumerate(cell_files):\n")
            f.write("    col = i % COLS\n")
            f.write("    row = i // COLS\n")
            f.write("    layout_parts.append(f'{col*CELL_W}_{row*CELL_H}')\n")
            f.write("layout = '|'.join(layout_parts)\n")
            f.write("xstack_in = ''.join(cell_inputs)\n")
            f.write("if len(cell_files) == 1:\n")
            f.write("    # Grid is 1x1, no xstack needed — just rename video and\n")
            f.write("    # use input 0's audio directly.\n")
            f.write("    xstack_line = f'{cell_inputs[0]}fps={FPS},setpts=PTS-STARTPTS[out]'\n")
            f.write("    audio_map = '0:a'\n")
            f.write("    audio_chain = None\n")
            f.write("else:\n")
            f.write("    xstack_line = (\n")
            f.write("        f'{xstack_in}xstack=inputs={len(cell_files)}:layout={layout}:fill=black,'\n")
            f.write("        f'fps={FPS},setpts=PTS-STARTPTS[out]'\n")
            f.write("    )\n")
            f.write("    # amix all cells' audio streams. duration=longest so no\n")
            f.write("    # cell's audio gets cut off. dropout_transition=0 avoids\n")
            f.write("    # audible fade at the tail if one cell ends early.\n")
            f.write("    audio_inputs = ''.join(f'[{i}:a]' for i in range(len(cell_files)))\n")
            f.write("    audio_chain = (\n")
            f.write("        f'{audio_inputs}amix=inputs={len(cell_files)}:'\n")
            f.write("        f'duration=longest:dropout_transition=0[aout]'\n")
            f.write("    )\n")
            f.write("    audio_map = '[aout]'\n")
            f.write("video_parts = pre_chains + [xstack_line]\n")
            f.write("if audio_chain is not None:\n")
            f.write("    filter_complex = ';'.join(video_parts + [audio_chain])\n")
            f.write("else:\n")
            f.write("    filter_complex = ';'.join(video_parts)\n")
            f.write("stack_cmd += [\n")
            f.write("    '-filter_complex', filter_complex,\n")
            f.write("    '-map', '[out]',\n")
            f.write("    '-map', audio_map,\n")
            f.write("] + ENC_OPTS + [OUTPUT]\n")
            f.write("print(f'[Stage3] Stacking {len(cell_files)} cells via xstack')\n")
            f.write("run_cmd('Stage3 xstack', stack_cmd)\n")
            f.write("print('[OK] grwm.mp4 produced')\n")
    except OSError as e:
        print(f"[GRWM] Failed to write runner: {e}")
        return

    # ---- Write thin .bat wrapper for double-click convenience ----
    # The bat file must be pure ASCII because cmd.exe reads batch files in
    # the active code page (typically cp1252 on Windows) — if the file
    # contains U+A789 "꞉" from the session folder name, cmd can't parse
    # the affected lines and you get "The system cannot find the path
    # specified." errors. %~dp0 is a runtime wide-char expansion that
    # produces the correct unicode path without appearing in the file
    # source, so the bat file stays ASCII-clean.
    bat_path = os.path.join(session_dir, "run_grwm.bat")
    try:
        with open(bat_path, "w", encoding="ascii", errors="replace", newline="\r\n") as f:
            f.write("@echo off\r\n")
            f.write(f"REM GRWM grid replay - {cg}x{rg} grid, {len(SCENES)} scenes\r\n")
            f.write("REM %~dp0 = this bat file's own dir, handles unicode paths correctly\r\n")
            f.write(f"\"{_ps.python_exe_for_bat()}\" \"%~dp0run_grwm.py\"\r\n")
            f.write("pause\r\n")
    except OSError as e:
        print(f"[GRWM] Failed to write bat: {e}")

    # ---- Also write a sequential fallback ffconcat ----
    seq_concat_path = os.path.join(session_dir, "grwm_sequential.ffconcat")
    try:
        flat_scenes = []
        for socket_idx, scenes in cell_scenes.items():
            for s in scenes:
                flat_scenes.append((socket_idx, s))
        # Sort by the wall time of the original entry
        flat_scenes_with_wall = []
        for socket_idx, s in flat_scenes:
            for e in by_socket.get(socket_idx, []):
                if e["filename"] == s["filename"] and abs(e["pos_sec"] - s["in_sec"]) < 0.01:
                    flat_scenes_with_wall.append((e["wall"], socket_idx, s))
                    break
        flat_scenes_with_wall.sort(key=lambda x: x[0])
        with open(seq_concat_path, "w", encoding="utf-8", newline="\n") as f:
            f.write("ffconcat version 1.0\n")
            f.write(f"# GRWM sequential fallback - {len(flat_scenes_with_wall)} scenes\n")
            f.write("# Use this if grid replay (run_grwm.bat) is failing.\n")
            for idx, (wall, socket_idx, s) in enumerate(flat_scenes_with_wall):
                src = _resolve_source_path(s["filename"]).replace("\\", "/").replace("'", "'\\''")
                f.write(f"# scene {idx+1}: socket {socket_idx} -> {s['filename']} "
                        f"@ {s['in_sec']:.1f}s for {s['duration']:.1f}s\n")
                f.write(f"file '{src}'\n")
                f.write(f"inpoint {s['in_sec']:.3f}\n")
                f.write(f"outpoint {s['in_sec'] + s['duration']:.3f}\n")
    except OSError as e:
        print(f"[GRWM] Failed to write sequential fallback: {e}")

    print(f"[GRWM] Grid replay: {cg}x{rg} grid, {len(SCENES)} scenes, {total_duration:.1f}s")
    print(f"[GRWM]   Runner: {bat_path} (or run_grwm.py)")
    print(f"[GRWM]   Sequential fallback: {seq_concat_path}")
    _ps.session_finished(session_dir)


def play_videos(config, video_list):
    """Starts MPV instances based on config and video list, prepares for cycling."""
    global sockets, processes, person_socket_map, interrupt_event
    global session_crop_width, session_crop_height
    global session_output_w, session_output_h
    global session_panscan, session_panscan_history

    # Snapshot the live crop factors so GRWM can reproduce the same view.
    session_crop_width = float(config.crop_width) if config.crop_width else 0.0
    session_crop_height = float(config.crop_height) if config.crop_height else 0.0
    # Reset panscan state for this session and seed history with the
    # default value so GRWM knows what was active before any +/- press.
    session_panscan = 1.0
    session_panscan_history = [(time.time(), 1.0)]
    # Snapshot the actual monitor/session dimensions so GRWM output matches
    # the real resolution (important for portrait/rotated setups).
    try:
        session_output_w = int(config.input_geometry[0])
        session_output_h = int(config.input_geometry[1])
    except Exception:
        session_output_w = None
        session_output_h = None

    # --- Reset global state ---
    print("Initializing playback...")
    if sockets or processes:
        print("Warning: play_videos called with existing sockets/processes. Cleaning up first.")
        cleanup_resources()

    sockets = []
    processes = []
    person_socket_map = {}
    interrupt_event.clear()

    # --- Validate Input ---
    if not video_list:
        print("Error: Video list is empty. Cannot start playback.")
        return
    if config.column_grid <= 0 or config.row_grid <= 0:
        print("Error: column_grid and row_grid must be positive.")
        return
    if config.cycled_persons < 1:
        print("Warning: cycled_persons is less than 1. Setting to 1.")
        config.cycled_persons = 1

    # --- Filter and Prepare Videos ---
    grid_size = config.column_grid * config.row_grid
    print(f"Grid size: {config.column_grid}x{config.row_grid} = {grid_size} windows.")
    print(f"Master type selected: {config.master_type}")
    print(f"Persons to cycle per window: {config.cycled_persons}")

    valid_videos = []
    for filename, metadata in video_list:
        if isinstance(metadata, dict) and config.master_type in metadata:
            valid_videos.append((filename, metadata))
        else:
            print(f"Skipping video '{filename}': Missing master_type '{config.master_type}' in metadata or metadata is not a dict.")

    if not valid_videos:
        print(f"Error: No videos found with the required master_type '{config.master_type}'. Cannot start playback.")
        return

    print(f"Found {len(valid_videos)} videos matching master_type '{config.master_type}'.")

    required_videos_count = grid_size * config.cycled_persons
    if len(valid_videos) < required_videos_count:
        original_cycled_persons = config.cycled_persons
        if grid_size > 0:
            config.cycled_persons = max(1, len(valid_videos) // grid_size)
            required_videos_count = grid_size * config.cycled_persons
            print(f"Warning: Not enough videos ({len(valid_videos)}) for {original_cycled_persons} cycles per window.")
            print(f"Adjusting cycled_persons to {config.cycled_persons} based on available videos.")
        else:
            print("Error: grid_size is zero, cannot distribute videos.")
            return
        valid_videos = valid_videos[:required_videos_count]
        if not valid_videos and required_videos_count > 0:
             print("Error: No videos available even after adjusting cycle count.")
             return
    elif len(valid_videos) > required_videos_count:
        print(f"Selecting the first {required_videos_count} valid videos for playback.")
        valid_videos = valid_videos[:required_videos_count]

    for i in range(grid_size):
        person_socket_map[i] = []

    for idx, (filename, metadata) in enumerate(valid_videos):
        socket_index = idx % grid_size
        person_socket_map[socket_index].append({filename: metadata})

    print("\nVideo Distribution (person_socket_map):")
    for k, v in person_socket_map.items():
        video_count = len(v)
        video_names = [list(vid.keys())[0] for vid in v[:3]]
        etc = "..." if video_count > 3 else ""
        print(f"  Socket Index {k}: {video_count} videos assigned. (e.g., {video_names}{etc})")

    # --- GridHost first: its cells are where each mpv will draw (--wid) ---
    grid_cells = _start_grid_host_with_cells(config) if config.production else None

    # --- Calculate Durations and Start Initial MPV Instances ---
    print("\nCalculating durations and starting initial MPV instances...")
    successful_starts = 0
    for i in range(grid_size):
        geometry = calculate_geometry(i, config)
        print(f"  Processing Socket Index {i} (Geometry: {geometry})")

        if not person_socket_map.get(i):
             print(f"    Warning: No videos assigned to socket index {i}. Skipping this grid cell.")
             sockets.append(None)
             processes.append(None)
             continue

        first_video_info = person_socket_map[i][0]
        first_filename = list(first_video_info.keys())[0]
        first_metadata = first_video_info[first_filename]
        first_start_time_str = first_metadata.get(config.master_type, "00:00")

        if not config.test_mode:
             for video_dict in person_socket_map[i]:
                filename, metadata = list(video_dict.items())[0]
                try:
                    end_time_key = "E" + config.master_type
                    start_time_key = config.master_type
                    if end_time_key in metadata and start_time_key in metadata:
                        metadata["videoTime"] = get_duration_of_session(metadata[end_time_key], metadata[start_time_key])
                    else:
                        metadata["videoTime"] = config.default_duration_map.get(config.master_type, 60)
                except Exception as e:
                    print(f"      Error calculating duration for video {filename} on socket {i}: {e}")
                    metadata["videoTime"] = config.default_duration_map.get(config.master_type, 60)

        command_list = create_mpv_command(first_filename, first_start_time_str, geometry, config, i,
                                          wid=grid_cells[i] if grid_cells else None)
        # Record what this cell is showing for NutCounter label overlay
        # and GRWM scene history. Pass full metadata so E-key CSP swap
        # can find the CSP time for this video later.
        _update_cell_state(i, first_filename, mmss_to_seconds(first_start_time_str), first_metadata)
        process = None
        if config.production and command_list:
            print(f"    Starting MPV for: {first_filename}")
            process = start_mpv_instance(command_list)
            if process is None:
                 print(f"    Error: Failed to start MPV instance for socket {i}. Skipping socket.")
                 sockets.append(None)
                 processes.append(None)
                 continue
            processes.append(process)
        elif not config.production:
             print(f"    (Production=False) Skipping MPV start for socket {i}.")
             processes.append(None)
        elif not command_list:
             print(f"    Error: Failed to create MPV command for socket {i}. Skipping socket.")
             sockets.append(None)
             processes.append(None)
             continue

        if config.production and process:
             try:
                 try:
                     socket_handle = create_socket(i, process=process)
                 except FileNotFoundError:
                     # An mpv sometimes hung before opening its pipe (seen when a
                     # session started right after another); the cell then stayed
                     # black. Start that one mpv once more.
                     print(f"    No answer from mpv on socket {i}; starting it again.")
                     if process.poll() is None:
                         process.kill()
                         try:
                             process.wait(timeout=5)
                         except subprocess.TimeoutExpired:
                             pass
                     process = start_mpv_instance(command_list)
                     processes[-1] = process
                     if process is None:
                         raise FileNotFoundError(f"mpv for socket {i} did not start again")
                     socket_handle = create_socket(i, process=process, timeout=20.0)
                 sockets.append(socket_handle)
                 successful_starts += 1
             except FileNotFoundError as e:
                 print(f"    Error creating/connecting to socket {i}: {e}.")
                 sockets.append(None)
                 if process and process.poll() is None:
                      process.terminate()
                      try:
                          process.wait(timeout=1)
                      except subprocess.TimeoutExpired:
                          process.kill()
                 if len(processes) > i and processes[i] == process:
                     processes[i] = None
                 continue
        elif not config.production:
              print(f"    (Production=False) Skipping socket connection for socket {i}.")
              sockets.append(None)

    # --- Post-Start Summary ---
    print(f"\n--- Initialization Summary ---")
    print(f"Successfully started and connected {successful_starts} out of {grid_size} MPV instances.")
    active_socket_indices = [idx for idx, s in enumerate(sockets) if s is not None]
    print(f"Active socket indices: {active_socket_indices}")

    try:
        mpv_pids = [p.pid for p in processes if p]
        if grid_cells:
            pass  # mpv already draws inside the GridHost cells
        elif mpv_pids:
            host_thread = threading.Thread(
                target=_grid_host_thread,
                args=(config, mpv_pids),
                name="GridHostThread",
                daemon=True,
            )
            host_thread.start()
        else:
            print("GridHost: No MPV PIDs available, not starting host.")
    except Exception as e:
        print(f"GridHost: failed to launch host thread: {e}")

    if successful_starts == 0 and grid_size > 0:
         print("\nError: No MPV instances could be started successfully. Exiting.")
         cleanup_resources()
         return

    # --- Start NutCounter ---
    try:
        bbox = (
            int(config.monitor_offset[0]),
            int(config.monitor_offset[1]),
            int(config.monitor_offset[0] + config.input_geometry[0]),
            int(config.monitor_offset[1] + config.input_geometry[1]),
        )
        nut_counter.start(bbox, column_grid=config.column_grid, row_grid=config.row_grid)
    except Exception as e:
        print(f"[NutCounter] Failed to start: {e}")

    # --- Main Application Loop ---
    if config.production:
        print("\nProduction mode started. Press 'q' or 'ESC' for immediate shutdown.")
        
        # Start the keyboard listener for ALL production modes (cycling or not)
        listener_thread = threading.Thread(target=run_listener, name="KeyListenerThread")
        listener_thread.daemon = True
        listener_thread.start()

        # Conditionally start the cycling thread if needed
        should_cycle = config.cycled_persons > 1 and grid_size > 0
        can_cycle = any(len(person_socket_map.get(idx, [])) > 1 for idx in active_socket_indices)
        if should_cycle and can_cycle:
            print("Cycling is enabled and possible. Starting cycling thread...")
            cycle_thread = threading.Thread(target=cycle_videos, args=(config,), name="CycleThread")
            cycle_thread.daemon = True
            cycle_thread.start()
        else:
            print("Running in non-cycling mode.")

        # The main thread waits here until the listener is stopped (by 'q' or 'esc').
        # When the key is pressed, force_shutdown_and_exit() is called, which terminates the script.
        listener_thread.join()
        
        # This part is unlikely to be reached due to os._exit(0) in the shutdown function,
        # but it's here for logical completeness.
        print("Listener has stopped. Main thread proceeding.")
        interrupt_event.set()

    else: # Not in production mode
         print("\nRunning in non-production, non-cycling mode. Setup complete.")
         print("Press Ctrl+C to exit.")
         try:
             while not interrupt_event.is_set():
                 time.sleep(1)
         except KeyboardInterrupt:
              print("\nCtrl+C detected. Exiting.")
         finally:
             cleanup_resources()

    print("Playback finished or interrupted.")
    cleanup_resources()


def run_listener():
    """Runs the pynput keyboard listener."""
    with Listener(on_press=on_press, on_release=on_release) as listener:
        print("Keyboard listener started.")
        listener.join() # Blocks until listener stops (returns False from callback)
    print("Keyboard listener stopped.")


def mmss_to_seconds(time_str: str) -> int:
    """
    Convert 'MM:SS' (where MM can be >= 60) into total seconds.
    Returns 0 on parse error.
    """
    try:
        parts = time_str.strip().split(":")
        if len(parts) != 2:
            return 0
        minutes = int(parts[0])
        seconds = int(parts[1])
        return max(0, minutes * 60 + seconds)
    except Exception:
        return 0


def seconds_to_mmss(total_seconds: int) -> str:
    """
    Convert seconds to 'MM:SS' (minutes not capped at 59).
    """
    if total_seconds < 0:
        total_seconds = 0
    minutes = total_seconds // 60
    seconds = total_seconds % 60
    return f"{minutes:02d}:{seconds:02d}"


def get_duration_of_session(time1_str: str, time2_str: str) -> int:
    """
    Duration in seconds between two 'MM:SS' strings (time1 - time2), clamped at 0.
    This works fine even for values like '60:59'.
    """
    t1 = mmss_to_seconds(time1_str)
    t2 = mmss_to_seconds(time2_str)
    return max(0, t1 - t2)


def cycle_videos(config):
    """
    Round-robin *segment* cycling using ephemeral Win32 pipes:

      - Does NOT reuse startup sockets[] handles.
      - For each switch & socket index:
          CreateFile(\\.\pipe\mpvsocket{idx}) -> WriteFile("loadfile ...") -> CloseHandle.
      - Segments are evenly spread inside each video's [master .. E{master}] window:
          * No negative start
          * No overshoot beyond end
          * Initial playback is treated as the first occurrence for socket's first video.

    Requires:
      - global person_socket_map : {socket_idx: [ {filename: metadata}, ... ]}
      - metadata["videoTime"] in seconds for the chosen master_type window.
      - global interrupt_event
      - mmss_to_seconds / seconds_to_mmss helpers already defined.
    """
    import math
    import time
    import win32file
    import pywintypes

    global person_socket_map, interrupt_event

    print("Cycle Thread: Started (RR segment mode, ephemeral Win32 pipes).")

    master_key = config.master_type
    interval = config.interval_cycle
    total_time = config.total_time

    if interval <= 0 or total_time <= 0:
        print("Cycle Thread: Invalid interval/total_time. Exiting.")
        return

    # Determine sockets which actually have videos
    active_socket_indices = sorted(
        idx for idx, lst in person_socket_map.items()
        if isinstance(lst, list) and len(lst) > 0
    )
    if not active_socket_indices:
        print("Cycle Thread: No active sockets in person_socket_map. Exiting.")
        return

    num_intervals = math.ceil(total_time / interval)  # includes initial interval
    num_switches = max(0, num_intervals - 1)

    print(f"Cycle Thread: Total time={total_time}s, interval={interval}s")
    print(f"Cycle Thread: {num_intervals} intervals, switches at 1..{num_switches}")
    print(f"Cycle Thread: Active sockets: {active_socket_indices}")

    if num_switches == 0:
        print("Cycle Thread: Not enough time for even one switch. Exiting.")
        return

    # --- Build per-socket list of videos with base & duration info ---
    #
    # per_socket_videos[socket_idx] = [
    #   {"filename": ..., "base_str": "MM:SS", "base_sec": int, "video_time": int}, ...
    # ]
    per_socket_videos = {}

    for socket_idx in active_socket_indices:
        entries = person_socket_map.get(socket_idx, [])
        vids = []
        for entry in entries:
            filename, meta = list(entry.items())[0]

            base_str = meta.get(master_key, "00:00")
            base_sec = mmss_to_seconds(base_str)
            # videoTime was computed earlier in play_videos; fallback to default if missing
            video_time = int(meta.get("videoTime", config.default_duration_map.get(master_key, interval)))

            vids.append({
                "filename": filename,
                "base_str": base_str,
                "base_sec": max(0, base_sec),
                "video_time": max(0, video_time),
            })

        if vids:
            per_socket_videos[socket_idx] = vids
            print(f"Cycle Thread: Socket {socket_idx} has {len(vids)} videos (RR).")

    if not per_socket_videos:
        print("Cycle Thread: No videos with valid metadata to cycle. Exiting.")
        return

    # --- Precompute segment offsets per (socket, video) ---
    #
    # approx_occ ≈ total times this video will appear across the session:
    #   approx_occ ≈ num_intervals / num_videos_on_socket
    # Initial playback counts as occurrence #0 for socket's first video.
    segment_offsets = {}      # (socket_idx, vid_idx) -> [offset_in_seconds, ...]
    approx_occurrences = {}   # (socket_idx, vid_idx) -> approx_occ

    for socket_idx, vids in per_socket_videos.items():
        n = len(vids)
        if n == 0:
            continue

        # Total appearances across full session (including initial interval)
        approx_occ = math.ceil(num_intervals / n)
        if approx_occ < 1:
            approx_occ = 1

        for vid_idx, v in enumerate(vids):
            video_time = v["video_time"]

            if video_time <= 0 or interval <= 0:
                offsets = [0]
                segment_offsets[(socket_idx, vid_idx)] = offsets
                approx_occurrences[(socket_idx, vid_idx)] = 1
                continue

            # we need start ∈ [0, video_time - interval]
            max_start = max(0, video_time - interval)

            if max_start == 0:
                # video shorter than or equal to interval: always start at 0
                offsets = [0] * approx_occ
            elif approx_occ == 1:
                # only once: put it near the middle of its window
                offsets = [max_start // 2]
            else:
                # spread approx_occ samples between 0 and max_start (inclusive)
                step = max_start / (approx_occ - 1)
                offsets = [int(round(i * step)) for i in range(approx_occ)]

            segment_offsets[(socket_idx, vid_idx)] = offsets
            approx_occurrences[(socket_idx, vid_idx)] = approx_occ

    # --- Track how many times each (socket, video) has been used ---
    #
    # Important: the *initial* MPV start already used video index 0 on each socket
    # once, at its base time. So we mark that occurrence as consumed.
    usage_counts = {}  # (socket_idx, vid_idx) -> count of times used so far

    for socket_idx, vids in per_socket_videos.items():
        if not vids:
            continue
        key0 = (socket_idx, 0)
        usage_counts[key0] = 1  # initial occurrence already used at base_sec

    start_mono = time.monotonic()
    print(f"Cycle Thread: Execution loop starting at ~{time.strftime('%Y-%m-%d %H:%M:%S')}")

    # --- Track dead pipes so we don't spam errors forever ---
    dead_sockets = set()

    # --- Helper: send a plain-text command via a fresh pipe handle ---
    def send_command_to_socket_index(socket_idx: int, cmd: str):
        if socket_idx in dead_sockets:
            return False

        pipe_name = fr"\\.\pipe\mpvsocket{socket_idx}"
        try:
            handle = win32file.CreateFile(
                pipe_name,
                win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                win32file.FILE_SHARE_READ | win32file.FILE_SHARE_WRITE | win32file.FILE_SHARE_DELETE,
                None,
                win32file.OPEN_EXISTING,
                0,
                None,
            )
        except pywintypes.error as e:
            print(f"    Socket {socket_idx}: Cannot open pipe {pipe_name}: {e}")
            dead_sockets.add(socket_idx)
            return False

        try:
            if not cmd.endswith("\n"):
                cmd_to_send = cmd + "\n"
            else:
                cmd_to_send = cmd
            win32file.WriteFile(handle, cmd_to_send.encode("utf-8"))
        except pywintypes.error as e:
            print(f"    Socket {socket_idx}: ERROR writing to {pipe_name}: {e}")
            dead_sockets.add(socket_idx)
            return False
        finally:
            try:
                win32file.CloseHandle(handle)
            except Exception:
                pass

        return True

    # --- Main switching loop ---
    for switch_idx in range(1, num_switches + 1):
        if interrupt_event.is_set():
            print("Cycle Thread: Interrupt detected before switch. Exiting.")
            break

        # Sleep until the next nominal switch time
        target = start_mono + switch_idx * interval
        now = time.monotonic()
        wait = target - now
        if wait > 0:
            if interrupt_event.wait(timeout=wait):
                print("Cycle Thread: Interrupt detected during sleep. Exiting.")
                break

        wall_ts = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"--- Cycle Switch {switch_idx}/{num_switches} at ~{wall_ts} ---")

        # E key was pressed — CSP-cycle mode: keep advancing through the
        # per-socket video rotation on each tick, but load each video's
        # CSP position instead of a scheduled RR offset. If a given video
        # lacks a CSP marker, skip that cell's swap for this tick.
        csp_mode = e_locked.is_set()
        if csp_mode:
            print(f"Cycle Thread: e_locked engaged, CSP-cycling (switch {switch_idx}).")

        for socket_idx in active_socket_indices:
            if socket_idx in dead_sockets:
                continue

            vids = per_socket_videos.get(socket_idx)
            if not vids:
                continue
            n = len(vids)
            if n == 0:
                continue

            # Round-robin across vids on this socket.
            # First switch picks vid 1 when n>1 (so we don't repeat vid 0 immediately).
            vid_idx = switch_idx % n
            v = vids[vid_idx]

            if csp_mode:
                csp_start = _csp_start_sec_for(socket_idx, v["filename"])
                if csp_start is None:
                    # No CSP for this video — leave the cell on whatever
                    # it was last showing (don't swap on this tick).
                    print(
                        f"    Socket {socket_idx}: no CSP for '{v['filename']}', "
                        f"holding on current video (CSP-cycle).",
                        flush=True,
                    )
                    continue
                final_start_sec = csp_start
                final_start_str = seconds_to_mmss(csp_start)
                debug_info = f"CSP @ {final_start_str} (-{CSP_PRE_ROLL_SEC}s pre-roll)"
            else:
                key = (socket_idx, vid_idx)
                offsets = segment_offsets.get(key, [0])
                occ_limit = approx_occurrences.get(key, len(offsets))

                used_so_far = usage_counts.get(key, 0)
                occ_index = used_so_far
                if occ_index >= len(offsets):
                    occ_index = len(offsets) - 1  # clamp

                offset_in_window = offsets[occ_index]
                final_start_sec = v["base_sec"] + offset_in_window
                final_start_str = seconds_to_mmss(final_start_sec)

                # bump usage counter
                usage_counts[key] = used_so_far + 1

                debug_info = (
                    f"video_time={v['video_time']}s, approx_occ={occ_limit}, "
                    f"offset={offset_in_window}s, occ_index={occ_index}"
                )

            print(
                f"    Socket {socket_idx}: load '{v['filename']}' at {final_start_str} "
                f"({'CSP-cycle' if csp_mode else 'RR segment'}: {debug_info})"
            )

            cmd = _ps.mpv_loadfile_cmd(v["filename"], final_start_str)
            send_command_to_socket_index(socket_idx, cmd)
            # Re-apply user's current view adjustments (panscan + video-pan-x/y)
            # so the newly-loaded video starts at the same zoom/center the
            # user had before the swap. MPV's default loadfile-replace may
            # reset these properties depending on version/config — reapplying
            # guarantees per-cell consistency regardless.
            _reapply_view_state_to_socket(socket_idx)
            # Record new state for NutCounter + GRWM. Pull full metadata
            # from person_socket_map (v dict only has filename/base_sec).
            scene_metadata = None
            try:
                for entry in person_socket_map.get(socket_idx, []):
                    if v["filename"] in entry:
                        scene_metadata = entry[v["filename"]]
                        break
            except Exception:
                pass
            _update_cell_state(socket_idx, v["filename"], mmss_to_seconds(final_start_str), scene_metadata)

        # 🔥 If everything is dead, just bail out
        if len(dead_sockets) == len(active_socket_indices):
            print("Cycle Thread: All pipes dead, exiting early.")
            break

        if interrupt_event.is_set():
            print("Cycle Thread: Interrupt detected after switch. Exiting.")
            break

    print("Cycle Thread: Finished all switches or interrupted. Exiting.")






def cleanup_resources():
    """Closes all IPC clients and terminates all MPV processes gracefully."""
    global sockets, processes, interrupt_event
    print("--- Starting Resource Cleanup ---")
    # Ensure interrupt is set to signal other threads (like cycle_videos)
    if not interrupt_event.is_set():
        print("Setting interrupt event during cleanup...")
        interrupt_event.set()

    # Save nut screenshots before killing MPVs (idempotent — safe if
    # force_shutdown_and_exit already called this).
    try:
        nut_counter.finalize()
    except Exception as e:
        print(f"[NutCounter] Finalize failed in cleanup: {e}")

    # Generate GRWM script (concat file + bat runner) alongside screenshots.
    try:
        generate_grwm_script(nut_counter.final_dir)
    except Exception as e:
        print(f"[GRWM] Generate failed in cleanup: {e}")

    # --- Close IPC clients ---
    print(f"Closing {len(sockets)} socket handles...")
    closed_count = 0
    for i in range(len(sockets) - 1, -1, -1):
        client = sockets[i]
        if isinstance(client, MpvIpcClient):
            try:
                client.close()
                closed_count += 1
            except Exception as e:
                print(f"  Error closing IPC client index {i}: {e}")
        sockets[i] = None

    print(f"Closed {closed_count} socket handles.")
    sockets = []  # Clear the global list

    # --- Terminate Processes (same as before) ---
    print(f"Terminating {len(processes)} MPV processes...")
    terminated_count = 0
    killed_count = 0
    for i, proc in enumerate(processes):
        if proc and proc.poll() is None:  # Check if process exists and is running
            try:
                print(f"  Terminating process index {i} (PID: {proc.pid})...")
                proc.terminate()  # Ask nicely first
                try:
                    proc.wait(timeout=1.0)  # Wait max 1 second for graceful exit
                    print(f"    Process {i} terminated gracefully.")
                    terminated_count += 1
                except subprocess.TimeoutExpired:
                    print(f"    Process {i} did not terminate after 1s, killing...")
                    proc.kill()  # Force kill
                    proc.wait(timeout=0.5)  # Wait briefly for kill to register
                    print(f"    Process {i} killed.")
                    killed_count += 1
            except Exception as e:
                print(f"  Error terminating/killing process index {i} (PID: {proc.pid if proc else 'N/A'}): {e}")
        else:
            # Process is None or already terminated
            pass

    print(f"Terminated {terminated_count} processes gracefully, killed {killed_count} processes.")
    processes = []  # Clear the global list
    person_socket_map.clear()  # Clear the video mapping
    print("--- Resource Cleanup Complete ---")


def force_shutdown_and_exit():
    """
    Instantly kills all mpv processes and terminates the script.
    This is the "ONE SHOT" emergency exit.
    """
    global processes
    print("\n--- IMMEDIATE SHUTDOWN INITIATED ---")

    # Save nut screenshots BEFORE killing MPVs — the emergency-fill path
    # in NutCounter.finalize() needs the screen to still be alive if the
    # buffer is short of the target count.
    try:
        nut_counter.finalize()
    except Exception as e:
        print(f"[NutCounter] Finalize failed in force_shutdown: {e}")

    # Generate GRWM script (concat file + bat runner) alongside screenshots.
    try:
        generate_grwm_script(nut_counter.final_dir)
    except Exception as e:
        print(f"[GRWM] Generate failed in force_shutdown: {e}")

    # Create a copy of the list to kill from, to avoid modification issues
    procs_to_kill = list(processes)
    
    killed_count = 0
    # Iterate and kill every process without waiting
    for proc in procs_to_kill:
        if proc and proc.poll() is None: # Check if process object exists and is running
            try:
                print(f"  Force-killing process PID: {proc.pid}")
                proc.kill()
                killed_count += 1
            except Exception as e:
                print(f"  Error killing process {proc.pid}: {e}")

    print(f"--- Sent kill signal to {killed_count} processes. Exiting script NOW. ---")
    # os._exit(0) is the most abrupt way to exit a Python script.
    # It stops everything immediately without running any more cleanup code.
    os._exit(0)


def _run_async(target, *args, name="KeyAction"):
    """Spawn a daemon thread to run target(*args). Used to keep the pynput
    listener thread from blocking on IPC sends — if send() hangs because
    MPV isn't consuming its pipe, the listener stays responsive to Q/ESC."""
    def _wrap():
        try:
            target(*args)
        except Exception as e:
            print(f"[{name}] error: {e}")
    t = threading.Thread(target=_wrap, daemon=True, name=name)
    t.start()


def on_press(key):
    """Keyboard listener callback for key presses.

    Keybinds:
      q / ESC       - immediate shutdown (synchronous)
      e             - CSP swap across all cells + lock cycling (async)
      + / =         - increase panscan (async)
      -             - decrease panscan (async)
      0             - reset video-pan-x/y to center (async)
      left / right  - pan video horizontally within each cell (async)
      up / down     - pan video vertically within each cell (async)

    E / +/- / 0 / arrows are dispatched to background daemon threads so a
    blocking IPC write never freezes the listener and Q/ESC stay responsive.
    """
    should_quit = False
    try:
        if hasattr(key, 'char') and key.char:
            ch = key.char
            low = ch.lower()
            if low == 'q':
                should_quit = True
            elif low == 'e':
                _run_async(trigger_csp_swap, name="CSPSwap")
                return
            elif ch in ('+', '='):
                _run_async(adjust_panscan, PANSCAN_STEP, name="Panscan+")
                return
            elif ch == '-':
                _run_async(adjust_panscan, -PANSCAN_STEP, name="Panscan-")
                return
            elif ch == '0':
                # Ergonomic recenter: snap video-pan-x/y back to 0 in a
                # single keypress. Small arrow-key deltas make it tedious
                # to walk manually back to center.
                _run_async(reset_video_pan, name="VPanReset")
                return
        # Arrow keys: smooth continuous pan via the animator thread. Gated
        # DYNAMICALLY on _arrow_pan_meaningful() which re-checks the current
        # GridHost client rect vs the reference aspect at EVERY keydown. This
        # means:
        #   - Landscape grids (row<col): always enabled.
        #   - Non-landscape grids at their original monitor size: disabled
        #     (post-crop aspect matches cell aspect, pan pushes into black).
        #   - Non-landscape grids AFTER the user resizes the GridHost
        #     non-proportionally (e.g. drag a 2x2 into a 960x1080 portrait
        #     frame): ENABLED, because cell aspect diverges from the baked-
        #     in post-crop aspect and pan now has real range.
        #
        # We send ONE immediate tap-sized delta on the first press (for
        # instant feedback on quick taps), then mark the direction held
        # so _pan_animator takes over at PAN_TICK_HZ until release.
        #
        # Sign convention: "camera moves left" mental model. Press Left
        # -> see more of the LEFT source. MPV's video-pan-x positive
        # shifts the image RIGHT in the cell, which visually reveals
        # LEFT source content, so arrow-left -> positive video-pan-x.
        if key in (Key.left, Key.right, Key.up, Key.down):
            if not _arrow_pan_meaningful():
                # Throttle log to once per second so OS key-repeat doesn't
                # spam the console while the user holds an arrow.
                global _pan_disabled_log_ts
                now = time.time()
                if now - _pan_disabled_log_ts > 1.0:
                    _pan_disabled_log_ts = now
                    print(
                        "[video-pan] Arrow keys disabled at current aspect: "
                        "resize GridHost non-proportionally to unlock pan range.",
                        flush=True,
                    )
                return
            if key == Key.left:
                if not _pan_held[("x", +1)]:
                    _run_async(adjust_video_pan_x, VIDEO_PAN_STEP, name="VPanLeftTap")
                _pan_key_down("x", +1)
                return
            if key == Key.right:
                if not _pan_held[("x", -1)]:
                    _run_async(adjust_video_pan_x, -VIDEO_PAN_STEP, name="VPanRightTap")
                _pan_key_down("x", -1)
                return
            if key == Key.up:
                if not _pan_held[("y", +1)]:
                    _run_async(adjust_video_pan_y, VIDEO_PAN_STEP, name="VPanUpTap")
                _pan_key_down("y", +1)
                return
            if key == Key.down:
                if not _pan_held[("y", -1)]:
                    _run_async(adjust_video_pan_y, -VIDEO_PAN_STEP, name="VPanDownTap")
                _pan_key_down("y", -1)
                return
        if key == Key.esc:
            should_quit = True

        if should_quit:
            # Don't do any complex logic here. Just call the emergency shutdown.
            # This function will terminate the entire program.
            force_shutdown_and_exit()

            # This part will likely not be reached, but it's good practice
            # to stop the listener if the program were to somehow continue.
            return False

    except Exception as e:
        print(f"Error in on_press callback: {e}")
        # If anything goes wrong, still try to force a shutdown.
        force_shutdown_and_exit()
        return False


def on_release(key):
    """Keyboard release handler. Shutdown logic stays in on_press; this
    handler only clears the held-state flags for the arrow-key smooth-pan
    animator so the animator thread exits when the user lets go.

    Safe to call unconditionally even when _arrow_pan_enabled is False —
    the held flags would be False anyway (on_press gated the keydown),
    and _pan_key_up is a cheap no-op in that case."""
    try:
        if key == Key.left:
            _pan_key_up("x", +1)
        elif key == Key.right:
            _pan_key_up("x", -1)
        elif key == Key.up:
            _pan_key_up("y", +1)
        elif key == Key.down:
            _pan_key_up("y", -1)
    except Exception as e:
        # Never propagate from the listener callback — a crash here would
        # kill the keyboard thread and freeze shutdown.
        print(f"Error in on_release callback: {e}", flush=True)


def main():
    """Main function to parse arguments, set up config, and start playback."""
    print("--- Video Grid Player Initializing ---")
    config = None # Initialize config to None
    try:
        # --- Monitor Selection ---
        monitor_geometry = select_monitor() # Returns [width, height, x_offset, y_offset]
        if not monitor_geometry or len(monitor_geometry) != 4:
             print("Error: Invalid monitor selection. Exiting.")
             return

        input_geometry = monitor_geometry[:2]  # [width, height]
        monitor_offset = monitor_geometry[2:]  # [x, y] offset
        print(f"Selected Monitor: {input_geometry[0]}x{input_geometry[1]} at ({monitor_offset[0]},{monitor_offset[1]})")

        # --- User Inputs ---
        # Get Master Type
        master_type = input("Enter Master Type [PC/RC/PM/D/M/CSP/BJ] (Default: RC): ").strip().upper() or "RC"
        valid_types = ["PC", "RC", "PM", "D", "M", "CSP", "BJ"] # Add other valid types if needed
        if master_type not in valid_types:
             print(f"Warning: Invalid master_type '{master_type}'. Using default 'RC'.")
             master_type = "RC"

        # Get Grid Dimensions
        try:
            col_str = input(f"Horizontal Videos (Columns) (Default: 1): ").strip()
            column_grid = int(col_str) if col_str else 1
            row_str = input(f"Vertical Videos (Rows) (Default: 1): ").strip()
            row_grid = int(row_str) if row_str else 1
            if column_grid <= 0 or row_grid <= 0:
                print("Grid dimensions must be positive integers. Using 1x1.")
                column_grid, row_grid = 1, 1
        except ValueError:
            print("Invalid grid input. Using 1x1.")
            column_grid, row_grid = 1, 1
        grid_size = column_grid * row_grid
        print(f"Grid: {column_grid}x{row_grid} ({grid_size} windows total)")


        # Get Cycling Parameters (only if grid size > 0)
        cycled_persons = 1
        interval_cycle = 15 # Default cycle interval
        total_time = 300 # Default total session time in seconds

        if grid_size > 0: # Only ask for cycling if there are windows
            try:
                cycle_str = input(f"Number of videos/persons to cycle per window (Default: 1): ").strip()
                cycled_persons = int(cycle_str) if cycle_str else 1
                if cycled_persons < 1:
                    print("Cycle count must be at least 1. Using 1.")
                    cycled_persons = 1
            except ValueError:
                print("Invalid cycle count input. Using 1.")
                cycled_persons = 1

            if cycled_persons > 1:
                print(f"Cycling enabled with {cycled_persons} videos per window.")
                # Special handling for CSP type interval/time
                if master_type == 'CSP':
                    interval_cycle = 28 # CSP specific default interval
                    try:
                        # Ask for time in minutes, default to 5
                        csp_time_str = input(f"Total session duration for CSP (minutes) (Default: 5): ").strip()
                        total_time_minutes = int(csp_time_str) if csp_time_str else 5
                        total_time = total_time_minutes * 60 # Convert to seconds
                        if total_time <= 0:
                             print("Total time must be positive. Using 5 minutes (300s).")
                             total_time = 300
                    except ValueError:
                         print(f"Invalid total time input. Using 5 minutes (300s).")

                    print(f"CSP Mode: Interval fixed at {interval_cycle}s. Total Time set to {total_time // 60} minutes ({total_time}s).")
                else:
                    # Ask for interval and total time for other types when cycling
                    try:
                        interval_str = input(f"Interval per video switch (seconds) (Default: {interval_cycle}): ").strip()
                        interval_cycle = int(interval_str) if interval_str else interval_cycle
                        if interval_cycle <= 0:
                            print("Interval must be positive. Using 15s.")
                            interval_cycle = 15
                    except ValueError:
                        print(f"Invalid interval input. Using {interval_cycle}s.")

                    try:
                        # Ask for time in minutes, default to 5
                        time_str = input(f"Total session duration (minutes) (Default: 5): ").strip()
                        total_time_minutes = int(time_str) if time_str else 5
                        total_time = total_time_minutes * 60 # Convert to seconds
                        if total_time <= 0:
                            print("Total time must be positive. Using 5 minutes (300s).")
                            total_time = 300
                        # Ensure total time is at least one cycle interval if cycling
                        if total_time < interval_cycle:
                            print(f"Warning: Total time ({total_time}s) is less than interval ({interval_cycle}s). Effective cycles may be limited.")
                    except ValueError:
                        print(f"Invalid total time input. Using 5 minutes (300s).")

                print(f"Cycling Parameters: Interval={interval_cycle}s, Total Duration={total_time // 60} minutes ({total_time}s)")
            else:
                print("Single video per window (cycling disabled).")
                # Set interval/total_time defaults even if not cycling? Or set to 0?
                # Keep defaults, they might be used elsewhere or for future features.

        else: # grid_size is 0
             print("Grid size is 0, cannot display videos.")
             # Set defaults, though they won't be used for playback
             cycled_persons = 1
             interval_cycle = 0
             total_time = 0


        # --- Create Config Object ---
        # Note: pixel_drop, crop_height, crop_width calculated *after* basic config init
        config = VideoPlayerConfig(
            master_type=master_type,
            column_grid=column_grid,
            row_grid=row_grid,
            cycled_persons=cycled_persons,
            interval_cycle=interval_cycle,
            total_time=total_time,
            input_geometry=input_geometry,
            pixel_drop=0, # Placeholder, calculated below
            crop_height=0.88, # Default/Constant crop height factor
            crop_width=0, # Placeholder, calculated below
            production=True, # Assume production unless testing flag added
            test_mode=False, # Assume not testing
            monitor_offset=monitor_offset
        )

        # --- Calculate Dynamic Config Values ---
        config.pixel_drop = config.pixel_drop_map.get(master_type, 100) # Get type-specific pixel drop

        # Calculate crop_width based on geometry and pixel drop
        try:
            # Ensure dimensions are positive and avoid division by zero
            if column_grid > 0 and row_grid > 0 and input_geometry[0] > 0 and input_geometry[1] > 0:
                cell_width = input_geometry[0] / column_grid
                cell_height = input_geometry[1] / row_grid
                denominator = cell_height + config.pixel_drop
                if abs(denominator) < 1e-9: # Avoid division by near-zero
                    print("Warning: Calculated denominator for crop_width is near zero. Using default.")
                    calculated_value = 0.8 # Default fallback crop width
                else:
                    # Original formula: ((Width/Col) / ((Height/Row) + PixelDrop) * (Height * CropHeight)) / Width
                    # Simplified: (cell_width / denominator) * (input_geometry[1] * config.crop_height) / input_geometry[0]
                    # Let's re-evaluate the formula's intent. It seems aimed at adjusting width based on height+drop.
                    # Aspect ratio of the *cropped* video within the cell:
                    # target_cropped_video_height = cell_height * config.crop_height
                    # target_cropped_video_width = ? (this is what we want)
                    # Formula seems related to aspect ratio adjustment. Let's use the original provided one carefully.
                    calculated_value = (cell_width / denominator * (input_geometry[1] * config.crop_height)) / input_geometry[0]

                    # Clamp the calculated value to a reasonable range [0.0, 1.0]
                    calculated_value = max(0.0, min(calculated_value, 1.0))
                    print(f"Calculated crop_width value (intermediate): {calculated_value:.4f}")
            else:
                 print("Warning: Invalid grid or screen dimensions for crop_width calculation. Using default.")
                 calculated_value = 0.8 # Default fallback crop width

        except ZeroDivisionError:
             print("Warning: Division by zero during crop_width calculation. Using default.")
             calculated_value = 0.8 # Default fallback crop width
        except Exception as e:
            print(f"Error during crop_width calculation: {e}. Using default.")
            calculated_value = 0.8

        # Landscape-grid override: the pre-fit formula above shrinks source width
        # so post-crop aspect matches cell aspect, which collapses panscan range
        # to zero on 2x1 (and generally destroys up to ~56% of width). For
        # landscape grids we keep the full source width (calc_w=1.0) and rely on
        # the arrow-key video-pan-x / video-pan-y bindings to scroll through
        # the oversized frame. Portrait grids already clamped to 1.0 naturally.
        #
        # Also configures the reference cell aspect used by _arrow_pan_meaningful()
        # at runtime. Two regimes:
        #   - Landscape override on: post-crop aspect is source-dependent and
        #     NEVER matches cell aspect (except by coincidence). Arrow keys
        #     always produce visible movement. Sentinel: None = always on.
        #   - Prefit formula on: post-crop aspect == INITIAL cell aspect BY
        #     DESIGN. If the user later resizes the GridHost non-proportionally,
        #     current cell aspect diverges and arrow keys become meaningful.
        #     Store the initial aspect for later comparison.
        global _pan_ref_cell_aspect
        if row_grid > 0 and column_grid > 0 and row_grid < column_grid:
            print(
                f"Landscape grid {column_grid}x{row_grid}: overriding "
                f"calculated crop_width {calculated_value:.4f} -> 1.0 "
                f"(use arrow keys to pan the camera within each cell)."
            )
            calculated_value = 1.0
            _pan_ref_cell_aspect = None  # sentinel: always meaningful
        else:
            # Compute initial cell aspect: (mon_w/cols) / (mon_h/rows)
            try:
                _pan_ref_cell_aspect = (
                    (float(input_geometry[0]) / column_grid)
                    / (float(input_geometry[1]) / row_grid)
                )
            except (ZeroDivisionError, IndexError, TypeError):
                _pan_ref_cell_aspect = 1.0  # fallback

        # Set final crop_width based on master_type, using calculated value as fallback/default for most
        config.crop_width = {
            "PC": calculated_value,
            "BJ": calculated_value,
            "M": calculated_value,
            "RC": calculated_value,
            "PM": 1.0, # PM uses full width (no horizontal crop)
            "D": calculated_value,
            "CSP": calculated_value
        }.get(master_type, calculated_value) # Use calculated as default if type not listed

        print(f"Final Config Settings:")
        print(f"  Master Type: {config.master_type}")
        print(f"  Grid: {config.column_grid}x{config.row_grid}")
        print(f"  Cycling: {config.cycled_persons} videos per window")
        print(f"  Interval: {config.interval_cycle}s")
        print(f"  Total Time: {config.total_time // 60} minutes ({config.total_time}s)")
        print(f"  Monitor Res: {config.input_geometry[0]}x{config.input_geometry[1]}")
        print(f"  Monitor Offset: {config.monitor_offset}")
        print(f"  Pixel Drop: {config.pixel_drop}")
        print(f"  Crop Height Factor: {config.crop_height:.2f}")
        print(f"  Crop Width Factor: {config.crop_width:.4f}")
        print(f"  Production Mode: {config.production}")
        print(f"  Test Mode: {config.test_mode}")

        # --- Prepare Video List ---
        print("\nPreparing video list...")
        video_list = prepare_video_list() # Loads map, handles args, shuffles
        if not video_list:
            print("Error: Failed to prepare video list. Exiting.")
            # No resources to clean yet, just exit
            return

        # --- Start Playback ---
        if grid_size > 0:
            print("\n--- Starting Playback ---")
            play_videos(config, video_list)
        else:
            print("\nGrid size is 0. No videos will be played.")

    except KeyboardInterrupt:
        print("\nKeyboard interrupt detected during setup or main loop. Shutting down...")
        if 'interrupt_event' in globals() and interrupt_event: # Check if event exists
            interrupt_event.set() # Signal threads to stop
    except EOFError:
         print("\nEOF detected (likely Ctrl+Z or input pipe closed). Shutting down...")
         if 'interrupt_event' in globals() and interrupt_event: interrupt_event.set()
    except Exception as e:
        print(f"\n--- An unexpected error occurred in main ---")
        print(f"Error Type: {type(e).__name__}")
        print(f"Error Details: {e}")
        import traceback
        print("\n--- Traceback ---")
        traceback.print_exc()
        print("-----------------")
        if 'interrupt_event' in globals() and interrupt_event: # Check if event exists
             interrupt_event.set() # Signal threads to stop on error
    finally:
        # --- Final Cleanup ---
        # This block ensures cleanup runs regardless of how main exits
        print("\n--- Main function finished or terminated. Running final cleanup... ---")
        # The cleanup_resources() call is inside play_videos's own finally block usually,
        # but calling it here ensures cleanup even if play_videos wasn't reached or errored early.
        # Make sure cleanup_resources() is safe to call multiple times or if resources are already gone.
        cleanup_resources()
        print("\n--- Program Exited ---")


if __name__ == "__main__":
    # Ensure the interrupt event exists globally before starting
    interrupt_event = threading.Event()
    _ps.run_player(main)
