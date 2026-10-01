import base64
import hashlib
import json
import os
import sys
import time
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional, Union

# Add parent dir to path so protobufs import cleanly on Vercel
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(CURRENT_DIR)
if PARENT_DIR not in sys.path:
    sys.path.insert(0, PARENT_DIR)
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

import coincurve
import grpc
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

import auth_frontend_pb2 as auth_pb
import auth_frontend_pb2_grpc as auth_grpc
import grok_api_pb2 as chat_pb
import grok_api_pb2_grpc as chat_grpc

app = FastAPI(title="Grok 4.6 OpenAI Proxy for Vercel", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

HOST = "grok.com"
PORT = 443

BASE_METADATA = [
    ("user-agent", "Grok/1.2.22 (Android; arm64-v8a)"),
    ("x-app-name", "Grok Android"),
    ("x-app-version", "1.2.22"),
    ("x-app-language", "en-US"),
]

# In-memory cached creds (persists during Vercel lambda warm state)
_current_creds: Optional[dict] = None


def make_channel() -> grpc.Channel:
    return grpc.secure_channel(f"{HOST}:{PORT}", grpc.ssl_channel_credentials())


def generate_keypair() -> tuple[bytes, bytes]:
    privkey_bytes = os.urandom(32)
    privkey = coincurve.PrivateKey(privkey_bytes)
    return privkey_bytes, privkey.public_key.format(compressed=True)


def _der_to_compact(der: bytes) -> bytes:
    assert der[0] == 0x30 and der[2] == 0x02
    r_len = der[3]
    r = der[4 : 4 + r_len].lstrip(b"\x00")
    rest = der[4 + r_len :]
    assert rest[0] == 0x02
    s = rest[2 : 2 + rest[1]].lstrip(b"\x00")
    return r.rjust(32, b"\x00") + s.rjust(32, b"\x00")


def sign_challenge(privkey_bytes: bytes, challenge: bytes) -> bytes:
    digest = hashlib.sha256(challenge).digest()
    der = coincurve.PrivateKey(privkey_bytes).sign(digest, hasher=None)
    return _der_to_compact(der)


def _auth_meta() -> list:
    return BASE_METADATA + [("x-xai-request-id", str(uuid.uuid4()))]


def register_new_anon_user(channel: grpc.Channel) -> dict:
    stub = auth_grpc.AuthFrontendStub(channel)
    privkey_bytes, pubkey = generate_keypair()

    anon_user_id = stub.CreateAnonUser(
        auth_pb.CreateAnonUserRequest(user_public_key=pubkey),
        metadata=_auth_meta(),
    ).anon_user_id

    challenge = stub.CreateAnonUserChallenge(
        auth_pb.CreateChallengeRequest(anon_user_id=anon_user_id),
        metadata=_auth_meta(),
    ).challenge

    signature = sign_challenge(privkey_bytes, challenge)
    return {
        "anon_user_id": anon_user_id,
        "privkey": privkey_bytes.hex(),
        "pubkey": pubkey.hex(),
        "challenge_b64": base64.b64encode(challenge).decode(),
        "signature_b64": base64.b64encode(signature).decode(),
    }


def get_active_creds(channel: grpc.Channel, force_refresh: bool = False) -> dict:
    global _current_creds
    if _current_creds is None or force_refresh:
        _current_creds = register_new_anon_user(channel)
    return _current_creds


def _auth_headers(creds: dict) -> list:
    return [
        ("x-anonuserid", creds["anon_user_id"]),
        ("x-challenge", creds["challenge_b64"]),
        ("x-signature", creds["signature_b64"]),
    ]


class ChatMessage(BaseModel):
    role: str
    content: Any


class ChatRequest(BaseModel):
    model: str = "grok-4.6"
    messages: List[ChatMessage]
    stream: Optional[bool] = False
    temperature: Optional[float] = 0.7


@app.get("/")
def home():
    return {
        "status": "online",
        "service": "Grok 4.6 OpenAI-Compatible Proxy for Vercel",
        "default_model": "grok-4.6",
        "models_endpoint": "/v1/models",
        "chat_endpoint": "/v1/chat/completions",
    }


@app.get("/v1/models")
def list_models():
    now = int(time.time())
    return {
        "object": "list",
        "data": [
            {
                "id": "grok-4.6",
                "object": "model",
                "created": now,
                "owned_by": "xai",
                "description": "Grok 4.6 — xAI Frontier Thinking Model",
            },
            {
                "id": "grok-4.6-thinking",
                "object": "model",
                "created": now,
                "owned_by": "xai",
                "description": "Grok 4.6 Deep Reasoning Model",
            },
            {
                "id": "grok-3",
                "object": "model",
                "created": now,
                "owned_by": "xai",
                "description": "Grok 3 Base Model",
            },
            {
                "id": "auto",
                "object": "model",
                "created": now,
                "owned_by": "xai",
                "description": "Alias for grok-4.6",
            },
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatRequest):
    if not req.messages:
        raise HTTPException(status_code=400, detail="Messages list cannot be empty")

    # Format full conversation into prompt
    prompt_parts = []
    for m in req.messages:
        content = m.content
        if isinstance(content, list):
            content = " ".join([c.get("text", "") for c in content if isinstance(c, dict)])
        prompt_parts.append(f"{m.role.capitalize()}: {content}")
    full_prompt = "\n\n".join(prompt_parts)

    is_reasoning = "thinking" in req.model.lower()
    target_model = "grok-3"  # grok-3 alias in backend maps directly to Grok 4.6

    session_token = os.environ.get("GROK_SESSION_TOKEN", "").strip()

    def generate_stream():
        nonlocal session_token
        channel = make_channel()
        stub = chat_grpc.ChatStub(channel)
        creds = None
        if not session_token:
            creds = get_active_creds(channel)

        request_proto = chat_pb.CreateConversationAndRespondRequest(
            model_name=target_model,
            message=full_prompt,
            disable_search=False,
            is_reasoning=is_reasoning,
        )

        def build_meta():
            base = BASE_METADATA + [("x-xai-request-id", str(uuid.uuid4()))]
            if session_token:
                return base + [("cookie", f"sso={session_token}; sso-rw={session_token}")]
            return base + _auth_headers(creds)

        # Initial assistant chunk
        cid = f"chatcmpl-grok-{uuid.uuid4().hex[:12]}"
        init_chunk = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": req.model,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }
        yield f"data: {json.dumps(init_chunk)}\n\n"

        thought_handled = False
        max_retries = 3
        for attempt in range(max_retries):
            try:
                for chunk in stub.CreateConversationAndRespond(request_proto, metadata=build_meta()):
                    if chunk.HasField("add_response") and chunk.add_response.token:
                        token_str = chunk.add_response.token
                        if not thought_handled and "Thinking about your request" in token_str:
                            token_str = token_str.replace("Thinking about your request", "")
                            thought_handled = True
                            if not token_str:
                                continue
                        out_chunk = {
                            "id": cid,
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": req.model,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": token_str},
                                    "finish_reason": None,
                                }
                            ],
                        }
                        yield f"data: {json.dumps(out_chunk)}\n\n"
                break
            except grpc.RpcError as e:
                if e.code() in (grpc.StatusCode.RESOURCE_EXHAUSTED, grpc.StatusCode.UNAUTHENTICATED):
                    if not session_token and attempt < max_retries - 1:
                        # Auto-rotate to a fresh anonymous user keypair instantly
                        creds = get_active_creds(channel, force_refresh=True)
                        continue
                err_chunk = {
                    "id": cid,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": req.model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": f"\n\n[Grok Error: {e.details() if hasattr(e, 'details') else str(e)}]"},
                            "finish_reason": "error",
                        }
                    ],
                }
                yield f"data: {json.dumps(err_chunk)}\n\n"
                break

        # Stop chunk
        stop_chunk = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": req.model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        yield f"data: {json.dumps(stop_chunk)}\n\n"
        yield "data: [DONE]\n\n"
        channel.close()

    if req.stream:
        return StreamingResponse(generate_stream(), media_type="text/event-stream")

    # Non-streaming response
    channel = make_channel()
    stub = chat_grpc.ChatStub(channel)
    creds = None
    if not session_token:
        creds = get_active_creds(channel)

    request_proto = chat_pb.CreateConversationAndRespondRequest(
        model_name=target_model,
        message=full_prompt,
        disable_search=False,
        is_reasoning=is_reasoning,
    )

    def build_meta_unary():
        base = BASE_METADATA + [("x-xai-request-id", str(uuid.uuid4()))]
        if session_token:
            return base + [("cookie", f"sso={session_token}; sso-rw={session_token}")]
        return base + _auth_headers(creds)

    tokens = []
    max_retries = 3
    for attempt in range(max_retries):
        try:
            for chunk in stub.CreateConversationAndRespond(request_proto, metadata=build_meta_unary()):
                if chunk.HasField("add_response") and chunk.add_response.token:
                    tokens.append(chunk.add_response.token)
            break
        except grpc.RpcError as e:
            if e.code() in (grpc.StatusCode.RESOURCE_EXHAUSTED, grpc.StatusCode.UNAUTHENTICATED):
                if not session_token and attempt < max_retries - 1:
                    creds = get_active_creds(channel, force_refresh=True)
                    continue
            raise HTTPException(status_code=502, detail=f"Grok API error: {e.details() if hasattr(e, 'details') else str(e)}")
        finally:
            channel.close()

    full_reply = "".join(tokens).replace("Thinking about your request", "").strip()
    return {
        "id": f"chatcmpl-grok-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": req.model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": full_reply},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": len(full_prompt.split()), "completion_tokens": len(full_reply.split()), "total_tokens": len(full_prompt.split()) + len(full_reply.split())},
    }
