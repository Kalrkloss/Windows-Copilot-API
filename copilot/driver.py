"""M365 Copilot driver using the SignalR-based Substrate/Helix backend.

Speaks the Microsoft 365 Copilot (m365.cloud.microsoft) chat protocol over a
``curl_cffi`` WebSocket session — no browser required after initial sign-in.
The protocol is SignalR JSON over
``wss://substrate.svc.cloud.microsoft/m365Copilot/Chathub/..."""

import base64
import json
import os
import time
import uuid
from select import select
from typing import Dict, Optional, Union
from urllib.parse import quote


def _resolve_ssl_verify(
    ssl_verify: Union[bool, str, None] = None,
) -> Union[bool, str]:
    """Return the ``verify`` value to pass to a curl_cffi Session.

    Resolution order (first match wins):

    1. Explicit ``ssl_verify`` argument.
    2. ``REQUESTS_CA_BUNDLE`` env var — path to a CA bundle (the same var used
       by *requests*, *httpx*, *pip*, etc.; your IT team may set it globally).
    3. ``SSL_CERT_FILE`` env var — alternative CA bundle path.
    4. ``CURL_CA_BUNDLE`` env var — curl-specific CA bundle path.
    5. ``COPILOT_SSL_VERIFY=0`` / ``false`` / ``no`` — disable verification (last
       resort; useful in isolated dev environments).
    6. Default: ``True`` (verify with bundled CAs).

    In a corporate environment with TLS inspection, set one of the env vars to
    point at your organisation's root certificate bundle so SSL errors disappear
    without disabling verification entirely.
    """
    if ssl_verify is not None:
        return ssl_verify
    for var in ("REQUESTS_CA_BUNDLE", "SSL_CERT_FILE", "CURL_CA_BUNDLE"):
        val = os.environ.get(var, "")
        if val:
            return val
    raw = os.environ.get("COPILOT_SSL_VERIFY", "").lower()
    if raw in ("0", "false", "no"):
        import warnings
        warnings.warn(
            "SSL verification is disabled (COPILOT_SSL_VERIFY=0). "
            "Set REQUESTS_CA_BUNDLE to your corporate CA bundle instead.",
            stacklevel=4,
        )
        return False
    return True

from curl_cffi.const import CurlECode, CurlInfo
from curl_cffi.curl import CurlError
from curl_cffi.requests import Session, CurlWsFlag

_CURL_SOCKET_BAD = -1

from .models import AbstractProvider, Conversation, ImageResponse, ImageType
from .protocol import (
    M365_ALLOWED_MESSAGE_TYPES, M365_OPTION_SETS,
    M365_WS_HOST, M365_WS_PATH, M365_WS_STATIC_PARAMS, SIGNALR_SEP,
)
from .useragent import CHROME_CLIENT_HINTS, CHROME_UA, IMPERSONATE_TARGET
from .utils import is_accepted_format, raise_for_status, to_bytes


def _decode_jwt_payload(token: str) -> dict:
    """Decode the payload of a JWT without verifying the signature."""
    try:
        parts = token.split('.')
        if len(parts) < 2:
            return {}
        payload = parts[1]
        payload += '=' * (4 - len(payload) % 4)  # pad base64
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _build_m365_ws_url(
    access_token: str,
    conversation_id: str,
    session_id: str,
) -> str:
    """Construct the full m365 Copilot WebSocket URL.

    Extracts ``oid`` (user object-id) and ``tid`` (tenant-id) from the
    access-token JWT payload so the URL can be built without a separate
    identity call.
    """
    payload = _decode_jwt_payload(access_token)
    user_id = payload.get('oid') or payload.get('sub', '')
    tenant_id = payload.get('tid', '')
    path = f"{M365_WS_PATH}/{user_id}@{tenant_id}"

    params = [
        ('chatsessionid',           session_id),
        ('XRoutingParameterSessionKey', session_id),
        ('clientrequestid',         session_id),
        ('X-SessionId',             session_id),
        ('ConversationId',          conversation_id),
        ('access_token',            access_token),
    ]
    params.extend(M365_WS_STATIC_PARAMS)
    qs = '&'.join(f"{k}={quote(str(v), safe='')}" for k, v in params)
    return f"{M365_WS_HOST}{path}?{qs}"


