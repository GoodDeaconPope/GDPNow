import tkinter as tk
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
try:
    _sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    _sys.stderr.reconfigure(encoding='utf-8', errors='replace')
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
    """

    def __init__(self, pipe_name):
        self.pipe_name = pipe_name
        self.handle = None
        self.lock = threading.Lock()
        self.running = False
        self.reader_thread = None
        self._buffer = b""
        self._messages = queue.Queue()

    def connect(self, timeout=10.0):
        """Connect to the named pipe and start the reader thread."""
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
    Try to find up to grid_size mpv top-level windows.

    1) Prefer windows whose PID is in pid_list.
    2) If we still don't have enough, fallback to visible windows whose
       class == 'mpv' or whose title looks like an mpv window.

    IMPORTANT FIX:
      - We now KEEP accumulating hwnds across attempts
      - We only stop early when we've collected >= grid_size windows
    """
    import time
    import win32gui
    import win32process

    wanted_pids = set(pid_list)
    found_hwnds = []

    def pid_callback(hwnd, extra):
        # If we've already got enough, don't bother doing more work
        if len(found_hwnds) >= grid_size:
            return

        if not win32gui.IsWindowVisible(hwnd):
            return

        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:
            return

        if pid in wanted_pids and hwnd not in found_hwnds:
            found_hwnds.append(hwnd)

    deadline = time.time() + max_wait
    attempt = 0

    # --- Primary search: by PID ---
    while time.time() < deadline and len(found_hwnds) < grid_size:
        attempt += 1
        win32gui.EnumWindows(pid_callback, None)
        print(f"GridHost: PID search attempt {attempt}, found so far: {found_hwnds}")
        if len(found_hwnds) >= grid_size:
            break
        time.sleep(interval)

    # --- Fallback search: by class/title, ONLY if still short ---
    if len(found_hwnds) < grid_size:
        extra_hwnds = []

        def fallback_callback(hwnd, extra):
            if len(found_hwnds) + len(extra_hwnds) >= grid_size:
                return

            if not win32gui.IsWindowVisible(hwnd):
                return

            try:
                cls = win32gui.GetClassName(hwnd)
                title = win32gui.GetWindowText(hwnd)
            except Exception:
                return

            title_l = title.lower()
            looks_like_mpv = (
                cls.lower() == "mpv"
                or ".mp4" in title_l
                or "mpv" in title_l
            )

            if looks_like_mpv and hwnd not in found_hwnds and hwnd not in extra_hwnds:
                extra_hwnds.append(hwnd)

        win32gui.EnumWindows(fallback_callback, None)
        if extra_hwnds:
            print(f"GridHost: Fallback HWNDs by class/title: {extra_hwnds}")
            for h in extra_hwnds:
                if len(found_hwnds) >= grid_size:
                    break
                found_hwnds.append(h)

    # Final trim (safety)
    if len(found_hwnds) > grid_size:
        found_hwnds = found_hwnds[:grid_size]

    return found_hwnds

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

        child_hwnds = _enum_windows_for_pids(mpv_pids, grid_size=grid_size, max_wait=5.0, interval=0.2)
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
        self.lower_resolution = True  # Set to True to reduce resolution

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


def create_mpv_command(video_file, start_time, geometry, config, socket_index):
    """Creates the MPV command string."""
    # Base parameters
    pc_parameters = f"--no-border --video-sync=display-resample --keepaspect --no-keepaspect-window --panscan=1 "

    # Only add interpolation if enabled
    if config.use_interpolation:
        pc_parameters += "--interpolation "

    # Add grid-based optimizations (from snippet 2)
    if config.column_grid * config.row_grid >= 9:
        pc_parameters += "--vd-lavc-skiploopfilter=all --vd-lavc-fast "

    # Performance optimization parameters (only if enabled)
    performance_params = ""
    scale_param = ""
    if config.optimize_performance:
        performance_params = "--scale=bilinear --cscale=bilinear --dscale=bilinear --hwdec=auto-copy --vd-lavc-threads=4 "

        # Add additional optimizations if enabled
        if config.extra_optimization:
            performance_params += "--vo=gpu --cache=yes --demuxer-max-bytes=100M --vd-lavc-skiploopfilter=nonkey "

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
    if config.column_grid == 1 and config.row_grid == 1:
        pc_parameters += "--fs "

    # IPC socket parameter using the provided index
    socket_params = f"--input-ipc-server=mpvsocket{socket_index} "

    # Assemble the command
    command = f"mpv --start={start_time} \"{video_file}\" --geometry={geometry} {pc_parameters} {performance_params} {socket_params} {ffmpeg_params}"
    # print(f"Generated Command (Socket {socket_index}): {command}") # Debug print
    # Use shlex for proper quoting, especially for filenames with spaces
    # Ensure quotes around video_file are handled correctly by shlex if needed, but usually better handled by Popen directly if command is passed as list
    # Passing the whole string to Popen with shell=True is generally discouraged for security.
    # shlex.split works well here.
    try:
        split_command = shlex.split(command)
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


