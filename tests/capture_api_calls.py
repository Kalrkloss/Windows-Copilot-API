"""Capture all API calls the m365 chat SPA makes on load and interaction.

Run from the project root: python tests/capture_api_calls.py
"""
import json
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

from playwright.sync_api import sync_playwright

captured = []

def on_request(req):
    if any(p in req.url for p in ["/c/api/", "/chat/api", "/chat/c/"]):
        body = ""
        try:
            body = req.post_data or ""
        except Exception:
            pass
        captured.append({
            "method": req.method,
            "url": req.url,
            "headers": {k: v for k, v in req.headers.items()
                        if k.lower() not in ("user-agent", "sec-ch-ua", "sec-ch-ua-platform",
                                             "sec-ch-ua-mobile", "sec-fetch-site", "sec-fetch-mode",
                                             "sec-fetch-dest", "accept-encoding", "accept-language",
                                             "sec-ch-prefers-color-scheme")},
            "body": body[:300],
        })

def on_response(resp):
    for c in captured:
        if c["url"] == resp.url and "status" not in c:
            c["status"] = resp.status
            c["response_ct"] = resp.headers.get("content-type", "")
            try:
                c["response_body"] = resp.text()[:300]
            except Exception:
                c["response_body"] = "<error reading body>"

with sync_playwright() as pw:
    ctx = pw.chromium.launch_persistent_context(
        "session/profile",
        headless=True,
        args=["--disable-blink-features=AutomationControlled"],
        ignore_default_args=["--enable-automation"],
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.on("request", on_request)
    page.on("response", on_response)

    print("Loading m365 chat page (watching all /c/api/ requests)...")
    page.goto("https://m365.cloud.microsoft/chat/", wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(5000)

    # Try to trigger a conversation by interacting with the page
    print("Attempting to trigger conversation creation via UI interaction...")

    # Try clicking New Chat button
    for sel in ("[aria-label='New chat']", "[aria-label='New Chat']",
                "button[data-tid='new-chat-button']", "[aria-label*='new']"):
        try:
            page.click(sel, timeout=2000)
            page.wait_for_timeout(1500)
            print(f"  Clicked: {sel}")
            break
        except Exception:
            pass

    # Try to find and click the text input to trigger API calls
    for sel in ("[aria-label='Message Copilot']", "[aria-label*='Message']",
                "[aria-label*='message']", "div[contenteditable='true']",
                "[role='textbox']", "textarea"):
        try:
            page.wait_for_selector(sel, state="visible", timeout=4000)
            page.click(sel)
            page.keyboard.type("hi", delay=50)
            page.wait_for_timeout(2000)  # wait for any pre-send API calls
            print(f"  Typed in: {sel}")
            break
        except Exception:
            pass

    # Wait for API calls to complete
    page.wait_for_timeout(3000)

    print(f"\nCaptured {len(captured)} API calls:")
    for c in captured:
        print(f"\n  {c['method']} {c['url']}")
        print(f"  Status: {c.get('status', '?')}  Content-Type: {c.get('response_ct', '?')}")
        if c.get("body"):
            print(f"  Request body: {c['body']!r}")
        important_headers = {k: v for k, v in c["headers"].items()
                             if k.lower() not in ("cookie",)}
        for k, v in important_headers.items():
            print(f"  {k}: {v[:80]}")
        if c.get("response_body") and c.get("status", 0) not in (200,):
            print(f"  Response: {c['response_body'][:150]!r}")
        elif c.get("response_body") and len(c.get("response_body","")) < 200:
            print(f"  Response: {c['response_body']!r}")

    ctx.close()
