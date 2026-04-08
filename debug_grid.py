import win32gui
import win32con
import subprocess
import time
import threading
import win32file
import json
import sys
import os

# --- CONFIGURATION ---
# UPDATE THIS PATH to your video file
TEST_VIDEO = r"E:\1SystemFile\246.mp4" 
SOCKET_NAME = r"\\.\pipe\mpv_debug_socket"

def create_host_window():
    """Creates Host + Child Container."""
    print("[GUI] Creating Host Window...")
    wc = win32gui.WNDCLASS()
    wc.lpfnWndProc = win32gui.DefWindowProc
    wc.lpszClassName = "DebugHostSync"
    wc.hbrBackground = win32gui.GetStockObject(win32con.BLACK_BRUSH)
    try: win32gui.RegisterClass(wc)
    except: pass

    hwnd_main = win32gui.CreateWindowEx(
        0, "DebugHostSync", "DEBUG SYNC TEST",
        win32con.WS_OVERLAPPEDWINDOW | win32con.WS_VISIBLE | win32con.WS_CLIPCHILDREN,
        100, 100, 800, 600, 0, 0, win32gui.GetModuleHandle(None), None
    )

    hwnd_child = win32gui.CreateWindowEx(
        0, "Static", "",
        win32con.WS_CHILD | win32con.WS_VISIBLE | win32con.WS_CLIPSIBLINGS | win32con.WS_CLIPCHILDREN,
        50, 50, 700, 500, hwnd_main, 0, win32gui.GetModuleHandle(None), None
    )
    return hwnd_main, hwnd_child

def launch_mpv(wid):
    """Launches MPV Clean (No Pause, No Speed Hacks)."""
    if not os.path.exists(TEST_VIDEO):
        print(f"[ERROR] Video not found: {TEST_VIDEO}")
        sys.exit(1)

    cmd = [
        "mpv", TEST_VIDEO,
        f"--wid={wid}",
        f"--input-ipc-server={SOCKET_NAME}",
        
        # --- CRITICAL: BOOT NORMAL ---
        # No --pause, no --speed. Keep D3D11 alive.
        "--loop=inf", 
        "--force-window=immediate",
        "--no-border",
        
        # --- HARDWARE ---
        "--hwdec=d3d11va", "--gpu-api=d3d11", "--vo=gpu"
    ]
    
    print(f"[MPV] Launching Clean: {cmd}")
    return subprocess.Popen(cmd)

def send_json_command(handle, cmd_dict):
    """Helper to send JSON and wait for read."""
    msg = json.dumps(cmd_dict) + "\n"
    win32file.WriteFile(handle, msg.encode('utf-8'))
    
    # Read response (Simple blocking read)
    hr, data = win32file.ReadFile(handle, 4096)
    response = data.decode('utf-8').strip()
    return response

def ipc_sync_test_thread():
    """Performs the Sync Logic Test."""
    print("[IPC] Thread started. Waiting 2s for D3D11 warmup...")
    time.sleep(2.0)
    
    print(f"[IPC] Connecting to {SOCKET_NAME}...")
    try:
        handle = win32file.CreateFile(
            SOCKET_NAME,
            win32file.GENERIC_READ | win32file.GENERIC_WRITE,
            0, None, win32file.OPEN_EXISTING, 0, None
        )
    except Exception as e:
        print(f"[IPC] FATAL: Connection failed. {e}")
        return

    print("[IPC] Connected!")

    # --- STEP 1: PAUSE ---
    print("\n[IPC] TEST 1: Sending PAUSE command...")
    try:
        resp = send_json_command(handle, { "command": ["set_property", "pause", True] })
        print(f"[IPC] Response: {resp}")
        print("[IPC] >>> PAUSE SUCCESSFUL (No Deadlock!)")
    except Exception as e:
        print(f"[IPC] FATAL: Pause failed. Deadlock? {e}")
        return

    # --- STEP 2: BUFFER WAIT ---
    print("\n[IPC] Waiting 2s (Simulating Buffering)...")
    time.sleep(2.0)

    # --- STEP 3: UNPAUSE ---
    print("\n[IPC] TEST 2: Sending UNPAUSE command...")
    try:
        resp = send_json_command(handle, { "command": ["set_property", "pause", False] })
        print(f"[IPC] Response: {resp}")
        print("[IPC] >>> UNPAUSE SUCCESSFUL")
        print("\n[RESULT] THEORY CONFIRMED: Booting Normal -> Syncing Later works.")
    except Exception as e:
        print(f"[IPC] FATAL: Unpause failed. {e}")

    win32file.CloseHandle(handle)

if __name__ == "__main__":
    h_main, h_child = create_host_window()
    
    t = threading.Thread(target=ipc_sync_test_thread, daemon=True)
    t.start()
    
    proc = launch_mpv(h_child)
    
    print("[GUI] Entering Message Loop...")
    import win32gui
    try:
        while t.is_alive():
            win32gui.PumpWaitingMessages()
            if proc.poll() is not None: break
            time.sleep(0.01)
    except KeyboardInterrupt:
        pass
        
    proc.terminate()