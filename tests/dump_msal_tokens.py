"""Dump MSAL tokens from m365.cloud.microsoft/chat localStorage.

Run from the project root: python tests/dump_msal_tokens.py
"""
import json
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

from playwright.sync_api import sync_playwright

FIND_TOKENS_JS = """
() => {
    const tokens = [];
    for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i);
        const v = localStorage.getItem(k);
        if (!v) continue;
        if (v.includes("credentialType") || v.includes("AccessToken")) {
            try {
                const o = JSON.parse(v);
                tokens.push({
                    key: k.substring(0, 80),
                    target: o.target || "",
                    credType: o.credentialType || "",
                    hasSecret: !!o.secret,
                    secretLen: (o.secret || "").length,
                    secretStart: (o.secret || "").substring(0, 20)
                });
            } catch(e) {
                tokens.push({key: k.substring(0, 80), raw: v.substring(0, 80)});
            }
        }
    }
    // Also list all localStorage keys
    const allKeys = [];
    for (let i = 0; i < localStorage.length; i++) {
        allKeys.push(localStorage.key(i));
    }
    return {tokens: tokens, allKeyCount: allKeys.length, keysSample: allKeys.slice(0, 30)};
}
"""

with sync_playwright() as pw:
    ctx = pw.chromium.launch_persistent_context(
        "session/profile",
        headless=True,
        args=["--disable-blink-features=AutomationControlled"],
        ignore_default_args=["--enable-automation"],
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    print("Loading m365 chat page...")
    page.goto("https://m365.cloud.microsoft/chat/", wait_until="domcontentloaded")
    page.wait_for_timeout(5000)
    print("Page loaded. Dumping localStorage tokens...")

    result = page.evaluate(FIND_TOKENS_JS)

    print(f"\nTotal localStorage keys: {result['allKeyCount']}")
    print(f"Sample keys: {result['keysSample'][:15]}")
    print(f"\nMSAL credential entries: {len(result['tokens'])}")
    for t in result["tokens"]:
        if "raw" in t:
            print(f"  [raw] key={t['key']!r}")
        else:
            print(f"  target={t['target']!r:70}  type={t['credType']!r}  secretLen={t['secretLen']}")

    ctx.close()
