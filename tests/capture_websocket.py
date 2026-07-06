"""Capture WebSocket connections the m365 SPA opens when sending a chat message.

Run from the project root: python tests/capture_websocket.py
"""
import json
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

from playwright.sync_api import sync_playwright

captured_ws = []
captured_requests = []

def on_ws(ws):
    captured_ws.append({"url": ws.url, "frames": []})
    def on_frame(data):
        try:
            text = data if isinstance(data, str) else bytes(data).decode("utf-8", "ignore")
            captured_ws[-1]["frames"].append(text[:300])
        except Exception:
            pass
    ws.on("framesent", on_frame)
    ws.on("framereceived", on_frame)

def on_request(req):
    if req.resource_type in ("xhr", "fetch") and "microsoft" in req.url:
        captured_requests.append({
            "method": req.method,
            "url": req.url,
            "body": (req.post_data or "")[:200],
        })

with sync_playwright() as pw:
    ctx = pw.chromium.launch_persistent_context(
        "session/profile",
        headless=False,  # visible so we can see what happens
        args=["--disable-blink-features=AutomationControlled"],
        ignore_default_args=["--enable-automation"],
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.on("websocket", on_ws)
    page.on("request", on_request)

    print("Loading m365 chat page...")
    page.goto("https://m365.cloud.microsoft/chat/", wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(4000)

    # Click New chat
    for sel in ("[aria-label='New chat']", "[aria-label='New Chat']"):
        try:
            page.click(sel, timeout=2000)
            page.wait_for_timeout(1500)
            print(f"Clicked: {sel}")
            break
        except Exception:
            pass

    # Type and send message
    for sel in ("[aria-label='Message Copilot']", "[aria-label*='Message']",
                "div[contenteditable='true']", "[role='textbox']"):
        try:
            page.wait_for_selector(sel, state="visible", timeout=4000)
            page.click(sel)
            page.keyboard.type("hi", delay=50)
            page.wait_for_timeout(500)
            page.keyboard.press("Enter")
            print(f"Sent message via: {sel}")
            break
        except Exception:
            pass

    # Wait for WebSocket activity
    page.wait_for_timeout(8000)

    print(f"\n=== WebSocket connections: {len(captured_ws)} ===")
    for ws in captured_ws:
        print(f"  URL: {ws['url']}")
        print(f"  Frames: {len(ws['frames'])}")
        for f in ws["frames"][:3]:
            print(f"    {f[:150]!r}")

    print(f"\n=== XHR/Fetch requests: {len(captured_requests)} ===")
    for r in captured_requests[:10]:
        print(f"  {r['method']} {r['url']}")
        if r["body"]:
            print(f"    body: {r['body']!r}")

    ctx.close()
