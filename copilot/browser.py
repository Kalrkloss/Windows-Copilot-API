"""Browser-backed Copilot driver.

A Playwright fallback for the pure-HTTP :class:`copilot.client.Copilot`: it runs
the *exact same protocol* inside a real browser that already holds Cloudflare
clearance and (optionally) a signed-in Microsoft session. Useful if Microsoft
ever escalates the challenge to a Cloudflare Turnstile CAPTCHA, which needs a
browser-solved token.

``BrowserCopilot`` launches a **persistent** Playwright Chromium profile so that
Cloudflare clearance and any sign-in survive restarts. The chat protocol
(``POST /c/api/conversations`` then a ``wss://.../c/api/chat`` WebSocket speaking
``send`` -> ``appendText``* -> ``done``) is executed *in the page* via
``page.evaluate`` so the browser's own ``fetch``/``WebSocket`` carry the cookies,
Cloudflare token, and auth headers.

It exposes the same ``create_completion(prompt, stream=...)`` generator API as
:class:`copilot.client.Copilot`, so it is a drop-in replacement.

PROTOCOL ASSUMPTIONS (verify at runtime against a live session):
  * Conversation create:  POST /c/api/conversations  -> {"id": "..."}
  * Chat socket:          wss://copilot.microsoft.com/c/api/chat?api-version=2
                          (with &accessToken=<token> when signed in)
  * Send frame:           {"event":"send","conversationId":...,
                           "content":[{"type":"text","text":...}],"mode":"chat"}
  * Stream frames:        {"event":"appendText","text":...}, then {"event":"done"}
These mirror the captured protocol in ``client.py``. If Microsoft changes them,
adjust the JS templates below.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict, Generator, Optional

from playwright.sync_api import sync_playwright, Error as PlaywrightError

from .auth import DEFAULT_AUTH_FILE, DEFAULT_PROFILE_DIR

COPILOT_URL = "https://copilot.microsoft.com/"

# --- in-page JavaScript -----------------------------------------------------

# Create a conversation. Runs in the page so cookies/Cloudflare apply.
_CREATE_CONVERSATION_JS = """
async () => {
  const res = await fetch('/c/api/conversations', {
    method: 'POST',
    credentials: 'include',
    headers: {'content-type': 'application/json'},
  });
  const text = await res.text();
  if (!res.ok) return {ok: false, status: res.status, text: text};
  let data = {};
  try { data = JSON.parse(text); } catch (e) {}
  return {ok: true, id: data.id || data.conversationId || null, raw: text};
}
"""

# Best-effort discovery of an MSAL access token from localStorage. Returns null
# for anonymous sessions (anonymous chat may still work via cookies alone).
_FIND_TOKEN_JS = """
() => {
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      const v = localStorage.getItem(k);
      if (v && v.indexOf('"credentialType":"AccessToken"') !== -1) {
        try { const o = JSON.parse(v); if (o && o.secret) return o.secret; } catch (e) {}
      }
    }
  } catch (e) {}
  return null;
}
"""

# Open the chat WebSocket and wire handlers that push into a window-scoped
# buffer. Returns immediately; messages accumulate while Python polls.
_START_STREAM_JS = """
([conversationId, accessToken, prompt]) => {
  const state = {queue: [], done: false, error: null, started: false};
  window.__copilot = state;
  let url = 'wss://copilot.microsoft.com/c/api/chat?api-version=2';
  if (accessToken) url += '&accessToken=' + encodeURIComponent(accessToken);
  let ws;
  try { ws = new WebSocket(url); } catch (e) { state.error = 'ws-init: ' + e; state.done = true; return false; }
  window.__copilotWs = ws;
  ws.onopen = () => {
    ws.send(JSON.stringify({
      event: 'send',
      conversationId: conversationId,
      content: [{type: 'text', text: prompt}],
      mode: 'chat'
    }));
  };
  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (e) { return; }
    const e = msg.event;
    if (e === 'appendText') { state.started = true; if (msg.text) state.queue.push(msg.text); }
    else if (e === 'done') { state.done = true; try { ws.close(); } catch (x) {} }
    else if (e === 'error') { state.error = JSON.stringify(msg); state.done = true; try { ws.close(); } catch (x) {} }
  };
  ws.onerror = () => { state.error = state.error || 'websocket error'; state.done = true; };
  ws.onclose = () => { state.done = true; };
  return true;
}
"""

# Drain the buffer and report status in one round-trip.
_POLL_JS = """
() => {
  const s = window.__copilot || {queue: [], done: true, error: 'not started', started: false};
  const q = s.queue;
  s.queue = [];
  return {q: q, done: s.done, error: s.error, started: s.started};
}
"""


class BrowserCopilot:
    """Drives Microsoft Copilot through a real Playwright browser.

    Parameters
    ----------
    profile_dir:
        Directory for the persistent Chromium profile (cookies, Cloudflare
        clearance, sign-in). Reused across runs.
    headless:
        Run without a visible window. Use ``False`` (or :meth:`login`) for the
        first interactive sign-in, then ``True`` afterwards.
    """

    label = "Microsoft Copilot (browser)"
    default_model = "Copilot"

    def __init__(
        self,
        profile_dir: str = DEFAULT_PROFILE_DIR,
        headless: bool = True,
        nav_timeout: int = 60,
        proxy: Optional[str] = None,
    ):
        self.profile_dir = str(Path(profile_dir).resolve())
        self.headless = headless
        self.nav_timeout = nav_timeout
        # Copilot consumer chat is geo-restricted. If you are outside a supported
        # region, route the browser through a proxy/VPN in a supported region,
        # e.g. proxy="http://user:pass@host:port" or "socks5://host:port".
        self.proxy = proxy

        self._pw = None
        self._context = None
        self._page = None

    # -- lifecycle ----------------------------------------------------------

    def start(self, headless: Optional[bool] = None) -> "BrowserCopilot":
        """Launch the persistent browser context and open Copilot."""
        if self._context is not None:
            return self
        if headless is not None:
            self.headless = headless
        try:
            self._pw = sync_playwright().start()
            launch_kwargs = dict(
                headless=self.headless,
                args=["--disable-blink-features=AutomationControlled"],
            )
            if self.proxy:
                launch_kwargs["proxy"] = self._parse_proxy(self.proxy)
            self._context = self._pw.chromium.launch_persistent_context(
                self.profile_dir,
                **launch_kwargs,
            )
            self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
            self._page.set_default_timeout(self.nav_timeout * 1000)
            self._page.goto(COPILOT_URL, wait_until="domcontentloaded")
            # Give Cloudflare a moment to clear on first paint.
            self._page.wait_for_load_state("networkidle", timeout=self.nav_timeout * 1000)
        except PlaywrightError as exc:
            self.close()
            raise ConnectionError(f"Failed to start browser: {exc}") from exc
        return self

    @staticmethod
    def _parse_proxy(proxy: str) -> dict:
        """Turn a ``scheme://user:pass@host:port`` string into Playwright form."""
        from urllib.parse import urlparse

        u = urlparse(proxy)
        server = f"{u.scheme}://{u.hostname}:{u.port}" if u.port else f"{u.scheme}://{u.hostname}"
        cfg = {"server": server}
        if u.username:
            cfg["username"] = u.username
        if u.password:
            cfg["password"] = u.password
        return cfg

    def region_blocked(self) -> bool:
        """True if Copilot is showing the 'Not available in your region' notice."""
        if self._page is None:
            return False
        try:
            text = self._page.evaluate("() => document.body ? document.body.innerText : ''")
        except PlaywrightError:
            return False
        return "available in your region" in (text or "").lower()

    def close(self) -> None:
        for attr, closer in (("_context", lambda c: c.close()), ("_pw", lambda p: p.stop())):
            obj = getattr(self, attr, None)
            if obj is not None:
                try:
                    closer(obj)
                except Exception:
                    pass
                setattr(self, attr, None)
        self._page = None

    def __enter__(self) -> "BrowserCopilot":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()

    # -- auth ---------------------------------------------------------------

    def login(self, path: str = DEFAULT_AUTH_FILE) -> dict:
        """Open a visible window for interactive Microsoft sign-in.

        Blocks until you press Enter in the console. The session is persisted in
        ``profile_dir`` (and snapshotted to ``path``), so subsequent headless
        runs reuse it. Returns the captured auth dict.
        """
        self.close()
        self.start(headless=False)
        print(
            "\nA browser window is open at copilot.microsoft.com.\n"
            "Sign in (or just solve any Cloudflare check for anonymous use),\n"
            "then return here and press Enter to save the session..."
        )
        try:
            input()
        except EOFError:
            pass
        # Snapshot fresh auth so the headless curl_cffi path works immediately.
        auth: dict = {}
        try:
            auth = self.export_auth(path=path, stamp=time.time())
            print(f"Auth snapshot saved to {path}")
        except Exception as exc:
            print(f"(could not snapshot auth: {exc})")
        self.close()
        print(f"Session saved to {self.profile_dir}")
        return auth

    def access_token(self) -> Optional[str]:
        """Return the page's MSAL access token, or ``None`` if anonymous."""
        self._ensure_started()
        try:
            return self._page.evaluate(_FIND_TOKEN_JS)
        except PlaywrightError:
            return None

    def cookies(self) -> Dict[str, str]:
        """Return the signed-in Microsoft cookies as a name->value dict."""
        self._ensure_started()
        try:
            raw = self._context.cookies()
        except PlaywrightError:
            return {}
        return {c["name"]: c["value"] for c in raw if "microsoft.com" in c.get("domain", "")}

    def export_auth(self, path: str = DEFAULT_AUTH_FILE, stamp: Optional[float] = None) -> dict:
        """Snapshot the signed-in cookies + access token to ``path`` as JSON.

        ``stamp`` is the epoch seconds to record as ``saved_at`` (pass
        ``time.time()`` from the caller). Returns the auth dict.
        """
        auth = {
            "cookies": self.cookies(),
            "access_token": self.access_token(),
            "saved_at": stamp if stamp is not None else 0,
        }
        dest = Path(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(auth, indent=2), encoding="utf-8")
        return auth

    # -- chat ---------------------------------------------------------------

    def create_completion(
        self,
        prompt: str,
        stream: bool = False,
        timeout: int = 900,
        **kwargs,
    ) -> Generator[str, None, None]:
        """Stream a Copilot reply to ``prompt``. Mirrors ``Copilot.create_completion``.

        Yields text chunks as they arrive. ``stream`` is accepted for API
        compatibility; chunks are always produced incrementally.
        """
        self._ensure_started()

        if self.region_blocked():
            raise RuntimeError(
                "Microsoft Copilot is not available in your region. "
                "Route the browser through a proxy/VPN in a supported region, e.g.:\n"
                "    BrowserCopilot(proxy='http://user:pass@host:port')\n"
                "or 'socks5://host:port'. See README for details."
            )

        conv = self._page.evaluate(_CREATE_CONVERSATION_JS)
        if not conv.get("ok"):
            status = conv.get("status")
            body = (conv.get("text") or "")[:500]
            if status in (401, 403):
                raise RuntimeError(
                    f"Conversation create returned HTTP {status}. "
                    f"Run login() / `python -m copilot login` to sign in. Body: {body}"
                )
            raise RuntimeError(f"Conversation create failed (HTTP {status}): {body}")

        conversation_id = conv.get("id")
        if not conversation_id:
            raise RuntimeError(f"No conversation id in response: {conv.get('raw')!r}")

        token = self._page.evaluate(_FIND_TOKEN_JS)

        started_ok = self._page.evaluate(_START_STREAM_JS, [conversation_id, token, prompt])
        if started_ok is False:
            state = self._page.evaluate(_POLL_JS)
            raise ConnectionError(f"WebSocket failed to start: {state.get('error')}")

        yield from self._pump(timeout)

    # -- internals ----------------------------------------------------------

    def _pump(self, timeout: int) -> Generator[str, None, None]:
        deadline = time.time() + timeout
        any_text = False
        while True:
            state = self._page.evaluate(_POLL_JS)
            for chunk in state.get("q") or []:
                if chunk:
                    any_text = True
                    yield chunk
            if state.get("error"):
                raise RuntimeError(f"Copilot error: {state['error']}")
            if state.get("done") and not state.get("q"):
                break
            if time.time() > deadline:
                raise TimeoutError(f"No 'done' within {timeout}s")
            time.sleep(0.08)

        if not any_text and not state.get("started"):
            raise RuntimeError("Invalid response: stream produced no text")

    def _ensure_started(self) -> None:
        if self._context is None or self._page is None:
            self.start()