def create_socket(index, retries=100, delay=0.05):
    """Create an MpvIpcClient for the given socket index and connect it.

    Returns an MpvIpcClient instance instead of a raw handle.
    """
    pipe_name = fr'\\.\pipe\mpvsocket{index}'
    print(f"Attempting to connect to pipe: {pipe_name}")
    client = MpvIpcClient(pipe_name)
    try:
        # retries * delay ~= total timeout window
        client.connect(timeout=retries * delay)
    except RuntimeError as e:
        print(f"Error connecting to pipe {pipe_name}: {e}")
        raise FileNotFoundError(f"Failed to connect to socket after {retries} attempts: {pipe_name}")
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
    
    # 1. Hardcoded path to the map file
    map_file = r'E:\1SystemFile\systemFileMap.json'
    
    # 2. Define directories to search for video files
    #    '.' is the current directory.
    video_search_dirs = ['.', r'E:\1SystemFile']

    if not os.path.exists(map_file):
         print(f"Error: Cannot find video map file: {map_file}")
         return []
    try:
        with open(map_file, 'r', encoding='utf-8-sig') as d:
            system_file_map = json.load(d)
    except json.JSONDecodeError as e:
         print(f"Error decoding JSON from {map_file}: {e}")
         return []
    except Exception as e:
         print(f"Error reading {map_file}: {e}")
         return []

    if not isinstance(system_file_map, dict):
        print(f"Error: Content of {map_file} is not a JSON object (dictionary).")
        return []

    # 3. Build a dictionary of available files: { filename: full_path }
    #    We search E:\1SystemFile first, then Current Dir.
    #    This means if a file exists in BOTH, the Current Dir version takes priority (overwrites).
    available_files = {}

    for search_dir in video_search_dirs:
        if os.path.exists(search_dir):
            # Find all mp4s in this directory
            search_pattern = os.path.join(search_dir, '*.mp4')
            found_paths = glob.glob(search_pattern)
            
            for path in found_paths:
                filename = os.path.basename(path)
                # Store mapped to the specific path found (local relative or E: absolute)
                available_files[filename] = path

    # 4. Filter and Update the map
    #    We only keep entries from JSON that exist on disk.
    #    We update the Key to be the `full_path` so the downstream player can find it.
    valid_system_map = {}
    
    for key, metadata in system_file_map.items():
        # The JSON key is likely just "video.mp4". We check if we found it.
        if key in available_files:
            actual_path = available_files[key]
            valid_system_map[actual_path] = metadata

    system_file_map = valid_system_map

    if not system_file_map:
        print("Warning: No matching video files found in '.' or 'E:\\1SystemFile'.")

    # Handle command-line arguments
    args_file_map = {}
    remaining_map = system_file_map.copy() 

    for arg in sys.argv[1:]:
        found_key = None
        for key in list(remaining_map.keys()):
            # 'key' might now be a full path like 'E:\1SystemFile\vid.mp4'
            # 'arg' might be 'vid'. Substring match still works perfectly.
            if arg in key: 
                found_key = key
                break 

        if found_key:
            args_file_map[found_key] = remaining_map.pop(found_key)
            print(f"Prioritizing video '{os.path.basename(found_key)}' based on argument '{arg}'.")

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

    DEFAULT_OUTPUT_DIR = r"%USERPROFILE%\Pictures\gdpnow_nuts"
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
        self._font_cache = {}  # size -> ImageFont

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
        for candidate in (r"C:\Windows\Fonts\arialbd.ttf", r"C:\Windows\Fonts\arial.ttf"):
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
                stem = os.path.splitext(os.path.basename(info["filename"]))[0]
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
                print(f"[NutCounter] Host window found, capturing client area: {bbox}")
                self._host_seen = True
        else:
            bbox = self.fallback_bbox
            if bbox is None:
                return
            if not self._fallback_warned:
                print(f"[NutCounter] Host window not found, using fallback bbox: {bbox}")
                self._fallback_warned = True
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