def _build_chat_invocation(prompt: str, session_id: str) -> bytes:
    """Build the SignalR StreamInvocation frame for a chat turn.

    The frame ends with the SignalR record separator ``\\x1e`` as required by
    the SignalR JSON protocol.
    """
    correlation_id = session_id.replace('-', '')
    frame = {
        "type": 4,                    # SignalR StreamInvocation
        "target": "chat",
        "invocationId": "0",
        "arguments": [{
            "source": "officeweb",
            "clientCorrelationId": correlation_id,
            "sessionId": session_id,
            "optionsSets": M365_OPTION_SETS,
            "streamingMode": "ConciseWithPadding",
            "options": {},
            "extraExtensionParameters": {},
            "allowedMessageTypes": M365_ALLOWED_MESSAGE_TYPES,
            "sliceIds": [],
            "threadLevelGptId": {},
            "traceId": correlation_id,
            "isStartOfSession": False,
            "clientInfo": {
                "clientPlatform": "mcmcopilot-web",
                "clientAppName": "Office",
                "clientEntrypoint": "mcmcopilot-officeweb",
                "clientSessionId": session_id,
                "ProductCategory": "Chat",
                "clientAppType": "Web",
                "productEntryPoint": "ChatPanel",
                "deviceOS": "Windows",
                "deviceType": "Desktop",
                "clientPlatformVersion": "10",
            },
            "message": {
                "author": "user",
                "inputMethod": "Keyboard",
                "text": prompt,
                "entityAnnotationTypes": [
                    "People", "File", "Event", "Email", "TeamsMessage",
                ],
                "requestId": correlation_id,
                "locale": "en-us",
                "messageType": "Chat",
                "experienceType": "Default",
                "adaptiveCards": [],
                "clientPreferences": {},
                "connectedFederatedConnections": ["dummyId"],
            },
            "plugins": [{"Id": "BingWebSearch", "Source": "BuiltIn"}],
            "isSbsSupported": True,
            "tone": "Magic",
            "renderReferencesBehindEOS": True,
            "disconnectBehavior": "continue",
        }],
    }
    return json.dumps(frame, ensure_ascii=False).encode() + SIGNALR_SEP


def _drain_signalr(buffer: bytes):
    """Split ``buffer`` on the SignalR record separator and parse JSON objects.

    Returns ``(list_of_parsed_dicts, remaining_buffer)``.
    """
    messages = []
    while SIGNALR_SEP in buffer:
        idx = buffer.index(SIGNALR_SEP)
        raw = buffer[:idx].strip()
        buffer = buffer[idx + 1:]
        if raw:
            try:
                messages.append(json.loads(raw))
            except json.JSONDecodeError:
                pass
    return messages, buffer


class ClearanceRequired(RuntimeError):
    """Raised when the chat endpoint demands re-authentication.

    For the consumer copilot.microsoft.com this indicated a Cloudflare Turnstile
    challenge. For m365.cloud.microsoft it is raised when the access token is
    expired or has insufficient scope, so the caller should re-run login.
    """


