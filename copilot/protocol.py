"""Shared Microsoft Copilot chat-protocol constants — the single source of truth.

Both the pure-HTTP driver (:mod:`copilot.driver`) and the browser driver
(:mod:`copilot.browser`) speak the *same* chat-socket protocol, so the wire
shapes live here once. When Microsoft changes the protocol, recapture it with
``tests/capture_signalr.py`` and update this file.

Captured from a live m365.cloud.microsoft session. The connect sequence is:

    ws_connect(M365_WS_URL_TEMPLATE + per-session params)
    -> send {"protocol":"json","version":1} + SIGNALR_SEP
    -> receive {} + SIGNALR_SEP   (handshake OK)
    -> send chat StreamInvocation (type=4, target="chat") + SIGNALR_SEP
    -> receive update frames (type=1, target="update") with streaming text
    -> receive completion frame (type=3) when stream ends
"""

# SignalR JSON protocol record separator.
SIGNALR_SEP = b"\x1e"

# Base WebSocket host for m365 Copilot (Helix/Substrate backend).
# The full URL requires userId, tenantId, and per-call UUIDs — see driver.py.
M365_WS_HOST = "wss://substrate.svc.cloud.microsoft"
M365_WS_PATH = "/m365Copilot/Chathub"   # /{userId}@{tenantId} appended at runtime

# Feature flags sent in every chat request.
# Captured from a live session; controls which capabilities the server enables.
M365_OPTION_SETS = [
    "search_result_progress_messages_with_search_queries",
    "update_textdoc_response_after_streaming",
    "deepleo_networking_timeout_10minutes_canmore",
    "cwc_flux_image",
    "cwc_code_interpreter",
    "cwc_code_interpreter_amsfix",
    "cwcfluxgptv",
    "gptvnorm2048",
    "cwc_code_interpreter_citation_fix",
    "cwc_fileupload_odb",
    "add_custom_instructions",
    "cwc_flux_v3",
    "flux_v3_progress_messages",
    "enable_batch_token_processing",
    "enable_gg_gpt",
    "flux_v3_references",
    "rich_responses",
    "pages_citations",
    "pages_citations_multiturn",
]

# Message types the client accepts from the server.
M365_ALLOWED_MESSAGE_TYPES = [
    "Chat",
    "Suggestion",
    "InternalSearchQuery",
    "Disengaged",
    "InternalLoaderMessage",
    "Progress",
    "GeneratedCode",
    "SearchQuery",
    "AuthError",
    "HintInvocation",
    "MemoryUpdate",
    "EndOfRequest",
    "ReferencesListComplete",
    "SwitchRespondingEndpoint",
]

# Static query-string parameters appended to every WebSocket URL.
# Per-call params (chatsessionid, ConversationId, access_token) are added in driver.py.
M365_WS_STATIC_PARAMS = (
    ('source', '"officeweb"'),
    ('product', 'Office'),
    ('agentHost', 'Bizchat.FullScreen'),
    ('licenseType', 'Starter'),
    ('isEdu', 'false'),
    ('agent', 'web'),
    ('scenario', 'OfficeWebIncludedCopilot'),
)

# Legacy consumer copilot constants — kept for reference only.
# Consumer copilot at copilot.microsoft.com uses a different protocol and is
# not targeted by this project anymore.
_LEGACY_CHAT_WEBSOCKET_URL = "wss://copilot.microsoft.com/c/api/chat?api-version=2"
_LEGACY_SET_OPTIONS_FRAME = {"event": "setOptions", "supportedFeatures": []}
_LEGACY_CONSENTS_FRAME = {"event": "reportLocalConsents", "grantedConsents": []}