_SOURCE_ROOT_DEFAULT = r"E:\1SystemFile"


def _resolve_source_path(filename):
    """Resolve a bare filename (e.g. '321.mp4') to an absolute path under
    the source directory. Returns the original if it's already absolute."""
    if os.path.isabs(filename):
        return filename
    return os.path.join(_SOURCE_ROOT_DEFAULT, filename)


CSP_PRE_ROLL_SEC = 2  # start playback this many seconds before the CSP marker


def trigger_csp_swap():
    """E key handler: for every active cell, send a loadfile swapping to
    that video's CSP position (with a small pre-roll), then engage
    e_locked so the cycle thread stops further switching."""
    global sockets
    print("\n[E] CSP SWAP TRIGGERED ============================")
    snap = _snapshot_cell_state()
    if not snap:
        print("[E] No cell state — nothing to swap.")
        return
    swapped = 0
    skipped = 0
    for socket_idx in sorted(snap.keys()):
        info = snap[socket_idx]
        if socket_idx >= len(sockets) or sockets[socket_idx] is None:
            print(f"  Socket {socket_idx}: no live socket, skipping.")
            skipped += 1
            continue
        meta = info.get("metadata") or {}
        csp_str = meta.get("CSP")
        if not csp_str:
            print(f"  Socket {socket_idx}: no CSP in metadata for {info['filename']}, skipping.")
            skipped += 1
            continue
        try:
            csp_sec = mmss_to_seconds(csp_str)
        except Exception:
            print(f"  Socket {socket_idx}: bad CSP value {csp_str!r}, skipping.")
            skipped += 1
            continue
        # Pre-roll: back up a couple seconds so the climax has lead-in.
        start_sec = max(0, csp_sec - CSP_PRE_ROLL_SEC)
        start_str = seconds_to_mmss(start_sec)
        filename = info["filename"]
        cmd = f'loadfile {filename} replace "start={start_str}"'
        try:
            sockets[socket_idx].send(cmd)
            _update_cell_state(socket_idx, filename, start_sec, meta)
            print(f"  Socket {socket_idx}: {filename} -> CSP @ {csp_str} (start {start_str}, -{CSP_PRE_ROLL_SEC}s)")
            swapped += 1
        except Exception as e:
            print(f"  Socket {socket_idx}: send failed: {e}")
            skipped += 1
    e_locked.set()
    print(f"[E] Swapped {swapped}, skipped {skipped}. Cycling locked on CSP.")
    print("[E] Press Q or ESC to finalize the session.")


