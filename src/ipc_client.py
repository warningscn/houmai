#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ipc_client — Codex Desktop 本机直跑通道（实验性）。

原理（2026-09-13 实机验证通过，见 docs/DESIGN-ipc-direct-resume.md）：
Codex Desktop 运行时在本机开一条 IPC 总线，Windows 为命名管道
\\\\.\\pipe\\codex-ipc，帧格式为「4 字节小端长度前缀 + JSON 体」。
以客户端身份握手后，可以：
  1. thread-owner-discovery   按完整任务 ID 查询该会话是否被桌面持有；
  2. thread-follower-start-turn  以跟随者身份向原会话定向投递一条文本。
指令从桌面自己的入口进来，不与 CLI 的单写者锁冲突，并在原窗口中原样呈现。

边界与教训（都来自实机）：
  · 回执 start-turn-timeout 不代表失败——turn 可能实际已启动。
    因此结果只会是 sent / unknown / rejected，调用方对 unknown 必须
    「读回核对，不盲目重发」。
  · 这是桌面私有协议，Codex 升级可能失效；所有异常都收敛为
    unavailable，让调用方回落到 codex exec 通道。
只连本机端点，不发送任何凭据，不做键盘模拟，不碰 Codex 自身 UI。
"""

from __future__ import annotations

import json
import os
import struct
import threading
import time
import uuid

# 桌面版 IPC 端点。测试可通过参数注入假管道名，绝不摸真实端点。
PIPE_PATH = r"\\.\pipe\codex-ipc"
SOCK_PATH = os.path.join(os.path.expanduser("~"), ".codex", "ipc", "ipc.sock")

# 这些错误意味着「结果未知」：请求可能已被桌面受理。
UNKNOWN_ERRORS = {
    "start-turn-timeout", "request-timeout",
    "client-disconnected", "server-closed",
}


def default_endpoint() -> str:
    return PIPE_PATH if os.name == "nt" else SOCK_PATH


def encode_frame(msg: dict) -> bytes:
    body = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    return struct.pack("<I", len(body)) + body


def decode_frame(body: bytes) -> dict:
    return json.loads(body.decode("utf-8"))


def desktop_resume(thread_id: str, text: str, timeout: float = 30.0,
                   endpoint: str | None = None) -> dict:
    """一次性会话：握手 → 查持有者 → 投递一条文本。

    返回 {"status": sent|unknown|rejected|no_owner|unavailable, "error"?, "owner"?}。
    任何异常（管道不存在、协议不兼容、超时等）都收敛为对应状态，绝不抛出。
    """
    result: dict = {}
    done = threading.Event()

    def worker():
        try:
            result.update(_run(thread_id, text, timeout, endpoint))
        except Exception as exc:  # noqa: BLE001 — 边界处必须兜住
            result["status"] = "unavailable"
            result["error"] = repr(exc)
        finally:
            done.set()

    threading.Thread(target=worker, daemon=True, name="houmai-ipc").start()
    # 网络动作自身有 timeout，外层再放宽一点收尸；超时按「结果未知」处理，
    # 因为请求可能已经送进桌面。
    if not done.wait(timeout + 10):
        result = {"status": "unknown", "error": "local deadline exceeded"}
    return result


def _connect(ep: str):
    """连一次桌面端点。管道会有瞬时抖动（实测 FileNotFound 过一次），
    调用方应短暂重试几次再下结论。"""
    if os.name == "nt":
        return open(ep, "r+b", buffering=0)  # noqa: SIM115 — 生命周期在 _run 内
    import socket as _socket
    io = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    io.settimeout(30.0)
    io.connect(ep)
    return io


def _run(thread_id: str, text: str, timeout: float, endpoint: str | None) -> dict:
    ep = endpoint or default_endpoint()
    is_nt = os.name == "nt"
    io = None
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            io = _connect(ep)
            break
        except OSError as exc:
            last_err = exc
            time.sleep(0.5)
    if io is None:
        return {"status": "unavailable", "error": repr(last_err)}

    def send(obj: dict) -> None:
        if is_nt:
            io.write(encode_frame(obj))
        else:
            io.sendall(encode_frame(obj))

    def recv_exact(n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = io.recv(n - len(buf)) if not is_nt else io.read(n - len(buf))
            if not chunk:
                raise EOFError("desktop ipc closed")
            buf += chunk
        return buf

    def read_frame() -> dict:
        (size,) = struct.unpack("<I", recv_exact(4))
        if size == 0 or size > 256 * 1024 * 1024:
            raise ValueError(f"bad frame size {size}")
        return decode_frame(recv_exact(size))

    def request(method: str, params: dict, version: int,
                target: str | None = None) -> dict | None:
        rid = str(uuid.uuid4())
        msg = {"type": "request", "requestId": rid,
               "sourceClientId": "houmai", "version": version,
               "method": method, "params": params, "timeoutMs": 10000}
        if target:
            msg["targetClientId"] = target
        send(msg)
        deadline = timeout + 10
        end = threading.Event()
        # 读帧循环可能阻塞在 recv/read 上，放到守护线程里兜底。
        got: dict | None = None

        def reader():
            nonlocal got
            try:
                while True:
                    m = read_frame()
                    if m.get("type") == "response" and m.get("requestId") == rid:
                        got = m
                        return
                    if m.get("type") == "client-discovery-request":
                        send({"type": "client-discovery-response",
                              "requestId": m.get("requestId"),
                              "response": {"canHandle": False}})
                    # 其余 broadcast（turn 事件流等）对一次性投递无用，忽略。
            except Exception:
                return
            finally:
                end.set()

        threading.Thread(target=reader, daemon=True).start()
        if not end.wait(deadline):
            return {"type": "response", "resultType": "error", "error": "request-timeout"}
        return got

    try:
        r = request("initialize", {"clientType": "houmai"}, 0)
        if not r or r.get("resultType") != "success":
            return {"status": "unavailable",
                    "error": f"handshake failed: {(r or {}).get('error')}"}

        r = request("thread-owner-discovery",
                    {"hostId": "local", "conversationId": thread_id}, 1)
        if not r:
            return {"status": "unavailable", "error": "owner discovery no reply"}
        if r.get("resultType") != "success":
            err = r.get("error") or ""
            if err == "no-client-found":
                # 桌面没开着这个会话——不是故障，是换道的信号。
                return {"status": "no_owner"}
            return {"status": "unavailable", "error": f"owner discovery: {err}"}
        owner = r.get("handledByClientId")
        if not owner:
            return {"status": "no_owner"}

        r = request("thread-follower-start-turn", {
            "conversationId": thread_id,
            "turnStart": {
                "request": {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": text, "text_elements": []}],
                    "clientUserMessageId": str(uuid.uuid4()),
                },
                "context": {"inheritThreadSettings": True},
            },
        }, 2, target=owner)
        if not r:
            return {"status": "unknown", "error": "start-turn no reply", "owner": owner}
        if r.get("resultType") == "success":
            return {"status": "sent", "owner": owner}
        err = r.get("error") or "unknown error"
        if err in UNKNOWN_ERRORS:
            # 实机教训：start-turn-timeout 时 turn 可能已启动，只能算「结果未知」。
            return {"status": "unknown", "error": err, "owner": owner}
        return {"status": "rejected", "error": err, "owner": owner}
    finally:
        try:
            io.close()
        except Exception:
            pass
