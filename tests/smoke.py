#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
smoke.py — 协议复刻服务端冒烟测试

覆盖：健康检查 / 设备注册与 JWT / 配置下发 / Serverpod 路由形态 / 信封形态 /
      OpenAI 兼容形态 / 流式 / 参数校验失败路径 / secret 校验 / 每日额度耗尽。

零第三方依赖。运行：python tests/smoke.py
退出码：0 全绿，1 有失败。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server" / "mock_serverpod.py"
DAILY_FREE = 2
passed: list[str] = []
failed: list[tuple[str, str]] = []

TEXT_MSG = [{"role": "user", "content": [{"type": "text", "text": "hello protocol replica"}]}]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def req(base: str, path: str, payload=None, method: str = "POST"):
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
    r = urllib.request.Request(
        base + path, data=data, headers={"Content-Type": "application/json"}, method=method
    )
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw.strip().startswith(("{", "[")) else raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        passed.append(name)
        print(f"  PASS  {name}")
    else:
        failed.append((name, detail))
        print(f"  FAIL  {name}  {detail}")


def wait_up(base: str, seconds: float = 20.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/health", timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.2)
    return False


def main() -> int:
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = dict(os.environ)
    env.update({"MOCK_PORT": str(port), "MOCK_QUIET": "1", "MOCK_LATENCY_MS": "10"})
    proc = subprocess.Popen(
        [sys.executable, str(SERVER)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        if not wait_up(base):
            print("server failed to start")
            return 1

        print("\n[1] health")
        code, body = req(base, "/health", None, "GET")
        check("health 200", code == 200, str(code))
        check("health status ok", isinstance(body, dict) and body.get("status") == "ok", str(body))

        print("\n[2] auth.register (device + client-signed JWT)")
        code, body = req(base, "/auth/register", {"deviceId": "smoke-device"})
        check("register 200", code == 200, str(code))
        check("register returns deviceId", body.get("deviceId") == "smoke-device", str(body))
        secret = body.get("secret") or ""
        check("register returns JWT secret", secret.count(".") == 2, secret[:40])
        check("register reports daily quota", body.get("creditsDaily") == DAILY_FREE, str(body))

        print("\n[3] config.get")
        code, body = req(base, "/config/get", {})
        check("config 200", code == 200, str(code))
        check("config has model", isinstance(body.get("ai", {}).get("model"), str), str(body))
        check(
            "config credits match remote config sample",
            body.get("ai", {}).get("creditsDailyFree") == DAILY_FREE,
            str(body),
        )

        print("\n[4] chatCompletion.chat — Serverpod route form")
        payload = {"request": {"model": "gemini-3.5-flash-lite", "messages": TEXT_MSG, "secret": secret}}
        code, body = req(base, "/chatCompletion/chat", payload)
        check("chat 200", code == 200, str(code))
        check("chat returns MyCompletionResponse.content", isinstance(body.get("content"), str), str(body))
        check("chat reports creditsLeft", "creditsLeft" in (body or {}), str(body))

        print("\n[5] envelope form")
        code, body = req(base, "/", {"method": "config.get", "arguments": {}})
        check("envelope 200", code == 200, str(code))
        check("envelope wraps result", isinstance(body, dict) and "result" in body, str(body))

        print("\n[6] request validation")
        code, _ = req(base, "/chatCompletion/chat", {"request": {"messages": []}})
        check("empty messages rejected", code == 400, str(code))
        code, _ = req(base, "/chatCompletion/chat", {"request": {"messages": [{"role": "user"}]}})
        check("missing content rejected", code == 400, str(code))
        code, _ = req(base, "/chatCompletion/chat", {"request": {"messages": [{"role": "bot", "content": [{"type": "text", "text": "x"}]}]}})
        check("invalid role rejected", code == 400, str(code))
        code, _ = req(base, "/chatCompletion/chat", {"request": {"messages": [{"role": "user", "content": [{"type": "video", "url": "x"}]}]}})
        check("invalid content type rejected", code == 400, str(code))
        code, _ = req(base, "/nope/nothing", {})
        check("unknown endpoint 404", code == 404, str(code))

        print("\n[7] secret (client JWT) enforcement")
        code, body = req(
            base,
            "/chatCompletion/chat",
            {"request": {"deviceId": "no-token", "secret": "not.a.jwt", "messages": TEXT_MSG}},
        )
        check("invalid secret rejected", code == 401, str(code))
        forged = secret[:-4] + ("aaaa" if not secret.endswith("aaaa") else "bbbb")
        code, body = req(
            base, "/chatCompletion/chat", {"request": {"secret": forged, "messages": TEXT_MSG}}
        )
        check("tampered signature rejected", code == 401, str(code))

        print("\n[8] daily quota (per device)")
        codes = []
        msgs = [{"role": "user", "content": [{"type": "text", "text": "ping"}]}]
        for _ in range(DAILY_FREE + 1):
            c, _b = req(
                base,
                "/chatCompletion/chat",
                {"request": {"deviceId": "smoke-quota", "messages": msgs}},
            )
            codes.append(c)
        check("free tier allowed", codes[:DAILY_FREE] == [200] * DAILY_FREE, str(codes))
        check("blocked after free tier", codes[-1] == 429, str(codes))

        print("\n[9] openai compatible")
        code, body = req(
            base,
            "/v1/chat/completions",
            {"model": "gemini-3.5-flash-lite", "user": "smoke-openai", "messages": msgs},
        )
        check("openai 200", code == 200, str(code))
        check("openai object shape", (body or {}).get("object") == "chat.completion", str(body))
        check(
            "openai content is string",
            isinstance((body or {}).get("choices", [{}])[0].get("message", {}).get("content"), str),
            str(body),
        )

        print("\n[10] openai streaming")
        r = urllib.request.Request(
            base + "/v1/chat/completions",
            data=json.dumps({"user": "smoke-stream", "stream": True, "messages": msgs}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        chunks = 0
        ctype = ""
        with urllib.request.urlopen(r, timeout=30) as resp:
            ctype = resp.headers.get("Content-Type", "")
            for line in resp:
                if line.startswith(b"data: "):
                    chunks += 1
        check("stream content-type", "event-stream" in ctype, ctype)
        check("stream produced chunks", chunks >= 3, f"chunks={chunks}")

        print("\n[11] max input words guard")
        long_text = " ".join(["word"] * 5000)
        code, _ = req(
            base,
            "/chatCompletion/chat",
            {"request": {"deviceId": "smoke-long", "messages": [{"role": "user", "content": [{"type": "text", "text": long_text}]}]}},
        )
        check("over-length input rejected", code == 413, str(code))

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    total = len(passed) + len(failed)
    print(f"\n{'=' * 46}")
    print(f"RESULT  {len(passed)}/{total} passed")
    if failed:
        for name, detail in failed:
            print(f"  - {name}: {detail}")
        return 1
    print("ALL GREEN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
