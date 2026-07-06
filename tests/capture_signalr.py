"""Capture the full SignalR chat exchange on m365.cloud.microsoft.

Run from the project root: python tests/capture_signalr.py
Captures ALL frames from the substrate.svc.cloud.microsoft WebSocket.
"""
import json
import sys
import warnings
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

from playwright.sync_api import sync_playwright

ws_data = []   # list of {url, params, sent: [], received: []}

def on_ws(ws):
    url = ws.url
    if "substrate.svc.cloud.microsoft" not in url:
        return
    parsed = urlparse(url)
    params = {k: v[0] if len(v) == 1 else v for k, v in parse_qs(parsed.query).items()}
    entry = {
        "url": url[:120] + "...",
        "path": parsed.path,
        "ConversationId": params.get("ConversationId", "?"),
        "chatsessionid": params.get("chatsessionid", "?"),
        "access_token": params.get("access_token", "")[:50] + "...",
        "sent": [],
        "received": [],
    }
    ws_data.append(entry)

    def on_sent(data):
        text = data if isinstance(data, str) else bytes(data).decode("utf-8", "ignore")
        entry["sent"].append(text)

    def on_recv(data):
        text = data if isinstance(data, str) else bytes(data).decode("utf-8", "ignore")
        entry["received"].append(text)

    ws.on("framesent", on_sent)
    ws.on("framereceived", on_recv)


with sync_playwright() as pw:
    ctx = pw.chromium.launch_persistent_context(
        "session/profile",
        headless=False,
        args=["--disable-blink-features=AutomationControlled"],
        ignore_default_args=["--enable-automation"],
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.on("websocket", on_ws)

    print("Loading m365 chat page...")
    page.goto("https://m365.cloud.microsoft/chat/", wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(5000)

    for sel in ("[aria-label='New chat']", "[aria-label='New Chat']"):
        try:
            page.click(sel, timeout=2000)
            page.wait_for_timeout(1500)
            print(f"Clicked: {sel}")
            break
        except Exception:
            pass

    for sel in ("[aria-label='Message Copilot']", "[aria-label*='Message']",
                "div[contenteditable='true']", "[role='textbox']"):
        try:
            page.wait_for_selector(sel, state="visible", timeout=4000)
            page.click(sel)
            page.keyboard.type("Say hello in exactly 5 words.", delay=50)
            page.wait_for_timeout(500)
            page.keyboard.press("Enter")
            print(f"Message sent via: {sel}")
            break
        except Exception:
            pass

    # Wait for full chat response (up to 30s)
    print("Waiting for chat response...")
    page.wait_for_timeout(20000)
    ctx.close()

print(f"\n=== Captured {len(ws_data)} substrate.svc.cloud.microsoft WebSockets ===")
for i, ws in enumerate(ws_data):
    print(f"\n--- WebSocket {i+1} ---")
    print(f"  Path: {ws['path']}")
    print(f"  ConversationId: {ws['ConversationId']}")
    print(f"  access_token start: {ws['access_token']}")
    print(f"  Sent frames: {len(ws['sent'])}")
    print(f"  Received frames: {len(ws['received'])}")
    print("\n  SENT frames:")
    for f in ws["sent"]:
        for part in f.split("\x1e"):
            if part.strip():
                try:
                    parsed = json.loads(part)
                    print(f"    {json.dumps(parsed, indent=2)[:3000]}")
                except Exception:
                    print(f"    {part[:3000]!r}")
    print("\n  ALL RECEIVED frames:")
    for j, f in enumerate(ws["received"]):
        for part in f.split("\x1e"):
            if part.strip():
                try:
                    parsed = json.loads(part)
                    print(f"    [{j}] {json.dumps(parsed, indent=2)[:1000]}")
                except Exception:
                    print(f"    [{j}] {part[:200]!r}")
