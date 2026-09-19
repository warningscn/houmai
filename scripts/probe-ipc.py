# -*- coding: utf-8 -*-
"""只读探针：验证 Codex Desktop IPC 管道通道。

用途：Codex Desktop 运行时，连本机 codex-ipc 命名管道，握手并查询候脉在册任务的
持有者（owner discovery）。不发送任何 turn，不改变任何状态。

用法：py -3 scripts/probe-ipc.py
输出：state/probe-ipc-result.txt
"""
import json
import os
import re
import struct
import threading
import time
import uuid

# Derived from this file's own location so the probe works from any checkout.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PIPE = r"\\.\pipe\codex-ipc"
OUT = os.path.join(ROOT, "state", "probe-ipc-result.txt")
STATE = os.path.join(ROOT, "state", "state.json")


def run(log):
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

    def request(method, params, version, timeout=12):
        rid = str(uuid.uuid4())
        send({"type": "request", "requestId": rid, "sourceClientId": "houmai-probe",
              "version": version, "method": method, "params": params,
              "timeoutMs": 10000})
        deadline = time.time() + timeout
        while time.time() < deadline:
            m = read_frame()
            log.append("<< " + json.dumps(m, ensure_ascii=False)[:600])
            if m.get("type") == "response" and m.get("requestId") == rid:
                return m
            if m.get("type") == "client-discovery-request":
                send({"type": "client-discovery-response",
                      "requestId": m["requestId"],
                      "response": {"canHandle": False}})
        return None

    r = request("initialize", {"clientType": "houmai-probe"}, 0)
    log.append("INIT: " + (json.dumps(r, ensure_ascii=False)[:400] if r else "None"))

    ids = set()
    try:
        raw = open(STATE, encoding="utf-8").read()
        ids.update(re.findall(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", raw))
    except Exception as e:
        log.append("state read err: %r" % e)
    log.append("IDS: %s" % sorted(ids))

    for tid in sorted(ids):
        try:
            r = request("thread-owner-discovery",
                        {"hostId": "local", "conversationId": tid}, 1)
        except Exception as e:
            log.append("OWNER[%s] EXC: %r" % (tid, e))
            continue
        if r is None:
            log.append("OWNER[%s]: timeout" % tid)
        else:
            log.append("OWNER[%s]: handledBy=%s result=%s" % (
                tid, r.get("handledByClientId"),
                json.dumps(r.get("result"), ensure_ascii=False)[:400]))
    try:
        f.close()
    except Exception:
        pass


def main():
    log = []
    t = threading.Thread(target=lambda: _safe(run, log), daemon=True)
    t.start()
    t.join(90)
    if t.is_alive():
        log.append("TIMEOUT after 90s")
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(log))
    print("done ->", OUT)


def _safe(fn, log):
    try:
        fn(log)
    except Exception as e:
        log.append("FATAL: %r" % (e,))


if __name__ == "__main__":
    main()
