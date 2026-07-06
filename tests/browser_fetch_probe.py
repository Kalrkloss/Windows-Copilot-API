"""Probe the conversation API by running fetch() inside the Playwright browser.

The browser handles all auth transparently (cookies, CSRF, etc.).
Run from the project root: python tests/browser_fetch_probe.py
"""
import json
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

from playwright.sync_api import sync_playwright

PROFILE = "session/profile"

JS = """
async () => {
    const r = await fetch("/chat/c/api/conversations", {
        method: "POST",
        headers: {"Content-Type": "application/json", "Accept": "application/json"},
        body: JSON.stringify({})
    });
    const body = await r.text();
    // Also capture request headers that the browser sent
    return {status: r.status, ct: r.headers.get("content-type"), body: body.slice(0, 500)};
}
"""

JS_HEADERS = """
async () => {
    // Use XMLHttpRequest to capture sent headers
    return new Promise((resolve) => {
        const xhr = new XMLHttpRequest();
        xhr.open("POST", "/chat/c/api/conversations");
        xhr.setRequestHeader("Content-Type", "application/json");
        xhr.setRequestHeader("Accept", "application/json");
        xhr.onload = () => resolve({
            status: xhr.status,
            ct: xhr.getResponseHeader("content-type"),
            body: xhr.responseText.slice(0, 500)
        });
        xhr.onerror = () => resolve({error: "network error"});
        xhr.send(JSON.stringify({}));
    });
}
"""

with sync_playwright() as pw:
    ctx = pw.chromium.launch_persistent_context(
        PROFILE,
        headless=True,
        args=["--disable-blink-features=AutomationControlled"],
        ignore_default_args=["--enable-automation"],
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    print("Loading m365 chat page...")
    page.goto("https://m365.cloud.microsoft/chat/", wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    print("Page loaded. Running fetch probe...")

    result = page.evaluate(JS)
    print("fetch result:", json.dumps(result, indent=2))

    # Also try intercepting the actual network request to see request headers
    print()
    print("Intercepting real request headers...")
    captured = []
    def on_request(req):
        if "/c/api/conversations" in req.url:
            captured.append({"url": req.url, "method": req.method, "headers": dict(req.headers)})
    page.on("request", on_request)

    page.evaluate(JS_HEADERS)
    page.wait_for_timeout(1000)

    if captured:
        for c in captured:
            print("  URL:", c["url"])
            for k, v in c["headers"].items():
                if k.lower() not in ("user-agent", "sec-ch-ua", "sec-ch-ua-platform", "sec-ch-ua-mobile"):
                    print(f"    {k}: {v[:80]}")
    else:
        print("  No /c/api/conversations requests intercepted")

    ctx.close()
