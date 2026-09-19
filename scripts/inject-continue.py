# -*- coding: utf-8 -*-
"""调试工具：向桌面打开的指定会话投递一条 continue（或自定义文本）。

用法：py -3 scripts/inject-continue.py <thread_id> [文本]
仅用于验收与排障，值守续跑不直接调用本脚本。
"""
import json
import os
import struct
import sys
import threading
import time
import uuid

PIPE = r"\\.\pipe\codex-ipc"


def _safe(fn, log):
    try:
        fn(log)
    except Exception as e:
        log.append("FATAL: %r" % (e,))


def run(log, tid, text):
    f = open(PIPE, "r+b", buffering=0)

    def send(obj):
        body = json.dumps(obj).encode("utf-8")
        f.write(struct.pack("<I", len(body)))
        f.write(body)

    def read_exact(n):
        buf = b""
        while len(buf) < n:
            chunk = f.read(n - len(buf))
            if not chunk:
                raise EOFError("pipe closed")
            buf += chunk
        return buf

    def read_frame():
        (size,) = struct.unpack("<I", read_exact(4))
        if size == 0 or size > 256 * 1024 * 1024:
            raise ValueError("bad frame size %d" % size)
        return json.loads(read_exact(size).decode("utf-8"))

    def request(method, params, version, target=None, timeout=12):
        rid = str(uuid.uuid4())
        send({"type": "request", "requestId": rid, "sourceClientId": "houmai-inject",
              "version": version, "method": method, "params": params,
              "timeoutMs": 10000} | ({"targetClientId": target} if target else {}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            m = read_frame()
            log.append("<< " + json.dumps(m, ensure_ascii=False)[:500])
            if m.get("type") == "response" and m.get("requestId") == rid:
                return m
            if m.get("type") == "client-discovery-request":
                send({"type": "client-discovery-response",
                      "requestId": m["requestId"],
                      "response": {"canHandle": False}})
        return None

    r = request("initialize", {"clientType": "houmai-inject"}, 0)
    if not r or r.get("resultType") != "success":
        log.append("ABORT: handshake failed")
        return
    r = request("thread-owner-discovery", {"hostId": "local", "conversationId": tid}, 1)
    owner = (r or {}).get("handledByClientId")
    log.append("OWNER: %s" % owner)
    if not owner:
        log.append("ABORT: no owner")
        return
    r = request("thread-follower-start-turn", {
        "conversationId": tid,
        "turnStart": {
            "request": {
                "threadId": tid,
                "input": [{"type": "text", "text": text, "text_elements": []}],
                "clientUserMessageId": str(uuid.uuid4()),
            },
            "context": {"inheritThreadSettings": True},
        },
    }, 2, target=owner)
    log.append("START-TURN: " + (json.dumps(r, ensure_ascii=False)[:400] if r else "None"))
    # 注意：回执超时(start-turn-timeout)不代表失败，需读日志核对 turn 是否实际启动。
    try:
        f.close()
    except Exception:
        pass


def main():
    if len(sys.argv) < 2:
        print("usage: py -3 scripts/inject-continue.py <thread_id> [text]")
        return
    tid = sys.argv[1]
    text = sys.argv[2] if len(sys.argv) > 2 else "continue"
    log = []
    t = threading.Thread(target=lambda: _safe(run, log, tid, text), daemon=True)
    t.start()
    t.join(45)
    if t.is_alive():
        log.append("TIMEOUT after 45s")
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "state", "inject-continue-result.txt")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(log))
    print("done ->", out)


if __name__ == "__main__":
    main()
