"""Pure-HTTP Copilot driver.

Speaks Microsoft Copilot's consumer chat protocol directly over a
Cloudflare-impersonating ``curl_cffi`` session — no browser required. This is the
low-level engine; most callers should use :class:`copilot.client.CopilotClient`.
See :mod:`copilot.browser` for the Playwright-backed fallback.
"""

import json
import time
from typing import Dict, Optional
from urllib.parse import quote

from curl_cffi.requests import Session, CurlWsFlag

from .challenges import solve_copilot_challenge, solve_hashcash
from .models import AbstractProvider, Conversation, ImageResponse, ImageType
from .utils import drain_json, is_accepted_format, raise_for_status, to_bytes


class Copilot(AbstractProvider):
    label = "Microsoft Copilot"
    url = "https://copilot.microsoft.com"
    working = True
    supports_stream = True
    default_model = "Copilot"
    needs_auth = False  # consumer chat works anonymously (cookies only)
    websocket_url = "wss://copilot.microsoft.com/c/api/chat?api-version=2"
    conversation_url = f"{url}/c/api/conversations"

    def create_completion(
            self,
            prompt: str,
            stream: bool = False,
            proxy: str = None,
            timeout: int = 900,
            image: ImageType = None,
            conversation: Optional[Conversation] = None,
            conversation_id: str = None,
            return_conversation: bool = False,
            cookies: Dict[str, str] = None,
            access_token: str = None,
            **kwargs
        ):
        """Stream a Copilot reply to ``prompt``.

        Runs Copilot's own chat protocol over a Cloudflare-impersonating
        ``curl_cffi`` session: ``POST /c/api/conversations`` then a chat
        WebSocket (``send`` -> proof-of-work ``challenge`` -> ``appendText``* ->
        ``done``). The challenge is solved in-process (see
        :mod:`copilot.challenges`); no browser is required.

        ``prompt`` is the user message sent straight to the chat socket (the
        protocol has no separate system/role channel). Anonymous by default;
        pass ``cookies`` and/or ``access_token`` (e.g. exported from a signed-in
        browser session) to run as a logged-in user — required where anonymous
        consumer chat is region-restricted.

        Conversation targeting (first match wins):
          * ``conversation`` — reuse an existing :class:`Conversation` object;
          * ``conversation_id`` — resume a conversation by its id string (no
            create call), e.g. one saved from a previous run;
          * neither — create a fresh conversation. With ``return_conversation``
            the new :class:`Conversation` is yielded first.
        """
        # Resolve auth: explicit args win, else fall back to the conversation's.
        if cookies is None and conversation is not None:
            cookies = conversation.cookies
        if access_token is None and conversation is not None:
            access_token = conversation.access_token

        websocket_url = self.websocket_url
        headers = None
        if access_token:
            websocket_url = f"{websocket_url}&accessToken={quote(access_token)}"
            headers = {"authorization": f"Bearer {access_token}"}

        with Session(
            timeout=timeout,
            proxy=proxy,
            impersonate="chrome",
            cookies=cookies,
            headers=headers,
        ) as session:
            # Establish cookies + Cloudflare clearance (anonymous is fine).
            session.get(f"{self.url}/")

            if conversation is not None:
                conversation_id = conversation.conversation_id
            elif conversation_id is not None:
                pass  # resume an existing conversation by id; skip create
            else:
                response = session.post(self.conversation_url)
                raise_for_status(response)
                conversation_id = response.json().get("id")
                if return_conversation:
                    yield Conversation(conversation_id, session.cookies.jar)

            images = []
            if image is not None:
                data = to_bytes(image)
                response = session.post(
                    f"{self.url}/c/api/attachments",
                    headers={"content-type": is_accepted_format(data)},
                    data=data,
                )
                raise_for_status(response)
                images.append({"type": "image", "url": response.json().get("url")})

            send_frame = json.dumps({
                "event": "send",
                "conversationId": conversation_id,
                "content": [*images, {"type": "text", "text": prompt}],
                "mode": "chat",
            }).encode()

            wss = session.ws_connect(websocket_url)
            wss.send(send_frame, CurlWsFlag.TEXT)
            yield from self._read_stream(wss, send_frame, timeout)

    def _read_stream(self, wss, send_frame: bytes, timeout: int):
        """Consume chat-socket frames, solving challenges, yielding text/images."""
        buffer = b""
        is_started = False
        answered = False
        image_prompt = None
        last_msg = None

        deadline = time.time() + timeout
        while True:
            try:
                chunk = wss.recv()[0]
            except Exception:
                break
            if not chunk:
                if time.time() > deadline:
                    break
                continue

            buffer += chunk if isinstance(chunk, (bytes, bytearray)) else chunk.encode()
            messages, buffer = drain_json(buffer)
            for msg in messages:
                last_msg = msg
                event = msg.get("event")
                if event == "challenge" and not answered:
                    token = self._solve_challenge(msg)
                    if token is not None:
                        wss.send(json.dumps({
                            "event": "challengeResponse",
                            "token": token,
                            "method": msg.get("method"),
                        }).encode(), CurlWsFlag.TEXT)
                        answered = True
                        # The client re-sends the held message after a challenge.
                        wss.send(send_frame, CurlWsFlag.TEXT)
                elif event == "appendText":
                    is_started = True
                    yield msg.get("text")
                elif event == "generatingImage":
                    image_prompt = msg.get("prompt")
                elif event == "imageGenerated":
                    yield ImageResponse(msg.get("url"), image_prompt, {"preview": msg.get("thumbnailUrl")})
                elif event == "done":
                    return
                elif event == "error":
                    code = msg.get("errorCode") or msg
                    if code == "chat-service-unavailable":
                        raise RuntimeError(
                            "Copilot error: chat-service-unavailable. The chat backend is "
                            "typically geo-restricted; if you are outside a supported region, "
                            "retry via a proxy in a supported region, e.g. "
                            "create_completion(..., proxy='http://user:pass@host:port')."
                        )
                    raise RuntimeError(f"Copilot error: {code}")

        if not is_started:
            raise RuntimeError(f"Invalid response: {last_msg}")

    @staticmethod
    def _solve_challenge(msg: dict):
        """Return the challenge response token, or None if unsupported."""
        method = msg.get("method")
        parameter = msg.get("parameter")
        if not parameter:
            return None
        if method == "hashcash":
            return solve_hashcash(parameter)
        if method == "copilot":
            return solve_copilot_challenge(parameter)
        # 'cloudflare' (Turnstile) needs a browser-solved token; unsupported here.
        return None
