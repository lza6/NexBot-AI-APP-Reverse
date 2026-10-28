#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mock_serverpod.py — NexBot AI (com.nexbot.app 1.9.3) 协议复刻服务端

背景
----
真实后端 `nexbot.globeapp.dev`（Serverpod）与 Supabase 项目
`zlymralqctgzydksekht.supabase.co` 均已注销（DNS NXDOMAIN，见
evidence/dns-records.txt）。上游不可达，因此本服务端不转发任何流量，
只按反编译还原出的结构做本地响应，用于：

  1. 把 docs/02-protocol.md 的协议描述固化成可执行代码；
  2. 给 demo/ 与 tools/stress.js 提供压测靶子；
  3. 作为未来上游恢复时的 server-side 参考实现。

协议还原依据（blutter 反编译 asm/nexbot_client/src/protocol/*）：

  MyCompletionRequest  : { model, system, messages, length, secret }
  MyCompletionMessage  : { role, content }
  MessageContent       : { type, text, imageUrl, url }
  MessageContentImageUrl: { url }
  MyCompletionResponse : { content }
  Endpoint             : chatCompletion（唯一 endpoint）
  传输                  : Serverpod，HTTP POST 用于普通调用，WebSocket 用于流式

暴露的接口：
  A. Serverpod 路由形态  POST /{endpoint}/{method}
  B. Serverpod 信封形态  POST /  {"method":"ep.m","arguments":{...}}
  C. OpenAI 兼容形态      POST /v1/chat/completions（便于 new-api 直接指向）

零第三方依赖。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HOST = os.environ.get("MOCK_HOST", "127.0.0.1")
PORT = int(os.environ.get("MOCK_PORT", "8787"))

DEFAULT_DAILY_FREE = int(os.environ.get("MOCK_DAILY_FREE", "2"))
DEFAULT_MODEL = os.environ.get("MOCK_MODEL", "gemini-3.5-flash-lite")
MAX_INPUT_WORDS = int(os.environ.get("MOCK_MAX_INPUT_WORDS", "3000"))
MIN_INPUT_WORDS = int(os.environ.get("MOCK_MIN_INPUT_WORDS", "50"))
LATENCY_MS = int(os.environ.get("MOCK_LATENCY_MS", "120"))
ERROR_RATE = float(os.environ.get("MOCK_ERROR_RATE", "0"))

# 反编译 app_core/utils/utils.crypto.dart::UtilsCrypto.generateSecret() 得到的算法：
#   JWT(payload: {'app': <appId>, 'dev': <deviceId>})
#       .sign(SecretKey(<secretSeed>.substring(0, 30)), expiresIn: N minutes)
# secretSeed 取自 app_core/config.dart 内嵌常量（见 docs/02-protocol.md 第 6 节）。
SECRET_SEED = os.environ.get(
    "MOCK_SECRET_SEED",
    "BIxNxvzZzWLzRtf3_IYKmBIkhanz1pQtdgTHG6E4D7-cgi2Kl4tAqvFR1ZV0CyoxX8MFNY5JGVu8e5n81J0TPbY",
)
JWT_KEY = SECRET_SEED[:30]
JWT_TTL_MINUTES = int(os.environ.get("MOCK_JWT_TTL_MINUTES", "30"))

ROLES = {"assistant": 0, "user": 1, "system": 2}
CONTENT_TYPES = ("text", "image_url", "input_audio")


# ---------------------------------------------------------------------------
# JWT（HS256）—— 复刻 dart_jsonwebtoken 的 sign()
# ---------------------------------------------------------------------------


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def sign_secret(app_id: str, device_id: str, ttl_minutes: int = JWT_TTL_MINUTES) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    now = int(time.time())
    payload = {"app": app_id, "dev": device_id, "iat": now, "exp": now + ttl_minutes * 60}
    segments = [
        _b64url(json.dumps(header, separators=(",", ":")).encode()),
        _b64url(json.dumps(payload, separators=(",", ":")).encode()),
    ]
    signing_input = ".".join(segments).encode()
    sig = hmac.new(JWT_KEY.encode(), signing_input, hashlib.sha256).digest()
    segments.append(_b64url(sig))
    return ".".join(segments)


def verify_secret(token: str) -> Optional[Dict[str, Any]]:
    try:
        head, body, sig = token.split(".")
    except ValueError:
        return None
    signing_input = f"{head}.{body}".encode()
    expected = _b64url(hmac.new(JWT_KEY.encode(), signing_input, hashlib.sha256).digest())
    if not hmac.compare_digest(expected, sig):
        return None
    padded = body + "=" * (-len(body) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, json.JSONDecodeError):
        return None
    if int(claims.get("exp", 0)) < time.time():
        return None
    return claims


# ---------------------------------------------------------------------------
# 内存态
# ---------------------------------------------------------------------------


@dataclass
class Device:
    device_id: str
    credits_used: int = 0
    day: str = ""
    messages: List[Dict[str, Any]] = field(default_factory=list)


class Store:
    def __init__(self) -> None:
        # RLock: 内部方法互相嵌套取锁（device() 会被 consume() 调用）
        self._lock = threading.RLock()
        self._devices: Dict[str, Device] = {}
        self._stats = {"requests": 0, "errors": 0, "chars_out": 0}

    @staticmethod
    def _today() -> str:
        return time.strftime("%Y-%m-%d")

    def device(self, device_id: str) -> Device:
        with self._lock:
            dev = self._devices.get(device_id)
            if dev is None:
                dev = Device(device_id=device_id, day=self._today())
                self._devices[device_id] = dev
            if dev.day != self._today():
                dev.day, dev.credits_used = self._today(), 0
            return dev

    def consume(self, device_id: str) -> Tuple[bool, int]:
        with self._lock:
            dev = self.device(device_id)
            if dev.credits_used >= DEFAULT_DAILY_FREE:
                return False, 0
            dev.credits_used += 1
            return True, DEFAULT_DAILY_FREE - dev.credits_used

    def bump(self, ok: bool, chars: int = 0) -> None:
        with self._lock:
            self._stats["requests"] += 1
            if not ok:
                self._stats["errors"] += 1
            self._stats["chars_out"] += chars

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "requests": self._stats["requests"],
                "errors": self._stats["errors"],
                "devices": len(self._devices),
                "charsOut": self._stats["chars_out"],
            }


store = Store()


# ---------------------------------------------------------------------------
# 协议错误
# ---------------------------------------------------------------------------


class ProtocolError(Exception):
    def __init__(self, message: str, code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# 复刻推理（本地桩，不调用任何模型）
# ---------------------------------------------------------------------------

_PREFIX = "[mock] "


def _words(text: str) -> int:
    return len([w for w in text.split() if w])


def _normalize_messages(raw: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise ProtocolError("MyCompletionRequest.messages must be a non-empty list")
    out: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ProtocolError("MyCompletionMessage must be an object")
        role = item.get("role")
        if not isinstance(role, str) or role not in ROLES:
            raise ProtocolError(f"MyCompletionMessage.role must be one of {list(ROLES)}")
        if "content" not in item:
            raise ProtocolError("MyCompletionMessage.content is required")
        parts = item["content"]
        if isinstance(parts, str):
            parts = [{"type": "text", "text": parts}]
        if not isinstance(parts, list) or not parts:
            raise ProtocolError("MyCompletionMessage.content must be a non-empty list")
        clean = []
        for part in parts:
            if not isinstance(part, dict):
                raise ProtocolError("MessageContent must be an object")
            ptype = part.get("type")
            if ptype not in CONTENT_TYPES:
                raise ProtocolError(f"MessageContent.type must be one of {list(CONTENT_TYPES)}")
            entry: Dict[str, Any] = {"type": ptype}
            if ptype == "text":
                if not isinstance(part.get("text"), str):
                    raise ProtocolError("MessageContent.text is required for type=text")
                entry["text"] = part["text"]
            elif ptype == "image_url":
                entry["imageUrl"] = part.get("imageUrl") or part.get("url")
            else:
                entry["url"] = part.get("url")
            clean.append(entry)
        out.append({"role": role, "content": clean})
    return out


def _flatten(messages: List[Dict[str, Any]], role: str) -> str:
    chunks = []
    for msg in messages:
        if msg["role"] != role:
            continue
        for part in msg["content"]:
            if part["type"] == "text":
                chunks.append(part["text"])
    return "\n".join(chunks)


def _reply(model: str, system: str, prompt: str, length: Optional[int]) -> str:
    body = (
        f"{_PREFIX}({model}) received {_words(prompt)} words"
        + (f", system prompt {_words(system)} words" if system else "")
        + f". First line: {prompt.strip().splitlines()[0][:120]!r}. "
        "No upstream model was called."
    )
    if isinstance(length, int) and length > 0:
        words = body.split(" ")
        body = " ".join(words[:length])
    return body


# ---------------------------------------------------------------------------
# endpoint 实现
# ---------------------------------------------------------------------------


def endpoint_health(*_a: Any, **_k: Any) -> Dict[str, Any]:
    return {"status": "ok", "time": int(time.time()), "stats": store.snapshot()}


def endpoint_auth_register(args: Dict[str, Any]) -> Dict[str, Any]:
    """app_core/hive/models/device.hive.dart 的匿名设备注册 + 客户端自签 JWT。"""
    device_id = args.get("deviceId") or args.get("device_id") or "dev-" + uuid.uuid4().hex[:12]
    dev = store.device(str(device_id))
    return {
        "deviceId": dev.device_id,
        "secret": sign_secret("nexbot", dev.device_id),
        "creditsDaily": DEFAULT_DAILY_FREE,
        "creditsLeft": DEFAULT_DAILY_FREE - dev.credits_used,
        "model": DEFAULT_MODEL,
    }


def endpoint_config_get(*_a: Any, **_k: Any) -> Dict[str, Any]:
    """core_client EndpointConfig 下发的运行时配置（对照 Remote Config 实测值）。"""
    return {
        "build": {"minBuildNumber": 0, "forceUpdate": False},
        "links": {
            "privacy": "https://nexbot.ai/privacy",
            "terms": "https://nexbot.ai/terms",
            "support": "https://nexbot.ai/",
        },
        "ai": {
            "model": DEFAULT_MODEL,
            "maxInputWords": MAX_INPUT_WORDS,
            "minInputWords": MIN_INPUT_WORDS,
            "creditsDailyFree": DEFAULT_DAILY_FREE,
        },
    }


def endpoint_chat_completion(args: Dict[str, Any]) -> Dict[str, Any]:
    """复刻 chatCompletion 的调用：入参 MyCompletionRequest，返回 MyCompletionResponse。"""
    request = args.get("request")
    if request is None:
        request = args
    if not isinstance(request, dict):
        raise ProtocolError("chatCompletion expects a MyCompletionRequest object")

    messages = _normalize_messages(request.get("messages"))
    model = request.get("model") or DEFAULT_MODEL
    system = request.get("system") or ""
    length = request.get("length")

    # secret 是客户端自签 JWT；上游用它校验，这里按同样算法验证
    secret = request.get("secret")
    device_id = "anonymous"
    if isinstance(secret, str) and secret:
        claims = verify_secret(secret)
        if claims is None:
            raise ProtocolError("invalid or expired client secret", code=401)
        device_id = str(claims.get("dev") or "anonymous")
    else:
        device_id = str(request.get("deviceId") or "anonymous")

    prompt = _flatten(messages, "user")
    words = _words(prompt)
    if words > MAX_INPUT_WORDS:
        raise ProtocolError(f"input exceeds maxInputWords={MAX_INPUT_WORDS}", code=413)

    allowed, left = store.consume(device_id)
    if not allowed:
        raise ProtocolError(
            f"daily credit exhausted: free tier allows {DEFAULT_DAILY_FREE} messages per device per day",
            code=429,
        )

    time.sleep(LATENCY_MS / 1000.0)
    content = _reply(model, system, prompt, length if isinstance(length, int) else None)
    store.bump(ok=True, chars=len(content))
    return {"content": content, "creditsLeft": left}


ROUTES: Dict[str, Dict[str, Any]] = {
    # 方法名 `chat` 依据 Serverpod 端点命名惯例推断；反编译快照未保留该字符串常量
    "chatCompletion": {"chat": endpoint_chat_completion},
    "auth": {"register": endpoint_auth_register},
    "config": {"get": endpoint_config_get},
    "health": {"check": endpoint_health},
}


def dispatch(endpoint: str, method: str, args: Dict[str, Any]) -> Dict[str, Any]:
    group = ROUTES.get(endpoint)
    if group is None:
        raise ProtocolError(f"no endpoint named '{endpoint}'", code=404)
    handler = group.get(method)
    if handler is None:
        raise ProtocolError(f"endpoint '{endpoint}' has no method '{method}'", code=404)
    return handler(args)


# ---------------------------------------------------------------------------
# OpenAI 兼容层（给 new-api / 各类客户端用）
# ---------------------------------------------------------------------------


def openai_to_request(body: Dict[str, Any]) -> Dict[str, Any]:
    raw = body.get("messages")
    if not isinstance(raw, list):
        raise ProtocolError("messages must be a list")
    system = ""
    messages = []
    for msg in raw:
        role = msg.get("role", "user")
        if role == "system":
            content = msg.get("content")
            system = content if isinstance(content, str) else json.dumps(content)
            continue
        parts = msg.get("content")
        if isinstance(parts, str):
            parts = [{"type": "text", "text": parts}]
        messages.append({"role": role if role in ROLES else "user", "content": parts})
    max_tokens = body.get("max_tokens")
    return {
        "model": body.get("model") or DEFAULT_MODEL,
        "system": system,
        "messages": messages,
        "length": max_tokens if isinstance(max_tokens, int) else None,
        "secret": body.get("secret") or "",
        "deviceId": body.get("user") or "openai-client",
    }


def request_to_openai(req: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    content = result.get("content", "")
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex[:24],
        "object": "chat.completion",
        "created": int(time.time()),
        "model": req.get("model", DEFAULT_MODEL),
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
        ],
        "usage": {
            "prompt_tokens": _words(_flatten(_normalize_messages(req["messages"]), "user")),
            "completion_tokens": _words(content),
            "total_tokens": _words(_flatten(_normalize_messages(req["messages"]), "user")) + _words(content),
        },
    }


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "NexBotProtocolReplica/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        if os.environ.get("MOCK_QUIET") == "1":
            return
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- helpers ---------------------------------------------------------
    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolError(f"body is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ProtocolError("body must be a JSON object")
        return data

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _send_json(self, code: int, payload: Dict[str, Any]) -> None:
        blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(blob)))
        self._cors()
        self.end_headers()
        self.wfile.write(blob)

    def _send_sse(self, model: str, content: str) -> None:
        chunk_id = "chatcmpl-" + uuid.uuid4().hex[:24]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self._cors()
        self.end_headers()

        def emit(obj: Any) -> None:
            payload = f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8")
            self.wfile.write(b"%X\r\n" % len(payload))
            self.wfile.write(payload + b"\r\n")
            self.wfile.flush()

        for idx, word in enumerate(content.split(" ")):
            emit(
                {
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "model": model,
                    "choices": [
                        {"index": 0, "delta": {"content": word if idx == 0 else " " + word}, "finish_reason": None}
                    ],
                }
            )
            time.sleep(0.01)
        emit(
            {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
        )
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _serverpod_error(self, err: ProtocolError) -> Dict[str, Any]:
        return {"error": err.message, "code": err.code, "stackTrace": None, "type": "ProtocolError"}

    # -- verbs -----------------------------------------------------------
    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/health", "/healthz"):
            self._send_json(200, endpoint_health())
            return
        if self.path == "/stats":
            self._send_json(200, store.snapshot())
            return
        self._send_json(404, {"error": {"message": f"no route for GET {self.path}"}})

    def do_POST(self) -> None:  # noqa: N802
        try:
            if self.path.startswith("/v1/chat/completions"):
                self._handle_openai()
            elif self.path in ("/", ""):
                self._handle_envelope()
            else:
                self._handle_route()
        except ProtocolError as err:
            store.bump(ok=False)
            self._send_json(err.code, {"error": {"message": err.message, "code": err.code}})
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001 - mock server: log and surface
            store.bump(ok=False)
            self._send_json(500, {"error": {"message": f"internal error: {exc}"}})

    # -- handlers --------------------------------------------------------
    def _handle_openai(self) -> None:
        body = self._read_json()
        if ERROR_RATE > 0 and (time.time() * 1000) % 1000 < ERROR_RATE * 1000:
            raise ProtocolError("synthetic upstream failure (MOCK_ERROR_RATE)", code=502)
        req = openai_to_request(body)
        result = endpoint_chat_completion({"request": req})
        if body.get("stream"):
            self._send_sse(req.get("model", DEFAULT_MODEL), result.get("content", ""))
        else:
            self._send_json(200, request_to_openai(req, result))

    def _handle_envelope(self) -> None:
        body = self._read_json()
        method_spec = body.get("method")
        if not isinstance(method_spec, str) or "." not in method_spec:
            raise ProtocolError("envelope body must carry 'method': '<endpoint>.<method>'")
        endpoint, method = method_spec.split(".", 1)
        args = body.get("arguments") or {}
        if not isinstance(args, dict):
            raise ProtocolError("'arguments' must be an object")
        try:
            self._send_json(200, {"result": dispatch(endpoint, method, args)})
        except ProtocolError as err:
            self._send_json(err.code, self._serverpod_error(err))

    def _handle_route(self) -> None:
        parts = [p for p in self.path.split("/") if p]
        if len(parts) < 2:
            raise ProtocolError("expected path /{endpoint}/{method}", code=404)
        endpoint, method = parts[0], parts[1]
        try:
            self._send_json(200, dispatch(endpoint, method, self._read_json()))
        except ProtocolError as err:
            self._send_json(err.code, self._serverpod_error(err))


class ReplicaServer(ThreadingHTTPServer):
    """压测友好：放大 listen backlog，避免 20+ 并发下 accept 队列溢出被 reset。"""

    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 512


def main() -> int:
    server = ReplicaServer((HOST, PORT), Handler)
    print(f"protocol replica listening on http://{HOST}:{PORT}")
    print(f"  model={DEFAULT_MODEL} dailyFree={DEFAULT_DAILY_FREE} latency={LATENCY_MS}ms")
    print(f"  jwt key = secretSeed[:30] = {JWT_KEY!r}")
    print("  routes:")
    print("    POST /chatCompletion/chat        Serverpod route form")
    print("    POST /                           Serverpod envelope form")
    print("    POST /auth/register              device registration + client-signed JWT")
    print("    POST /config/get                 runtime config")
    print("    POST /v1/chat/completions        OpenAI compatible (stream supported)")
    print("    GET  /health  /stats")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
