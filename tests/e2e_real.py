#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""houmai 真实端到端验证（需显式授权，默认不运行）。

它会用构造的会话记录触发一次**真实的** codex resume 调用，因此必须显式指定目标会话，
且目标应当是一个可弃用的会话。运行方式：

    HOUMAI_E2E_SESSION=<会话id> \
    HOUMAI_E2E_CWD=<该会话的工作目录> \
    HOUMAI_E2E_MODEL=<模型，如 gpt-5.6-sol> \
    python tests/e2e_real.py

未设置 HOUMAI_E2E_SESSION 时脚本直接跳过，不会发起任何调用。

运行时与网络出口默认照搬项目 config.json（可用 HOUMAI_E2E_CONFIG 指定别的），
只有会话目录换成构造的 fixture —— 否则验的是"裸环境"，等于绕开了要验的东西。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable
ENTRY = os.path.join(ROOT, "src", "houmai.py")

SESSION = os.environ.get("HOUMAI_E2E_SESSION", "").strip()
CWD = os.environ.get("HOUMAI_E2E_CWD", "").strip()
MODEL = os.environ.get("HOUMAI_E2E_MODEL", "gpt-5.6-sol").strip()
# 默认取本项目自己的 config.json：验的就是生产那条路。
CONFIG = os.environ.get("HOUMAI_E2E_CONFIG", "").strip() or os.path.join(ROOT, "config.json")



