"""Ask every running gdpnow mpv (pipes mpvsocket0..N) what file it plays and where.

    python probe_mpv.py [N=4] [delay_seconds=0]
"""
import json
import sys
import time

n = int(sys.argv[1]) if len(sys.argv) > 1 else 4
time.sleep(float(sys.argv[2]) if len(sys.argv) > 2 else 0)


def ask(pipe, prop):
    try:
        with open(pipe, "r+b", buffering=0) as f:
            f.write((json.dumps({"command": ["get_property", prop], "request_id": 1}) + "\n").encode())
            buf = b""
            while True:
                ch = f.read(1)
                if not ch:
                    return None
                if ch != b"\n":
                    buf += ch
                    continue
                msg = json.loads(buf)
                buf = b""
                if msg.get("request_id") == 1:
                    return msg.get("data")
    except OSError as e:
        return f"<{e.__class__.__name__}>"


for i in range(n):
    pipe = rf"\\.\pipe\mpvsocket{i}"
    print(f"cell {i}: path={ask(pipe, 'path')!r} time-pos={ask(pipe, 'time-pos')} "
          f"paused={ask(pipe, 'pause')} mpv={ask(pipe, 'mpv-version')}")
