"""Evaluate JS in the GDPNow window through WebView2's remote debugging port.

    python cdp_eval.py PORT "js expression"
"""
import asyncio
import json
import sys
import urllib.request

import websockets


async def main(port, expr):
    pages = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json"))
    page = next(p for p in pages if p.get("type") == "page")
    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=50_000_000) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                  "params": {"expression": expr, "awaitPromise": True, "returnByValue": True}}))
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("id") == 1:
                r = msg["result"]
                print(json.dumps(r.get("result", {}).get("value", r), indent=1)[:4000])
                return


asyncio.run(main(sys.argv[1], sys.argv[2]))
