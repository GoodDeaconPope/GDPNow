import tkinter as tk
import shlex
import subprocess
import time
import math
import random
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
import concurrent.futures

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
            "PC": 0, "RC": 0, "PM": 0, "D": 100, "M": 130, "CSP": 200
        }
        self.default_duration_map = {
            "PC": 60, "M": 360, "RC": 360, "PM": 60, "D": 360, "PD": 60,
            "CP": self.interval_cycle, "CSP": self.interval_cycle, "CS": self.interval_cycle,
            "B": 180, "BP": 60, "BEP": 180
        }
        # Update default duration map again in case interval_cycle was corrected
        for key in ["CP", "CSP", "CS"]:
            self.default_duration_map[key] = self.interval_cycle


def create_mpv_command(video_file, start_time, geometry, config, socket_index):
    """
    Creates the MPV command string using Native Hardware Decoding (d3d11va).
    This ensures zero-copy rendering (Decoder -> VRAM -> Display).
    """
    # 1. The "Gold Standard" Windows Hardware flags
    base_flags = (
        "--no-border "
        "--vo=gpu "
        "--gpu-api=d3d11 "
        "--hwdec=d3d11va "             # Native DirectX11 hardware decoding (Crucial)
        "--video-sync=display-desync " # Prevent VSync lag (Crucial for grids)
        "--keepaspect --no-keepaspect-window --panscan=1 "
    )

    # 2. Grid Optimizations
    # If grid is large, disable loop filters to save massive GPU resources
    if config.column_grid * config.row_grid >= 4:
        base_flags += "--vd-lavc-skiploopfilter=all "
        # Limit cache to save RAM (16 instances * 32MB is better than default)
        base_flags += "--demuxer-max-bytes=32M --demuxer-max-back-bytes=8M "

    # 3. GPU-Safe Cropping (Replaces 'lavfi' software filters)
    # We use video-zoom and video-align to simulate cropping on the GPU.
    gpu_crop_flags = ""
    
    # Your logic: crop_height 0.88 means show 88% of the video.
    # To fill the window with 88% of the image, we zoom in.
    if config.crop_height > 0 and config.crop_height < 1.0:
        zoom_factor = 1.0 / config.crop_height
        # MPV expects log2 for zoom (e.g., 2^x)
        log_zoom = math.log(zoom_factor, 2)
        # Align Y to -1 (Top) because your original script assumed y=0
        gpu_crop_flags = f"--video-zoom={log_zoom:.3f} --video-align-y=-1 "

    # 4. Scaling Performance
    perf_flags = ""
    if config.optimize_performance:
        # Bilinear is the fastest scaling algorithm (good for 4K -> small window)
        perf_flags = "--scale=bilinear --cscale=bilinear --dscale=bilinear "

    # 5. Standard Flags
    socket_params = f"--input-ipc-server=mpvsocket{socket_index} "
    
    extra_flags = ""
    if config.column_grid == 1 and config.row_grid == 1:
        extra_flags += "--fs "
    if config.test_mode:
        extra_flags += "--no-video --no-audio "
    if config.master_type == "PC":
        extra_flags += "--brightness=6 --gamma=7 "

    # Construct final command
    # Note: We rely on shlex to handle quoting of filenames with spaces
    command = (
        f"mpv --start={start_time} \"{video_file}\" "
        f"--geometry={geometry} "
        f"{base_flags} {perf_flags} {gpu_crop_flags} {extra_flags} {socket_params}"
    )

    return shlex.split(command)

def start_mpv_instance(command_list):
    """Starts an MPV instance with HIGH PRIORITY to prevent stuttering."""
    if command_list is None:
        return None
    try:
        # Windows Priority Flags:
        # HIGH_PRIORITY_CLASS (0x00000080): Gives MPV preference over background tasks
        # CREATE_NEW_PROCESS_GROUP: Helps with clean termination
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000080
        
        process = subprocess.Popen(command_list, creationflags=creationflags)
        print(f"Started MPV instance with PID: {process.pid}")
        return process
    except Exception as e:
        print(f"Error starting MPV process: {e}")
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
        with open(map_file, 'r') as d:
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



