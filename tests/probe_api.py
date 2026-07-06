"""Quick probe: find the correct conversation-creation endpoint on the configured host.

Run from the project root:
    python tests/probe_api.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from copilot.auth import DEFAULT_AUTH_FILE
from copilot.driver import _resolve_ssl_verify
from copilot.useragent import CHROME_CLIENT_HINTS, CHROME_UA, IMPERSONATE_TARGET
from curl_cffi.requests import Session

TOKEN_FILE = Path("examples") / DEFAULT_AUTH_FILE

if not TOKEN_FILE.exists():
    TOKEN_FILE = Path(DEFAULT_AUTH_FILE)
if not TOKEN_FILE.exists():
    sys.exit(f"No token.json found (tried {TOKEN_FILE}). Run `python -m copilot login` first.")

auth = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
access_token = auth.get("access_token") or ""
cookies = auth.get("cookies") or {}

BASE = "https://m365.cloud.microsoft"
EXTRA_BASES = ["https://copilot.microsoft.com"]
CANDIDATE_PATHS = [
    "/c/api/conversations",          # current (405)
    "/chat/c/api/conversations",     # with /chat prefix
]

# Use False here so the probe works regardless of whether REQUESTS_CA_BUNDLE
# is set in the current shell — this is just endpoint discovery, not production.
verify = False
auth_header = {"Authorization": f"Bearer {access_token}"} if access_token else {}

print(f"Using token: {'yes (' + str(len(access_token)) + ' chars)' if access_token else 'no'}")
print(f"Cookies:     {list(cookies.keys())}")
print(f"SSL verify:  {verify!r}")
print()

with Session(
    impersonate=IMPERSONATE_TARGET,
    headers={"User-Agent": CHROME_UA, **CHROME_CLIENT_HINTS},
    cookies=cookies,
    verify=verify,
) as s:
    for base in [BASE] + EXTRA_BASES:
        print(f"\n--- {base} ---")
        print(f"{'PATH':<45}  {'GET':>6}  {'POST (no auth)':>15}  {'POST (bearer)':>14}")
        print("-" * 90)
        for path in CANDIDATE_PATHS:
            url = base + path
            try:
                rg = s.get(url, headers=auth_header, allow_redirects=False)
                rg_code = str(rg.status_code)
            except Exception as e:
                rg_code = f"ERR"

            try:
                rp_no_auth = s.post(url, json={}, allow_redirects=False)
                rp_no_auth_code = str(rp_no_auth.status_code)
            except Exception as e:
                rp_no_auth_code = "ERR"

            try:
                rp_auth = s.post(url, headers=auth_header, json={}, allow_redirects=False)
                rp_auth_code = str(rp_auth.status_code)
                if rp_auth.status_code not in (405, 404, 301, 302, 307, 308):
                    print(f"  *** {rp_auth.status_code} POST {path} → {rp_auth.text[:160]!r}")
            except Exception as e:
                rp_auth_code = "ERR"

            print(f"  {path:<43}  {rg_code:>6}  {rp_no_auth_code:>15}  {rp_auth_code:>14}")