def generate_grwm_script(session_dir):
    """Write GRWM grid-replay artifacts to session_dir.

    Recreates the actual grid the user was watching: each cell shows the
    same scenes at the same times, scaled to a 1920x1080 output, and
    xstacked into the same column×row grid layout the live session used.

    Outputs:
      grwm_filter.txt    — ffmpeg -filter_complex_script body
      run_grwm.py        — Python runner that builds the ffmpeg arg list
                            (sidesteps Windows cmd 8191-char limit)
      run_grwm.bat       — thin wrapper: 'python run_grwm.py'
      grwm_sequential.ffconcat — fallback simple-stitch concat file

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

    # ---- Group history by cell, compute per-scene durations ----
    by_socket = {}
    for entry in history:
        by_socket.setdefault(entry["socket_idx"], []).append(entry)

    cell_scenes = {}
    for socket_idx in range(grid_size):
        entries = by_socket.get(socket_idx, [])
        entries.sort(key=lambda e: e["wall"])
        scenes = []
        for i, e in enumerate(entries):
            if i + 1 < len(entries):
                duration = entries[i + 1]["wall"] - e["wall"]
            else:
                duration = session_end - e["wall"]
            duration = max(0.5, duration)
            scenes.append({
                "filename": e["filename"],
                "in_sec": e["pos_sec"],
                "duration": duration,
            })
        cell_scenes[socket_idx] = scenes

    # ---- Output dimensions ----
    out_w, out_h = 1920, 1080
    cell_w = (out_w // cg) - ((out_w // cg) % 2)  # even
    cell_h = (out_h // rg) - ((out_h // rg) % 2)  # even

    # ---- Build inputs list and filter graph ----
    inputs = []          # absolute paths, one -i per scene
    filter_lines = []
    cell_stream_labels = []  # in grid order

    input_idx = 0
    for socket_idx in range(grid_size):
        scenes = cell_scenes[socket_idx]
        if not scenes:
            # No history for this cell — emit a black source for the full
            # session duration so the grid layout still works.
            label = f"[cell{socket_idx}]"
            filter_lines.append(
                f"color=c=black:s={cell_w}x{cell_h}:d={total_duration:.3f}{label}"
            )
            cell_stream_labels.append(label)
            continue

        per_scene_labels = []
        for scene in scenes:
            inputs.append(_resolve_source_path(scene["filename"]))
            in_lbl = f"[{input_idx}:v]"
            scene_lbl = f"[s{input_idx}]"
            filter_lines.append(
                f"{in_lbl}trim=start={scene['in_sec']:.3f}:duration={scene['duration']:.3f},"
                f"setpts=PTS-STARTPTS,"
                f"scale={cell_w}:{cell_h}:force_original_aspect_ratio=decrease,"
                f"pad={cell_w}:{cell_h}:(ow-iw)/2:(oh-ih)/2:color=black"
                f"{scene_lbl}"
            )
            per_scene_labels.append(scene_lbl)
            input_idx += 1

        cell_lbl = f"[cell{socket_idx}]"
        if len(per_scene_labels) == 1:
            # Avoid degenerate concat=n=1; rename the single scene's label.
            old = per_scene_labels[0]
            filter_lines[-1] = filter_lines[-1].replace(old, cell_lbl)
        else:
            concat_in = "".join(per_scene_labels)
            filter_lines.append(
                f"{concat_in}concat=n={len(per_scene_labels)}:v=1:a=0{cell_lbl}"
            )
        cell_stream_labels.append(cell_lbl)

    # ---- xstack the cells into a grid ----
    if grid_size == 1:
        # Single cell — no xstack needed, rename to [out]
        filter_lines[-1] = filter_lines[-1].replace("[cell0]", "[out]")
    else:
        layout_parts = []
        for i in range(grid_size):
            col = i % cg
            row = i // cg
            layout_parts.append(f"{col * cell_w}_{row * cell_h}")
        xstack_in = "".join(cell_stream_labels)
        filter_lines.append(
            f"{xstack_in}xstack=inputs={grid_size}:layout={'|'.join(layout_parts)}:fill=black[out]"
        )

    # ---- Write filter script ----
    filter_script_path = os.path.join(session_dir, "grwm_filter.txt")
    try:
        with open(filter_script_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(";\n".join(filter_lines))
            f.write("\n")
    except OSError as e:
        print(f"[GRWM] Failed to write filter script: {e}")
        return

    # ---- Write Python runner (sidesteps cmd 8191-char limit) ----
    runner_path = os.path.join(session_dir, "run_grwm.py")
    output_video = os.path.join(session_dir, "grwm.mp4")
    try:
        with open(runner_path, "w", encoding="utf-8", newline="\n") as f:
            f.write("# GRWM grid replay runner\n")
            f.write(f"# Session: {os.path.basename(session_dir)}\n")
            f.write(f"# Grid: {cg}x{rg}, {len(inputs)} scenes, {total_duration:.1f}s total\n")
            f.write("# NVENC H264 high quality (preset p7, cq 18)\n")
            f.write("import subprocess, sys\n\n")
            f.write("INPUTS = [\n")
            for inp in inputs:
                f.write(f"    {inp!r},\n")
            f.write("]\n\n")
            f.write(f"FILTER_SCRIPT = {filter_script_path!r}\n")
            f.write(f"OUTPUT = {output_video!r}\n\n")
            f.write("cmd = ['ffmpeg', '-y', '-hide_banner']\n")
            f.write("for p in INPUTS:\n")
            f.write("    cmd += ['-i', p]\n")
            f.write("cmd += [\n")
            f.write("    '-filter_complex_script', FILTER_SCRIPT,\n")
            f.write("    '-map', '[out]',\n")
            f.write("    '-c:v', 'h264_nvenc',\n")
            f.write("    '-preset', 'p7',\n")
            f.write("    '-tune', 'hq',\n")
            f.write("    '-rc', 'vbr',\n")
            f.write("    '-cq', '18',\n")
            f.write("    '-b:v', '20M',\n")
            f.write("    '-maxrate', '40M',\n")
            f.write("    '-bufsize', '60M',\n")
            f.write("    '-pix_fmt', 'yuv420p',\n")
            f.write("    '-profile:v', 'high',\n")
            f.write("    '-level', '4.2',\n")
            f.write("    '-an',\n")  # no audio (grid mode)
            f.write("    OUTPUT,\n")
            f.write("]\n")
            f.write("print(f'Running ffmpeg with {len(INPUTS)} inputs...')\n")
            f.write("sys.exit(subprocess.run(cmd).returncode)\n")
    except OSError as e:
        print(f"[GRWM] Failed to write runner: {e}")
        return

    # ---- Write thin .bat wrapper for double-click convenience ----
    bat_path = os.path.join(session_dir, "run_grwm.bat")
    try:
        with open(bat_path, "w", encoding="utf-8", newline="\r\n") as f:
            f.write("@echo off\r\n")
            f.write(f"REM GRWM grid replay - {cg}x{rg} grid, {len(inputs)} scenes\r\n")
            f.write(f"cd /d \"{session_dir}\"\r\n")
            f.write("python run_grwm.py\r\n")
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
                src = _resolve_source_path(s["filename"]).replace("\\", "/")
                f.write(f"# scene {idx+1}: socket {socket_idx} -> {s['filename']} "
                        f"@ {s['in_sec']:.1f}s for {s['duration']:.1f}s\n")
                f.write(f"file '{src}'\n")
                f.write(f"inpoint {s['in_sec']:.3f}\n")
                f.write(f"outpoint {s['in_sec'] + s['duration']:.3f}\n")
    except OSError as e:
        print(f"[GRWM] Failed to write sequential fallback: {e}")

    print(f"[GRWM] Grid replay: {cg}x{rg} grid, {len(inputs)} scenes, {total_duration:.1f}s")
    print(f"[GRWM]   Filter: {filter_script_path}")
    print(f"[GRWM]   Runner: {bat_path} (or run_grwm.py)")
    print(f"[GRWM]   Sequential fallback: {seq_concat_path}")


def play_videos(config, video_list):
    """Starts MPV instances based on config and video list, prepares for cycling."""
    global sockets, processes, person_socket_map, interrupt_event

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

        command_list = create_mpv_command(first_filename, first_start_time_str, geometry, config, i)
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
                 socket_handle = create_socket(i)
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
        if mpv_pids:
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

        # E key was pressed — hold everyone on CSP, skip this switch.
        if e_locked.is_set():
            print(f"Cycle Thread: e_locked engaged, holding on CSP (switch {switch_idx} skipped).")
            continue

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
                f"(RR segment: {debug_info})"
            )

            cmd = f'loadfile {v["filename"]} replace "start={final_start_str}"'
            send_command_to_socket_index(socket_idx, cmd)
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


def on_press(key):
    """Keyboard listener callback for key presses. 'q' or ESC triggers
    immediate shutdown. 'e' triggers a CSP swap across all cells and
    locks cycling."""
    should_quit = False
    try:
        if hasattr(key, 'char') and key.char:
            ch = key.char.lower()
            if ch == 'q':
                should_quit = True
            elif ch == 'e':
                try:
                    trigger_csp_swap()
                except Exception as e:
                    print(f"[E key] trigger_csp_swap error: {e}")
                return  # don't quit
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
    """This function is kept for compatibility but main shutdown logic is in on_press."""
    pass


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
    main()