class SmartProxyManager:
    def __init__(self, output_height=540, max_workers=4):
        self.output_height = int(output_height)
        # Thread pool: 4 workers allows 4 segments to generate simultaneously
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
        self.futures = {} 
        self.lock = threading.Lock()
        self.running = True
        self.ffmpeg_pids = set() # Track PIDs for shutdown

    def shutdown(self):
        self.running = False
        self.executor.shutdown(wait=False)
        # Nuclear option for PIDs is handled in force_shutdown

    def get_segment_path(self, original_path, start_time, duration):
        """
        Returns path to segment if ready, else None.
        """
        original_path = os.path.abspath(original_path)
        base, ext = os.path.splitext(original_path)
        # Filename includes Duration so it adapts if you change settings
        seg_name = f"{base}_seg_{int(start_time)}_{int(duration)}_{self.output_height}p.mp4"
        
        if os.path.exists(seg_name):
            return seg_name
        return None

    def request_segment(self, original_path, start_time, duration):
        """
        Submits a job to generate this specific time slice.
        """
        if not self.running: return

        original_path = os.path.abspath(original_path)
        base, ext = os.path.splitext(original_path)
        seg_name = f"{base}_seg_{int(start_time)}_{int(duration)}_{self.output_height}p.mp4"
        temp_name = f"{base}_seg_{int(start_time)}_{int(duration)}_temp.mp4"

        if os.path.exists(seg_name): return
        with self.lock:
            if seg_name in self.futures: return

        # Submit job
        future = self.executor.submit(
            self._hybrid_generate_worker, 
            original_path, seg_name, temp_name, start_time, duration
        )
        
        with self.lock:
            self.futures[seg_name] = future

    def _hybrid_generate_worker(self, input_path, output_path, temp_path, start_time, duration):
        if not self.running: return
        
        # --- HYBRID ACCURATE SEEK ---
        # 1. We jump to 'fast_seek' (Hardware seek). This is instant but inaccurate.
        # 2. We set 'slow_seek' (Software decode). This processes the gap to find the exact frame.
        # 3. 10s buffer is usually enough to cover any Keyframe Interval (GOP).
        SEEK_BUFFER = 10.0

        if start_time > SEEK_BUFFER:
            fast_seek = start_time - SEEK_BUFFER 
            slow_seek = SEEK_BUFFER              
        else:
            fast_seek = 0.0
            slow_seek = start_time

        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            
            # 1. FAST JUMP (Input Option)
            "-ss", f"{fast_seek:.3f}",
            
            "-i", input_path,
            
            # 2. ACCURATE DECODE (Output Option)
            "-ss", f"{slow_seek:.3f}",
            
            # 3. CUT DURATION
            "-t", f"{duration:.3f}",
            
            "-vf", f"scale=-2:{self.output_height}",
            "-c:v", "libx264", 
            "-preset", "ultrafast", 
            "-crf", "20", 
            "-c:a", "copy",
            temp_path
        ]

        proc = None
        try:
            # Low Priority Flags for Windows
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            
            proc = subprocess.Popen(
                cmd, 
                startupinfo=startupinfo, 
                creationflags=0x00004000 # BELOW_NORMAL_PRIORITY_CLASS
            )
            
            with self.lock:
                self.ffmpeg_pids.add(proc.pid)
            
            proc.wait()
            
            with self.lock:
                if proc.pid in self.ffmpeg_pids: self.ffmpeg_pids.remove(proc.pid)

            if self.running and proc.returncode == 0 and os.path.exists(temp_path):
                os.replace(temp_path, output_path)
                # print(f"[JIT] Segment Ready: {os.path.basename(output_path)}")
            else:
                if os.path.exists(temp_path): os.remove(temp_path)
                
        except Exception as e:
            # print(f"Seg Error: {e}")
            if os.path.exists(temp_path): os.remove(temp_path)
        finally:
            # Cleanup future tracking
            with self.lock:
                # We don't remove from futures dict immediately to prevent 
                # re-queueing the same job instantly if loop is fast.
                # Ideally, we let it sit or use a 'completed' set.
                pass
# Initialize Global
proxy_manager = SmartProxyManager(output_height=540)