def iso(epoch: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def main() -> int:
    if not SESSION:
        print("未设置 HOUMAI_E2E_SESSION，跳过真实端到端验证。")
        return 0
    if not CWD:
        print("缺少 HOUMAI_E2E_CWD，退出。")
        return 2
    tmp = tempfile.mkdtemp(prefix="houmai-e2e-")
    home = os.path.join(tmp, "codexhome")
    day = os.path.join(home, "sessions", "2026", "09", "13")
    os.makedirs(day, exist_ok=True)
    now = time.time()
    lines = [
        {"timestamp": iso(now - 900), "type": "session_meta", "payload": {
            "id": SESSION, "session_id": SESSION, "parent_thread_id": SESSION,
            "cwd": CWD, "originator": "Codex Desktop", "thread_source": "user"}},
        {"timestamp": iso(now - 890), "type": "turn_context", "payload": {
            "turn_id": "e2e-t1", "root_turn_id": "e2e-t1", "cwd": CWD, "model": MODEL,
            "effort": "low", "approval_policy": "on-request", "approvals_reviewer": "auto_review",
            "sandbox_policy": {"type": "read-only"}, "timezone": "Asia/Shanghai"}},
        {"timestamp": iso(now - 880), "type": "event_msg", "payload": {
            "type": "token_count", "info": {"rate_limits": {
                "primary": {"used_percent": 100.0, "window_minutes": 300, "resets_at": int(now - 600)},
                "secondary": {"used_percent": 10.0, "window_minutes": 10080, "resets_at": int(now + 86400 * 5)}}}}},
        {"timestamp": iso(now - 870), "type": "event_msg", "payload": {
            "type": "task_complete", "turn_id": "e2e-t1", "last_agent_message": None,
            "error": {"message": "You've hit your usage limit.", "codex_error_info": "usage_limit_exceeded"}}},
    ]
    fixture = os.path.join(day, f"rollout-e2e-{SESSION}.jsonl")
    with open(fixture, "w", encoding="utf-8") as f:
        for rec in lines:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # 运行时与网络出口必须照搬生产配置，否则验的是"裸环境"这条路，
    # 恰好绕开了真正要验的东西（代理出口、运行时独立性）。只有会话目录指向 fixture。
    codex_cfg: dict = {"command": None, "codex_home": home, "min_version": "0.150.0"}
    if os.path.exists(CONFIG):
        try:
            with open(CONFIG, "r", encoding="utf-8") as f:
                real = json.load(f)
            codex_cfg.update({k: v for k, v in (real.get("codex") or {}).items()})
        except Exception as exc:
            print(f"读取生产配置失败（{CONFIG}）：{exc}")
    codex_cfg["codex_home"] = home

    cfg = {
        "codex": codex_cfg,
        "watch": {"ignore_threads": [], "lookback_days": 3, "silence_minutes": 0,
                  "initial_lookback_hours": 24},
        "retry": {"prompt": "continue", "buffer_seconds": 60, "max_attempts": 1,
                  "backoff_seconds": [60], "coalesce_minutes": 10},
        "notify": {"provider": "none", "serverchan_key": "", "on": []},
        "state_dir": os.path.join(tmp, "state"),
    }
    cfg_path = os.path.join(tmp, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)
    state_path = os.path.join(tmp, "state", "state.json")

    print(f"目标会话：{SESSION}")
    print(f"工作目录：{CWD}  模型：{MODEL}  沙箱：read-only")
    print(f"生产配置：{CONFIG if os.path.exists(CONFIG) else '（不存在，用默认）'}")

    # 先把"这次到底用哪个运行时、走哪个出口"打出来，省得事后靠猜。
    sys.path.insert(0, os.path.join(ROOT, "src"))
    import houmai as h

    loaded = h.load_config(cfg_path)
    resolved = h.resolve_codex(loaded)
    markers = h.external_markers(loaded)
    print(f"Codex 入口：{' '.join(resolved) if resolved else '未找到'}")
    print(f"排除目录  ：{', '.join(markers) or '（无）'}")
    if resolved:
        leaked = [p for p in resolved if not h.is_external(p, markers)]
        print(f"运行时独立：{'✓ 是' if not leaked else '✗ 否 —— ' + str(leaked)}")
    print(f"网络出口  ：{json.dumps(codex_cfg.get('env') or {}, ensure_ascii=False)}")
    print("开始真实续跑……")
    started = time.time()
    proc = subprocess.run([PY, ENTRY, "--config", cfg_path, "--state", state_path, "run", "-v"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (proc.stdout or "") + (proc.stderr or "")
    print(out[-1500:])

    ok = False
    events_path = os.path.join(tmp, "state", "events.jsonl")
    if os.path.exists(events_path):
        with open(events_path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                kind = ev.get("kind")
                if kind == "resumed":
                    ok = True
                    secs = ev.get("elapsed")
                    shown = f"{secs:.1f}s" if isinstance(secs, (int, float)) else "?"
                    print(f"记录到续跑事件：verdict={ev.get('verdict')} "
                          f"elapsed={shown} model={ev.get('model')}")
                elif kind in ("retry", "abandoned"):
                    print(f"记录到未成功事件：{kind} reason={ev.get('reason')}")

    # 独立佐证：fixture 只是"诱饵"，真身会话文件在真实 codex 目录里。
    # codex resume 是往同一个 rollout 文件追加的，所以它应当在本轮之后仍在写入。
    # 少了这一步，就分不清"真接回了"和"进程退 0 但什么也没发生"。
    real_home = os.environ.get("HOUMAI_E2E_REAL_HOME", "").strip() or \
        os.path.join(os.path.expanduser("~"), ".codex")
    real_files: list[str] = []
    for dirpath, _dirs, names in os.walk(os.path.join(real_home, "sessions")):
        for n in names:
            if n.endswith(".jsonl") and SESSION in n:
                real_files.append(os.path.join(dirpath, n))
    print(f"真身会话文件：{len(real_files)} 个（{real_home}）")
    for fp in real_files:
        mtime = os.path.getmtime(fp)
        grew = mtime >= started
        stamp = time.strftime("%H:%M:%S", time.localtime(mtime))
        print(f"  {os.path.basename(fp)}  最后写入 {stamp}  "
              f"{'✓ 续跑后仍在写入 —— 真身已接回' if grew else '· 本轮未变动'}")
        ok = ok or grew

    print("端到端结果：" + ("通过（续跑已发送并核对）" if ok else "未通过（详见上方输出）"))
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
