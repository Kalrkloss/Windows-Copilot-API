"""FastAPI app wiring Copilot onto the OpenAI Chat Completions API."""

import threading
import time

from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse

from copilot import CopilotClient

from .config import MODEL_NAME
from .openai_format import (
    completion_response,
    new_id,
    sse_event,
    stream_chunk,
)
from .prompt import messages_to_prompt
from .schemas import ChatCompletionRequest

app = FastAPI(title="Copilot OpenAI-compatible API", version="1.0.0")
client = CopilotClient()

# Copilot's per-account chat socket doesn't tolerate concurrent conversations
# from one process (parallel requests error out or hang). This server bridges a
# single signed-in account, so we serialize upstream calls: concurrent HTTP
# requests queue here and run one at a time. Predictable, at the cost of
# parallelism — fine for a personal bridge.
_upstream_lock = threading.Lock()


def _stream(prompt: str, model: str, conversation_id=None):
    """Yield OpenAI ``chat.completion.chunk`` SSE events for ``prompt``.

    ``conversation_id`` continues an existing Copilot thread; ``None`` starts a
    fresh one (its id is emitted on the final chunk).
    """
    cid = new_id()
    created = int(time.time())
    try:
        with _upstream_lock:  # one upstream chat at a time (released on disconnect)
            yield sse_event(stream_chunk(cid, created, model, {"role": "assistant"}))
            stream = client.stream(prompt, conversation_id=conversation_id)
            for piece in stream:
                if isinstance(piece, str) and piece:
                    yield sse_event(stream_chunk(cid, created, model, {"content": piece}))
            # Copilot's conversation id is known once the stream has run; emit it
            # on the final chunk so callers can track the upstream thread.
            yield sse_event(
                stream_chunk(
                    cid, created, model, {}, finish="stop",
                    conversation_id=stream.conversation_id,
                )
            )
    except Exception as exc:  # surface errors to the client instead of hanging
        yield sse_event(
            stream_chunk(cid, created, model, {"content": f"\n[error: {exc}]"}, finish="error")
        )
    yield "data: [DONE]\n\n"


@app.get("/v1/models")
def list_models():
    return {
        "object": "list",
        "data": [
            {"id": MODEL_NAME, "object": "model", "created": 0, "owned_by": "microsoft"}
        ],
    }


@app.post("/v1/chat/completions")
def chat_completions(req: ChatCompletionRequest):
    prompt = messages_to_prompt(req.messages)
    if not prompt.strip():
        return JSONResponse(
            status_code=400,
            content={"error": {"message": "no text content in messages", "type": "invalid_request_error"}},
        )
    model = req.model or MODEL_NAME

    if req.stream:
        return StreamingResponse(
            _stream(prompt, model, req.conversation_id), media_type="text/event-stream"
        )

    try:
        with _upstream_lock:  # serialize: one upstream chat at a time
            reply = client.chat(prompt, conversation_id=req.conversation_id)
    except Exception as exc:
        return JSONResponse(
            status_code=502,
            content={"error": {"message": str(exc), "type": "upstream_error"}},
        )
    return completion_response(reply.text, model, reply.conversation_id)


@app.get("/")
def root():
    return {"service": "Copilot OpenAI-compatible API", "endpoints": ["/v1/models", "/v1/chat/completions"]}