def play_videos(config, video_list):
    """
    Starts MPV instances and initiates the JIT Hybrid Proxy queue.
    """
    global sockets, processes, person_socket_map, interrupt_event, proxy_manager

    # --- Reset global state ---
    print("Initializing playback...")
    if sockets or processes:
        cleanup_resources()

    sockets = []
    processes = []
    person_socket_map = {}
    interrupt_event.clear()

    # --- Basic Validation ---
    if not video_list: return
    grid_size = config.column_grid * config.row_grid
    
    # --- Filter Videos ---
    valid_videos = [v for v in video_list if config.master_type in v[1]]
    if not valid_videos:
        print("Error: No valid videos found.")
        return

    # --- Distribute Videos ---
    required_count = grid_size * config.cycled_persons
    if len(valid_videos) > required_count:
        valid_videos = valid_videos[:required_count]
    elif len(valid_videos) < required_count and grid_size > 0:
        config.cycled_persons = max(1, len(valid_videos) // grid_size)
        valid_videos = valid_videos[:grid_size * config.cycled_persons]

    for i in range(grid_size):
        person_socket_map[i] = []

    for idx, (filename, metadata) in enumerate(valid_videos):
        socket_index = idx % grid_size
        person_socket_map[socket_index].append({filename: metadata})

    # --- NEW: Pre-fill the JIT Queue (Hybrid API Update) ---
    # We request the FIRST segment (Start=BaseTime, Duration=Interval) for every video
    if proxy_manager:
        print("\n[JIT] Queueing initial segments...")
        for socket_idx, vid_list in person_socket_map.items():
            for vid_entry in vid_list:
                fname = list(vid_entry.keys())[0]
                meta = list(vid_entry.values())[0]
                
                # Calculate start time from metadata
                base_sec = mmss_to_seconds(meta.get(config.master_type, "00:00"))
                
                # Request the segment immediately
                # This starts the background workers
                proxy_manager.request_segment(fname, base_sec, config.interval_cycle)

    # --- Start MPV Instances ---
    successful_starts = 0
    for i in range(grid_size):
        geometry = calculate_geometry(i, config)
        
        if not person_socket_map.get(i):
             sockets.append(None); processes.append(None)
             continue

        first_video_info = person_socket_map[i][0]
        first_filename = list(first_video_info.keys())[0]
        first_metadata = first_video_info[first_filename]
        first_start_time_str = first_metadata.get(config.master_type, "00:00")
        
        # Convert start string to seconds for checking
        first_start_sec = mmss_to_seconds(first_start_time_str)

        # Metadata calculations...
        if not config.test_mode:
             for video_dict in person_socket_map[i]:
                filename, metadata = list(video_dict.items())[0]
                try:
                    end_key, start_key = "E" + config.master_type, config.master_type
                    if end_key in metadata and start_key in metadata:
                        metadata["videoTime"] = get_duration_of_session(metadata[end_key], metadata[start_key])
                    else:
                        metadata["videoTime"] = config.default_duration_map.get(config.master_type, 60)
                except:
                    metadata["videoTime"] = 60

        # --- FIX: Check for Hybrid Segment ---
        path_to_play = first_filename
        actual_start_str = first_start_time_str # If original, use metadata time

        if proxy_manager:
            # Check if the segment we just requested is somehow already ready 
            # (Unlikely on fresh start, but possible on restart)
            seg_path = proxy_manager.get_segment_path(first_filename, first_start_sec, config.interval_cycle)
            if seg_path:
                path_to_play = seg_path
                actual_start_str = "00:00" # Proxy segments always start at 0
        
        # Ensure path is safe for Windows
        path_to_play = path_to_play.replace("\\", "/")

        command_list = create_mpv_command(path_to_play, actual_start_str, geometry, config, i)
        
        process = None
        if config.production and command_list:
            process = start_mpv_instance(command_list)
            processes.append(process)
            if process:
                try:
                    socket_handle = create_socket(i)
                    sockets.append(socket_handle)
                    successful_starts += 1
                except:
                    sockets.append(None)
        else:
             processes.append(None); sockets.append(None)

    # --- Post-Start Logic ---
    try:
        mpv_pids = [p.pid for p in processes if p]
        if mpv_pids:
            threading.Thread(target=_grid_host_thread, args=(config, mpv_pids), daemon=True).start()
    except Exception as e: print(e)

    if config.production:
        listener_thread = threading.Thread(target=run_listener)
        listener_thread.daemon = True
        listener_thread.start()

        if config.cycled_persons > 1 and grid_size > 0:
            threading.Thread(target=cycle_videos, args=(config,), daemon=True).start()
        
        listener_thread.join()
        # Ensure strict shutdown if listener returns
        force_shutdown_and_exit()
    else:
         try:
             while not interrupt_event.is_set(): time.sleep(1)
         except: pass
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
    global person_socket_map, interrupt_event, sockets, proxy_manager

    print("Cycle Thread: Started (JIT Segment + Lookahead).")

    master_key = config.master_type
    interval = config.interval_cycle
    total_time = config.total_time

    active_socket_indices = sorted(
        idx for idx, lst in person_socket_map.items() 
        if lst and idx < len(sockets) and sockets[idx] is not None
    )
    
    if not active_socket_indices: return

    num_intervals = math.ceil(total_time / interval)
    num_switches = max(0, num_intervals - 1)

    # --- 1. Data Prep ---
    per_socket_videos = {}
    for socket_idx in active_socket_indices:
        vids = []
        for entry in person_socket_map[socket_idx]:
            filename, meta = list(entry.items())[0]
            base_sec = mmss_to_seconds(meta.get(master_key, "00:00"))
            v_time = int(meta.get("videoTime", interval))
            if v_time <= 0: v_time = interval
            vids.append({
                "filename": filename, 
                "base_sec": max(0, base_sec), 
                "video_time": max(0, v_time)
            })
        per_socket_videos[socket_idx] = vids

    segment_offsets = {}
    for socket_idx, vids in per_socket_videos.items():
        n = len(vids)
        if n == 0: continue
        approx_occ = max(1, math.ceil(num_intervals / n))
        for vid_idx, v in enumerate(vids):
            max_start = max(0, v["video_time"] - interval)
            if max_start == 0: offsets = [0] * approx_occ
            elif approx_occ == 1: offsets = [max_start // 2]
            else:
                step = max_start / (approx_occ - 1)
                offsets = [int(round(i * step)) for i in range(approx_occ)]
            segment_offsets[(socket_idx, vid_idx)] = offsets

    # Usage tracking
    usage_counts = {} 
    for s_idx, vids in per_socket_videos.items():
        if vids: usage_counts[(s_idx, 0)] = 1

    start_mono = time.monotonic()
    print(f"Cycle Thread: First switch at T+{interval}s.")

    # --- 2. Helper: Predict Next Video ---
    def get_predictive_params(s_idx, target_switch_idx):
        """
        Calculates which video/offset will play at 'target_switch_idx'.
        """
        vids = per_socket_videos.get(s_idx)
        if not vids: return None, 0
        
        # Round Robin Index
        vid_idx = target_switch_idx % len(vids)
        v = vids[vid_idx]
        key = (s_idx, vid_idx)
        
        # Predict usage count (How many times has THIS video played so far?)
        # It plays every len(vids) switches.
        # Current usage = 1 + (target_switch_idx // len(vids)) roughly
        # We clamp this to valid offsets
        pred_usage = target_switch_idx // len(vids)
        if target_switch_idx % len(vids) >= vid_idx:
            pred_usage += 1 # It has played in the current rotation
            
        # Since usage is 1-based in our loop logic (start=1), we subtract 1 for index
        offsets = segment_offsets.get(key, [0])
        occ_idx = min(max(0, pred_usage - 1), len(offsets) - 1)
        
        abs_start = v["base_sec"] + offsets[occ_idx]
        return v["filename"], abs_start

    # --- 3. Pre-Queue Switch #1 ---
    if proxy_manager:
        print("[Lookahead] Queueing segments for Switch #1...")
        for s_idx in active_socket_indices:
            fname, fstart = get_predictive_params(s_idx, 1)
            if fname:
                proxy_manager.request_segment(fname, fstart, interval)

    def send_cmd(idx, cmd_str):
        try:
            if idx < len(sockets) and sockets[idx]:
                sockets[idx].send(cmd_str)
        except: pass

    # --- 4. Main Loop ---
    for switch_idx in range(1, num_switches + 1):
        if interrupt_event.is_set(): break

        target = start_mono + switch_idx * interval
        wait = target - time.monotonic()
        if wait > 0:
            if interrupt_event.wait(timeout=wait): break

        print(f"--- Cycle Switch {switch_idx}/{num_switches} ---")

        # A. TRIGGER LOOKAHEAD for NEXT switch (Switch + 1)
        next_switch = switch_idx + 1
        if proxy_manager and next_switch <= num_switches:
            for s_idx in active_socket_indices:
                fname, fstart = get_predictive_params(s_idx, next_switch)
                if fname:
                    proxy_manager.request_segment(fname, fstart, interval)

        # B. PLAY CURRENT
        for s_idx in active_socket_indices:
            vids = per_socket_videos.get(s_idx)
            if not vids: continue
            
            vid_idx = switch_idx % len(vids)
            v = vids[vid_idx]
            key = (s_idx, vid_idx)
            
            offsets = segment_offsets.get(key, [0])
            occ_idx = min(usage_counts.get(key, 0), len(offsets) - 1)
            final_start_sec = v["base_sec"] + offsets[occ_idx]
            usage_counts[key] = usage_counts.get(key, 0) + 1

            # 1. Check for Segment
            play_path = v["filename"]
            start_pos = final_start_sec
            
            if proxy_manager:
                # Request exactly what we expect: start_time, length=interval
                seg_path = proxy_manager.get_segment_path(v["filename"], final_start_sec, interval)
                if seg_path:
                    play_path = seg_path
                    start_pos = 0 # Segment is trimmed, so start at 0
            
            # 2. Send Command
            play_path = play_path.replace("\\", "/")
            final_start_str = seconds_to_mmss(start_pos)
            cmd = f'loadfile "{play_path}" replace "start={final_start_str}"'
            send_cmd(s_idx, cmd)

    print("Cycle Thread: Finished.")


def cleanup_resources():
    """Closes all IPC clients and terminates all MPV processes gracefully."""
    global sockets, processes, interrupt_event, proxy_manager
    print("--- Starting Resource Cleanup ---")
    
    if 'interrupt_event' in globals() and not interrupt_event.is_set():
        interrupt_event.set()

    # 1. Shutdown Proxy Manager
    if proxy_manager:
        try:
            # The new Hybrid class uses .shutdown()
            if hasattr(proxy_manager, 'shutdown'):
                proxy_manager.shutdown()
            # Fallback if using an older version
            elif hasattr(proxy_manager, 'stop'):
                proxy_manager.stop()
        except Exception as e:
            print(f"Error stopping proxy manager: {e}")

    # 2. Close Sockets
    print(f"Closing {len(sockets)} socket handles...")
    for i, client in enumerate(sockets):
        if client:
            try:
                # Check if it's a client object or raw handle (just in case)
                if hasattr(client, 'close'):
                    client.close()
                else:
                    win32file.CloseHandle(client)
            except Exception: pass
        sockets[i] = None
    sockets = []

    # 3. Terminate Processes
    print(f"Terminating {len(processes)} MPV processes...")
    for proc in processes:
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            except Exception: pass
    processes = []
    print("--- Resource Cleanup Complete ---")

def force_shutdown_and_exit():
    """
    Instantly kills all mpv processes, ffmpeg processes, and terminates the script.
    """
    global processes, proxy_manager
    print("\n--- IMMEDIATE SHUTDOWN INITIATED ---")

    # 1. Shutdown Proxy Manager & Kill Child FFmpegs
    if proxy_manager:
        try:
            if hasattr(proxy_manager, 'shutdown'):
                proxy_manager.shutdown()
            
            # The Hybrid class tracks PIDs in 'ffmpeg_pids'
            # We iterate and kill them explicitly
            if hasattr(proxy_manager, 'ffmpeg_pids') and hasattr(proxy_manager, 'lock'):
                with proxy_manager.lock:
                    for pid in list(proxy_manager.ffmpeg_pids):
                        try:
                            print(f"  Killing FFmpeg PID: {pid}")
                            subprocess.run(f"taskkill /F /T /PID {pid}", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        except: pass
        except Exception as e:
            print(f"  Error cleaning up proxy manager: {e}")

    # 2. Nuclear Safety Net: Kill ANY ffmpeg.exe
    # (In case the PID tracking missed one)
    try:
        subprocess.run("taskkill /F /IM ffmpeg.exe", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except: pass

    # 3. Kill MPV Processes
    killed_count = 0
    for proc in processes:
        if proc and proc.poll() is None:
            try:
                subprocess.run(f"taskkill /F /T /PID {proc.pid}", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                killed_count += 1
            except: pass

    print(f"--- Nuke complete. Killed {killed_count} MPV processes. Exiting script NOW. ---")
    os._exit(0)
def on_press(key):
    """Keyboard listener callback for key presses. 'q' or ESC triggers immediate shutdown."""
    should_quit = False
    try:
        if hasattr(key, 'char') and key.char == 'q':
            should_quit = True
        elif key == Key.esc:
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
    """Main function with Dynamic Proxy Resolution calculation."""
    print("--- Video Grid Player Initializing ---")
    global proxy_manager
    
    try:
        # --- Monitor Selection ---
        monitor_geometry = select_monitor()
        if not monitor_geometry: return
        
        input_geometry = monitor_geometry[:2]
        monitor_offset = monitor_geometry[2:]
        
        # --- User Inputs ---
        master_type = input("Enter Master Type [PC/RC/PM/D/M/CSP] (Default: RC): ").strip().upper() or "RC"
        
        try:
            col_str = input(f"Horizontal Videos (Columns) (Default: 1): ").strip()
            column_grid = int(col_str) if col_str else 1
            row_str = input(f"Vertical Videos (Rows) (Default: 1): ").strip()
            row_grid = int(row_str) if row_str else 1
        except ValueError:
            column_grid, row_grid = 1, 1

        # --- Cycling Config ---
        grid_size = column_grid * row_grid
        cycled_persons = 1; interval_cycle = 15; total_time = 300
        
        if grid_size > 0:
            try:
                c_str = input("Videos per window (Default: 1): ").strip()
                cycled_persons = max(1, int(c_str)) if c_str else 1
            except: pass
            
            if cycled_persons > 1:
                # (Simplified your extensive interval/time logic for brevity, 
                #  but keep your original prompt logic here if you prefer)
                if master_type == 'CSP':
                    interval_cycle = 28
                else:
                    try:
                        i_str = input(f"Interval (s) (Default: {interval_cycle}): ").strip()
                        if i_str: interval_cycle = int(i_str)
                    except: pass
                try:
                    t_str = input("Duration (min) (Default: 5): ").strip()
                    if t_str: total_time = int(t_str) * 60
                except: pass

        # --- Config Object ---
        config = VideoPlayerConfig(
            master_type=master_type, column_grid=column_grid, row_grid=row_grid,
            cycled_persons=cycled_persons, interval_cycle=interval_cycle, total_time=total_time,
            input_geometry=input_geometry, pixel_drop=0, crop_height=0.88, crop_width=0,
            production=True, test_mode=False, monitor_offset=monitor_offset
        )
        
        # Fix Pixel Drop / Crop Width (Your original logic)
        config.pixel_drop = config.pixel_drop_map.get(master_type, 100)
        try:
            cell_w = input_geometry[0] / column_grid
            cell_h = input_geometry[1] / row_grid
            denom = cell_h + config.pixel_drop
            calc_w = (cell_w / denom * (input_geometry[1] * config.crop_height)) / input_geometry[0]
            calc_w = max(0.0, min(calc_w, 1.0))
        except: calc_w = 0.8
        
        config.crop_width = {
            "PM": 1.0 
        }.get(master_type, calc_w)

        # --- NEW: DYNAMIC PROXY CALCULATION ---
        # 1. Determine ideal proxy height based on grid rows
        if row_grid > 0:
            raw_height = input_geometry[1] / row_grid
        else:
            raw_height = 540

        # 2. Round to nearest even integer
        target_proxy_height = int(raw_height)
        if target_proxy_height % 2 != 0: target_proxy_height += 1
        
        # 3. Clamp (144p min, 1080p max)
        target_proxy_height = max(144, min(target_proxy_height, 1080))
        
        print(f"\n[Config] Grid is {column_grid}x{row_grid}. Dynamic Proxy Target: {target_proxy_height}p")
        
        # 4. Initialize the Global Manager
# Initialize the SEGMENT Manager (renamed to SmartProxyManager)
        proxy_manager = SmartProxyManager(output_height=target_proxy_height, max_workers=4)
        # --- Start ---
        video_list = prepare_video_list()
        if video_list and grid_size > 0:
            play_videos(config, video_list)

    except KeyboardInterrupt:
        if interrupt_event: interrupt_event.set()
    finally:
        cleanup_resources()
if __name__ == "__main__":
    # Ensure the interrupt event exists globally before starting
    interrupt_event = threading.Event()
    main()