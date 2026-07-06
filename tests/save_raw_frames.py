"""Save the raw SignalR frames to JSON files for analysis.

Run from the project root: python tests/save_raw_frames.py
"""
import json
import sys
import warnings
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

from playwright.sync_api import sync_playwright

raw_sent = []
raw_recv = []

def on_ws(ws):
    if "substrate.svc.cloud.microsoft" not in ws.url:
        return
    ws.on("framesent", lambda d: raw_sent.append(d if isinstance(d, str) else bytes(d).decode("utf-8", "ignore")))
    ws.on("framereceived", lambda d: raw_recv.append(d if isinstance(d, str) else bytes(d).decode("utf-8", "ignore")))

with sync_playwright() as pw:
    ctx = pw.chromium.launch_persistent_context(
        "session/profile",
        headless=False,
        args=["--disable-blink-features=AutomationControlled"],
        ignore_default_args=["--enable-automation"],
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.on("websocket", on_ws)
    page.goto("https://m365.cloud.microsoft/chat/", wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(4000)
    for sel in ("[aria-label='New chat']", "[aria-label='New Chat']"):
        try:
            page.click(sel, timeout=2000)
            page.wait_for_timeout(1500)
            break
        except Exception:
            pass
    for sel in ("[aria-label='Message Copilot']", "[aria-label*='Message']",
                "div[contenteditable='true']", "[role='textbox']"):
        try:
            page.wait_for_selector(sel, state="visible", timeout=4000)
            page.click(sel)
            page.keyboard.type("hi", delay=50)
            page.keyboard.press("Enter")
            break
        except Exception:
            pass
    page.wait_for_timeout(15000)
    ctx.close()

# Parse and save all frames
all_frames = []
for raw in raw_sent:
    for part in raw.split("\x1e"):
        if part.strip():
            try:
                all_frames.append({"direction": "sent", "parsed": json.loads(part)})
            except Exception:
                all_frames.append({"direction": "sent", "raw": part})
for raw in raw_recv:
    for part in raw.split("\x1e"):
        if part.strip():
            try:
                all_frames.append({"direction": "recv", "parsed": json.loads(part)})
            except Exception:
                all_frames.append({"direction": "recv", "raw": part})

out = Path("tests/raw_frames.json")
out.write_text(json.dumps(all_frames, indent=2), encoding="utf-8")
print(f"Saved {len(all_frames)} frames to {out}")

# Print summary of target/type fields
for i, f in enumerate(all_frames):
    p = f.get("parsed", {})
    direction = f["direction"]
    t = p.get("type", "?")
    target = p.get("target", "")
    print(f"  [{i}] {direction:4} type={t} target={target!r}")
