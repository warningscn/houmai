#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""houmai 固定测试：用构造的会话记录验证识别、判定与续跑参数，不触碰真实 Codex。

运行：py -3 tests/smoke.py（用 py -3：受限目录里的解释器会让用例 7 故意失败）
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable
ENTRY = os.path.join(ROOT, "src", "houmai.py")

# 被看护程序的产品名 / 它的数据目录名：**故意拼出来**，让本文件自身不出现这两个词。
# 原因：下面有若干"文档、面板、默认配置里不许出现产品名"的断言，若把词面写在这里，
# 断言的判据本身就留在仓库里了——而这个仓库是要公开的，测试里也不该带上它。
_WATCHED = "Work" + "Buddy"
_WATCHED_DIR = "." + "work" + "buddy"

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  — {detail}" if detail and not ok else ""))


def iso(epoch: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def write_rollout(home: str, name: str, lines: list[dict]) -> str:
    day = os.path.join(home, "sessions", "2026", "09", "13")
    os.makedirs(day, exist_ok=True)
    path = os.path.join(day, name)
    with open(path, "w", encoding="utf-8") as f:
        for rec in lines:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return path


def meta(root_id: str, cwd: str, source: str = "user") -> dict:
    return {"timestamp": iso(time.time() - 600), "type": "session_meta", "payload": {
        "id": root_id, "session_id": root_id, "parent_thread_id": root_id,
        "cwd": cwd, "originator": "Codex Desktop", "thread_source": source,
    }}


def turn_context(turn_id: str, cwd: str, model: str, effort: str, sandbox: str) -> dict:
    # 必须照真实 codex 格式写：turn_context 的类型在**顶层**，payload 里没有 type 字段。
    # 若这里图省事在 payload 里也塞一个 type，就会把"只认 payload.type"的缺陷藏起来
    # ——那正好是续跑丢掉 model/effort/cwd 的原因。
    return {"timestamp": iso(time.time() - 590), "type": "turn_context", "payload": {
        "turn_id": turn_id, "root_turn_id": turn_id, "cwd": cwd, "model": model,
        "effort": effort, "approval_policy": "on-request", "approvals_reviewer": "auto_review",
        "sandbox_policy": {"type": sandbox}, "timezone": "Asia/Shanghai",
    }}


def token_count(primary_reset: float, primary_used: float, secondary_reset: float,
                secondary_used: float, primary_window: float = 300, secondary_window: float = 10080) -> dict:
    return {"timestamp": iso(time.time() - 580), "type": "event_msg", "payload": {
        "type": "token_count", "info": {"rate_limits": {
            "limit_id": "codex", "primary": {
                "used_percent": primary_used, "window_minutes": primary_window,
                "resets_at": int(primary_reset)},
            "secondary": {"used_percent": secondary_used, "window_minutes": secondary_window,
                          "resets_at": int(secondary_reset)},
            "credits": {"has_credits": False, "unlimited": False, "balance": "0"}}}}}


def quota_error(turn_id: str) -> dict:
    return {"timestamp": iso(time.time() - 570), "type": "event_msg", "payload": {
        "type": "task_complete", "turn_id": turn_id, "last_agent_message": None,
        "error": {"message": "You've hit your usage limit. try again at 3:06 AM.",
                  "codex_error_info": "usage_limit_exceeded"}}}


def done_turn(turn_id: str) -> dict:
    return {"timestamp": iso(time.time() - 60), "type": "event_msg", "payload": {
        "type": "task_complete", "turn_id": turn_id, "last_agent_message": "ok"}}


def stream_error(turn_id: str) -> dict:
    return {"timestamp": iso(time.time() - 570), "type": "event_msg", "payload": {
        "type": "task_complete", "turn_id": turn_id, "last_agent_message": None,
        "error": {"message": "stream disconnected before completion", "codex_error_info": "other"}}}


def event_kinds(state_path: str) -> list:
    ev_path = os.path.join(os.path.dirname(state_path), "events.jsonl")
    kinds = []
    if os.path.exists(ev_path):
        with open(ev_path, encoding="utf-8") as f:
            kinds = [json.loads(line).get("kind") for line in f if line.strip()]
    return kinds


def make_config(tmp: str, home: str, retry: dict | None = None, **watch) -> str:
    cfg = {
        "codex": {"command": None, "codex_home": home, "min_version": "0.150.0"},
        "watch": {"ignore_threads": [], "lookback_days": 3, "silence_minutes": 0,
                  "initial_lookback_hours": 24},
        "retry": {"prompt": "continue", "buffer_seconds": 120, "max_attempts": 3,
                  "backoff_seconds": [60, 300, 900], "coalesce_minutes": 10},
        "notify": {"provider": "none", "serverchan_key": "", "on": ["detect", "success", "failure", "long_cycle"]},
        "state_dir": os.path.join(tmp, "state"),
    }
    cfg["watch"].update(watch)
    if retry:
        cfg["retry"].update(retry)
    path = os.path.join(tmp, "config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)
    return path


def run(cfg_path: str, state_path: str) -> str:
    proc = subprocess.run([PY, ENTRY, "--config", cfg_path, "--state", state_path, "run", "--dry-run", "-v"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    return (proc.stdout or "") + (proc.stderr or "")


def load_state(state_path: str) -> dict:
    if not os.path.exists(state_path):
        return {}
    with open(state_path, "r", encoding="utf-8") as f:
        return json.load(f)


def case_short_cycle() -> None:
    print("\n[用例 1] 主窗口额度耗尽，应进入等待并复刻上下文续跑")
    tmp = tempfile.mkdtemp(prefix="houmai-1-")
    home = os.path.join(tmp, "codexhome")
    root = "11111111-1111-7111-8111-111111111111"
    cwd = r"D:\fake\project-a"
    write_rollout(home, f"rollout-{root}.jsonl", [
        meta(root, cwd),
        turn_context("t1", cwd, "gpt-5.6-luna", "max", "workspace-write"),
        token_count(time.time() - 600, 100.0, time.time() + 86400 * 5, 20.0),
        quota_error("t1"),
    ])
    cfg = make_config(tmp, home)
    state_path = os.path.join(tmp, "state", "state.json")
    out = run(cfg, state_path)
    st = load_state(state_path)
    task = (st.get("pending") or {}).get(root) or {}
    check("识别为等待中", task.get("status") == "waiting", f"status={task.get('status')} out={out[-300:]}")
    check("判定为主窗口短周期", (task.get("window") or {}).get("kind") == "short_cycle",
          str(task.get("window")))
    argv = " ".join(task.get("last_argv") or [])
    check("复刻模型", "-m gpt-5.6-luna" in argv, argv)
    check("复刻思考强度", 'model_reasoning_effort="max"' in argv, argv)
    check("复刻沙箱策略", "-s workspace-write" in argv, argv)
    check("复刻工作目录", cwd in argv, argv)
    check("续跑目标为根会话", f"resume {root}" in argv, argv)
    check("续跑指令正确", argv.strip().endswith("continue"), argv)
    shutil.rmtree(tmp, ignore_errors=True)


def case_long_cycle() -> None:
    print("\n[用例 2] 周额度耗尽，默认排期续跑（不再只提醒）")
    tmp = tempfile.mkdtemp(prefix="houmai-2-")
    home = os.path.join(tmp, "codexhome")
    root = "22222222-2222-7222-8222-222222222222"
    cwd = r"D:\fake\project-b"
    secondary_reset = time.time() + 86400 * 3
    write_rollout(home, f"rollout-{root}.jsonl", [
        meta(root, cwd),
        turn_context("t1", cwd, "gpt-5.6-sol", "high", "workspace-write"),
        token_count(time.time() + 3600, 30.0, secondary_reset, 100.0),
        quota_error("t1"),
    ])
    cfg = make_config(tmp, home)
    state_path = os.path.join(tmp, "state", "state.json")
    out = run(cfg, state_path)
    st = load_state(state_path)
    task = (st.get("pending") or {}).get(root) or {}
    check("进入等待队列", task.get("status") == "waiting", f"status={task.get('status')} out={out[-300:]}")
    check("判定为长周期窗口", (task.get("window") or {}).get("kind") == "long_cycle",
          str(task.get("window")))
    check("按重置时刻排期续跑（resets_at + buffer）",
          abs((task.get("retry_at") or 0) - (secondary_reset + 120)) < 5,
          f"retry_at={task.get('retry_at')}")
    kinds = event_kinds(state_path)
    check("事件记为 detected 而非 long_cycle",
          "detected" in kinds and "long_cycle" not in kinds, str(kinds))
    shutil.rmtree(tmp, ignore_errors=True)


def case_long_cycle_off() -> None:
    print("\n[用例 2e] 长周期续跑开关关闭 → 只提醒")
    tmp = tempfile.mkdtemp(prefix="houmai-2e-")
    home = os.path.join(tmp, "codexhome")
    root = "2e2e2e2e-2e2e-7e2e-8e2e-2e2e2e2e2e2e"
    cwd = r"D:\fake\project-b3"
    write_rollout(home, f"rollout-{root}.jsonl", [
        meta(root, cwd),
        turn_context("t1", cwd, "gpt-5.6-sol", "high", "workspace-write"),
        token_count(time.time() + 3600, 30.0, time.time() + 86400 * 3, 100.0),
        quota_error("t1"),
    ])
    cfg = make_config(tmp, home, retry={"resume_long_cycle": False})
    state_path = os.path.join(tmp, "state", "state.json")
    run(cfg, state_path)
    st = load_state(state_path)
    task = (st.get("pending") or {}).get(root) or {}
    check("标记为只提醒", task.get("status") == "notify_only", f"status={task.get('status')}")
    check("跳过原因为开关关闭", task.get("notify_reason") == "switch_off", str(task.get("notify_reason")))
    check("事件记为 long_cycle", "long_cycle" in event_kinds(state_path), str(event_kinds(state_path)))
    shutil.rmtree(tmp, ignore_errors=True)


def case_long_cycle_beyond() -> None:
    print("\n[用例 2f] 长周期重置超出等待上限 → 只提醒")
    tmp = tempfile.mkdtemp(prefix="houmai-2f-")
    home = os.path.join(tmp, "codexhome")
    root = "2f2f2f2f-2f2f-7f2f-8f2f-2f2f2f2f2f2f"
    cwd = r"D:\fake\project-b4"
    write_rollout(home, f"rollout-{root}.jsonl", [
        meta(root, cwd),
        turn_context("t1", cwd, "gpt-5.6-sol", "high", "workspace-write"),
        token_count(time.time() + 3600, 30.0, time.time() + 86400 * 20, 100.0),
        quota_error("t1"),
    ])
    cfg = make_config(tmp, home)  # 默认 max_wait_days=8，20 天 > 8 天
    state_path = os.path.join(tmp, "state", "state.json")
    run(cfg, state_path)
    st = load_state(state_path)
    task = (st.get("pending") or {}).get(root) or {}
    check("标记为只提醒", task.get("status") == "notify_only", f"status={task.get('status')}")
    check("跳过原因为超出上限", task.get("notify_reason") == "beyond_max_wait",
          str(task.get("notify_reason")))
    shutil.rmtree(tmp, ignore_errors=True)


def case_unknown_reset() -> None:
    print("\n[用例 2b] 会话里读不到额度信息：仅提醒（不得谎报为长周期）")
    tmp = tempfile.mkdtemp(prefix="houmai-2b-")
    home = os.path.join(tmp, "codexhome")
    root = "26262626-2626-7262-8262-262626262626"
    cwd = r"D:\fake\project-b2"
    # 只给额度耗尽，不给 token_count/rate_limits —— 复现 01a09ae6 的真实场景
    write_rollout(home, f"rollout-{root}.jsonl", [
        meta(root, cwd),
        turn_context("t1", cwd, "gpt-5.6-sol", "high", "workspace-write"),
        quota_error("t1"),
    ])
    cfg = make_config(tmp, home)
    state_path = os.path.join(tmp, "state", "state.json")
    out = run(cfg, state_path)
    st = load_state(state_path)
    task = (st.get("pending") or {}).get(root) or {}
    check("标记为只提醒", task.get("status") == "notify_only", f"status={task.get('status')}")
    check("窗口判定为 unknown（不是 long_cycle）",
          (task.get("window") or {}).get("kind") == "unknown", str(task.get("window")))
    ev_path = os.path.join(tmp, "state", "events.jsonl")
    kinds = []
    if os.path.exists(ev_path):
        with open(ev_path, encoding="utf-8") as f:
            kinds = [json.loads(line).get("kind") for line in f if line.strip()]
    check("事件记为 unknown_reset 而非 long_cycle",
          "unknown_reset" in kinds and "long_cycle" not in kinds, str(kinds))
    check("未发起续跑", "开始续跑" not in out and "last_argv" not in task, out[-300:])
    shutil.rmtree(tmp, ignore_errors=True)


def case_borrow_window() -> None:
    print("\n[用例 2c] 本会话无额度数据，借用同账号最近观测来排期")
    tmp = tempfile.mkdtemp(prefix="houmai-2c-")
    home = os.path.join(tmp, "codexhome")
    root_a = "27272727-2727-7272-8272-272727272727"
    root_b = "28282828-2828-7282-8282-282828282828"
    cwd = r"D:\fake\project-b3"
    write_rollout(home, f"rollout-{root_a}.jsonl", [
        meta(root_a, cwd),
        turn_context("t1", cwd, "gpt-5.6-sol", "high", "workspace-write"),
        quota_error("t1"),
    ])
    write_rollout(home, f"rollout-{root_b}.jsonl", [
        meta(root_b, cwd),
        turn_context("t2", cwd, "gpt-5.6-sol", "high", "workspace-write"),
        token_count(time.time() + 3600, 100.0, time.time() + 86400 * 3, 46.0),
    ])
    cfg = make_config(tmp, home)
    state_path = os.path.join(tmp, "state", "state.json")
    run(cfg, state_path)
    st = load_state(state_path)
    task = (st.get("pending") or {}).get(root_a) or {}
    win = task.get("window") or {}
    check("借用成功：进入等待而非仅提醒", task.get("status") == "waiting",
          f"status={task.get('status')}")
    check("判定为短周期（主窗口 300 分钟）", win.get("kind") == "short_cycle",
          str(win))
    check("窗口标注为借用", win.get("borrowed") is True, str(win))
    check("已排定续跑时间", bool(task.get("retry_at")), str(task.get("retry_at")))
    shutil.rmtree(tmp, ignore_errors=True)


def case_already_progressed() -> None:
    print("\n[用例 3] 中断后任务已自行继续，应放弃接管")
    tmp = tempfile.mkdtemp(prefix="houmai-3-")
    home = os.path.join(tmp, "codexhome")
    root = "33333333-3333-7333-8333-333333333333"
    cwd = r"D:\fake\project-c"
    write_rollout(home, f"rollout-{root}.jsonl", [
        meta(root, cwd),
        turn_context("t1", cwd, "gpt-5.6-sol", "high", "workspace-write"),
        token_count(time.time() - 60, 100.0, time.time() + 86400 * 5, 10.0),
        quota_error("t1"),
        done_turn("t2"),
    ])
    cfg = make_config(tmp, home)
    state_path = os.path.join(tmp, "state", "state.json")
    out = run(cfg, state_path)
    st = load_state(state_path)
    check("未登记为待续跑", not (st.get("pending") or {}), str(st.get("pending")))
    check("记录为已自行继续", "已自行继续" in out, out[-300:])
    shutil.rmtree(tmp, ignore_errors=True)


def case_non_quota_ignored() -> None:
    print("\n[用例 4] 非额度类故障不处理")
    tmp = tempfile.mkdtemp(prefix="houmai-4-")
    home = os.path.join(tmp, "codexhome")
    root = "44444444-4444-7444-8444-444444444444"
    cwd = r"D:\fake\project-d"
    write_rollout(home, f"rollout-{root}.jsonl", [
        meta(root, cwd),
        turn_context("t1", cwd, "gpt-5.6-sol", "high", "workspace-write"),
        token_count(time.time() - 60, 40.0, time.time() + 86400 * 5, 10.0),
        stream_error("t1"),
    ])
    cfg = make_config(tmp, home)
    state_path = os.path.join(tmp, "state", "state.json")
    out = run(cfg, state_path)
    st = load_state(state_path)
    check("未产生任何在册任务", not (st.get("pending") or {}), str(st.get("pending")))
    check("未出现额度判定", "发现额度耗尽" not in out, out[-300:])
    shutil.rmtree(tmp, ignore_errors=True)


def case_stale_skipped() -> None:
    print("\n[用例 5] 首次运行时的历史中断不接管")
    tmp = tempfile.mkdtemp(prefix="houmai-5-")
    home = os.path.join(tmp, "codexhome")
    root = "55555555-5555-7555-8555-555555555555"
    cwd = r"D:\fake\project-e"
    old = time.time() - 3600 * 48
    write_rollout(home, f"rollout-{root}.jsonl", [
        {"timestamp": iso(old), "type": "session_meta", "payload": {
            "id": root, "session_id": root, "parent_thread_id": root,
            "cwd": cwd, "originator": "Codex Desktop", "thread_source": "user"}},
        {"timestamp": iso(old + 10), "type": "turn_context", "payload": {
            "type": "turn_context", "turn_id": "t1", "cwd": cwd, "model": "gpt-5.6-sol",
            "effort": "high", "approval_policy": "on-request", "approvals_reviewer": "auto_review",
            "sandbox_policy": {"type": "workspace-write"}, "timezone": "Asia/Shanghai"}},
        {"timestamp": iso(old + 20), "type": "event_msg", "payload": {
            "type": "token_count", "info": {"rate_limits": {
                "primary": {"used_percent": 100.0, "window_minutes": 300, "resets_at": int(old + 1800)},
                "secondary": {"used_percent": 10.0, "window_minutes": 10080, "resets_at": int(old + 86400 * 5)}}}}},
        {"timestamp": iso(old + 30), "type": "event_msg", "payload": {
            "type": "task_complete", "turn_id": "t1", "last_agent_message": None,
            "error": {"message": "usage limit", "codex_error_info": "usage_limit_exceeded"}}},
    ])
    cfg = make_config(tmp, home, initial_lookback_hours=12)
    state_path = os.path.join(tmp, "state", "state.json")
    out = run(cfg, state_path)
    st = load_state(state_path)
    check("未登记为待续跑", not (st.get("pending") or {}), str(st.get("pending")))
    check("记录为历史跳过", "跳过历史中断记录" in out, out[-300:])
    shutil.rmtree(tmp, ignore_errors=True)


def case_idempotent() -> None:
    print("\n[用例 6] 重复运行不产生重复续跑")
    tmp = tempfile.mkdtemp(prefix="houmai-6-")
    home = os.path.join(tmp, "codexhome")
    root = "66666666-6666-7666-8666-666666666666"
    cwd = r"D:\fake\project-f"
    write_rollout(home, f"rollout-{root}.jsonl", [
        meta(root, cwd),
        turn_context("t1", cwd, "gpt-5.6-sol", "high", "workspace-write"),
        token_count(time.time() - 60, 100.0, time.time() + 86400 * 5, 10.0),
        quota_error("t1"),
    ])
    cfg = make_config(tmp, home)
    state_path = os.path.join(tmp, "state", "state.json")
    run(cfg, state_path)
    first = load_state(state_path)
    attempts_first = ((first.get("pending") or {}).get(root) or {}).get("attempts")
    out2 = run(cfg, state_path)
    second = load_state(state_path)
    attempts_second = ((second.get("pending") or {}).get(root) or {}).get("attempts")
    check("第二次运行未重复登记", "新增额度中断 0 个" in out2, out2[-200:])
    check("尝试次数未被重复累加", attempts_first == attempts_second,
          f"{attempts_first} -> {attempts_second}")
    shutil.rmtree(tmp, ignore_errors=True)


def case_dry_run_readonly() -> None:
    """演练必须无副作用：不发、不计数、不改状态、不写事件。

    这条用例存在的理由：attempts 的自增曾经写在 dry-run 分支之前，演练一次
    attempts 就变成 1，面板亮出"已尝试 N 次"，而日志和事件里都没有对应动作
    ——一句查无对证的警示比没有警示更坏。
    """
    print("\n[用例 6b] 演练（dry-run）不产生副作用")
    tmp = tempfile.mkdtemp(prefix="houmai-6b-")
    home = os.path.join(tmp, "codexhome")
    root = "6b6b6b6b-6b6b-7b6b-8b6b-6b6b6b6b6b6b"
    cwd = r"D:\fake\project-g"
    write_rollout(home, f"rollout-{root}.jsonl", [
        meta(root, cwd),
        turn_context("t1", cwd, "gpt-5.6-sol", "high", "workspace-write"),
        # 重置时刻在过去 → retry_at 也已到期，演练会真的走到"开始续跑"这一步
        token_count(time.time() - 600, 100.0, time.time() + 86400 * 5, 10.0),
        quota_error("t1"),
    ])
    cfg = make_config(tmp, home)
    state_path = os.path.join(tmp, "state", "state.json")
    out = run(cfg, state_path)
    st = load_state(state_path)
    task = (st.get("pending") or {}).get(root) or {}
    check("演练确实走到续跑判定", "开始续跑" in out, out[-300:])
    check("演练给出将发送的命令", bool(task.get("last_argv")), str(task.get("last_argv")))
    check("演练不累加尝试次数", not task.get("attempts"), f"attempts={task.get('attempts')}")
    check("演练不改状态（仍在等待）", task.get("status") == "waiting", str(task.get("status")))
    kinds = event_kinds(state_path)
    check("演练不写续跑事件", kinds == ["detected"], str(kinds))
    check("summary 标注为演练", (st.get("last_run_summary") or {}).get("dry_run") is True,
          str(st.get("last_run_summary")))
    shutil.rmtree(tmp, ignore_errors=True)


def case_runtime_independence() -> None:
    """候脉是看护工具，绝不能跑在被看护对象的运行时上。"""
    sys.path.insert(0, os.path.join(ROOT, "src"))
    import houmai as h

    print("\n[用例 7] 运行时不得落在被排除目录内")

    # 单元检查用一个中性的假标记：**排除清单是"配置"，不是"某台机器的事实"**。
    # 把写测试那台机器的真实目录名钉进测试，会让这套测试只在那一台机器上成立
    # （别人克隆下来必红，而故障其实在测试自己身上）。下面那条不变式因此改为
    # 取自配置 —— 见紧接着的注释。
    markers = [".bundled-app"]
    check("排除判定：标记目录内的解释器被拒",
          h.is_external(r"C:\Users\<user>\.bundled-app\binaries\python\python.exe", markers) is False)
    check("排除判定：普通安装被接受",
          h.is_external(r"C:\Python312\python.exe", markers) is True)
    check("排除判定：大小写与斜杠不敏感",
          h.is_external("C:/X/.Bundled-App/y/python.exe", markers) is False)
    check("排除判定：空路径视为不可用", h.is_external("", markers) is False)

    cfg = h.load_config(None)

    # 不变式：无论怎么解析，结果里都不能出现**配置里排除的**目录。
    # 清单必须取自配置：写死在测试里等于断言"这台机器的配置是对的"，
    # 别人克隆下来必然红——那说明测试绑定了机器，不是他的环境有问题。
    configured = list(cfg["codex"].get("avoid_paths") or [])
    cmd = h.resolve_codex(cfg)
    check("默认配置能解析出 Codex 入口", bool(cmd), str(cmd))
    if cmd:
        bad = [p for p in cmd if not h.is_external(p, configured)]
        check("解析结果不含被排除运行时", not bad, str(bad))

    # 清单为空本身**不是错误**（没配就没有要排除的东西），但必须看得见：
    # `check` 要照实打「（无）」，不许把"没有这道防线"渲染成"一切正常"。
    hsrc = open(os.path.join(ROOT, "src", "houmai.py"), encoding="utf-8").read()
    check("排除清单为空时照实说明（不许把没有防线显示成一切正常）",
          "排除目录" in hsrc and "（无）" in hsrc)

    rt = h.collect_runtime(cfg)
    check("体检能给出解释器独立性", isinstance(rt.get("python_external"), bool), str(rt))

    # 反向用例：再排除一个真实存在的 node 目录，解析结果必须绕开它。
    cfg2 = h.load_config(None)
    cfg2["codex"]["avoid_paths"] = markers + ["Nodejs"]
    cfg2["codex"]["node"] = None
    cmd2 = h.resolve_codex(cfg2)
    if cmd2:
        leaked = [p for p in cmd2 if "nodejs" in p.lower()]
        check("扩大排除范围后仍不泄漏该目录", not leaked, str(cmd2))

    # 这一项是硬要求：测试本身也必须用独立解释器来跑。
    check("解释器独立于被排除目录（请用 houmai.bat 或 py -3 运行）",
          rt["python_external"], rt["python"])


def case_history_window() -> None:
    """历史记录展示窗口：只影响"看什么"，绝不影响"存什么"。"""
    sys.path.insert(0, os.path.join(ROOT, "src"))
    import houmai as h

    print("\n[用例 8] 历史记录展示窗口（默认近 3 天，只裁展示不删底账）")

    now = time.time()
    D = 86400.0
    cfg = {"panel": {"history_days": 3, "show_skipped_stale": False}}

    # 关键回归：skipped_stale 的 at 是"我发现并跳过它"的时刻。若按 at 过窗口，
    # 同一轮里记下的"51 天前的中断"会被算进"近 3 天"，窗口形同虚设。
    stale_ev = {"at": now, "kind": "skipped_stale", "age_hours": 51.9 * 24}
    check("归属时间：历史跳过按其对象年龄折算",
          abs(h.event_when(stale_ev) - (now - 51.9 * D)) < 1,
          f"{h.event_when(stale_ev)}")

    normal_ev = {"at": now - 10 * D, "kind": "resumed", "verdict": "progressed"}
    check("归属时间：普通事件用写入时刻",
          abs(h.event_when(normal_ev) - (now - 10 * D)) < 1)

    progressed = {"at": now, "kind": "already_progressed", "resumed_at": now - 5 * D}
    check("归属时间：已自行继续用 resumed_at",
          abs(h.event_when(progressed) - (now - 5 * D)) < 1)

    events = [
        {"at": now - 3600, "kind": "detected"},        # 1 小时前，保留
        {"at": now - 2 * D, "kind": "resumed"},        # 2 天前，保留
        {"at": now - 2.9 * D, "kind": "retry"},        # 2.9 天前，保留
        {"at": now - 3.5 * D, "kind": "resumed"},      # 3.5 天前，裁掉
        {"at": now - 40 * D, "kind": "abandoned"},     # 40 天前，裁掉
        stale_ev,                                      # 51.9 天前，折叠
        {"at": now - 1 * D, "kind": "skipped_stale", "age_hours": 0.5},  # 折叠
    ]
    s = h.split_events(list(events), cfg, now)
    kinds = [e["kind"] for e in s["events"]]
    check("窗口内事件全保留", kinds == ["detected", "resumed", "retry"], str(kinds))
    check("窗口外事件被裁掉且计数正确", s["dropped"] == 2, str(s["dropped"]))
    check("窗口天数随配置回传", s["history_days"] == 3, str(s["history_days"]))
    check("历史跳过默认折叠", s["stale_summary"]["count"] == 2, str(s["stale_summary"]))
    check("折叠汇总给出最早年龄",
          abs(s["stale_summary"]["oldest_hours"] - 51.9 * 24) < 1, str(s["stale_summary"]))

    # 展开后：历史跳过要按自己的归属时间过窗口，不能因为 at 是"刚刚"就漏过。
    s2 = h.split_events(list(events), {"panel": {"history_days": 3,
                                                 "show_skipped_stale": True}}, now)
    k2 = [e["kind"] for e in s2["events"]]
    check("展开后只留窗口内的历史跳过",
          k2 == ["detected", "resumed", "retry", "skipped_stale"], str(k2))
    check("展开后不再有折叠汇总", s2["stale_summary"] is None, str(s2["stale_summary"]))

    # 老 config 没有 panel 段也必须能用。
    s3 = h.split_events(list(events), {}, now)
    check("缺 panel 配置时回落到 3 天", s3["history_days"] == 3 and s3["dropped"] == 2, str(s3))

    s4 = h.split_events(list(events), {"panel": {"history_days": 0}}, now)
    check("窗口填 0 表示全看", s4["dropped"] == 0, str(s4["dropped"]))

    # 结论型事件合并：同一会话的多个中断轮次各记一条，展示上应合成一行。
    dupes = [
        {"at": now - 100, "kind": "already_progressed", "root_id": "aaaa", "resumed_at": now - 200},
        {"at": now - 90, "kind": "already_progressed", "root_id": "aaaa", "resumed_at": now - 200},
        {"at": now - 80, "kind": "already_progressed", "root_id": "aaaa", "resumed_at": now - 200},
        {"at": now - 70, "kind": "already_progressed", "root_id": "bbbb", "resumed_at": now - 200},
        {"at": now - 60, "kind": "resumed", "root_id": "aaaa", "verdict": "progressed"},
    ]
    s5 = h.split_events(dupes, cfg, now)
    got = [e["kind"] for e in s5["events"]]
    check("同类结论合并成一行", got == ["already_progressed", "already_progressed", "resumed"], str(got))
    check("合并计数正确", s5["merged"] == 2, str(s5["merged"]))
    check("合并后保留重复次数",
          [e.get("repeat") for e in s5["events"] if e["kind"] == "already_progressed"] == [3, None],
          str([e.get("repeat") for e in s5["events"]]))
    check("续跑事件不被合并", len([e for e in s5["events"] if e["kind"] == "resumed"]) == 1)

    # 设计承诺：events.jsonl 永远 append-only，展示窗口不删任何一条底账。
    tmp = tempfile.mkdtemp(prefix="houmai-8-")
    st = h.State(os.path.join(tmp, "state.json"))
    for ev in events:
        item = dict(ev)
        st.record(item.pop("kind"), **item)
    raw = st.recent(400)
    check("底账保留全部原始记录（只裁展示不删底账）",
          len(raw) == len(events), f"{len(raw)}/{len(events)}")
    shutil.rmtree(tmp, ignore_errors=True)


def case_record_shape() -> None:
    """真实会话里记录类型有两个存放位置，只认一处会静默丢掉 turn_context。

    这条用例存在的理由：曾经因为只看 payload.type，769 条真实 turn_context 全部读不到，
    续跑命令里的 model/effort/cwd 全是空——而当时的 fixture 恰好用了假格式，测试全绿。
    """
    sys.path.insert(0, os.path.join(ROOT, "src"))
    import houmai as h

    print("\n[用例 9] 记录格式兼容（顶层的 turn_context + payload 里的事件）")

    # 真实格式：turn_context 在顶层
    real_tc = {"timestamp": iso(time.time()), "type": "turn_context",
               "payload": {"turn_id": "t1", "cwd": "C:/proj", "model": "gpt-5.6-sol",
                           "effort": "high", "sandbox_policy": {"type": "read-only"}}}
    # 真实格式：事件在 payload
    real_ev = {"timestamp": iso(time.time()), "type": "event_msg",
               "payload": {"type": "task_complete", "turn_id": "t1",
                           "error": {"codex_error_info": "usage_limit_exceeded"}}}
    # 旧的假格式也得继续认（向后兼容，别把老记录读废）
    legacy = {"timestamp": iso(time.time()), "type": "turn_context",
              "payload": {"type": "turn_context", "turn_id": "t2", "model": "gpt-5.5",
                          "cwd": "C:/old"}}

    check("kind_of 认顶层 turn_context", h.kind_of(real_tc) == "turn_context", h.kind_of(real_tc))
    check("kind_of 认 payload 里的 task_complete",
          h.kind_of(real_ev) == "task_complete", h.kind_of(real_ev))
    check("kind_of 兼容旧格式", h.kind_of(legacy) == "turn_context", h.kind_of(legacy))

    tmp = tempfile.mkdtemp(prefix="houmai-9-")
    home = os.path.join(tmp, "codexhome")
    root = "99999999-9999-7999-8999-999999999999"
    fp = write_rollout(home, "rollout-real-shape.jsonl", [
        meta(root, "C:/proj"),
        real_tc,
        token_count(time.time() + 3600, 100.0, time.time() + 86400, 5.0),
        real_ev,
    ])

    # 这是本用例的核心：真实格式下必须能读出上下文。
    ctx = h.last_turn_context(fp)
    check("真实格式能读出 turn_context", bool(ctx), str(ctx))
    check("读出的 model 正确", (ctx or {}).get("model") == "gpt-5.6-sol", str(ctx))
    check("读出的 cwd 正确", (ctx or {}).get("cwd") == "C:/proj", str(ctx))

    # 端到端也要带上上下文，否则续跑就是"裸跑"。
    cfg_path = make_config(tmp, home)
    state_path = os.path.join(tmp, "state.json")
    out = run(cfg_path, state_path)
    st = load_state(state_path)
    pending = st.get("pending") or {}
    check("识别到额度中断", len(pending) == 1, str(list(pending.keys())))
    if pending:
        task = next(iter(pending.values()))
        task_ctx = task.get("context") or {}
        check("在册任务带上 model（否则续跑丢模型）",
              task_ctx.get("model") == "gpt-5.6-sol", str(task_ctx))
        check("在册任务带上 cwd", task_ctx.get("cwd") == "C:/proj", str(task_ctx))

    shutil.rmtree(tmp, ignore_errors=True)


def case_panel_html() -> None:
    """面板前端的两条易回退约定。

    它们都不改变后端行为，所以后端测试全绿也拦不住回退，只能直接对页面源码设卡。
    """
    sys.path.insert(0, os.path.join(ROOT, "src"))
    import houmai as h

    print("\n[用例 10] 面板前端约定（滚动条样式 + 日志不被拽回底部）")

    page = h.load_panel_html()

    check("滚动条：收细并压暗，不再用系统默认",
          "::-webkit-scrollbar" in page and "scrollbar-width:thin" in page)
    check("滚动条：Firefox 兜底不得覆盖精细样式",
          "@supports not selector(::-webkit-scrollbar)" in page)
    check("滚动条：滚动日志不带动整页", "overscroll-behavior:contain" in page)

    # 日志直接落在卡片上。曾套过第二层框（背景 + 边框 + 圆角 + 内边距），
    # 于是卡片里还有一个"框"，正文被挤在框里——主人要求去掉。
    log_rule = page[page.index("pre.log {"):]
    log_rule = log_rule[:log_rule.index("}")]
    check("日志：不套第二层框（无背景/边框/圆角/内边距）",
          all(k not in log_rule for k in ("background", "border", "padding")),
          log_rule.replace("\n", " ")[:120])
    check("日志：限高与滚动保留（最多 200 行，去掉会撑爆面板且失去贴底跟随）",
          "max-height:340px" in log_rule and "overflow:auto" in log_rule)

    check("日志：绑定了贴底跟随的滚动监听", "bindLogScroll" in page)
    check("日志：不再无条件滚到底（往上翻会被拽回）",
          "box.scrollTop = box.scrollHeight;" not in page)
    check("日志：仍在贴底时才跟随",
          "box.scrollTop = keepTop === null ? box.scrollHeight : keepTop;" in page)

    check("事件区：展示窗口写在标题上", 'id="evmeta"' in page)
    check("事件区：历史噪声折叠有提示位", "show_skipped_stale" in page)


def case_sandbox_fidelity() -> None:
    """沙箱策略要复刻到 `-s <type>` 之外，否则可能把原轮次禁用的临时目录重新放开。"""
    sys.path.insert(0, os.path.join(ROOT, "src"))
    import houmai as h

    print("\n[用例 11] 沙箱策略完整复刻（不靠猜，键名实测有效）")

    # toml_string 的两条规则：正常路径走单引号字面量、含单引号退回双引号 + 转义
    # 样本路径刻意用中性占位（仓库会被公开，不放使用者的真实目录）。
    check("toml_string：正常路径用单引号字面量",
          h.toml_string(r"C:\work\sample") == r"'C:\work\sample'")
    check("toml_string：含单引号时退回双引号（包在双引号里就无需转义）",
          h.toml_string(r"o'reilly") == '"o\'reilly"')
    check("toml_string：含反斜杠时不会被吃",
          r"'C:\foo\bar'" in h.toml_string(r"C:\foo\bar"))

    # 非 workspace-write 不带附加项
    check("read-only 不带任何 -c 覆盖",
          h.sandbox_overrides({"sandbox_policy": {"type": "read-only",
                                                  "writable_roots": ["/x"], "network_access": True}}) == [])

    # workspace-write + writable_roots：应追加为 TOML 数组
    ov = h.sandbox_overrides({"sandbox_policy": {
        "type": "workspace-write",
        "writable_roots": [r"C:\work\sample", r"C:\work\sample\.cache"]}})
    check("writable_roots 序列化为 TOML 数组",
          any("sandbox_workspace_write.writable_roots=" in a and r"work\\sample" not in a
              and "['C:" in a for a in ov), str(ov))

    # workspace-write + 全 4 项
    ov = h.sandbox_overrides({"sandbox_policy": {
        "type": "workspace-write",
        "writable_roots": [r"D:\repo"],
        "network_access": True,
        "exclude_tmpdir_env_var": True,
        "exclude_slash_tmp": False}})
    joined = " ".join(ov)
    check("writable_roots 出现在覆盖里", "writable_roots=" in joined)
    check("network_access 原值复刻", "network_access=true" in joined)
    check("exclude_tmpdir_env_var 原值复刻", "exclude_tmpdir_env_var=true" in joined)
    check("exclude_slash_tmp 原值复刻", "exclude_slash_tmp=false" in joined)

    # 拿不准时按"宁窄不宽"——exclude_* 接收任何 truthy 视为 true
    ov = h.sandbox_overrides({"sandbox_policy": {
        "type": "workspace-write", "exclude_tmpdir_env_var": "是"}})
    check("exclude_* 拿不准时按 true（不放开临时目录）",
          "exclude_tmpdir_env_var=true" in " ".join(ov), str(ov))

    # network_access 拿不准就跳过（默认 false 更窄，更安全）
    ov = h.sandbox_overrides({"sandbox_policy": {
        "type": "workspace-write", "network_access": "是"}})
    check("network_access 拿不准时跳过（默认 false）",
          "network_access" not in " ".join(ov), str(ov))

    # 端到端：含完整沙箱策略的上下文，续跑命令应带上 -s 与所有覆盖
    root = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    fp = "/tmp/fake.jsonl"
    argv = h.build_resume_argv({"retry": {"prompt": "continue"}}, ["codex"],
                               {"root_id": root, "session_id": root, "file": fp,
                                "files": [fp], "turn_id": "t1", "cwd": "/w",
                                "context": {"cwd": "/w", "model": "gpt-5",
                                            "sandbox_policy": {
                                                "type": "workspace-write",
                                                "writable_roots": [r"D:\repo"],
                                                "network_access": False,
                                                "exclude_tmpdir_env_var": True}}})
    check("build_resume_argv 保留 -s workspace-write", "-s" in argv and "workspace-write" in argv)
    check("build_resume_argv 保留 writable_roots 覆盖",
          any("sandbox_workspace_write.writable_roots=" in a for a in argv))
    check("build_resume_argv 保留 exclude_tmpdir_env_var 覆盖",
          any("exclude_tmpdir_env_var=true" in a for a in argv))


def case_review_unconfirmed() -> None:
    """待确认任务的三种走向：已确认/转人工/继续等。"""
    sys.path.insert(0, os.path.join(ROOT, "src"))
    import houmai as h

    print("\n[用例 12] 待确认复查（不重发、不撒谎）")

    tmp = tempfile.mkdtemp(prefix="houmai-12-")
    home = os.path.join(tmp, "codexhome")
    root = "cccccccc-1234-4abc-8999-aaaaaaaaaaaa"
    day = os.path.join(home, "sessions", "2026", "09", "13")
    os.makedirs(day, exist_ok=True)
    fp = os.path.join(day, "rollout-rev.jsonl")
    now = time.time()

    # 真实格式：turn_context 在顶层 type，task_complete 在 payload.type
    recs = [
        meta(root, "C:/repo"),
        {"timestamp": iso(now - 600), "type": "turn_context",
         "payload": {"turn_id": "t1", "cwd": "C:/repo", "model": "gpt-5"}},
        {"timestamp": iso(now - 590), "type": "event_msg",
         "payload": {"type": "token_count", "info": {"rate_limits": {
             "primary": {"used_percent": 100.0, "window_minutes": 300, "resets_at": int(now - 500)}}}}},
        {"timestamp": iso(now - 580), "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": "t1",
                     "error": {"codex_error_info": "usage_limit_exceeded"}}},
    ]
    with open(fp, "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # 场景 1：已发出 + 新轮次出现 → confirmed
    def append_new_turn(turn_id: str) -> None:
        with open(fp, "a", encoding="utf-8") as f:
            f.write(json.dumps({"timestamp": iso(now - 30), "type": "turn_context",
                                "payload": {"turn_id": turn_id, "cwd": "C:/repo",
                                            "model": "gpt-5"}}, ensure_ascii=False) + "\n")
            f.write(json.dumps({"timestamp": iso(now - 25), "type": "event_msg",
                                "payload": {"type": "task_complete", "turn_id": turn_id,
                                            "last_agent_message": "ok"}}, ensure_ascii=False) + "\n")

    append_new_turn("t2")
    task1 = {"status": "unconfirmed", "root_id": root, "last_attempt_at": now - 60,
             "confirm_deadline_at": now + 600}
    check("已确认：新轮次出现 → confirmed",
          h.review_unconfirmed(task1, [fp], now) == "confirmed")

    # 场景 2：已发出 + 没新轮次 + 未到截止 → pending
    # 重新建一个文件，不追加新轮次
    fp2 = os.path.join(day, "rollout-rev2.jsonl")
    with open(fp2, "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    task2 = {"status": "unconfirmed", "root_id": root, "last_attempt_at": now - 60,
             "confirm_deadline_at": now + 600}
    check("继续等：没新轮次且未到期 → pending",
          h.review_unconfirmed(task2, [fp2], now) == "pending")

    # 场景 3：已发出 + 没新轮次 + 已到截止 → expired（转人工）
    task3 = {"status": "unconfirmed", "root_id": root, "last_attempt_at": now - 60,
             "confirm_deadline_at": now - 1}
    check("转人工：已到截止仍无新轮次 → expired",
          h.review_unconfirmed(task3, [fp2], now) == "expired")

    shutil.rmtree(tmp, ignore_errors=True)


def case_panel_stop() -> None:
    """面板要有关得掉的入口：UI 按钮、stop 命令、pid 文件、禁止重复绑端口。"""
    sys.path.insert(0, os.path.join(ROOT, "src"))
    import houmai as h

    print("\n[用例 13] 面板可停止（按钮 / stop 命令 / pid / 端口独占）")

    page = h.load_panel_html()

    # 体检卡片约定（对 collect_health 源码设卡，与面板前端同一手法——
    # 它们不影响后端行为，后端全绿拦不住回退）。
    src = open(os.path.join(ROOT, "src", "houmai.py"), encoding="utf-8").read()
    health_body = src[src.index("def collect_health"):]
    check("体检：运行时合并为一张卡（key=runtime，不再有 python/node 两张）",
          'add("runtime", "运行时"' in health_body
          and 'add("python"' not in health_body
          and 'add("node"' not in health_body)
    check("体检：运行时常态不显示卡片（独立运行没信息量，异常才出现）",
          "独立运行" not in health_body
          and health_body.count('add("runtime"') == 2)
    check("体检：异常态才给具体问题和修法（fail 分支含路径与修法指引）",
          "双击 houmai.cmd 选 3 重新注册" in health_body
          and "在 config.json 里指定 codex.node" in health_body)
    check("体检：修法不指向用户碰不到的文件（面板文案不提 houmai.bat）",
          "houmai.bat" not in health_body)
    check("体检：网络出口不再进检查列表（连通性判定归 netcheck）",
          'add("net"' not in health_body)
    check("体检：node unknown 分支保留（codex 存在但 node 无法解析时仍要说话）",
          "无法确认它用哪个" in health_body)
    check("体检：心跳超时给出修法（选 3 修复 + 自动恢复预期）",
          "选 3 修复" in health_body and "自动转绿" in health_body)
    check("体检：'值守日志'行不再报路径（与面板路径条同步删；曾引用已删的"
          "log_path 载荷字段，health 一跑就 KeyError）",
          "h['log_path']" not in src and 'h["log_path"]' not in src)
    check("面板：徽标按问题类型命名，不再笼统说有待处理项",
          "值守疑似异常" in src and "值守疑似没在跑" not in src
          and '"verdict": verdict' in health_body
          and "d.verdict" in page)
    check("面板可见文本不出现被看护程序的产品名（卡片、日志警告、页面源码）",
          ("来自 " + _WATCHED) not in src and (_WATCHED + " 一旦卸载") not in src
          and _WATCHED not in h.load_panel_html())

    # 事件表收敛：底账不删，展示至多最新 10 条（新在上），超出时注明
    check("事件表：至多最新 10 条（新在上）",
          "PANEL_EVENT_LIMIT = 10" in src
          and "list(reversed(events[-PANEL_EVENT_LIMIT:]))" in health_body)
    check("事件表：有更早记录被收起时标题注明，但不说实现规则（10 条上限不上页面）",
          "未列出" in page and "events_hidden" in page
          and "仅显示最新 10 条" not in page)
    check("续跑失败可追查：识别桌面端占用锁 + 日志带 codex 报错尾行",
          "thread_locked" in src and "already has an active writer" in src
          and "hint[:200]" in src)

    # 可移植性断言共用两个小工具：
    #   ① 文档里那个占位示例 `C:\path\to\python.exe` 不是硬编码，先摘掉再判；
    #   ② 判据是"文件里还剩下任何盘符绝对路径吗" —— 剩下就说明它绑定了某台机器，
    #      而不是"用没用 %~dp0"这一种写法。
    _doc_ph = "C:" + "\\path\\to\\python.exe"
    def _no_baked(text: str) -> bool:
        return re.search(r"[A-Za-z]:\\", text.replace(_doc_ph, "")) is None

    # 后台化：无窗口启动入口必须存在，且仍走 houmai.bat 这个唯一出口
    for name in ("panel-hidden.vbs", "run-hidden.vbs"):
        vbs = os.path.join(ROOT, "scripts", name)
        data = open(vbs, "rb").read() if os.path.exists(vbs) else b""
        text = data.decode("ascii", errors="replace")
        check(f"后台启动：{name} 存在且隐藏窗口调用包装脚本",
              "houmai.bat" in text and ", 0," in text, vbs)
        # Windows 脚本宿主按 ANSI 解析 .vbs：UTF-8 中文注释会被读乱，
        # 曾导致「缺少对象 'sh'」800A01A8——所以 vbs 必须纯 ASCII。
        check(f"后台启动：{name} 纯 ASCII（脚本宿主按 ANSI 解析，中文注释会炸）",
              bool(data) and all(b < 128 for b in data))
        # 可移植性：启动器必须从自身位置算路径。写死盘符 = 只能在写它那台机器上跑，
        # 而"克隆下来就能用"是本项目的硬要求（见 README 的安装一节）。
        check(f"后台启动：{name} 从自身位置推导路径（不写死盘符）",
              "WScript.ScriptFullName" in text and _no_baked(text), vbs)

    # 唯一入口收敛在仓库内，并由脚本自身位置推导 —— 换目录、换用户名、换机器都
    # 直接可用，不需要"改几行源码再跑"。
    bat_path = os.path.join(ROOT, "scripts", "houmai.bat")
    bat = open(bat_path, encoding="ascii", errors="replace").read() \
        if os.path.exists(bat_path) else ""
    check("入口：scripts\\houmai.bat 随仓库分发（Python 侧唯一入口）", bool(bat), bat_path)
    check("入口：houmai.bat 用 %~dp0 自定位，且不含盘符绝对路径",
          "%~dp0" in bat and _no_baked(bat))
    check("入口：houmai.bat 仍坚持解释器独立性（拒绝随其它应用分发的运行时）",
          "HOUMAI_PYTHON" in bat and "independent" in bat.lower())

    menu_path = os.path.join(ROOT, "houmai.cmd")
    menu_txt = open(menu_path, encoding="ascii", errors="replace").read()
    check("入口：菜单调用仓库内入口（不再指向使用者 PATH 下的外部脚本）",
          'call "%~dp0scripts\\houmai.bat" stop' in menu_txt and _no_baked(menu_txt))

    it = open(os.path.join(ROOT, "scripts", "install-task.cmd"),
              encoding="ascii", errors="replace").read()
    check("入口：注册脚本指向仓库内入口（同样自定位）",
          'set "WRAPPER=%~dp0houmai.bat"' in it and _no_baked(it))
    # 缺件时**不许指错方向**：这个脚本自己就在 scripts\ 里，所以"找不到 houmai.bat"
    # 的含义是"这份拷贝不完整"，而不是"项目没放好"。旧措辞让使用者去确认一件他
    # 已经做对了的事，会在错误的方向上耗掉很久 —— 这比报错本身更伤。
    check("注册脚本：缺件文案说清缺的是哪个文件（不许说'确认项目已就位'）",
          "launcher not found" in it
          and "make sure the houmai project is in place" not in it)
    check("install-task.cmd：注册的是隐藏入口（run-hidden.vbs）",
          "run-hidden.vbs" in it and "wscript.exe" in it)
    # 窗口不该干等：**全绿**才 5 秒自动关；出问题必须停住等确认 —— 注册报告
    # 只在屏幕上出现一次，跟着定时器一起消失就再也看不到了。
    check("注册脚本：全绿 5 秒自动关、出问题停住等按键，菜单调用时不重复等待",
          "Closing in 5 seconds" in it and 'pause >nul' in it
          and "Press any key to close this window" in it
          and 'if /i "%~1"=="quiet"' in it)
    # 被 call 的一方不得无条件 exit /b 0 —— 否则调用方无从判断，失败也会定时收场。
    # 四处失败点：wrapper 缺失 / 启动器缺失 / register-task.ps1 非零 / PROTOBAD。
    check("注册脚本：四处失败点都置 RESULT，quiet 按 RESULT 退出（不再无条件成功）",
          it.count('set "RESULT=1"') == 4
          and 'if /i "%~1"=="quiet" exit /b %RESULT%' in it
          and 'if not "%RESULT%"=="0"' in it)
    ut = open(os.path.join(ROOT, "scripts", "uninstall-task.cmd"),
              encoding="ascii", errors="replace").read()
    check("install-task.cmd：同时注册 houmai:// 协议（面板页的启动按钮靠它）",
          "HKCU\\Software\\Classes\\houmai" in it and "panel-hidden.vbs" in it)
    # 协议注册曾静默失败：键没了，面板"启动面板"按钮点了毫无反应——
    # 所以注册完必须回读注册表复核，读不到就报错，不能只看 reg add 的返回码。
    check("注册脚本：协议注册后回读校验（按钮失效属静默失败，必须报警）",
          "PROTOBAD" in it and "findstr" in it
          and "was not registered correctly" in it)
    # 回读必须看三处，缺一处就漏一类故障：
    #   ① URL Protocol 标记  ② (默认) 显示名  ③ 处理器指向「本副本」的整条路径
    # ③ 原来只核文件名 panel-hidden.vbs——项目搬家后，指向旧路径的失效值照样
    #    算通过（用真实 .cmd 实测：stale 路径会报 OK），所以改成核整条路径。
    check("注册脚本：协议回读核三处（标记 + 显示名 + 整条路径，不只核文件名）",
          'reg query "HKCU\\Software\\Classes\\houmai" /v "URL Protocol"' in it
          and 'reg query "HKCU\\Software\\Classes\\houmai" /ve' in it
          and '/c:"URL:houmai panel launcher"' in it
          and '/c:"%PANELVBS%"' in it)
    # 路径为空时 findstr 变成「匹配任意内容」→ 假阳性，所以先确认启动器存在。
    check("注册脚本：启动器缺失时直接判协议不可用（空路径会让 findstr 全匹配）",
          'if not exist "%PANELVBS%" set "PROTOBAD=1"' in it)
    # reg add 的返回码是故意丢的（三条都 >nul 2>&1），回读才是权威——
    # 写清楚，免得后人「顺手」补个 errorlevel 检查又以为那一步在把关。
    check("注册脚本：写明 reg add 不看返回码、以回读为准（防后人顺手改回去）",
          "exit codes are deliberately ignored" in it
          and "not reg add's status" in it)
    # 任务没建好时 errorlevel>=1 → goto :end → 协议段整段被跳过，
    # 所以 [FAIL] 得说清「任务本身可能仍然存在」且「协议没注册」。
    check("注册脚本：任务失败时 [FAIL] 说明任务现状，且点明协议没注册",
          "was not registered cleanly" in it
          and "the task does exist but would stop on battery" in it
          and "protocol was NOT registered either" in it)
    check("uninstall-task.cmd：卸载时清掉协议注册",
          "Software\\Classes\\houmai" in ut)

    # 入口收敛：根目录只留一个 houmai.cmd 菜单，其余入口收进 scripts\
    check("入口收敛：根目录有菜单入口 houmai.cmd",
          os.path.exists(os.path.join(ROOT, "houmai.cmd")))
    menu = open(os.path.join(ROOT, "houmai.cmd"),
                encoding="ascii", errors="replace").read()
    check("菜单：启动/停止相邻（1 启动、2 停止），注册排 3 且完成后顺带启动面板",
          menu.index("[1] Start panel") < menu.index("[2] Stop panel") < menu.index("[3] Register")
          and "install-task.cmd" in menu
          and menu.count("panel-hidden.vbs") == 2)
    check("菜单：退出项说明白自己只是关窗口（不做任何操作）",
          "do nothing" in menu)
    # 三个动作各一处 5 秒自动关；`pause` 只允许出现在**失败分支**里（选 3 注册
    # 失败、选 2 停止未确认）。"不再 pause 干等"指的是常态路径不许等按键。
    check("菜单：三个动作各一处 5 秒自动关；pause 只出现在失败分支",
          menu.count("Closing in 5 seconds") == 3 and menu.count("timeout /t 5") == 3
          and menu.count("pause >nul") == 2)
    # 选 3 按注册脚本的退出码决定收尾：成功照旧自动关，出问题停住。
    # 退出码要在 call 之后**立刻**捕获 —— 中间的 echo / start 会把它冲掉。
    # （加了 tee 之后，退出码先落 RC 再落 REGFAIL，两跳都得紧贴。）
    check("菜单：选 3 立刻捕获注册退出码（中间的命令会冲掉它）",
          'install-task.cmd quiet > "%TEEF%" 2>&1\nset "RC=%ERRORLEVEL%"' in menu
          and 'set "REGFAIL=%RC%"' in menu)
    check("菜单：选 3 出问题停住等按键，成功仍 5 秒自动关",
          "Registration reported a problem" in menu
          and 'if not "%REGFAIL%"=="0"' in menu)
    check("菜单：调注册脚本时带 quiet（倒计时只做一次，不等两回）",
          "install-task.cmd quiet" in menu)

    # 输出不能只落在屏幕上：屏幕是一闪而过的，日志才是底账。三个动作都把
    # 命令输出 tee 进临时文件，收尾时由 :flush 同时打到屏幕**和**
    # state\install.log —— 5 秒定时关窗后再也回不去的那些字，日志里还在。
    check("菜单：详情日志落在 state\\install.log（屏幕一闪而过，日志不会）",
          menu.count('set "DETAIL=%~dp0state\\install.log"') == 1)
    check("菜单：三个动作各开一个临时文件接命令输出（tee 的取水口）",
          menu.count('set "TEEF=%TEMP%\\houmai-tee-%RANDOM%.log"') == 3)
    # 选 1 走的是 start：它立刻返回，重定向会把临时文件留给面板进程，
    # 所以只给一个空文件，让这段在日志里也有个段头。
    check("菜单：三个动作都把自己的输出 tee 进临时文件",
          'type nul > "%TEEF%"' in menu
          and 'install-task.cmd quiet > "%TEEF%" 2>&1' in menu
          and 'stop > "%TEEF%" 2>&1' in menu)

    # 按标签切出各段，逐段核「先捕获退出码，再调 :flush」——只数出现次数
    # 会漏掉顺序写反的情况（type / del 都会冲掉 %ERRORLEVEL%）。
    def _branch(text: str, label: str, after: tuple[str, ...]) -> str:
        a = text.index("\n:" + label + "\n") + 1
        b = len(text)
        for s in after:
            i = text.find("\n:" + s + "\n", a)
            if i != -1:
                b = min(b, i)
        return text[a:b]

    branch_order = []
    for lab, after in (("bg", ("reg", "stop", "flush")),
                       ("reg", ("stop", "flush")),
                       ("stop", ("flush",))):
        body = _branch(menu, lab, after)
        branch_order.append('set "RC=%ERRORLEVEL%"' in body
                            and "call :flush" in body
                            and body.index('set "RC=%ERRORLEVEL%"') < body.index("call :flush"))
    check("菜单：三个动作都在 call :flush 之前捕获退出码（type/del 会冲掉它）",
          all(branch_order) and menu.count('set "RC=%ERRORLEVEL%"') == 3
          # 只数真正的调用点：:flush 自己的注释里也写着 "call :flush"，
          # 裸数 "call :flush" 会把注释算进去（4 次），断言就假失败。
          and menu.count('call :flush "option') == 3)

    fl = _branch(menu, "flush", ())
    # 日志只许追加：一旦有人把哪行写成单箭头 >，每跑一次就把历史覆盖掉。
    check("菜单：:flush 的段头带时间戳，且只追加不覆盖（单箭头会毁掉历史）",
          'echo %DATE% %TIME%   [menu] %~1>> "%DETAIL%"' in fl
          and fl.count('"%DETAIL%"') == fl.count('>> "%DETAIL%"')
          and fl.count('>> "%DETAIL%"') >= 4)
    check("菜单：:flush 把捕获内容同时打到屏幕和日志，用完删掉临时文件",
          'type "%TEEF%">> "%DETAIL%"' in fl and 'del "%TEEF%"' in fl)
    check("菜单：:flush 原样传回退出码，非零时在日志里留一条（tee 不许吞成败）",
          "exit /b %RC%" in fl and 'if not "%RC%"=="0"' in fl)
    check("菜单：三个动作都告诉主人日志在哪（否则不知道有日志可看）",
          menu.count("Full log: %DETAIL%") == 3)

    # 收尾 D1：日志不能无限长。轮转必须在写新段之前，且**先判断文件存在**——
    # %%~zF 对不存在的文件展开为空，"if  GTR 524288" 会直接语法报错、把整套
    # 菜单打断（已用真实 .cmd 验过三种情形：614 KB 轮转 / 100 B 不动 / 不存在不报错）。
    check("菜单：install.log 超 512 KB 先轮转成 .1（与 run.log 同一口径）",
          'if exist "%DETAIL%" for %%F in ("%DETAIL%") do if %%~zF GTR 524288' in menu
          and 'move /y "%DETAIL%" "%DETAIL%.1"' in menu)

    # 收尾 D2a：选 2 的三种结果不是一回事，不许都报"已停止"；没能确认时要停住。
    check("菜单：选 2 按退出码分三种说法（已停 / 本来没在跑 / 未确认）",
          'if "%RC%"=="0" echo   Panel stopped. The watcher is not affected.' in menu
          and 'if "%RC%"=="1" echo   No panel was running' in menu
          and "Stop did NOT confirm" in menu)
    check("菜单：选 2 未确认时停住等按键（不许定时收场）",
          'if not "%RC%"=="0" if not "%RC%"=="1" (' in menu
          and menu.count("Press any key to close this window") == 2)

    # 收尾 D3：`start` 是"已投递"不是"已起来"，文案不许替它担保。
    bg_body = _branch(menu, "bg", ("reg", "stop", "flush"))
    check("菜单：选 1 只说'正在后台启动'，不承诺已启动成功",
          "Starting the panel in the background" in bg_body
          and "Panel started in background" not in bg_body)

    # 选 3 也走同一口径；而且注册失败要**先说**、再说启动——旧顺序是"先说
    # 面板已启动、再承认注册失败"，读起来像报喜。行为不变（失败照样开面板）。
    reg_body = _branch(menu, "reg", ("stop", "flush"))
    check("菜单：选 3 不承诺已启动，且失败提示在 start 之前",
          "Starting the panel in the background" in reg_body
          and "Panel started in background" not in reg_body
          and reg_body.index("Registration reported a problem")
          < reg_body.index('start "" wscript.exe'))
    # 这份名单管的是"根目录不散落入辅助入口"（辅助入口都该收在 scripts\ 下），
    # 不是"这些文件必须存在"。panel.cmd 已整份删除，名字留在这里会误导读者
    # 以为它还是个入口。
    leftovers = [f for f in ("panel-hidden.vbs", "run-hidden.vbs",
                             "install-task.cmd", "uninstall-task.cmd")
                 if os.path.exists(os.path.join(ROOT, f))]
    check("入口收敛：辅助入口都收进 scripts\\，根目录不散落", not leftovers, str(leftovers))

    # 前台窗口版入口 `scripts\panel.cmd` 已删除（2026-09-19，主人判"删掉"）。
    # 它当时没有任何功能性调用方（菜单选 1 走无窗口的 panel-hidden.vbs），与
    # `houmai web` 完全重复，且不可发现——唯一提到它的地方是 panel-hidden.vbs
    # 的一句注释、两份文档和下面这条用例，等于靠自证存活。
    # 这里把决定钉住：文件不许悄悄回来。否则"入口唯一"这个前提会在无人察觉时
    # 失效，而 README / DESIGN 都已不再描述它，回来也没人知道该怎么用它。
    check("入口收敛：scripts\\panel.cmd 已删除（不再有第二个前台入口）",
          not os.path.exists(os.path.join(ROOT, "scripts", "panel.cmd")))

    # 随仓库发布的文档与默认配置不出现被看护程序的产品名（本机真实排除由
    # config.json 显式承载，代码默认值与示例配置保持中性）。
    # 注意：DESIGN／ROADMAP／AGENTS 三份工作笔记，以及 docs\ 下的桌面 IPC 通道
    # 记录，都是**作者私有**的，已进 .gitignore 不随仓库发布（它们会写到具体机器
    # 路径、任务标识与桌面私有协议的实现细节）。私有不等于可以带产品名，所以本机
    # 存在时照样一起扫；别人克隆下来没有这几份文件，跳过即可——但 **README 必须
    # 在**，否则这条检查就变成了空转。
    _priv = ("DESIGN.md", "ROADMAP.md", "AGENTS.md",
             os.path.join("docs", "DESIGN-ipc-direct-resume.md"))
    doc_hits = []
    to_scan = ["README.md"] + [m for m in _priv if os.path.exists(os.path.join(ROOT, m))]
    for md in to_scan:
        t = open(os.path.join(ROOT, md), encoding="utf-8", errors="replace").read()
        if _WATCHED in t or _WATCHED_DIR in t:
            doc_hits.append(md)
    check("文档不出现产品名（README 必扫；作者私有笔记存在时一并扫）",
          not doc_hits and "README.md" in to_scan, str(doc_hits))
    check("代码默认值与示例配置不含产品名",
          _WATCHED_DIR not in src and _WATCHED_DIR not in open(
              os.path.join(ROOT, "config.example.json"), encoding="utf-8").read())

    # 开源必备件：LICENSE 要随仓库发布，而且是真 MIT 全文而非空壳——署名写在
    # copyright 行里。年份刻意不钉死（正则用 \d{4}），否则日后更新版权年份就红。
    _lic_p = os.path.join(ROOT, "LICENSE")
    _lic = open(_lic_p, encoding="utf-8").read() if os.path.exists(_lic_p) else ""
    check("LICENSE：MIT 全文，署名为 warnings_cn",
          _lic.startswith("MIT License")
          and re.search(r"Copyright \(c\) \d{4} warnings_cn", _lic) is not None
          and "Permission is hereby granted, free of charge" in _lic
          and "WITHOUT WARRANTY OF ANY KIND" in _lic,
          f"{len(_lic)} 字节" if _lic else "LICENSE 不存在")
    # README 是唯一的对外说明；LICENSE 入口必须找得到。
    # （早期版本要求在此列出所参考的外部实现作为署名；主人 2026-09-19 决定去掉
    # 参考与致谢章节，故此处不再校验，也不强制署名章节存在。）
    _rm = open(os.path.join(ROOT, "README.md"), encoding="utf-8", errors="replace").read()
    check("README：指向 LICENSE", "[LICENSE](LICENSE)" in _rm)

    # 仓库卫生：这个仓库会被公开，身份信息一旦进了历史就很难收回（fork、缓存、
    # 镜像都会留着）。这里只做**机械可判定**的那部分——主目录绝对路径：
    # `X:\Users\<具体名字>` 一律算泄漏，占位符（`<user>`、`%USERPROFILE%`）不算。
    # 判据刻意不依赖"当前是谁"，所以别人克隆下去跑这条同样有效。
    # 文件清单优先用 `git ls-files`（那正是"会被公开"的那个集合）；没有 git 就
    # 退化为走目录 + 跳过忽略名单，并把"到底扫了几个文件"一起纳入断言 —— 否则
    # 一个空清单会让这条检查**空转通过**，正是本项目最忌讳的那种假绿。
    _home_rx = re.compile(r"[A-Za-z]:\\" + "Users" + r"\\[^<>%$*\\]")
    _scan: list[str] = []
    # 注意：非 git 目录下 `git ls-files` **不抛异常**，只是返回非零码 + 空输出。
    # 只 catch OSError 会让文件清单变成空的，于是这条检查"全绿地空转"。
    try:
        _r = subprocess.run(["git", "-C", ROOT, "ls-files"], capture_output=True,
                            text=True, encoding="utf-8")
        if _r.returncode == 0 and _r.stdout.split():
            _scan = [os.path.join(ROOT, p) for p in _r.stdout.split()]
    except OSError:
        pass
    if not _scan:
        # 不是 git 检出（例如使用者只是把文件夹拷过去）→ 走目录 + 跳过忽略名单。
        # 这份名单要与忽略规则对齐（作者私有的四份笔记在 .git/info/exclude——
        # 本机私有、不随仓库发布），否则"本机带笔记拷贝"这一种情况下会把
        # 私有笔记里的真实路径算成发布内容，报出假红。
        _skipd = {".git", "state", "__pycache__"}
        _skipf = {"config.json", "DESIGN.md", "ROADMAP.md", "AGENTS.md",
                  "DESIGN-ipc-direct-resume.md"}
        for _d, _subs, _fs in os.walk(ROOT):
            _subs[:] = [x for x in _subs if x not in _skipd]
            for _f in _fs:
                if _f in _skipf or _f.endswith(".pyc"):
                    continue
                _scan.append(os.path.join(_d, _f))
    _leaks = []
    for _p in _scan:
        try:
            _body = open(_p, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for _n, _line in enumerate(_body.splitlines(), 1):
            if _home_rx.search(_line):
                _leaks.append(f"{os.path.relpath(_p, ROOT)}:{_n}")
    check("仓库卫生：版本库内不出现使用者的主目录绝对路径（占位符除外）",
          len(_scan) >= 10 and not _leaks,
          f"扫了 {len(_scan)} 个文件；命中 {_leaks[:5]}")

    check("面板：头部有停止按钮", 'id="stopbtn"' in page)
    check("面板：标签页有图标（内联 SVG favicon，不落额外文件）",
          'rel="icon"' in page and "data:image/svg+xml" in page)
    check("面板：左上角 logo 与标签页图标同款（同一脉搏波形）",
          '<div class="logo"><svg viewBox="0 0 64 64"' in page
          and 'points="10,36 22,36 27,20 33,48 38,28 42,36 54,36"' in page)
    check("面板：停止确认用主题内自绘弹窗（不再用系统原生 confirm）",
          "/api/stop" in page and "X-Houmai-Stop" in page
          and 'id="modal"' in page and "confirm(" not in page
          and "modal-mask" in page)
    check("面板：停止后不再轮询刷新", "PANEL_STOPPED" in page)
    check("面板：内容指纹相同就跳过重绘（任务卡不闪，倒计时本地走）",
          "LAST_REV" in page and "d.rev === LAST_REV" in page
          and '"rev"' in health_body)
    check("面板：停止后按钮变成高亮的「启动面板」，走 houmai:// 拉起并自动恢复",
          "启动面板" in page and "houmai://panel" in page
          and "PANEL_STOPPED = false" in page
          and 'stopbtn.classList.add("start")' in page
          and ".stopbtn.start" in page)
    check("面板：提示语说清值守不受影响", "值守不受影响" in page)

    check("服务端：/api/stop 有自定义头防护（防跨站代按）",
          'path == "/api/stop" and self.headers.get("X-Houmai-Stop") == "1"' in open(
              os.path.join(ROOT, "src", "houmai.py"), encoding="utf-8").read())
    check("服务端：Windows 禁止重复绑端口（第二实例必须报错而非悄悄并存）",
          h.PanelServer.allow_reuse_address is False)

    # pid 文件三件套：写入、读出、清除
    tmp = tempfile.mkdtemp(prefix="houmai-13-")
    cfg = {"state_dir": tmp}
    try:
        h.write_panel_pid(cfg, 8787)
        info = h.read_panel_pid(cfg)
        check("pid：写入后能读出端口", bool(info) and info.get("port") == 8787, str(info))
        check("pid：记录的是本进程号", bool(info) and info.get("pid") == os.getpid(), str(info))
        h.clear_panel_pid(cfg)
        check("pid：清除后读不到", h.read_panel_pid(cfg) is None)
        h.clear_panel_pid(cfg)  # 再清一次不应报错
        check("pid：重复清除幂等", True)

        # 陈旧 pid 清扫：端口没人听 → 清；有人听 → 保留；没有文件 → 无事发生
        h.write_panel_pid(cfg, 8787)  # 8787 此刻未必有人听，换个确定空闲的端口更稳
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]
        s.close()
        h.write_panel_pid(cfg, free_port)
        check("清扫：端口没人听 → 清掉残骸",
              h.clean_stale_panel_pid(cfg) is True and h.read_panel_pid(cfg) is None)
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        live_port = s.getsockname()[1]
        h.write_panel_pid(cfg, live_port)
        check("清扫：端口有人听 → 保留",
              h.clean_stale_panel_pid(cfg) is False and h.read_panel_pid(cfg) is not None)
        s.close()
        h.clear_panel_pid(cfg)
        check("清扫：没有 pid 文件 → 无事发生", h.clean_stale_panel_pid(cfg) is False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 停止的退出码是给菜单用的契约：三者混成一个非零，调用方就只能二选一地
    # 撒谎——把"没做成"说成成功，或把"无事可做"报成故障。
    stop_body = src[src.index("def cmd_stop"):src.index("def cmd_status")]
    check("停止：退出码区分'没做成'与'没事可做'（0 / 1 / 2 三种含义）",
          "return 2" in stop_body and "return 1" in stop_body
          and "本来就没有面板在运行" in stop_body)
    # taskkill 非零 ≠ 进程已不存在：也可能是杀不掉（权限/占用）。从前一律当
    # "已不存在"并报成功——把"没停成"说成"已结束"，正是最忌讳的静默失效。
    check("停止：taskkill 失败后回读 PID，不把'杀不掉'当'已不存在'",
          "alive = pid_alive(pid)" in stop_body and "alive is False" in stop_body
          and "alive is True" in stop_body and "无法确认" in stop_body)
    check("停止：pid_alive 用 OpenProcess 探测（不解析 tasklist 的本地化文案）",
          "def pid_alive" in src and "OpenProcess" in src
          and "ERROR_INVALID_PARAMETER" in src)


def case_ipc_channel() -> None:
    """桌面直跑通道：配置开关、帧协议、回落逻辑与面板文案。

    所有测试都指向一个必定不存在的假管道，绝不触碰真实桌面端点。
    """
    sys.path.insert(0, os.path.join(ROOT, "src"))
    import houmai as h
    import ipc_client as ic

    print("\n[用例 13] 桌面直跑通道（IPC 优先，命令行兜底）")

    check("默认通道为 auto（桌面优先、命令行兜底）",
          (h.DEFAULT_CONFIG.get("retry") or {}).get("channel") == "auto")

    # 帧协议：4 字节小端长度前缀 + JSON，必须能无损往返。
    msg = {"type": "request", "requestId": "r1", "method": "initialize",
           "params": {"clientType": "houmai"}, "version": 0, "timeoutMs": 10000}
    frame = ic.encode_frame(msg)
    check("帧编码：长度前缀为小端 4 字节且与体长一致",
          struct.unpack("<I", frame[:4])[0] == len(frame) - 4, str(len(frame)))
    check("帧解码：能无损还原消息", ic.decode_frame(frame[4:]) == msg)

    task = {"root_id": "dddddddd-1111-4111-8111-111111111111"}
    cfg_exec = {"retry": {"channel": "exec", "prompt": "continue"}}
    check("channel=exec 时完全不走桌面通道", h.try_ipc_resume(cfg_exec, task) is None)

    fake = r"\\.\pipe\houmai-no-such-pipe-0001" if os.name == "nt" \
        else "/tmp/houmai-no-such.sock-0001"
    res = ic.desktop_resume(task["root_id"], "continue", timeout=5, endpoint=fake)
    check("管道不存在收敛为 unavailable（不抛异常）",
          res.get("status") == "unavailable", str(res))

    cfg_auto = {"retry": {"channel": "auto", "prompt": "continue"}}
    check("auto 模式桌面不可用时回落命令行（返回 None）",
          h.try_ipc_resume(cfg_auto, task, pipe=fake) is None)

    cfg_ipc = {"retry": {"channel": "ipc", "prompt": "continue"}}
    r3 = h.try_ipc_resume(cfg_ipc, task, pipe=fake)
    check("强制 ipc 且不可用时明确失败（不悄悄换道）",
          r3 is not None and r3.get("ok") is False and r3.get("reason") == "ipc_unavailable",
          str(r3))

    page = h.load_panel_html()
    check("面板：续跑详情区分送达通道",
          "经桌面窗口送达" in page and "经命令行送达" in page)
    # 状态胶囊：终态必须落进 STATUS，否则兜底渲染出英文原文。
    check("面板：状态映射覆盖全部终态（succeeded/abandoned/superseded）",
          'succeeded:"已接回"' in page and 'abandoned:"放弃续跑"' in page
          and 'superseded:"任务已自行继续"' in page, "状态映射缺项")
    check("面板：已删除死条目 done（pending 用不到）",
          'done:"已完成"' not in page)
    check("面板：待人工提示位存在（id=attention）",
          'id="attention"' in page)
    check("面板：时间行只在活跃态渲染（终态不再显示已过去的时间）",
          't.status === "waiting" || t.status === "retrying" || t.status === "unconfirmed"' in page)
    check("面板：会话来源映射存在（Codex Desktop→桌面版 / codex-tui→CLI）",
          'const ORIGIN' in page and '"Codex Desktop":"桌面版"' in page
          and '"codex-tui":"CLI"' in page, "来源映射缺失")


def case_panel_hot_reload() -> None:
    print("\n[用例 14] 面板模板热加载（改文件刷新即生效，无需重启进程）")
    import houmai as h

    tmp = tempfile.mkdtemp(prefix="houmai-14-")
    tpl = os.path.join(tmp, "panel.html")
    old_tpl, old_cache = h.PANEL_TEMPLATE, dict(h._panel_cache)
    try:
        with open(tpl, "w", encoding="utf-8") as f:
            f.write("<!DOCTYPE html><p>v1</p>")
        h.PANEL_TEMPLATE = tpl
        h._panel_cache["mtime"] = None
        check("首次读取模板", "v1" in h.load_panel_html())

        with open(tpl, "w", encoding="utf-8") as f:
            f.write("<!DOCTYPE html><p>v2</p>")
        os.utime(tpl, (time.time() + 10, time.time() + 10))  # 强制 mtime 变化
        got = h.load_panel_html()
        check("改了模板文件后重新读到新内容（刷新即生效）",
              "v2" in got and "v1" not in got, got[:40])
    finally:
        h.PANEL_TEMPLATE = old_tpl
        h._panel_cache.clear()
        h._panel_cache.update(old_cache)
        shutil.rmtree(tmp, ignore_errors=True)

    src = open(os.path.join(ROOT, "src", "houmai.py"), encoding="utf-8").read()
    check("服务端每次请求读模板（不再冻结成启动时的副本）",
          "load_panel_html()" in src and "page = PAGE_HTML" not in src)
    real_tpl = os.path.join(ROOT, "src", "panel.html")
    check("模板已外置为独立文件（src/panel.html 存在）",
          os.path.exists(real_tpl) and os.path.getsize(real_tpl) > 1000)


def case_task_power_policy() -> None:
    """计划任务电源策略：解析任务 XML，体检据此报出「拔电即静默停摆」。

    这条踩过一次坑：任务在计划任务里显示"已启用/就绪"，注册也查得到，
    可笔记本一拔电它压根不启动——静默失效比明着失败更坏，所以必须能提前发现。
    """
    sys.path.insert(0, os.path.join(ROOT, "src"))
    import houmai as h

    print("\n[用例 15] 计划任务电源策略（拔电不再静默停摆）")

    # XML 解析：任一开关为 true 即受限；元素缺失视为不受限；空串不臆断。
    check("电源策略：DisallowStartIfOnBatteries=true 判为受限",
          h.xml_battery_restricted(
              "<Task><Settings><DisallowStartIfOnBatteries>true</DisallowStartIfOnBatteries>"
              "</Settings></Task>") is True)
    check("电源策略：只 StopIfGoingOnBatteries=true 也算受限",
          h.xml_battery_restricted(
              "<Task><Settings><StopIfGoingOnBatteries>true</StopIfGoingOnBatteries>"
              "</Settings></Task>") is True)
    check("电源策略：两个开关都 false 才判不受限",
          h.xml_battery_restricted(
              "<Task><Settings><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>"
              "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries></Settings></Task>") is False)
    check("电源策略：元素缺失视为不受限（旧任务无该项，不误报）",
          h.xml_battery_restricted("<Task><Settings></Settings></Task>") is False)
    check("电源策略：空串返回 None（查不到就不臆断）",
          h.xml_battery_restricted("") is None and h.xml_battery_restricted("   ") is None)

    # 注册脚本必须真的去改这两个开关，否则体检提示"选 3 重新注册"就是空话。
    # 走过两次弯路：① 导出 XML→文本替换→回写（声明被写坏）；② 先建任务再改设置
    # （改动可能被"创建"本身覆盖）。现在改为一步建好：电池设置内联进创建的 settings。
    it = open(os.path.join(ROOT, "scripts", "install-task.cmd"),
              encoding="ascii", errors="replace").read()
    rg_path = os.path.join(ROOT, "scripts", "register-task.ps1")
    rg = open(rg_path, encoding="ascii", errors="replace").read()
    check("注册脚本：一步建好（调 register-task.ps1，且不再用 schtasks 建任务）",
          "register-task.ps1" in it and "-File" in it and "schtasks /Create" not in it)
    check("注册脚本：不再手改任务 XML",
          "/XML" not in it and "[IO.File]::ReadAllText" not in it and "encoding=" not in it)
    check("注册脚本：电池设置在建任务时内联（AllowStart / DontStop）",
          "New-ScheduledTaskSettingsSet" in rg and "-AllowStartIfOnBatteries" in rg
          and "-DontStopIfGoingOnBatteries" in rg)
    check("注册脚本：注册后回读校验，仍受限即非零退出（不静默通过）",
          "DisallowStartIfOnBatteries" in rg and "StopIfGoingOnBatteries" in rg
          and "exit 3" in rg and "[WARN]" in rg)
    check("注册脚本：register-task.ps1 纯 ASCII（不留编码/引号陷阱）",
          os.path.exists(rg_path) and all(b < 128 for b in open(rg_path, "rb").read()))

    # 体检联动：带限制时报 power fail 卡，且徽标点名"电源策略"。
    tmp = tempfile.mkdtemp(prefix="houmai-15-")
    orig = (h.resolve_codex, h.task_registered, h.task_power_restriction, h.collect_runtime)
    try:
        st = h.State(os.path.join(tmp, "state.json"))
        st.data["last_run_at"] = time.time()
        h.resolve_codex = lambda cfg: None
        h.task_registered = lambda name=h.TASK_NAME: True
        h.task_power_restriction = lambda name=h.TASK_NAME: True
        h.collect_runtime = lambda cfg: {"python": "", "python_external": True,
                                         "node": "", "node_external": True, "codex": False}
        cfg = {"state_dir": tmp, "codex": {"min_version": "0"},
               "watch": {"lookback_days": 3}, "notify": {}}
        hd = h.collect_health(cfg, st)
        cards = {c["key"]: c for c in hd["checks"]}
        # 常态卡不能只有状态：主人问过"「计划任务」只写了已注册，它是干什么的？"
        # 卡片是给人看的地方，用途得写在卡上（措辞与 README 的用法说明一致）。
        task_detail = cards.get("task", {}).get("detail", "")
        check("体检：计划任务卡写明用途（不能只报'已注册'）",
              "每 10 分钟" in task_detail and "续跑" in task_detail, task_detail)
        check("体检：带电池限制 → power 卡为 fail",
              cards.get("power", {}).get("state") == "fail")
        check("体检：徽标点名问题类型（值守会被电源策略停摆）",
              hd["ok"] is False and hd["verdict"] == "值守会被电源策略停摆",
              hd.get("verdict"))
        check("体检：power 卡给出修法（houmai.cmd 选 3）",
              "houmai.cmd 选 3" in cards.get("power", {}).get("detail", ""))

        # 不受限时不该出现 power 卡（没消息就是好消息）。
        h.task_power_restriction = lambda name=h.TASK_NAME: False
        hd2 = h.collect_health(cfg, st)
        check("体检：不受限时不出现 power 卡",
              "power" not in {c["key"] for c in hd2["checks"]})

        # 待人工提示：候脉已放弃续跑的任务计入 attention，面板据此补琥珀一行。
        st.data["pending"] = {"k": {"root_id": "bbbbbbbb-0000-0000-0000-000000000000",
                                    "status": "abandoned", "attempts": 3}}
        hd3 = h.collect_health(cfg, st)
        check("体检：放弃续跑的任务计入待人工（attention=1）",
              hd3["attention"] == 1, hd3["attention"])
    finally:
        (h.resolve_codex, h.task_registered,
         h.task_power_restriction, h.collect_runtime) = orig
        shutil.rmtree(tmp, ignore_errors=True)


def case_resume_channel() -> None:
    """续跑通道按会话来源选：桌面版不兜底 CLI；CLI 直接走 CLI（不试桌面）。

    桌面会话本就该回到桌面；若桌面接不住还强行 CLI 续跑，CLI 会抢写锁，
    把会话从桌面手里夺走（桌面再打开就报"已在另一个应用中打开"）。
    CLI 会话本就没在桌面开着，应直接走命令行，不必先试桌面 IPC 再回落。
    """
    import houmai as h
    print("\n[用例 16] 续跑通道按会话来源选择（桌面版不兜底 CLI / CLI 直接走 CLI）")
    cfg_auto = {"retry": {"channel": "auto", "prompt": "continue"}}
    task_desk = {"root_id": "dddddddd-1111-4111-8111-111111111111",
                 "context": {"originator": "Codex Desktop"}}
    task_cli = {"root_id": "cccccccc-1111-4111-8111-111111111111",
                "context": {"originator": "codex-tui"}}
    orig_ipc, orig_exec = h.try_ipc_resume, h.run_resume
    calls: list[str] = []

    def mock_ipc_none(c, t, pipe=None):
        calls.append("ipc")
        return None

    def mock_ipc_ok(c, t, pipe=None):
        calls.append("ipc")
        return {"ok": True, "via": "ipc", "ipc_status": "sent"}

    def mock_exec(c, t, dry_run=False):
        calls.append("exec")
        return {"ok": True, "via": "exec"}

    try:
        # 桌面通道接不住 → 桌面版会话不兜底 CLI，返回 None（留待下轮）；只试过桌面
        h.try_ipc_resume = mock_ipc_none
        h.run_resume = mock_exec
        calls.clear()
        r = h.pick_resume(cfg_auto, task_desk)
        check("续跑：桌面版会话 auto 下桌面接不住时不兜底 CLI（返回 None，只试了桌面）",
              r is None and calls == ["ipc"], (r, calls))
        # CLI 会话 → 直接走 CLI，且不调用 try_ipc_resume（根本不试桌面）
        calls.clear()
        r = h.pick_resume(cfg_auto, task_cli)
        check("续跑：CLI 会话 auto 下直接走 CLI（不调 try_ipc_resume）",
              r is not None and r.get("via") == "exec" and calls == ["exec"], (r, calls))
        # CLI 会话即便桌面通道可用，也只走 CLI（不碰 ipc）
        calls.clear()
        h.try_ipc_resume = mock_ipc_ok
        r = h.pick_resume(cfg_auto, task_cli)
        check("续跑：CLI 会话即使桌面通道可用也只走 CLI（不碰 ipc）",
              r is not None and r.get("via") == "exec" and calls == ["exec"], (r, calls))
        # 显式 channel=exec → 尊重配置，桌面版也走 CLI
        calls.clear()
        h.try_ipc_resume = mock_ipc_none
        r = h.pick_resume({"retry": {"channel": "exec", "prompt": "continue"}}, task_desk)
        check("续跑：桌面版会话显式 channel=exec 下仍走 CLI（尊重配置）",
              r is not None and r.get("via") == "exec" and calls == ["exec"], (r, calls))
        # 桌面通道可用 → 走桌面，不调 CLI
        calls.clear()
        h.try_ipc_resume = mock_ipc_ok
        r = h.pick_resume(cfg_auto, task_desk)
        check("续跑：桌面版会话桌面通道可用时走桌面（不调 exec）",
              r is not None and r.get("via") == "ipc" and calls == ["ipc"], (r, calls))
    finally:
        h.try_ipc_resume, h.run_resume = orig_ipc, orig_exec


def case_config_seed() -> None:
    """配置自动播种：首次运行无需手动复制，load_config 会从模板生成 config.json。"""
    import houmai as h
    import tempfile
    import shutil as _sh
    print("\n[用例 17] 配置自动播种（首次运行免手动复制）")
    tmp = tempfile.mkdtemp(prefix="houmai-seed-")
    try:
        # _seed_config 把随仓库的模板复制成目标文件（不改写已有文件、不动项目根）。
        dst = os.path.join(tmp, "config.json")
        ok = h._seed_config(dst)
        check("配置：缺省配置文件时 _seed_config 能从模板生成 config.json",
              ok and os.path.exists(dst))
        # 生成的文件可被 load_config 正常解析，且合并出完整默认值（含 retry.channel）。
        cfg = h.load_config(dst)   # 显式路径：不触发再写、也不碰项目根
        check("配置：播种出的文件被 load_config 正常解析（合并默认值，retry.channel=auto）",
              isinstance(cfg, dict) and cfg.get("retry", {}).get("channel") == "auto"
              and "codex" in cfg and "watch" in cfg)
    finally:
        _sh.rmtree(tmp, ignore_errors=True)


def case_prune_terminal() -> None:
    """终态任务摘除：pending 只装活跃任务，摘除留痕、可防误检。

    防误检按 turn_id 记忆（resolved_turns），不按 root_id——同会话之后的新中断
    本就该生成新任务，按 root_id 挡会误杀。
    """
    print("\n[用例 18] 终态任务摘除（已接回次一轮、放弃过保留期，演练不摘）")
    tmp = tempfile.mkdtemp(prefix="houmai-prune-")
    home = os.path.join(tmp, "codexhome")
    os.makedirs(home, exist_ok=True)
    state_path = os.path.join(tmp, "state", "state.json")
    now = time.time()
    day = 86400.0

    def seed_state(tasks: dict, turns: dict | None = None) -> None:
        # resolved_turns 默认继承现有文件里的内容：它是摘除后的防误检记忆，
        # 重种 pending 不该把它一并抹掉（否则第三段的防误检就没了前提）。
        if turns is None:
            turns = (load_state(state_path).get("resolved_turns") or {}) if \
                os.path.exists(state_path) else {}
        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "offsets": {}, "sessions": {},
                       "pending": tasks, "done": [], "notified": {},
                       "resolved_turns": turns}, f, ensure_ascii=False)

    def real_run(cfg_path: str, extra: list[str] | None = None) -> str:
        proc = subprocess.run([PY, ENTRY, "--config", cfg_path, "--state", state_path,
                               "run"] + (extra or []),
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
        return (proc.stdout or "") + (proc.stderr or "")

    def task(root: str, turn: str, status: str, resolved_at: float | None = None,
             **extra) -> dict:
        t = {"root_id": root, "turn_id": turn, "status": status,
             "detected_at": now - 9000, "attempts": 3 if status == "abandoned" else 0}
        if resolved_at is not None:
            t["resolved_at"] = resolved_at
        t.update(extra)
        return t

    try:
        # —— 第一段：默认保留期（7 天），一轮真实 run ——
        seed_state({
            "r-succ": task("r-succ", "t-succ", "succeeded", resolved_at=now - 600),
            "r-sup": task("r-sup", "t-sup", "superseded", resolved_at=now - 600),
            "r-old": task("r-old", "t-old", "abandoned", resolved_at=now - 8 * day),
            "r-new": task("r-new", "t-new", "abandoned", resolved_at=now - day),
            "r-wait": task("r-wait", "t-wait", "waiting", retry_at=now + 6 * 3600),
        })
        cfg_path = make_config(tmp, home)   # 不带 resolved_retention_days → 走默认 7
        out1 = real_run(cfg_path)
        st = load_state(state_path)
        pend = st.get("pending") or {}
        check("摘除：已接回/自行继续次一轮即摘，超保留期的放弃续跑摘除",
              all(k not in pend for k in ("r-succ", "r-sup", "r-old")), str(sorted(pend)))
        check("摘除：保留期内的放弃续跑与活跃任务原样保留",
              "r-new" in pend and "r-wait" in pend and len(pend) == 2, str(sorted(pend)))
        rt = st.get("resolved_turns") or {}
        check("摘除：被摘任务的 turn_id 进 resolved_turns（防误检），未摘的不进",
              all(t in rt for t in ("t-succ", "t-sup", "t-old")) and "t-new" not in rt)
        kinds = event_kinds(state_path)
        check("摘除：每次摘除记一条 pruned 事件（共 3 条）", kinds.count("pruned") == 3,
              str(kinds))
        check("摘除：run 日志留痕（摘了什么、结案多久，可追查）", "已从在册摘除" in out1)

        # —— 第二段：保留期 0 = 永不摘除（回退旧行为）——
        seed_state({"r-old": task("r-old", "t-old2", "abandoned", resolved_at=now - 30 * day)})
        cfg0 = make_config(tmp, home, retry={"resolved_retention_days": 0})
        real_run(cfg0)
        st2 = load_state(state_path)
        check("摘除：保留期 0 表示永不摘除",
              "r-old" in (st2.get("pending") or {})
              and event_kinds(state_path).count("pruned") == 3)

        # —— 第三段：防误检 —— 已摘轮次重现在会话文件里，不得当成新中断 ——
        # （该文件此前从未被扫描过，等同"offset 丢失后整卷重扫"的最坏情形）
        write_rollout(home, "rollout-r-succ.jsonl", [
            meta("r-succ", r"D:\fake\prune-a"),
            turn_context("t-succ", r"D:\fake\prune-a", "gpt-5.6-sol", "high", "workspace-write"),
            token_count(now, 99.0, now + 3600, 1.0),
            quota_error("t-succ"),
        ])
        real_run(cfg0)
        st3 = load_state(state_path)
        check("防误检：已摘轮次重现也不生成新任务、不重发提醒",
              "r-succ" not in (st3.get("pending") or {})
              and len(st3.get("pending") or {}) == 1
              and "detected" not in event_kinds(state_path))

        # —— 第四段：演练不摘除（零副作用契约）——
        seed_state({
            "r-succ": task("r-succ", "t-succ3", "succeeded", resolved_at=now - 600),
            "r-old": task("r-old", "t-old3", "abandoned", resolved_at=now - 30 * day),
        })
        real_run(cfg_path, ["--dry-run"])
        st4 = load_state(state_path)
        check("摘除：演练模式不摘除（零副作用）",
              "r-succ" in (st4.get("pending") or {})
              and "r-old" in (st4.get("pending") or {})
              and event_kinds(state_path).count("pruned") == 3)

        # —— 第五段：面板标注 ——
        sys.path.insert(0, os.path.join(ROOT, "src"))
        import houmai as h
        ka = h.keep_until_at(h.DEFAULT_CONFIG, {"status": "abandoned", "resolved_at": now})
        cfg_off = {"retry": {"resolved_retention_days": 0}}
        check("面板：放弃/仅提醒算出保留截止；其余状态与保留期 0 均不标注",
              ka == now + 7 * 86400
              and h.keep_until_at(h.DEFAULT_CONFIG, {"status": "waiting", "resolved_at": now}) is None
              and h.keep_until_at(h.DEFAULT_CONFIG, {"status": "succeeded", "resolved_at": now}) is None
              and h.keep_until_at(cfg_off, {"status": "abandoned", "resolved_at": now}) is None)
        page = h.load_panel_html()
        check("面板：任务卡标注保留期，事件表有 pruned 标签与文案",
              "keep_until_local" in page and "到期自动摘除" in page
              and "在册任务归档" in page and "已从在册摘除" in page)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    print("houmai 固定测试（全部使用构造数据，不触碰真实 Codex）")
    for fn in (case_short_cycle, case_long_cycle, case_long_cycle_off, case_long_cycle_beyond,
               case_unknown_reset,
               case_borrow_window, case_already_progressed,
               case_non_quota_ignored, case_stale_skipped, case_idempotent,
               case_dry_run_readonly,
               case_runtime_independence, case_history_window, case_record_shape,
               case_panel_html, case_sandbox_fidelity, case_review_unconfirmed,
               case_resume_channel,
               case_panel_stop, case_ipc_channel, case_panel_hot_reload,
               case_task_power_policy, case_config_seed, case_prune_terminal):
        try:
            fn()
        except Exception as exc:
            check(f"{fn.__name__} 执行异常", False, repr(exc))
    # 自洽：README 里写着的断言数必须等于实际条数。这个数字**已经漂过一次**——
    # README 长期写着"147 项断言"，实际早已到 198，中间 51 条没人发现。放在这里
    # （所有用例跑完之后、汇总之前）才拿得到真实条数；比较用 `len(RESULTS) + 1`，
    # 因为下面这条自己也算一条断言，加进去之后总数才会变成那个值。
    # 注意：若上面有用例抛异常，会多出一条"执行异常"断言，此处同样会报不一致 ——
    # 那种情况下真正的故障已在上方列出，别误读成"文档写错了"。
    readme = open(os.path.join(ROOT, "README.md"), encoding="utf-8", errors="replace").read()
    claim = re.search(r"(\d+)\s*项断言", readme)
    check("文档：README 的断言数与实际一致（自洽，防文档漂移）",
          claim is not None and int(claim.group(1)) == len(RESULTS) + 1,
          f"README 写 {claim.group(1) if claim else '（找不到）'}，实际 {len(RESULTS) + 1}")

    passed = sum(1 for _n, ok, _d in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n结果：{passed}/{total} 通过")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  失败：{name}  {detail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