class Copilot(AbstractProvider):
    label = "Microsoft 365 Copilot"
    url = "https://m365.cloud.microsoft"
    working = True
    supports_stream = True
    default_model = "Copilot"
    needs_auth = True   # requires a signed-in M365 account

    def create_completion(
            self,
            prompt: str,
            stream: bool = False,
            proxy: str = None,
            timeout: int = 900,
            conversation: Optional[Conversation] = None,
            conversation_id: str = None,
            return_conversation: bool = False,
            cookies: Dict[str, str] = None,
            access_token: str = None,
            identity_type: str = None,
            ssl_verify: Union[bool, str, None] = None,
            image: ImageType = None,
            **kwargs
        ):
        """Stream an m365 Copilot reply using the SignalR chat protocol.

        Connects to ``wss://substrate.svc.cloud.microsoft/m365Copilot/Chathub/…``
        and speaks the SignalR JSON protocol:

        1. Handshake: ``{"protocol":"json","version":1}\\x1e``
        2. Ping/pong keep-alive: ``{"type":6}\\x1e``
        3. Chat invocation: ``{"type":4,"target":"chat",…}\\x1e``
        4. Stream: ``{"type":1,"target":"update",…}\\x1e`` frames with text
        5. Completion: ``{"type":3}\\x1e``

        ``access_token`` must be the ``sydney.readwrite``-scoped token captured
        by the browser on the first chat turn (see :mod:`copilot.browser`).

        Conversation targeting:
          * ``conversation`` — reuse an existing :class:`Conversation` object;
          * ``conversation_id`` — resume that conversation id;
          * neither — start a fresh conversation (``ConversationId`` is a new
            UUID generated client-side — no REST call needed).
        """
        if not access_token and conversation is not None:
            access_token = conversation.access_token
        if cookies is None and conversation is not None:
            cookies = conversation.cookies

        if not access_token:
            raise RuntimeError(
                "No access token available. Run `python -m copilot login` and "
                "send a chat message in the browser window to capture the token."
            )

        # Each new conversation and session gets fresh UUIDs.
        if conversation is not None:
            conversation_id = conversation.conversation_id
        elif not conversation_id:
            conversation_id = str(uuid.uuid4())

        session_id = str(uuid.uuid4())
        ws_url = _build_m365_ws_url(access_token, conversation_id, session_id)

        with Session(
            timeout=timeout,
            proxy=proxy,
            impersonate=IMPERSONATE_TARGET,
            headers={"User-Agent": CHROME_UA, **CHROME_CLIENT_HINTS},
            cookies=cookies or {},
            verify=_resolve_ssl_verify(ssl_verify),
        ) as session:
            if return_conversation and not conversation:
                conv = Conversation(conversation_id, session.cookies.jar)
                conv.access_token = access_token
                yield conv

            wss = session.ws_connect(ws_url)

            # --- SignalR handshake ---
            handshake = json.dumps({"protocol": "json", "version": 1}).encode() + SIGNALR_SEP
            wss.send(handshake, CurlWsFlag.TEXT)
            # Receive handshake response ({}\x1e)
            self._recv_until_sep(wss, time.time() + 15)

            # --- Send chat StreamInvocation ---
            wss.send(_build_chat_invocation(prompt, session_id), CurlWsFlag.TEXT)

            # --- Stream response ---
            yield from self._read_signalr_stream(wss, timeout)

    def _recv_until_sep(self, wss, deadline: float):
        """Consume raw WebSocket chunks until a SignalR record separator is seen."""
        buf = b""
        while SIGNALR_SEP not in buf:
            chunk = self._recv_frame(wss, deadline)
            if chunk is None:
                break
            buf += chunk if isinstance(chunk, (bytes, bytearray)) else chunk.encode()
        return buf

    def _read_signalr_stream(
        self, wss, timeout: int, idle_timeout: int = 60
    ):
        """Consume SignalR frames and yield text deltas until the stream completes.

        Text extraction strategy:
        - ``update`` frames with ``messages[0].text`` (growing): yield the delta.
        - ``update`` frames with ``writeAtCursor`` (delta string): yield directly.
        - ``ReferencesListComplete`` or ``Disengaged`` message type: stop.
        - ``type=3`` (Completion frame): stop.
        - ``type=6`` (Ping): respond with a pong.
        """
        buffer = b""
        accumulated_text = ""
        overall_deadline = time.time() + timeout

        while True:
            idle_deadline = time.time() + idle_timeout
            try:
                chunk = self._recv_frame(wss, min(overall_deadline, idle_deadline))
            except Exception:
                return  # socket closed
            if chunk is None:
                if time.time() >= overall_deadline:
                    raise TimeoutError(f"M365 Copilot stream exceeded {timeout}s")
                raise TimeoutError(
                    f"M365 Copilot chat socket went silent for {idle_timeout}s."
                )

            buffer += chunk if isinstance(chunk, (bytes, bytearray)) else chunk.encode()
            messages, buffer = _drain_signalr(buffer)

            for msg in messages:
                msg_type = msg.get("type")

                if msg_type == 6:  # Ping — respond with pong
                    try:
                        wss.send(
                            json.dumps({"type": 6}).encode() + SIGNALR_SEP,
                            CurlWsFlag.TEXT,
                        )
                    except Exception:
                        pass
                    continue

                if msg_type == 3:  # Completion — stream ended
                    return

                if msg_type == 1 and msg.get("target") == "update":
                    args = msg.get("arguments", [{}])
                    update = args[0] if args else {}

                    # Delta streaming via writeAtCursor
                    delta = update.get("writeAtCursor")
                    if delta:
                        accumulated_text += delta
                        yield delta

                    # Full accumulated text in messages[]
                    for m in update.get("messages", []):
                        if m.get("author") != "bot":
                            continue
                        # Stop signals
                        if m.get("messageType") in (
                            "ReferencesListComplete", "Disengaged", "EndOfRequest",
                        ):
                            return
                        text = m.get("text", "")
                        if text and text != accumulated_text:
                            if text.startswith(accumulated_text):
                                new_chars = text[len(accumulated_text):]
                                if new_chars:
                                    yield new_chars
                            accumulated_text = text

    @staticmethod
    def _recv_frame(wss, deadline: float):
        """Block for one complete WS frame, or return ``None`` past ``deadline``.

        Reassembles libcurl's fragments like ``curl_cffi``'s own ``recv()`` but
        breaks out of the ``CURLE_AGAIN`` wait once ``deadline`` (epoch seconds)
        is reached, so an idle socket can't hang us indefinitely. Non-AGAIN curl
        errors (e.g. a closed connection) propagate to the caller.
        """
        sock_fd = wss.curl.getinfo(CurlInfo.ACTIVESOCKET)
        if sock_fd == _CURL_SOCKET_BAD:
            raise ConnectionError("WebSocket has no active socket")
        chunks = []
        while True:
            try:
                chunk, frame = wss.recv_fragment()
                chunks.append(chunk)
                if frame.bytesleft == 0 and frame.flags & CurlWsFlag.CONT == 0:
                    return b"".join(chunks)
            except CurlError as e:
                if e.code != CurlECode.AGAIN:
                    raise
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                select([sock_fd], [], [], min(0.5, remaining))
