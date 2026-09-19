#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""houmai（候脉）— Codex 任务额度耗尽后的等待与自动续跑值守。

本工具只读 Codex 会话记录，只处理额度耗尽类中断，续跑时逐字段复刻原轮次的
执行上下文，不提升任何权限。用法与排障见仓库根目录的 README.md。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 同目录的桌面直跑通道封装（import 失败不致命：通道自动退回 exec）。
try:
    import ipc_client  # noqa: E402
except Exception:  # pragma: no cover
    ipc_client = None

VERSION = "0.1.0"
LOCAL_TZ = timezone(timedelta(hours=8))
TASK_NAME = "houmai"

# 值守日志：计划任务在后台窗口跑，控制台输出看不见，必须落盘。
LOG_FILENAME = "run.log"
LOG_BACKUP_SUFFIX = ".1"
LOG_MAX_BYTES = 512 * 1024
LOG_KEEP_LINES = 500
PANEL_EVENT_LIMIT = 10  # 面板事件表至多展示最新 10 条；底账 events.jsonl 不删
_log_path: str | None = None

DEFAULT_CONFIG = {
    "codex": {
        "command": None,
        "node": None,
        # 绝不复用这些目录下的运行时。候脉是看护工具，不能靠被看护对象
        # 的自带运行时活着；把它们的目录加进 config.json 的这份清单即可
        # （子串匹配、大小写与斜杠不敏感）。默认为空，由部署方按需填写。
        "avoid_paths": [],
        # 额外传给 Codex 子进程的环境变量。计划任务只继承用户级环境，
        # 如果联网依赖代理，就在这里钉死，别指望会话变量。
        "env": {},
        "codex_home": None,
        "min_version": "0.150.0",
    },
    "watch": {
        "ignore_threads": [],
        "lookback_days": 3,
        "silence_minutes": 5,
        "initial_lookback_hours": 12,
    },
    "retry": {
        "prompt": "continue",
        # 续跑通道：auto=桌面直跑通道优先、命令行兜底；ipc=只用桌面通道；
        # exec=只用命令行（旧行为）。桌面通道在会话被 Codex Desktop 打开时
        # 也能送达，绕开单写者锁，见 docs/DESIGN-ipc-direct-resume.md。
        "channel": "auto",
        "buffer_seconds": 120,
        "max_attempts": 3,
        # 失败退避间隔（秒）。按**轮巡粒度**生效：值守由计划任务每 ~10 分钟唤醒
        # 一次，小于轮巡间隔的退避等于"下一轮重试"；0 = 下一轮立即重试。
        # 默认 [0,600,1800] 的真实节奏：下一轮、再等一轮、再等三轮。
        "backoff_seconds": [0, 600, 1800],
        "coalesce_minutes": 10,
        # 续跑已发出、但当时还没看到新轮次时，最多再等多久复查；超时转人工。
        # 不设成"直接算成功"，是为了不让"进程退 0 却什么也没做"被记成成功。
        "confirm_deadline_minutes": 30,
        # 长周期额度（如周窗口）默认也排期续跑，不再只提醒。关掉即回退旧行为。
        "resume_long_cycle": True,
        # 长周期等待上限（天）：resets_at 超过这个天数则退回"仅提醒"，
        # 防止异常数据把续跑排到很遥远的将来。0 = 不设限。
        "max_wait_days": 8,
        # 终态任务（放弃续跑/仅提醒）在册保留天数：到期自动从 pending 摘除，
        # 审计仍在 events.jsonl（记 pruned 事件）。0 = 永不摘除（回退旧行为）。
        # 已接回/自行继续不适用保留期：它们已彻底了结，次一轮即摘。
        "resolved_retention_days": 7,
    },
    "notify": {
        "provider": "none",
        "serverchan_key": "",
        "on": ["detect", "success", "failure", "long_cycle"],
    },
    "panel": {
        # 面板/体检里"历史记录"的展示窗口（天）。只影响呈现，
        # events.jsonl 永远是 append-only 的审计底账，不做任何删改。
        "history_days": 3,
        # 历史中断跳过（skipped_stale）记录的是"我看过、我决定不管"的判定过程，
        # 属于内部噪声而非续跑历史，默认折叠成一行汇总。
        "show_skipped_stale": False,
    },
    "state_dir": "state",
}

QUOTA_ERROR = "usage_limit_exceeded"


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------

def log(msg: str) -> None:
    stamp = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp[11:]}] {msg}", flush=True)
    append_log_line(f"[{stamp}] {msg}")


def log_file(cfg: dict) -> str:
    """值守日志路径；计划任务在后台跑，输出只能靠落盘才看得到。"""
    return os.path.join(workdir(cfg), LOG_FILENAME)


def set_log_file(path: str | None) -> None:
    global _log_path
    if not path:
        _log_path = None
        return
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    except Exception:
        _log_path = None
        return
    _log_path = path


def append_log_line(line: str) -> None:
    if not _log_path:
        return
    try:
        if os.path.exists(_log_path) and os.path.getsize(_log_path) > LOG_MAX_BYTES:
            backup = _log_path + LOG_BACKUP_SUFFIX
            with open(_log_path, "r", encoding="utf-8", errors="replace") as f:
                carry = f.readlines()[-LOG_KEEP_LINES:]
            with open(backup, "w", encoding="utf-8") as f:
                f.writelines(carry)
            with open(_log_path, "w", encoding="utf-8") as f:
                pass
        with open(_log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def read_log_tail(path: str, lines: int) -> list[str]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.readlines()[-lines:]


def deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def root_dir() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _seed_config(dst: str) -> bool:
    """首次运行时从随仓库的模板自动生成 config.json，省去手动复制。

    只在默认配置文件本来就不存在时才写；已存在、或显式用 --config 指定了别的
    路径都不触发——既不覆盖用户已有配置，也不在测试／演练用的临时文件上动手脚。
    """
    example = os.path.join(root_dir(), "config.example.json")
    try:
        if os.path.exists(example):
            shutil.copyfile(example, dst)
        else:
            with open(dst, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
        log(f"已自动生成配置文件 {dst}（基于模板，可编辑后生效）")
        return True
    except Exception as exc:
        log(f"自动生成配置失败（不影响运行，将用内置默认值）：{exc}")
        return False


def load_config(path: str | None = None) -> dict:
    explicit = path is not None
    path = path or os.path.join(root_dir(), "config.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return deep_merge(DEFAULT_CONFIG, json.load(f))
        except Exception as exc:
            log(f"配置读取失败，改用默认值：{exc}")
    elif not explicit:
        # 首次运行且用的是默认项目配置：从模板自动生成，免手动复制。
        if _seed_config(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return deep_merge(DEFAULT_CONFIG, json.load(f))
            except Exception:
                pass
    return json.loads(json.dumps(DEFAULT_CONFIG))


def codex_home(cfg: dict) -> str:
    explicit = cfg["codex"].get("codex_home")
    if explicit:
        return os.path.expanduser(explicit)
    return os.path.join(os.path.expanduser("~"), ".codex")


def workdir(cfg: dict) -> str:
    d = cfg.get("state_dir") or "state"
    if not os.path.isabs(d):
        d = os.path.join(root_dir(), d)
    os.makedirs(d, exist_ok=True)
    return d


ISO_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.(\d+))?\s*(Z|[+-]\d{2}:?\d{2})?$")


def parse_iso(ts: str) -> float:
    """解析会话记录里的 UTC 时间戳为 epoch 秒。"""
    try:
        m = ISO_RE.match((ts or "").strip())
        if not m:
            raise ValueError(ts)
        date, clock, frac, tz = m.groups()
        frac = (frac or "0")[:6].ljust(6, "0")
        if not tz or tz == "Z":
            tz = "+00:00"
        elif ":" not in tz:
            tz = tz[:3] + ":" + tz[3:]
        return datetime.fromisoformat(f"{date}T{clock}.{frac}{tz}").timestamp()
    except Exception:
        return time.time()


def fmt_local(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, LOCAL_TZ).strftime("%m-%d %H:%M")


def fmt_delta(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{seconds // 60} 分钟"
    if seconds < 86400:
        return f"{seconds / 3600:.1f} 小时"
    return f"{seconds / 86400:.1f} 天"


# --------------------------------------------------------------------------
# 状态与记录
# --------------------------------------------------------------------------

class State:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.events_path = os.path.join(os.path.dirname(path), "events.jsonl")
        self.data = {"version": 1, "offsets": {}, "sessions": {}, "pending": {},
                     "done": [], "notified": {}, "resolved_turns": {}}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self.data = deep_merge(self.data, json.load(f))
            except Exception as exc:
                log(f"状态文件损坏，已隔离并重新开始：{exc}")
                try:
                    shutil.move(path, path + f".broken-{int(time.time())}")
                except Exception:
                    pass

    def save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def record(self, kind: str, **fields) -> None:
        entry = {"at": time.time(), "kind": kind}
        entry.update(fields)
        with open(self.events_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def recent(self, limit: int = 20) -> list[dict]:
        if not os.path.exists(self.events_path):
            return []
        out: list[dict] = []
        with open(self.events_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
        return out[-limit:]


def event_when(ev: dict) -> float:
    """事件"所指对象"的时间，而不是事件被写入的时间。

    这是展示窗口唯一靠谱的口径。反例：一条 skipped_stale 的 `at` 是"我发现并跳过它"
    的时刻，但如果只按 `at` 过滤，同一轮里记下的"51 天前的中断"就会被算进"近 3 天"，
    窗口形同虚设。所以这类事件必须换算回被跳过中断本身的时刻。
    """
    at = float(ev.get("at") or 0.0)
    kind = ev.get("kind")
    if kind == "skipped_stale":
        age = ev.get("age_hours")
        if isinstance(age, (int, float)):
            return at - float(age) * 3600.0
    if kind == "already_progressed":
        resumed_at = ev.get("resumed_at")
        if isinstance(resumed_at, (int, float)):
            return float(resumed_at)
    return at


def split_events(events: list[dict], cfg: dict, now: float) -> dict:
    """按 panel.history_days 切分事件，供面板与体检展示。

    只改变"看什么"，不改变"存什么"：events.jsonl 依旧逐行追加、永不删改。
    """
    panel = cfg.get("panel") or {}
    days = float(panel.get("history_days", 3) or 0)
    show_stale = bool(panel.get("show_skipped_stale", False))
    cutoff = now - days * 86400 if days > 0 else None

    kept: list[dict] = []
    dropped = 0
    stale: list[dict] = []
    for ev in events:
        if ev.get("kind") == "skipped_stale" and not show_stale:
            stale.append(ev)
            continue
        if cutoff is not None and event_when(ev) < cutoff:
            dropped += 1
            continue
        kept.append(ev)

    ages = [float(e["age_hours"]) for e in stale
            if isinstance(e.get("age_hours"), (int, float))]
    stale_summary = None
    if stale:
        stale_summary = {
            "count": len(stale),
            "oldest_hours": max(ages) if ages else None,
            "newest_hours": min(ages) if ages else None,
        }

    # "已自行继续"是结论型事件：一轮扫描会为同一会话的每个中断轮次各记一条，
    # 内容完全相同。底账照旧逐条保留，只在展示上合并，免得看起来像跑了三次。
    merged: list[dict] = []
    seen: dict[tuple, dict] = {}
    for ev in kept:
        if ev.get("kind") == "already_progressed":
            key = (ev.get("kind"), ev.get("root_id"))
            hit = seen.get(key)
            if hit is not None:
                hit["repeat"] = hit.get("repeat", 1) + 1
                continue
            copy = dict(ev)
            seen[key] = copy
            merged.append(copy)
        else:
            merged.append(ev)

    return {
        "events": merged,
        "dropped": dropped,
        "stale_summary": stale_summary,
        "history_days": days,
        "merged": len(kept) - len(merged),
    }


# --------------------------------------------------------------------------
# 探测：增量扫描 Codex 会话记录
# --------------------------------------------------------------------------

def session_files(cfg: dict) -> list[str]:
    base = os.path.join(codex_home(cfg), "sessions")
    if not os.path.isdir(base):
        return []
    cutoff = time.time() - cfg["watch"]["lookback_days"] * 86400
    found = []
    for dirpath, _dirnames, filenames in os.walk(base):
        for name in filenames:
            if not name.endswith(".jsonl"):
                continue
            fp = os.path.join(dirpath, name)
            try:
                if os.path.getmtime(fp) >= cutoff:
                    found.append(fp)
            except OSError:
                continue
    return found


def read_meta(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            first = f.readline()
        rec = json.loads(first)
    except Exception:
        return None
    if rec.get("type") != "session_meta":
        return None
    p = rec.get("payload") or {}
    src = p.get("source") or {}
    subagent = src.get("subagent") if isinstance(src, dict) else None
    own_id = p.get("id")
    session_id = p.get("session_id")
    return {
        "own_id": own_id,
        "session_id": session_id,
        "parent_thread_id": p.get("parent_thread_id"),
        "cwd": p.get("cwd"),
        "originator": p.get("originator"),
        "thread_source": p.get("thread_source"),
        "is_subagent": bool(subagent) or bool(session_id and own_id and session_id != own_id),
    }


def read_new_lines(path: str, offset: int) -> tuple[list[str], int]:
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read()
    except OSError:
        return [], offset
    if not data:
        return [], offset
    if data.endswith(b"\n"):
        chunk, advance = data, len(data)
    else:
        idx = data.rfind(b"\n")
        if idx < 0:
            return [], offset
        chunk, advance = data[: idx + 1], idx + 1
    lines = [ln.decode("utf-8", errors="replace") for ln in chunk.split(b"\n") if ln.strip()]
    return lines, offset + advance


def scan(cfg: dict, state: State, verbose: bool = False) -> list[dict]:
    """扫描增量内容，返回本次新发现的额度耗尽事件。"""
    incidents: list[dict] = []
    for fp in session_files(cfg):
        meta = state.data["sessions"].get(fp)
        first_sight = False
        if not meta:
            meta = read_meta(fp)
            if not meta:
                continue
            state.data["sessions"][fp] = meta
            state.data["offsets"].setdefault(fp, 0)
            first_sight = True
        offset = state.data["offsets"].get(fp, 0)
        lines, new_offset = read_new_lines(fp, offset)
        if not lines:
            state.data["offsets"][fp] = new_offset
            continue
        ctx: dict = state.data.setdefault("_ctx", {}).get(fp, {})
        rate_limits = ctx.get("rate_limits")
        contexts = ctx.get("contexts") or {}
        for line in lines:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            payload = rec.get("payload") or {}
            ptype = kind_of(rec)
            if ptype == "turn_context":
                tid = payload.get("turn_id")
                if tid:
                    contexts[tid] = context_from_payload(payload)
                    if len(contexts) > 40:
                        for old in list(contexts)[:-20]:
                            contexts.pop(old, None)
            elif ptype == "token_count":
                info = payload.get("info") or {}
                rl = info.get("rate_limits") or payload.get("rate_limits")
                if isinstance(rl, dict) and rl.get("primary"):
                    rate_limits = rl
            elif ptype == "task_complete":
                err = payload.get("error") or {}
                if err.get("codex_error_info") != QUOTA_ERROR:
                    continue
                tid = payload.get("turn_id") or ""
                root_id = meta.get("session_id") or meta.get("own_id")
                incident = {
                    "file": fp,
                    "turn_id": tid,
                    "session_id": meta["session_id"],
                    "root_id": root_id,
                    "is_subagent": meta["is_subagent"],
                    "at": parse_iso(rec.get("timestamp") or ""),
                    "message": (err.get("message") or "")[:200],
                    "rate_limits": rate_limits,
                    "context": contexts.get(tid) or (list(contexts.values())[-1] if contexts else None),
                    "cwd": meta.get("cwd"),
                    "first_sight": first_sight,
                }
                incidents.append(incident)
                if verbose:
                    log(f"发现额度耗尽：{os.path.basename(fp)} turn={tid[:13]}")
        state.data["_ctx"] = state.data.get("_ctx", {})
        state.data["_ctx"][fp] = {"rate_limits": rate_limits, "contexts": contexts}
        state.data["offsets"][fp] = new_offset
    return incidents


def latest_known_rate_limits(state) -> dict | None:
    """全账号共用的额度窗口：从所有会话最近观测到的 rate_limits 里挑最新的一份。

    某个会话自身没带额度数据时（偶发），用它来补齐判定与排期依据。"""
    best, best_key = None, -1
    for ctx in (state.data.get("_ctx") or {}).values():
        rl = (ctx or {}).get("rate_limits")
        if not isinstance(rl, dict):
            continue
        pr = (rl.get("primary") or {}).get("resets_at") or 0
        sr = (rl.get("secondary") or {}).get("resets_at") or 0
        if max(pr, sr) > best_key:
            best, best_key = rl, max(pr, sr)
    return best


# --------------------------------------------------------------------------
# 判定：窗口类型与最长等待
# --------------------------------------------------------------------------

def classify(rate_limits: dict | None) -> dict:
    """判定绑定的额度窗口。主窗口按小时级等待；长周期窗口默认也排期（受 retry 配置约束）。"""
    if not rate_limits:
        return {"binding": None, "kind": "unknown", "resets_at": None}
    primary = rate_limits.get("primary") or {}
    secondary = rate_limits.get("secondary") or {}
    p_used = float(primary.get("used_percent") or 0)
    s_used = float(secondary.get("used_percent") or 0)
    p_reset = primary.get("resets_at") or 0
    s_reset = secondary.get("resets_at") or 0

    if p_used >= 100 and s_used >= 100:
        win, name = (primary, "primary") if p_reset >= s_reset else (secondary, "secondary")
    elif s_used >= 100:
        win, name = secondary, "secondary"
    elif p_used >= 100:
        win, name = primary, "primary"
    else:
        win, name = (primary, "primary") if p_used >= s_used else (secondary, "secondary")

    window_minutes = float(win.get("window_minutes") or 0)
    return {
        "binding": name,
        "kind": "long_cycle" if window_minutes > 24 * 60 else "short_cycle",
        "used_percent": win.get("used_percent"),
        "window_minutes": window_minutes,
        "resets_at": win.get("resets_at"),
        "other": {
            "primary": {"used_percent": p_used, "window_minutes": primary.get("window_minutes")},
            "secondary": {"used_percent": s_used, "window_minutes": secondary.get("window_minutes")},
        },
    }


def long_cycle_skip_reason(cfg: dict, info: dict, now: float) -> str | None:
    """长周期额度未排期续跑的原因；返回 None 表示应当正常排期续跑。

    - `retry.resume_long_cycle` 关闭 → "switch_off"（回退到旧的"只提醒"）
    - `retry.max_wait_days > 0` 且重置时间超出该上限 → "beyond_max_wait"
      （防异常数据把续跑排到遥不可及的将来）
    """
    retry = cfg.get("retry") or {}
    if not retry.get("resume_long_cycle", True):
        return "switch_off"
    max_days = float(retry.get("max_wait_days") or 0)
    resets_at = info.get("resets_at") or 0
    if max_days > 0 and resets_at and (resets_at - now) > max_days * 86400:
        return "beyond_max_wait"
    return None


# --------------------------------------------------------------------------
# 执行：复刻上下文后定向续跑
# --------------------------------------------------------------------------

def parse_version(text: str) -> tuple:
    digits = []
    for part in text.strip().replace("v", "").split("."):
        num = ""
        for ch in part:
            if ch.isdigit():
                num += ch
            else:
                break
        digits.append(int(num) if num else 0)
    while len(digits) < 3:
        digits.append(0)
    return tuple(digits[:3])


def npm_global_root(markers: list[str] | None = None) -> str | None:
    npm = None
    if markers:
        hits = path_candidates("npm", markers)
        npm = hits[0] if hits else None
    if not npm:
        npm = shutil.which("npm")
    if not npm:
        return None
    argv = [npm, "root", "-g"]
    if npm.lower().endswith((".cmd", ".bat")):
        argv = ["cmd.exe", "/c", npm, "root", "-g"]
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        if out.returncode == 0:
            root = out.stdout.strip().splitlines()[-1].strip()
            # 被排除目录下的全局根一律不认，避免从被看护程序里捡出 codex。
            if root and (not markers or is_external(root, markers)):
                return root
    except Exception:
        return None
    return None


def external_markers(cfg: dict) -> list[str]:
    """绝不复用的运行时目录标记（来自 codex.avoid_paths，部署方按需填写）。"""
    return [s for s in (cfg["codex"].get("avoid_paths") or []) if s]


def is_external(path: str | None, markers: list[str]) -> bool:
    """路径是否落在被排除的目录之外。"""
    if not path:
        return False
    p = os.path.normpath(path).lower().replace("/", "\\")
    return not any(m.lower().replace("/", "\\") in p for m in markers)


def path_candidates(name: str, markers: list[str]) -> list[str]:
    """按 PATH 顺序找出可执行文件，跳过被排除目录。"""
    out: list[str] = []
    seen: set[str] = set()
    exts = [""] if os.path.splitext(name)[1] else ["", ".exe", ".cmd", ".bat"]
    for raw in (os.environ.get("PATH") or "").split(os.pathsep):
        d = raw.strip().strip('"')
        if not d:
            continue
        for ext in exts:
            cand = os.path.join(d, name + ext)
            try:
                key = os.path.normcase(os.path.abspath(cand))
            except Exception:
                continue
            if key in seen:
                continue
            seen.add(key)
            if os.path.isfile(cand) and is_external(cand, markers):
                out.append(cand)
    return out


def node_candidates(cfg: dict) -> list[str]:
    """node 可执行文件候选，独立安装优先。"""
    markers = external_markers(cfg)
    out: list[str] = []

    for explicit in (cfg["codex"].get("node"), os.environ.get("HOUMAI_NODE")):
        if explicit and os.path.isfile(explicit) and is_external(explicit, markers):
            out.append(os.path.normpath(explicit))

    out += path_candidates("node", markers)

    # PATH 里可能只有被排除的 node，或干脆没有；再补常见安装位置。
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                 os.path.join(os.environ.get("LOCALAPPDATA") or "", "Programs")):
        if not base:
            continue
        for sub in ("nodejs", os.path.join("nodejs", "bin")):
            cand = os.path.join(base, sub, "node.exe")
            if os.path.isfile(cand) and is_external(cand, markers):
                out.append(os.path.normpath(cand))

    deduped: list[str] = []
    seen: set[str] = set()
    for c in out:
        k = os.path.normcase(c)
        if k not in seen:
            seen.add(k)
            deduped.append(c)
    return deduped


def global_roots_for(node: str, npm_root: str | None = None) -> list[str]:
    """给定 node 可执行文件，推出它可能对应的 npm 全局安装根。"""
    d = os.path.dirname(os.path.abspath(node))
    raw = [
        os.path.join(d, "node_global", "node_modules"),
        os.path.join(d, "node_modules"),
        os.path.join(d, "..", "node_global", "node_modules"),
        os.path.join(d, "..", "lib", "node_modules"),
        os.path.join(d, "..", "..", "lib", "node_modules"),
        os.path.join(os.environ.get("APPDATA") or "", "npm", "node_modules"),
        os.path.join(os.environ.get("ProgramFiles") or "", "nodejs", "node_modules"),
    ]
    if npm_root:
        raw.append(npm_root)
    out: list[str] = []
    seen: set[str] = set()
    for r in raw:
        if not r:
            continue
        n = os.path.normpath(r)
        if n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    return out


CODEX_JS = os.path.join("@openai", "codex", "bin", "codex.js")


def resolve_codex(cfg: dict) -> list[str] | None:
    """返回可执行的 codex 命令前缀（不含子命令）。

    优先级：显式配置 > 独立 node + codex.js > PATH 上的独立启动器。
    任何落在 avoid_paths 内的运行时都不会被采用。
    """
    explicit = cfg["codex"].get("command")
    if explicit:
        return list(explicit) if isinstance(explicit, list) else [explicit]

    markers = external_markers(cfg)
    nodes = node_candidates(cfg)
    npm_root = npm_global_root(markers) if nodes else None

    for node in nodes:
        # 同目录的全局根先试，命中就不必再问 npm。
        for root in global_roots_for(node, npm_root):
            js = os.path.join(root, CODEX_JS)
            if os.path.isfile(js):
                return [node, js]

    for name in ("codex.exe", "codex.cmd", "codex.bat"):
        hits = path_candidates(name, markers)
        if hits:
            if hits[0].lower().endswith(".exe"):
                return [hits[0]]
            return ["cmd.exe", "/c", hits[0]]
    return None


def codex_env(cfg: dict) -> dict:
    """配置里指定的额外环境变量（代理之类），传给 Codex 子进程。"""
    extra = cfg["codex"].get("env") or {}
    return {str(k): str(v) for k, v in extra.items()}


def proxy_view(cfg: dict) -> dict:
    """看清 Codex 将要走的网络出口，便于判断"换个环境还能不能联网"。"""
    keys = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")
    merged = dict(os.environ)
    merged.update(codex_env(cfg))
    found = {}
    for k in keys:
        v = merged.get(k)
        if v:
            found[k] = v
    return {"proxy": found, "has_proxy": bool(found),
            "from_config": sorted(codex_env(cfg))}


def collect_runtime(cfg: dict) -> dict:
    """当前解释器与 node 是否独立于被排除目录。"""
    markers = external_markers(cfg)
    py = sys.executable or ""
    cmd = resolve_codex(cfg) or []
    node = None
    for part in cmd:
        if os.path.basename(part).lower().startswith("node"):
            node = part
            break
    return {
        "python": py,
        "python_external": is_external(py, markers),
        "node": node,
        "node_external": is_external(node, markers) if node else None,
        "codex": cmd,
        "codex_via_launcher": bool(cmd) and not node,
        "markers": markers,
    }


def toml_string(value: str) -> str:
    """TOML 字符串字面量。

    Windows 路径优先用单引号字面量串（不处理转义），免去反斜杠要写成双份的麻烦；
    路径里真的含单引号时再退回双引号 + 转义。
    """
    if "'" not in value:
        return "'" + value + "'"
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def sandbox_overrides(ctx: dict) -> list[str]:
    """把原轮次沙箱策略里 `-s <type>` 之外的约束补回命令行。

    为什么必须补：`-s workspace-write` 的默认可写范围是「工作目录 + 系统临时目录」。
    原轮次若把临时目录排除掉（`exclude_*`），只传类型就等于**把临时目录重新放开**，
    是提升权限；`writable_roots` 则是追加项，不补会让任务写不进原先能写的地方。
    （语义经 `codex sandbox` 实测确认：writable_roots 是追加而非替换。）

    键名与语法取自 codex 自己的 `--help`（点号路径 + TOML 值），
    并经 `codex sandbox` 对照实验证明真的生效，不是照文档猜的。
    """
    policy = ctx.get("sandbox_policy") or {}
    if policy.get("type") != "workspace-write":
        return []          # 其它沙箱类型没有这些附加项

    out: list[str] = []
    roots = policy.get("writable_roots")
    if isinstance(roots, list) and roots:
        joined = ",".join(toml_string(str(r)) for r in roots if r)
        if joined:
            out += ["-c", f"sandbox_workspace_write.writable_roots=[{joined}]"]

    for key in ("network_access", "exclude_tmpdir_env_var", "exclude_slash_tmp"):
        raw = policy.get(key)
        if raw is None:
            continue
        if isinstance(raw, bool):
            val = raw
        elif key.startswith("exclude_"):
            val = bool(raw)     # 拿不准时按"排除"处理：宁窄不宽
        else:
            continue            # 取值反常就不传，类型默认更窄，不会放宽
        out += ["-c", f"sandbox_workspace_write.{key}={str(val).lower()}"]
    return out


def review_unconfirmed(task: dict, task_files: list[str], now: float) -> str:
    """判断"待确认"任务本轮应当如何处理。

    返回值：
      - "confirmed"：会话文件里出现了新轮次（任务真的接回了）
      - "expired"  ：复查截止时间已到（转人工）
      - "pending"  ：还没到时间，本轮不动作
    把判定单独抽出来，方便测试覆盖。
    """
    if progressed_anywhere(task_files, task.get("last_attempt_at") or 0):
        return "confirmed"
    if now >= (task.get("confirm_deadline_at") or 0):
        return "expired"
    return "pending"


def build_resume_argv(cfg: dict, cmd: list[str], task: dict) -> list[str]:
    ctx = task.get("context") or {}
    argv = list(cmd) + ["exec", "--skip-git-repo-check"]
    cwd = ctx.get("cwd") or task.get("cwd")
    if cwd:
        argv += ["-C", cwd]
    if ctx.get("model"):
        argv += ["-m", ctx["model"]]
    if ctx.get("effort"):
        argv += ["-c", f'model_reasoning_effort="{ctx["effort"]}"']
    if ctx.get("approval_policy"):
        argv += ["-c", f'approval_policy="{ctx["approval_policy"]}"']
    if ctx.get("approvals_reviewer"):
        argv += ["-c", f'approvals_reviewer="{ctx["approvals_reviewer"]}"']
    sandbox = (ctx.get("sandbox_policy") or {}).get("type")
    if sandbox in ("read-only", "workspace-write", "danger-full-access"):
        argv += ["-s", sandbox]
    argv += sandbox_overrides(ctx)
    argv += ["resume", task["root_id"], cfg["retry"]["prompt"]]
    return argv


def run_resume(cfg: dict, task: dict, dry_run: bool = False) -> dict:
    cmd = resolve_codex(cfg)
    if not cmd:
        return {"ok": False, "reason": "codex_cli_not_found"}
    argv = build_resume_argv(cfg, cmd, task)
    if dry_run:
        return {"ok": True, "dry_run": True, "argv": argv}
    env = dict(os.environ)
    # 先复刻原轮次的时区，再叠加配置里的显式覆盖——人工指定优先，
    # 因为主人自己的启动脚本也会强制 TZ，默认值不该压过显式设定。
    tz = (task.get("context") or {}).get("timezone")
    if tz:
        env["TZ"] = tz
    env.update(codex_env(cfg))
    started = time.time()
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=1800, env=env)
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "timeout", "argv": argv}
    except Exception as exc:
        return {"ok": False, "reason": f"spawn_failed: {exc}", "argv": argv}
    tail = ((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()
    reason = None
    if proc.returncode != 0:
        joined = "\n".join(tail)
        if "already has an active writer" in joined:
            # 会话正被 Codex 桌面端打开着，线程被写锁占用，CLI 续跑进不去。
            reason = "thread_locked"
        else:
            reason = f"exit {proc.returncode}"
    return {
        "ok": proc.returncode == 0,
        "reason": reason,
        "returncode": proc.returncode,
        "elapsed": time.time() - started,
        "tail": tail[-6:],
        "argv": argv,
    }


def try_ipc_resume(cfg: dict, task: dict, pipe: str | None = None) -> dict | None:
    """尝试用桌面直跑通道把「继续」送进正被 Codex Desktop 持有的会话。

    返回 None 表示此路不通（未启用 / 桌面没运行 / 桌面没持有该会话），
    调用方应回落到 codex exec 通道；返回 dict 表示已经走了桌面通道，
    无论成败都不再走 exec——同一故障只能投递一次，绝不能两条通道都发。
    注意：本函数会真实发送文本，演练模式（dry_run）下调用方必须跳过。
    """
    channel = (cfg.get("retry") or {}).get("channel") or "auto"
    if channel == "exec":
        return None
    if ipc_client is None:
        if channel == "ipc":
            return {"ok": False, "via": "ipc", "reason": "ipc_unavailable"}
        return None
    res = ipc_client.desktop_resume(task["root_id"], cfg["retry"]["prompt"],
                                    endpoint=pipe)
    st = res.get("status")
    short = task["root_id"][:8]
    if st in ("unavailable", "no_owner"):
        why = "桌面未持有该会话" if st == "no_owner" else f"桌面通道不可用（{res.get('error')}）"
        if channel == "ipc":
            # 强制只用桌面通道时，不可用就是失败，不许悄悄换道。
            return {"ok": False, "via": "ipc", "reason": "ipc_unavailable"}
        log(f"任务 {short} {why}，改走命令行通道")
        return None
    if st in ("sent", "unknown"):
        if st == "unknown":
            # 回执超时但 turn 可能已启动（实机验证过），只能核对不能重发。
            log(f"任务 {short} 已向桌面窗口送达「继续」，回执未确认，稍后按新轮次核对，不重发")
        else:
            log(f"任务 {short} 已通过桌面窗口送达「继续」")
        return {"ok": True, "via": "ipc", "ipc_status": st}
    return {"ok": False, "via": "ipc",
            "reason": f"ipc_rejected: {res.get('error')}"}


def pick_resume(cfg: dict, task: dict) -> dict | None:
    """选一条续跑通道并发送，按会话来源分流。

    - 桌面端（Codex Desktop）发起：只在桌面直跑通道续跑，**绝不兜底到 CLI**——
      否则 CLI 会抢写锁，把会话从桌面手里夺走，桌面再打开就报「已在另一个应用中打开」。
      桌面此刻接不住（桌面没运行 / 未持有该会话）即返回 None，留待下一轮，不去碰 CLI。
    - CLI（codex-tui）发起：**直接走命令行通道，根本不试桌面**——它本就没在桌面开着，
      试桌面只是浪费一轮 IPC 握手再回落。
    - 来源不明：沿用 auto 安全默认（桌面优先、命令行兜底）。
    retry.channel 显式配置可覆盖来源：exec 全走命令行、ipc 全走桌面。
    返回 None 仅出现在「桌面会话且桌面此刻接不住」这一种情形，调用方应跳过本轮。
    """
    channel = (cfg.get("retry") or {}).get("channel") or "auto"
    origin = (task.get("context") or {}).get("originator")
    if channel == "exec":
        return run_resume(cfg, task)
    if channel == "ipc":
        return try_ipc_resume(cfg, task)
    # channel == "auto"：按来源分流
    if origin == "Codex Desktop":
        return try_ipc_resume(cfg, task)          # 只走桌面，不兜底 CLI
    if origin == "codex-tui":
        return run_resume(cfg, task)              # 直接 CLI，不试桌面
    res = try_ipc_resume(cfg, task)               # 来源不明：桌面优先、命令行兜底
    if res is None:
        res = run_resume(cfg, task)
    return res


def verify_after(state: State, cfg: dict, task: dict, since: float) -> str:
    """续跑后核对会话是否正常开始新轮次。返回 ok / still_limited / unknown。"""
    files = [f for f in (task.get("files") or [task.get("file")]) if f and os.path.exists(f)]
    if not files:
        return "unknown"
    saw_new_turn = False
    for fp in files:
        try:
            size = os.path.getsize(fp)
            with open(fp, "r", encoding="utf-8", errors="replace") as f:
                f.seek(max(0, size - 600_000))
                tail = f.read()
        except OSError:
            continue
        for line in tail.splitlines():
            if '"task_complete"' not in line and '"turn_context"' not in line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if parse_iso(rec.get("timestamp") or "") < since:
                continue
            if kind_of(rec) == "turn_context":
                saw_new_turn = True
            if kind_of(rec) == "task_complete":
                err = (rec.get("payload") or {}).get("error") or {}
                if err.get("codex_error_info") == QUOTA_ERROR:
                    return "still_limited"
                saw_new_turn = True
    return "ok" if saw_new_turn else "unknown"


def kind_of(rec: dict) -> str:
    """记录的"种类"，兼容两种存放位置。

    真实 codex 会话里两类记录的字段位置不同：
      · 事件类（task_complete / token_count）→ payload.type
      · 结构类（turn_context / session_meta）→ 顶层 type
    只看一处必然漏掉另一半，而漏掉的正好是 turn_context —— 续跑就没有
    model / effort / cwd 了。所以这里统一取值，调用方别再自己判断。
    """
    payload = rec.get("payload") or {}
    return payload.get("type") or rec.get("type") or ""


def context_from_payload(payload: dict) -> dict:
    return {
        "cwd": payload.get("cwd"),
        "model": payload.get("model"),
        "effort": payload.get("effort"),
        "approval_policy": payload.get("approval_policy"),
        "approvals_reviewer": payload.get("approvals_reviewer"),
        "sandbox_policy": payload.get("sandbox_policy"),
        "timezone": payload.get("timezone"),
    }


def last_turn_context(path: str, max_bytes: int = 800_000) -> dict | None:
    """从会话文件尾部取最后一个轮次上下文。"""
    if not path or not os.path.exists(path):
        return None
    try:
        size = os.path.getsize(path)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()
            data = f.read()
    except OSError:
        return None
    found = None
    for line in data.splitlines():
        if '"turn_context"' not in line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if kind_of(rec) == "turn_context":
            found = context_from_payload(rec.get("payload") or {})
    return found


def files_for_session(state: State, root_id: str) -> list[str]:
    """一个会话可能拆成多个记录文件，全部纳入。"""
    if not root_id:
        return []
    return [fp for fp, meta in state.data["sessions"].items()
            if meta.get("own_id") == root_id or meta.get("session_id") == root_id]


def newest_file(files: list[str]) -> str | None:
    alive = [fp for fp in files if os.path.exists(fp)]
    if not alive:
        return None
    return max(alive, key=os.path.getmtime)


def context_for_root(state: State, root_id: str) -> dict | None:
    """根会话最近的执行上下文，按文件新旧顺序回退。"""
    files = files_for_session(state, root_id)
    if not files:
        return None
    files.sort(key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0, reverse=True)
    for fp in files:
        ctx = last_turn_context(fp)
        if ctx and ctx.get("model"):
            return ctx
    return None


def progressed_anywhere(files: list[str], failed_at: float) -> float | None:
    for fp in files:
        ts = progressed_after(fp, failed_at)
        if ts:
            return ts
    return None


# --------------------------------------------------------------------------
# 提醒
# --------------------------------------------------------------------------

def tail_records(path: str, max_bytes: int = 800_000) -> list[dict]:
    """读取会话文件尾部的终态事件，用于判断任务是否已经自行继续。"""
    if not path or not os.path.exists(path):
        return []
    try:
        size = os.path.getsize(path)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()
            data = f.read()
    except OSError:
        return []
    out = []
    for line in data.splitlines():
        if '"task_complete"' not in line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def progressed_after(file_path: str, failed_at: float) -> float | None:
    """失败之后若已存在终态事件，说明任务已自行继续，返回该事件时间。"""
    if not file_path or not failed_at:
        return None
    for rec in tail_records(file_path):
        ts = parse_iso(rec.get("timestamp") or "")
        if ts > failed_at + 5:
            return ts
    return None


def notify(cfg: dict, state: State, kind: str, title: str, body: str, dedup_key: str | None = None) -> None:
    if kind not in (cfg["notify"].get("on") or []):
        return
    provider = (cfg["notify"].get("provider") or "none").lower()
    if provider == "serverchan" and not cfg["notify"].get("serverchan_key"):
        log("已选择 Server酱但未配置 key，跳过提醒")
        return
    if provider == "none":
        log(f"(未配置提醒通道) {title} — {body}")
        return
    if dedup_key:
        last = state.data["notified"].get(dedup_key)
        if last and time.time() - last < 6 * 3600:
            return
        state.data["notified"][dedup_key] = time.time()
    if provider == "serverchan":
        key = cfg["notify"]["serverchan_key"]
        data = urllib.parse.urlencode({"title": title, "desp": body}).encode()
        url = f"https://sctapi.ftqq.com/{key}.send"
        try:
            with urllib.request.urlopen(url, data=data, timeout=20) as resp:
                log(f"提醒已发送：{title}（HTTP {resp.status}）")
        except Exception as exc:
            log(f"提醒发送失败：{exc}")


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

CODEX_HOSTS = ("chatgpt.com", "api.openai.com")


def probe_host(host: str, proxy: str | None = None, timeout: int = 8) -> tuple[bool, str]:
    """测试到 Codex 后端的可达性：建立连接并完成 TLS 握手。

    proxy 形如 http://127.0.0.1:7897；为 None 时测直连。
    """
    sock = None
    try:
        if proxy:
            parts = urllib.parse.urlsplit(proxy)
            phost, pport = parts.hostname, parts.port or 80
            if not phost:
                return False, f"代理地址无法解析：{proxy}"
            sock = socket.create_connection((phost, pport), timeout=timeout)
            sock.sendall(
                f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n".encode())
            head = sock.recv(4096)
            first = head.split(b"\r\n")[0].decode("latin-1", "replace") if head else "(无响应)"
            if b" 200" not in head.split(b"\r\n")[0]:
                return False, f"代理拒绝建立隧道 —— {first}"
        else:
            sock = socket.create_connection((host, 443), timeout=timeout)
        tls = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        tls.close()
        return True, "TLS 握手成功"
    except Exception as exc:
        return False, f"{type(exc).__name__}"
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


def cmd_netcheck(cfg: dict, state: State, args) -> int:
    """续跑要联网。这里回答一个问题：到时候连不连得上。"""
    pv = proxy_view(cfg)
    from_config = codex_env(cfg)
    proxy = (pv["proxy"].get("HTTPS_PROXY") or pv["proxy"].get("https_proxy")
             or pv["proxy"].get("HTTP_PROXY") or pv["proxy"].get("http_proxy"))
    source = "config" if from_config else ("会话变量" if pv["has_proxy"] else "无")

    ok = True
    print("houmai 连通性检查")
    print(f"  代理      {proxy or '未设置'}（来源：{source}）")
    print()
    print(f"  {'目标':<18}{'直连':<24}{'经代理'}")
    print("  " + "-" * 62)
    for host in CODEX_HOSTS:
        dok, dmsg = probe_host(host, None)
        if proxy:
            pok, pmsg = probe_host(host, proxy)
            ptext = ("✓ " + pmsg) if pok else ("✗ " + pmsg)
        else:
            pok, pmsg = None, ""
            ptext = "（未设置）"
        dtext = ("✓ " + dmsg) if dok else ("✗ " + dmsg)
        print(f"  {host:<18}{dtext:<24}{ptext}")

    print()
    if proxy:
        all_ok = all(probe_host(h, proxy)[0] for h in CODEX_HOSTS)
        if all_ok:
            print(f"  结论：经代理 {proxy} 可以连上 Codex 后端，续跑具备联网条件。")
            print("        前提是那个时候代理开着；代理关掉就会像直连一样失败。")
        else:
            ok = False
            print(f"  结论：代理 {proxy} 连不上 Codex 后端，续跑会失败。")
            print("        请确认代理已启动、端口正确，然后在 config.json 的 codex.env 里改。")
    else:
        all_ok = all(probe_host(h, None)[0] for h in CODEX_HOSTS)
        if all_ok:
            print("  结论：直连可用，续跑具备联网条件。")
        else:
            ok = False
            print("  结论：直连不通，且未配置探测代理。")
            print("        注意：实测 codex 不读取 env 代理变量，联网走系统代理 ——")
            print("        系统代理开着时续跑可能仍然可用，以真实轮次为准。")
            print("        把代理写进 codex.env 只是让本检查多测一列「经代理」，例如：")
            print('          "env": {"HTTPS_PROXY": "http://127.0.0.1:7897"}')
    return 0 if ok else 2


# 终态任务：已彻底了结（succeeded/superseded）或球在主人这边（abandoned/notify_only）。
# pending 只装活跃任务，终态按 prune_resolved 的规则摘除，审计交给 events.jsonl。
TERMINAL_STATUSES = ("succeeded", "superseded", "abandoned", "notify_only")
# 已摘任务 turn_id 的记忆上限。done 列表 >500 时只留尾部 200；这里给足余量，
# 兜住"offset 丢失 + done 裁尾"双故障叠加时的假重检。2000 条约几十 KB，可忽略。
RESOLVED_TURNS_CAP = 2000


def keep_until_at(cfg: dict, task: dict) -> float | None:
    """放弃续跑/仅提醒任务的"保留至"时刻；不需要在面板标注的返回 None。

    已接回/自行继续不标注：它们次一轮就摘，卡片存在的那几分钟里标归档时间是噪声。
    """
    if task.get("status") not in ("abandoned", "notify_only"):
        return None
    resolved_at = task.get("resolved_at") or 0
    if not resolved_at:
        return None
    retention = float((cfg.get("retry") or {}).get("resolved_retention_days", 7) or 0)
    if retention <= 0:
        return None
    return resolved_at + retention * 86400


def prune_resolved(cfg: dict, state: State, now: float) -> int:
    """把终态任务从 pending 摘除，返回摘除个数。

    - succeeded / superseded：已彻底了结，进终态的次一轮即摘——在册留约一轮，
      面板上"已接回"的卡片能被看到，再消失。
    - abandoned / notify_only：球在主人这边，过 retry.resolved_retention_days
      （默认 7 天，0 = 永不摘）再摘，给主人留出看到它的时间。

    每次摘除记 pruned 事件（摘除不许静默）；被摘任务的 turn_id 进 resolved_turns，
    scan 据此跳过，防止旧中断被当成新的再来一遍。
    """
    retention = float((cfg.get("retry") or {}).get("resolved_retention_days", 7) or 0)
    pruned = 0
    for key in list(state.data.get("pending") or {}):
        task = state.data["pending"][key]
        st = task.get("status")
        if st not in TERMINAL_STATUSES:
            continue
        resolved_at = task.get("resolved_at") or 0
        if st in ("succeeded", "superseded"):
            due = True
        elif retention <= 0:
            due = False
        else:
            due = bool(resolved_at) and now - resolved_at >= retention * 86400
        if not due:
            continue
        tid = task.get("turn_id")
        if tid:
            turns = state.data.setdefault("resolved_turns", {})
            turns[tid] = now
            if len(turns) > RESOLVED_TURNS_CAP:
                for old in sorted(turns, key=lambda k: turns.get(k, 0))[
                        :len(turns) - RESOLVED_TURNS_CAP]:
                    turns.pop(old, None)
        state.record("pruned", root_id=task.get("root_id"), status=st,
                     resolved_at=resolved_at or None,
                     kept_days=round((now - resolved_at) / 86400, 1) if resolved_at else None)
        when = f"结案于 {fmt_delta(now - resolved_at)}前" if resolved_at else "结案时间未知"
        log(f"任务 {(task.get('root_id') or '')[:8]} 已从在册摘除（{st}，{when}）")
        del state.data["pending"][key]
        pruned += 1
    return pruned


def cmd_run(cfg: dict, state: State, args) -> int:
    now = time.time()
    log(f"—— 值守轮次开始（{'演练' if args.dry_run else '真实'}）——")

    # 顺手打扫：面板被硬关窗口时 pid 文件会留尸（finally 跑不到），每轮清一次。
    # 不打日志——干净的 state 目录不值得一条轮次日志，只有异常才值得说话。
    try:
        clean_stale_panel_pid(cfg)
    except Exception:
        pass

    # 护栏：看护工具跟着被看护对象一起死是最糟的失效方式——它安静且致命。
    # 这里只查解释器（不触发子进程），Node 的独立性由 check / health 负责。
    if not is_external(sys.executable, external_markers(cfg)):
        log(f"警告：当前解释器来自被排除目录（{sys.executable}），"
            f"被看护程序一旦卸载或搬迁，值守会静默失效。请双击 houmai.cmd 选 3 重新注册。")

    incidents = scan(cfg, state, verbose=args.verbose)
    incidents.sort(key=lambda i: (1 if i.get("is_subagent") else 0, i.get("at") or 0))
    fresh = 0
    for inc in incidents:
        if not inc["turn_id"]:
            continue
        key = f"{inc['root_id']}"
        if inc["turn_id"] in state.data["done"]:
            continue
        # 已摘除任务的轮次：done 列表会裁尾（>500 时只留 200），一旦叠加 offset
        # 丢失，旧中断就可能被当成新的再来一遍。resolved_turns 容量上限远大于
        # done 的裁剪尾，专门兜住这道双故障叠加；按 turn_id 挡，不挡同会话的新中断。
        if inc["turn_id"] in (state.data.get("resolved_turns") or {}):
            continue
        if inc["root_id"] in (cfg["watch"].get("ignore_threads") or []):
            continue
        if inc.get("first_sight") and inc["at"] < now - cfg["watch"]["initial_lookback_hours"] * 3600:
            state.data["done"].append(inc["turn_id"])
            state.record("skipped_stale", root_id=inc["root_id"],
                         age_hours=round((now - inc["at"]) / 3600, 1))
            log(f"跳过历史中断记录：{inc['root_id'][:8]}（{(now - inc['at']) / 3600:.1f} 小时前）")
            continue
        info = classify(inc.get("rate_limits"))
        if info["kind"] == "unknown":
            # 本会话没带额度数据（偶发）。额度窗口是账号级的，借最近一次观测到的
            # 账号额度来判定与排期；借不到（或已过期）才转"仅提醒"。
            borrow = classify(latest_known_rate_limits(state))
            if borrow["kind"] != "unknown" and (borrow.get("resets_at") or 0) > now:
                info = dict(borrow)
                info["borrowed"] = True
        files = files_for_session(state, inc["root_id"]) or [inc["file"]]
        root_file = newest_file(files) or inc["file"]
        already = progressed_anywhere(files, inc["at"])
        if already:
            state.data["done"].append(inc["turn_id"])
            state.record("already_progressed", root_id=inc["root_id"], resumed_at=already)
            log(f"任务在中断后已自行继续，无需接管：{inc['root_id'][:8]}（{fmt_local(already)} 起）")
            continue
        existing = state.data["pending"].get(key)
        if existing:
            if existing.get("turn_id") == inc["turn_id"] or \
                    now - existing.get("detected_at", 0) < cfg["retry"]["coalesce_minutes"] * 60:
                state.data["done"].append(inc["turn_id"])
                continue
        ctx = context_for_root(state, inc["root_id"]) or last_turn_context(root_file) or inc.get("context") or {}
        # 会话来源（桌面版 / CLI）：决定续跑走哪条通道，也供面板展示。
        # 取自会话 meta（read_meta 已解析）；meta 缺失则留空，后续按 auto 处理。
        sess_meta = state.data.get("sessions", {}).get(root_file) or {}
        origin = sess_meta.get("originator")
        if origin:
            ctx = dict(ctx); ctx["originator"] = origin
        task = {
            "root_id": inc["root_id"],
            "session_id": inc["session_id"],
            "file": root_file,
            "files": files,
            "incident_file": inc["file"],
            "turn_id": inc["turn_id"],
            "detected_at": now,
            "failed_at": inc["at"],
            "message": inc["message"],
            "window": info,
            "context": ctx,
            "originator": origin,
            "cwd": ctx.get("cwd") or inc.get("cwd"),
            "attempts": 0,
            "status": "waiting",
        }
        long_skip = long_cycle_skip_reason(cfg, info, now) if info["kind"] == "long_cycle" else None
        if not info.get("resets_at"):
            # 额度信息缺失：会话里读不到窗口/重置时间，无法排期续跑；
            # 与长周期是两回事，必须分开记录，否则标签在撒谎。
            task["status"] = "notify_only"
            task["resolved_at"] = now
            notify(cfg, state, "unknown_reset",
                   "候脉：额度信息缺失，无法安排自动续跑",
                   f"任务 {inc['root_id'][:8]} 的会话里读不到额度窗口与重置时间，"
                   f"无法确定何时续跑。按安全策略只提醒，请人工处理。",
                   dedup_key=f"unk:{inc['root_id']}")
            state.record("unknown_reset", root_id=inc["root_id"], window=info)
        elif long_skip:
            # 长周期额度：默认也排期续跑；但关掉开关、或重置时间超出等待上限时，
            # 退回"仅提醒"（理由记进事件，别让面板猜）。
            task["status"] = "notify_only"
            task["resolved_at"] = now
            task["notify_reason"] = long_skip
            days = max(1, round((info.get("window_minutes") or 0) / 1440))
            wait = fmt_delta(info["resets_at"] - now)
            why = "等待超出上限" if long_skip == "beyond_max_wait" else "长周期自动续跑已关闭"
            notify(cfg, state, "long_cycle",
                   "候脉：长周期额度耗尽，未自动排期",
                   f"任务 {inc['root_id'][:8]} 撞上 {days} 天额度窗口，约 {wait} 后重置。"
                   f"{why}，按策略只提醒，请人工决定。",
                   dedup_key=f"long:{inc['root_id']}")
            state.record("long_cycle", root_id=inc["root_id"], window=info, reason=long_skip)
        else:
            task["retry_at"] = info["resets_at"] + cfg["retry"]["buffer_seconds"]
            src = "（额度窗口取自同账号最近观测）" if info.get("borrowed") else ""
            long_note = ""
            if info["kind"] == "long_cycle":
                days = max(1, round((info.get("window_minutes") or 0) / 1440))
                long_note = f"（长周期窗口 {days} 天，已按配置排期）"
            notify(cfg, state, "detect",
                   "候脉：检测到额度耗尽，已进入等待",
                   f"任务 {inc['root_id'][:8]} 额度耗尽，将于 {fmt_local(task['retry_at'])} "
                   f"（约 {fmt_delta(task['retry_at'] - now)}后）自动续跑。{src}{long_note}",
                   dedup_key=f"detect:{inc['root_id']}")
            state.record("detected", root_id=inc["root_id"], window=info,
                         retry_at=task["retry_at"], context=task.get("context"))
        state.data["done"].append(inc["turn_id"])
        state.data["pending"][key] = task
        fresh += 1
    if len(state.data["done"]) > 500:
        state.data["done"] = state.data["done"][-200:]
    for fp in list(state.data["offsets"]):
        if not os.path.exists(fp):
            state.data["offsets"].pop(fp, None)
            state.data["sessions"].pop(fp, None)
            state.data.get("_ctx", {}).pop(fp, None)
    log(f"扫描完成：新增额度中断 {fresh} 个，在册 {len(state.data['pending'])} 个")

    # 终态任务摘除：pending 只装还要盯的。演练模式不摘——演练承诺零副作用，
    # 只写 last_argv 与轮次摘要，不碰 pending、不写事件。
    pruned = 0
    if not args.dry_run:
        pruned = prune_resolved(cfg, state, now)

    acted = 0
    for key, task in list(state.data["pending"].items()):
        task_files = [f for f in (task.get("files") or [task.get("file")]) if f]

        # 续跑已发出、但当时没看到新轮次：只复查，不重发——重发等于同一任务跑两遍。
        if task.get("status") == "unconfirmed":
            review = review_unconfirmed(task, task_files, now)
            if review == "confirmed":
                task["status"] = "succeeded"
                task["resolved_at"] = now
                state.record("resumed", root_id=task["root_id"], attempts=task.get("attempts", 0),
                             verdict="ok", elapsed=None, confirmed_at=time.time(),
                             model=(task.get("context") or {}).get("model"))
                notify(cfg, state, "success",
                       "候脉：额度恢复，任务已接回",
                       f"任务 {task['root_id'][:8]} 已在额度窗口重置后接回并开始新轮次。",
                       dedup_key=f"success:{task['root_id']}:{task.get('turn_id')}")
                log(f"续跑复查确认：{task['root_id'][:8]}")
                acted += 1
            elif review == "expired":
                task["status"] = "abandoned"
                task["resolved_at"] = now
                state.record("abandoned", root_id=task["root_id"], attempts=task.get("attempts", 0),
                             reason="unconfirmed")
                notify(cfg, state, "failure",
                       "候脉：续跑结果未确认，需要人工处理",
                       f"任务 {task['root_id'][:8]} 的续跑已发出，但始终没看到新轮次；"
                       f"既不能算成功也不敢重发（怕跑两遍），请人工看一眼。",
                       dedup_key=f"unconfirmed:{task['root_id']}:{task.get('turn_id')}")
                log(f"续跑结果始终未确认，转人工：{task['root_id'][:8]}")
            else:
                left = fmt_delta((task.get("confirm_deadline_at") or 0) - now)
                log(f"任务 {task['root_id'][:8]} 续跑结果待确认（还有 {left}），本轮不重发")
            continue

        if task.get("status") not in ("waiting", "retrying"):
            continue
        if now < task.get("retry_at", 0):
            continue
        already = progressed_anywhere(task_files, task.get("failed_at") or 0)
        if already:
            task["status"] = "superseded"
            task["resolved_at"] = now
            state.record("superseded", root_id=task["root_id"], progressed_at=already)
            log(f"任务 {task['root_id'][:8]} 已自行继续，取消本次续跑")
            continue
        alive = [f for f in task_files if os.path.exists(f)]
        if alive:
            idle = now - max(os.path.getmtime(f) for f in alive)
            if idle < cfg["watch"]["silence_minutes"] * 60:
                log(f"任务 {task['root_id'][:8]} 会话仍在写入（静默 {idle / 60:.1f} 分钟），本轮跳过")
                continue
        ctx = task.get("context") or {}
        log(f"开始续跑：{task['root_id'][:8]}（第 {task['attempts'] + 1} 次，模型 {ctx.get('model')}）")
        since = time.time()
        if args.dry_run:
            # 演练模式只演示 exec 通道的命令行拼装；桌面通道会真实送达，
            # 不能在演练里触发。
            result = run_resume(cfg, task, dry_run=True)
            log(f"演练模式，未真正发送：{' '.join(result.get('argv', []))}")
            task["last_argv"] = result.get("argv")
            # 演练不动任何计数：没发送就没有"尝试"。否则 attempts 会被顶高，
            # 面板出现查无对证的"已尝试 N 次"，日志里也找不到对应动作。
            continue
        result = pick_resume(cfg, task)
        if result is None:
            # 桌面端会话此刻无法经桌面通道续跑（桌面没运行 / 未持有该会话）：
            # 按主人要求不兜底 CLI，留到下一轮再试，避免 CLI 抢锁把会话从桌面夺走。
            log(f"任务 {task['root_id'][:8]} 桌面会话暂无法经桌面续跑，本轮跳过，留待下一轮")
            continue
        task["attempts"] = task.get("attempts", 0) + 1
        task["last_attempt_at"] = since
        verdict = verify_after(state, cfg, task, since) if result.get("ok") else "failed"
        if result.get("ok") and verdict == "ok":
            task["status"] = "succeeded"
            task["resolved_at"] = now
            task["result"] = result
            state.record("resumed", root_id=task["root_id"], attempts=task["attempts"],
                         verdict=verdict, elapsed=result.get("elapsed"), model=(task.get("context") or {}).get("model"),
                         via=result.get("via"))
            notify(cfg, state, "success",
                   "候脉：额度恢复，任务已接回",
                   f"任务 {task['root_id'][:8]} 已在额度窗口重置后接回并正常开始新轮次。",
                   dedup_key=f"success:{task['root_id']}:{task['turn_id']}")
            log(f"续跑成功：{task['root_id'][:8]}（{verdict}）")
            acted += 1
        elif result.get("ok") and verdict == "unknown":
            # 进程退 0，但没看到新轮次。既不能算成功（可能是"什么也没做"），
            # 也不能立刻重发（真接回了就会跑两遍）。留成待确认，由后续轮次复查。
            task["status"] = "unconfirmed"
            task["confirm_deadline_at"] = time.time() + \
                cfg["retry"]["confirm_deadline_minutes"] * 60
            state.record("resumed", root_id=task["root_id"], attempts=task["attempts"],
                         verdict="unknown", elapsed=result.get("elapsed"),
                         model=(task.get("context") or {}).get("model"),
                         via=result.get("via"))
            log(f"续跑已发出但尚未看到新轮次：{task['root_id'][:8]}"
                f"（{cfg['retry']['confirm_deadline_minutes']} 分钟内复查，期间不重发）")
        else:
            reason = result.get("reason") or verdict
            backoff = cfg["retry"]["backoff_seconds"]
            idx = min(task["attempts"] - 1, len(backoff) - 1)
            if task["attempts"] >= cfg["retry"]["max_attempts"]:
                task["status"] = "abandoned"
                task["resolved_at"] = now
                state.record("abandoned", root_id=task["root_id"], attempts=task["attempts"],
                             reason=reason, tail=result.get("tail"), via=result.get("via"))
                notify(cfg, state, "failure",
                       "候脉：续跑失败，需要人工处理",
                       f"任务 {task['root_id'][:8]} 续跑 {task['attempts']} 次未成功（{reason}）。",
                       dedup_key=f"failure:{task['root_id']}:{task['turn_id']}")
                log(f"续跑失败并放弃：{task['root_id'][:8]}（{reason}）")
            else:
                task["retry_at"] = time.time() + backoff[idx]
                task["status"] = "retrying"
                state.record("retry", root_id=task["root_id"], attempts=task["attempts"],
                             reason=reason, next_at=task["retry_at"], via=result.get("via"))
                # 失败原因要能追查：把 codex 的报错尾行落进日志，否则只有一句 failed。
                tail = result.get("tail") or []
                hint = next((ln for ln in reversed(tail) if "ERROR" in ln or "Error" in ln), "")
                log(f"续跑未成功，将在 {backoff[idx]} 秒后重试：{reason}"
                    + (f" —— {hint[:200]}" if hint else ""))
    state.data.pop("_ctx", None)
    state.data["last_run_at"] = time.time()
    state.data["last_run_summary"] = {
        "incidents": fresh,
        "acted": acted,
        "pruned": pruned,
        "pending": len(state.data["pending"]),
        "dry_run": bool(args.dry_run),
    }
    state.save()
    log(f"本轮结束：触发续跑 {acted} 次")
    return 0


def codex_version(cmd: list[str]) -> str | None:
    try:
        proc = subprocess.run(list(cmd) + ["--version"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60)
    except Exception:
        return None
    text = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return text.splitlines()[0].strip() if text else None


def task_registered(name: str = TASK_NAME) -> bool | None:
    """计划任务是否已注册；无法查询时返回 None（非 Windows 或本机策略受限）。"""
    exe = shutil.which("schtasks")
    if not exe:
        return None
    try:
        proc = subprocess.run([exe, "/Query", "/TN", name], capture_output=True, timeout=30)
    except Exception:
        return None
    return proc.returncode == 0


def task_power_restriction(name: str = TASK_NAME) -> bool | None:
    """计划任务是否带"仅交流电"限制（拔电不启动、改用电池还会被停）。

    直接读任务 XML，而不是解析 `schtasks /V` 里的中文界面行：系统语言换一种也不会失配。
    查不到（非 Windows／策略受限／任务不存在）返回 None——不臆断，也就不会误报。
    """
    exe = shutil.which("schtasks")
    if not exe:
        return None
    try:
        proc = subprocess.run([exe, "/Query", "/TN", name, "/XML"],
                              capture_output=True, timeout=30)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    raw = proc.stdout or b""
    for enc in ("utf-8", "utf-16", "gbk"):
        try:
            text = raw.decode(enc)
        except Exception:
            continue
        if "<Task" in text:      # 解错编码时会变成 <\x00T\x00...，据此换下一种
            return xml_battery_restricted(text)
    return None


def xml_battery_restricted(xml_text: str) -> bool | None:
    """任务 XML 里任一电源开关为 true 即算受限；元素缺失视为不限制，空串返回 None。"""
    if not xml_text or not xml_text.strip():
        return None
    restricted = False
    for tag in ("DisallowStartIfOnBatteries", "StopIfGoingOnBatteries"):
        m = re.search(r"<%s>\s*([^<]*?)\s*</%s>" % (tag, tag), xml_text, re.IGNORECASE)
        if m and m.group(1).strip().lower() in ("true", "1"):
            restricted = True
    return restricted


def collect_health(cfg: dict, state: State) -> dict:
    """体检数据。终端与网页共用同一套判定，避免两处逻辑各说各话。"""
    now = time.time()
    checks: list[dict] = []
    ok = True

    def add(key: str, label: str, level: str, detail: str) -> None:
        nonlocal ok
        checks.append({"key": key, "label": label, "state": level, "detail": detail})
        if level == "fail":
            ok = False

    cmd = resolve_codex(cfg)
    version_text = codex_version(cmd) if cmd else None
    if not version_text:
        add("cli", "Codex CLI", "fail", "不可用：探测不到 codex 命令")
    else:
        cur = parse_version(version_text.split()[-1])
        need = parse_version(cfg["codex"]["min_version"])
        if cur < need:
            add("cli", "Codex CLI", "fail",
                f"版本过低（{version_text}），需要 ≥ {cfg['codex']['min_version']}")
        else:
            add("cli", "Codex CLI", "ok", f"可用（{version_text}）")

    registered = task_registered()
    if registered is True:
        # 光说"已注册"等于让主人自己猜这东西是干什么用的——卡片是给人看的
        # 地方，常态卡要顺带交代用途（措辞与 README 的用法说明保持一致）。
        # 失败/未知卡不带用途：那两张卡要给的是"怎么办"。
        add("task", "计划任务", "ok",
            f"已注册（{TASK_NAME}）—— 每 10 分钟无窗口跑一轮，检查额度中断并续跑")
        # 电源策略是"静默失效"的重灾区：任务显示已启用、注册也查得到，
        # 但拔掉电源后它根本不启动，体检会一直报正常——比明着失败更坏。
        if task_power_restriction() is True:
            add("power", "计划任务电源策略", "fail",
                "带电池限制：拔电后不会启动（改用电池时还会被停），值守会静默停摆"
                " —— 双击 houmai.cmd 选 3 重新注册")
    elif registered is False:
        add("task", "计划任务", "fail", "未注册 —— 双击 houmai.cmd 选 3")
    else:
        add("task", "计划任务", "unknown", "无法查询（本机策略限制或非 Windows）")

    # 运行时独立性是硬性检查，但路径是给排障的人看的，不是给面板看的：
    # Python／Node 合并成一张「运行时」卡——只在异常或无法确认时才出现；
    # 正常独立是常态，常态没有信息量，没消息就是好消息。
    rt = collect_runtime(cfg)
    if rt["node"]:
        node_state = "ok" if rt["node_external"] else "fail"
    elif rt["codex"]:
        node_state = "unknown"  # 由 codex 启动器自行解析，无法确认
    else:
        node_state = "unknown"  # 没有 CLI 可判断（CLI 检查那行会报）
    if not rt["python_external"] or node_state == "fail":
        problems = []
        if not rt["python_external"]:
            problems.append(f"Python 来自受限目录（{rt['python']}）"
                            f"—— 双击 houmai.cmd 选 3 重新注册即可纠正")
        if node_state == "fail":
            problems.append(f"Node 来自受限目录（{rt['node']}）"
                            f"—— 在 config.json 里指定 codex.node")
        add("runtime", "运行时", "fail", "；".join(problems))
    elif node_state == "unknown":
        add("runtime", "运行时", "unknown", "Node 由 codex 启动器自行解析，无法确认它用哪个")

    # 网络出口不进体检：codex 不读 env 代理（实测），这一项永远答不出"能不能联网"，
    # 是常驻的 unknown 噪声。真正的连通性判定交给 houmai netcheck。

    summary = state.data.get("last_run_summary") or {}
    dry = bool(summary.get("dry_run"))
    last = state.data.get("last_run_at")
    if not last:
        add("beat", "上次值守", "fail",
            "从未执行过 —— 双击 houmai.cmd 选 3 注册，等第一轮跑完即可")
    else:
        age = now - last
        mark = "（演练，未真实发送）" if dry else ""
        if age > 25 * 60:
            add("beat", "上次值守", "fail",
                f"{fmt_delta(age)}前（{fmt_local(last)}）{mark}，已超过两个周期，值守疑似异常"
                " —— 双击 houmai.cmd 选 3 修复；若只是刚重启过，下一轮会自动转绿")
        else:
            add("beat", "上次值守", "ok",
                f"{fmt_delta(age)}前（{fmt_local(last)}）{mark}，频率正常")

    tasks = []
    for t in (state.data.get("pending") or {}).values():
        rid = t.get("root_id") or ""
        win = t.get("window") or {}
        # 待确认的任务拿"复查截止"当倒计时锚点，面板上不至于没有时间参照。
        deadline = t.get("confirm_deadline_at") if t.get("status") == "unconfirmed" else None
        at_ts = deadline or t.get("retry_at")
        # 放弃/仅提醒的卡片标注"保留至"：让任务什么时候从在册消失可预期，
        # 而不是某天突然不见了才去翻事件表。
        keep_until = keep_until_at(cfg, t)
        tasks.append({
            "root_id": rid,
            "short": rid[:8],
            "status": t.get("status"),
            "attempts": t.get("attempts", 0),
            "retry_at": at_ts,
            "retry_in": (at_ts or 0) - now if at_ts else None,
            "next_at": fmt_local(at_ts) if at_ts else None,
            "confirm_deadline_at": deadline,
            "confirm_in": (deadline - now) if deadline else None,
            "keep_until_local": fmt_local(keep_until) if keep_until else None,
            "detected_at": t.get("detected_at"),
            "detected_local": fmt_local(t.get("detected_at")) if t.get("detected_at") else None,
            "cwd": t.get("cwd") or "",
            "window_minutes": win.get("window_minutes"),
            "message": (t.get("message") or "")[:300],
        })
    # 能自动续跑的排前面（那才是要盯的），仅提醒的靠后。
    tasks.sort(key=lambda x: (0 if x["status"] in ("waiting", "retrying", "unconfirmed") else 1,
                              x.get("retry_at") or 0))

    # 待人工：候脉已放弃续跑、球在主人这边。跟徽标（只报机制健康）分两层，
    # 这里只给个数，面板据此补一行琥珀提示。notify_only 已靠卡片琥珀胶囊暴露，不计入。
    attention = sum(1 for t in tasks if t.get("status") == "abandoned")

    # 展示窗口：默认只看近 3 天。events.jsonl 本身不删改，这里只是"少看点"。
    slice_ = split_events(state.recent(400), cfg, now)
    events = slice_["events"]
    cutoff = now - 24 * 3600
    counts: dict[str, int] = {}
    for ev in events:
        ts = ev.get("at") or now
        ev["ago"] = fmt_delta(now - ts)
        ev["local"] = fmt_local(ts)
        if ts >= cutoff:
            counts[ev.get("kind", "?")] = counts.get(ev.get("kind", "?"), 0) + 1

    lpath = log_file(cfg)
    has_log = os.path.exists(lpath)
    log_lines = [l.rstrip() for l in read_log_tail(lpath, 200)] if has_log else []

    # 顶部徽标要说出"是什么问题"，而不是笼统的"有待处理项"——
    # 主人看到红字就该同时知道怎么修。
    fail_keys = {c["key"]: c["state"] for c in checks}
    verdict = "值守正常"
    if not ok:
        verdict = next((text for key, text in (
            ("beat", "值守疑似异常"),
            ("task", "值守未注册"),
            ("power", "值守会被电源策略停摆"),
            ("cli", "Codex 不可用"),
            ("runtime", "运行环境异常"),
        ) if fail_keys.get(key) == "fail"), "有待处理项")

    h = {
        "now": now,
        "now_local": fmt_local(now),
        "now_clock": datetime.now(LOCAL_TZ).strftime("%H:%M:%S"),
        "version": VERSION,
        "ok": ok,
        "verdict": verdict,
        "checks": checks,
        "last_run_at": last,
        "last_run_local": fmt_local(last) if last else None,
        "last_run_ago": fmt_delta(now - last) if last else None,
        "last_run_summary": summary,
        "dry_run": dry,
        "pending": tasks,
        "attention": attention,
        # 事件表只看最新几条：底账不删，展示收敛。新在上（最新的最显眼）。
        "events": list(reversed(events[-PANEL_EVENT_LIMIT:])),
        "events_hidden": max(0, len(events) - min(PANEL_EVENT_LIMIT, len(events))),
        "counts_24h": counts,
        "history_days": slice_["history_days"],
        "events_filtered": slice_["dropped"],
        "events_merged": slice_["merged"],
        "stale_summary": slice_["stale_summary"],
        "log": log_lines,
        "log_mtime": os.path.getmtime(lpath) if has_log else None,
        "task_name": TASK_NAME,
        "runtime": rt,
        "min_version": cfg["codex"]["min_version"],
        "lookback_days": cfg["watch"]["lookback_days"],
        "notify_provider": cfg["notify"].get("provider"),
    }

    # 面板每 5 秒轮询一次，但内容没变时不该整页重画（任务卡会闪、倒计时会被重建）。
    # 这里给"值得渲染的内容"算一个指纹：剔掉纯时钟性的字段（含秒的倒计时、相对时间），
    # 前端指纹相同就只更新刷新时间戳，跳过 render。
    def _stable_task(t: dict) -> dict:
        return {k: v for k, v in t.items() if k not in ("retry_in", "confirm_in")}

    def _stable_event(e: dict) -> dict:
        return {k: v for k, v in e.items() if k != "ago"}

    rev_src = {
        "ok": h["ok"], "verdict": h["verdict"], "checks": h["checks"],
        "pending": [_stable_task(t) for t in h["pending"]],
        "events": [_stable_event(e) for e in h["events"]],
        "counts_24h": h["counts_24h"], "history_days": h["history_days"],
        "events_hidden": h["events_hidden"], "events_filtered": h["events_filtered"],
        "events_merged": h["events_merged"], "stale_summary": h["stale_summary"],
        "log": h["log"], "log_mtime": h["log_mtime"], "version": h["version"],
    }
    h["rev"] = hashlib.md5(json.dumps(
        rev_src, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()[:12]
    return h


def cmd_health(cfg: dict, state: State, args) -> int:
    h = collect_health(cfg, state)
    now = h["now"]
    print("houmai 值守体检")

    for chk in h["checks"]:
        mark = {"ok": "✓", "fail": "✗", "unknown": "·"}[chk["state"]]
        print(f"  {mark} {chk['label']}  {chk['detail']}")

    summary = h["last_run_summary"]
    if summary:
        tail = "（演练）" if h["dry_run"] else ""
        print(f"  · 上次结果  新增中断 {summary.get('incidents', 0)} 个，"
              f"触发续跑 {summary.get('acted', 0)} 次{tail}")

    tasks = h["pending"]
    waiting = [t for t in tasks if t.get("status") in ("waiting", "retrying")]
    unconfirmed = [t for t in tasks if t.get("status") == "unconfirmed"]
    if tasks:
        extra = f"，待确认 {len(unconfirmed)} 个" if unconfirmed else ""
        print(f"  · 在册任务  {len(tasks)} 个（等待中 {len(waiting)} 个{extra}）")
        if h["attention"]:
            print(f"      需人工处理 {h['attention']} 个（候脉已放弃续跑，见面板的琥珀提示）")
        for t in waiting:
            print(f"      待续跑 {t['short']} → {t['next_at']}（{fmt_delta(t['retry_in'])}后）")
        for t in unconfirmed:
            left = fmt_delta(t["confirm_in"]) if t.get("confirm_in") is not None else "未知"
            print(f"      待确认 {t['short']}：续跑已发出，尚未看到新轮次；"
                  f"复查截止 {t['next_at']}（{left}后），超时转人工")
    else:
        print("  · 在册任务  无")

    if h["counts_24h"]:
        parts = "，".join(f"{k} {v}" for k, v in
                         sorted(h["counts_24h"].items(), key=lambda kv: -kv[1]))
        print(f"  · 近 24 小时  {parts}")
        for ev in [e for e in h["events"] if e.get("kind") == "resumed"][:3]:
            print(f"      续跑 {ev.get('root_id', '')[:8]} → 核对结果 {ev.get('verdict')}")
    else:
        print("  · 近 24 小时  无事件（没有中断很正常）")

    window = f"  · 历史窗口  近 {h['history_days']:g} 天（改 panel.history_days 可调）"
    if h["events_filtered"]:
        window += f"，另有 {h['events_filtered']} 条更早记录未展示"
    print(window)
    if h["events_merged"]:
        print(f"      同类结论已合并 {h['events_merged']} 条（同一会话的多个中断轮次）")
    stale = h["stale_summary"]
    if stale:
        oldest = stale.get("oldest_hours")
        tail = f"，最早 {fmt_delta(oldest * 3600)}前" if oldest is not None else ""
        print(f"      已折叠 {stale['count']} 条历史中断跳过记录{tail}"
              "（判定噪声，panel.show_skipped_stale 可展开）")

    if h["log_mtime"]:
        print(f"  · 值守日志  {fmt_delta(now - h['log_mtime'])}前更新（用 houmai log 看）")
    else:
        print("  · 值守日志  未生成（run 跑过才会有，用 houmai log 看）")

    print()
    if h["ok"]:
        print("  结论：值守正常，无需处理。")
        if not tasks:
            print("        当前没有需要等待的任务；下一次真实额度耗尽时会自动接管。")
    else:
        print("  结论：存在需要处理的项目，见上方 ✗ 标记。")
    return 0 if h["ok"] else 2


def cmd_log(cfg: dict, state: State, args) -> int:
    """计划任务在后台跑，输出看不见，这里读落盘的日志。"""
    path = log_file(cfg)
    print("houmai 值守日志")
    print(f"  文件  {path}")
    if not os.path.exists(path):
        print("  · 尚无日志：值守跑过一次才会生成")
        print()
        print("  现在就可以生成一份：houmai run --dry-run")
        return 0

    now = time.time()
    mtime = os.path.getmtime(path)
    total = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            total = sum(1 for _ in f)
    except Exception:
        pass
    print(f"  大小  {os.path.getsize(path)} 字节 / {total} 行")
    print(f"  更新  {fmt_local(mtime)}（{fmt_delta(now - mtime)}前）")
    backup = path + LOG_BACKUP_SUFFIX
    if os.path.exists(backup):
        print(f"  上一份 {backup}（{os.path.getsize(backup)} 字节）")

    want = total if args.all else max(1, int(args.lines))
    tail = read_log_tail(path, want)
    print()
    print(f"  最近 {len(tail)} 行：" if not args.all else f"  全部 {len(tail)} 行：")
    print("  " + "-" * 58)
    for ln in tail:
        print("  " + ln.rstrip())
    return 0


# --------------------------------------------------------------------------
# 值守面板（本地只读网页）
# --------------------------------------------------------------------------

PANEL_TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "panel.html")
_panel_cache: dict = {"mtime": None, "html": ""}


def load_panel_html() -> str:
    """读取面板模板（HTML/CSS/JS）。按 mtime 缓存：改了 panel.html，浏览器刷新即生效，
    无需重启面板进程——静态资源不该像后端代码那样在进程启动时被冻结。"""
    try:
        mtime = os.path.getmtime(PANEL_TEMPLATE)
    except OSError:
        cached = _panel_cache.get("html")
        if cached:
            return cached
        return ("<!DOCTYPE html><meta charset='utf-8'>"
                "<p>面板模板缺失：src/panel.html 未找到。</p>")
    if _panel_cache.get("mtime") != mtime:
        with open(PANEL_TEMPLATE, "r", encoding="utf-8") as f:
            _panel_cache["html"] = f.read()
        _panel_cache["mtime"] = mtime
    return _panel_cache.get("html") or ""


def panel_pid_path(cfg: dict) -> str:
    return os.path.join(workdir(cfg), "panel.pid")


def write_panel_pid(cfg: dict, port: int) -> None:
    with open(panel_pid_path(cfg), "w", encoding="utf-8") as f:
        json.dump({"pid": os.getpid(), "port": int(port), "started": time.time()}, f)


def read_panel_pid(cfg: dict) -> dict | None:
    try:
        with open(panel_pid_path(cfg), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def clear_panel_pid(cfg: dict) -> None:
    try:
        os.remove(panel_pid_path(cfg))
    except OSError:
        pass


def pid_alive(pid: int) -> bool | None:
    """指定 PID 是否还在运行；问不出来时返回 None，绝不臆断。

    用 OpenProcess 探测而不是解析 `tasklist` 的输出：后者的"没有匹配任务"
    提示随系统语言变，按文案判断会在换语言的机器上失配。返回 None 的两种
    情形（非 Windows、权限不足）都要当成"不知道"，不能顺势当成"已经没了"
    —— 那正是把"没停成"说成"已结束"的老毛病。
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        ERROR_INVALID_PARAMETER = 87
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if handle:
            k32.CloseHandle(handle)
            return True
        if ctypes.get_last_error() == ERROR_INVALID_PARAMETER:
            return False          # 没有这个 PID
        return None               # 权限不足等：不知道
    except Exception:
        return None


def clean_stale_panel_pid(cfg: dict) -> bool:
    """pid 文件在但端口没人听 = 面板被硬关过留下的残骸，顺手清掉。

    直接关面板窗口时进程来不及跑清理（finally 不执行），文件会留下。
    平时无害（houmai stop 和下次启动面板都会自愈），但值守每 10 分钟
    跑一轮，顺手扫一下让 state 目录保持干净。返回是否清理了。
    """
    info = read_panel_pid(cfg)
    if not info:
        return False
    try:
        s = socket.socket()
        s.settimeout(1)
        listening = s.connect_ex(("127.0.0.1", int(info.get("port") or 8787))) == 0
        s.close()
    except Exception:
        return False
    if listening:
        return False
    clear_panel_pid(cfg)
    return True


class PanelServer(ThreadingHTTPServer):
    # Windows 的 SO_REUSEADDR 允许两个进程同时绑同一端口且第二个不报错，
    # 会出现"两个面板悄悄并存"的怪局。本机只跑一个面板，宁可明确失败。
    allow_reuse_address = (os.name != "nt")


def cmd_web(cfg: dict, state: State, args) -> int:
    host = args.host or "127.0.0.1"
    port = int(args.port)
    state_path = args.state or os.path.join(workdir(cfg), "state.json")
    config_path = args.config

    class Handler(BaseHTTPRequestHandler):
        server_version = f"houmai/{VERSION}"

        def log_message(self, fmt, *a):  # 不在控制台刷访问日志
            pass

        def _send(self, code: int, ctype: str, body: bytes) -> None:
            # 浏览器导航/刷新时会中途掐断连接，这属于正常现象，不能刷堆栈。
            try:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                pass

        def do_GET(self) -> None:
            try:
                self._route()
            except OSError:
                pass
            except Exception as exc:  # 面板本身出错不能把服务带崩
                try:
                    self._send(500, "application/json; charset=utf-8",
                               json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8"))
                except Exception:
                    pass

        def do_POST(self) -> None:
            path = urllib.parse.urlsplit(self.path).path
            # 自定义头会触发跨域预检，而这里不回应预检——别的网页无法替主人按这个按钮。
            if path == "/api/stop" and self.headers.get("X-Houmai-Stop") == "1":
                self._send(200, "application/json; charset=utf-8",
                           json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8"))
                threading.Thread(target=httpd.shutdown, daemon=True).start()
                return
            self._send(404, "text/plain; charset=utf-8", "not found".encode("utf-8"))

        def _route(self) -> None:
            path = urllib.parse.urlsplit(self.path).path
            if path in ("/", "/index.html"):
                # 模板每次请求读盘（按 mtime 缓存）：改 panel.html 刷新即生效，无需重启。
                self._send(200, "text/html; charset=utf-8",
                           load_panel_html().encode("utf-8"))
                return
            if path == "/favicon.ico":
                self._send(204, "image/x-icon", b"")
                return
            if path == "/api/status":
                # 每次请求都重新读盘：计划任务刚写的轮次要立刻可见。
                live = State(state_path)
                # 配置也重新读，改完 config.json 不用重启面板。
                data = collect_health(load_config(config_path), live)
                data["state_path"] = state_path
                self._send(200, "application/json; charset=utf-8",
                           json.dumps(data, ensure_ascii=False).encode("utf-8"))
                return
            self._send(404, "text/plain; charset=utf-8", "not found".encode("utf-8"))

    try:
        httpd = PanelServer((host, port), Handler)
    except OSError as exc:
        print(f"无法监听 {host}:{port} —— {exc}")
        if read_panel_pid(cfg):
            print("已有面板在运行的可能很大。先停它：houmai stop")
        print("换个端口再试：houmai web --port 8788")
        return 2
    httpd.daemon_threads = True  # 让 Ctrl+C 能干脆退出
    write_panel_pid(cfg, port)

    url = f"http://{host}:{port}/"
    print("候脉值守面板已启动")
    print(f"  地址  {url}")
    print(f"  数据  {state_path}")
    print(f"  日志  {log_file(cfg)}")
    print("  只读面板：能看到状态，不触发任何续跑。")
    print("  停止：面板右上角「停止面板」按钮，或命令行 houmai stop，或 Ctrl+C。")
    if not args.no_open:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        clear_panel_pid(cfg)
        print("\n面板已停止。值守不受影响，可用 houmai.cmd 随时再开。")
    return 0


def cmd_stop(cfg: dict, state: State, args) -> int:
    """停止值守面板。值守（计划任务里的 run）完全不受影响。

    退出码是给菜单用的契约，别把三者混成一个非零：
    `0` = 面板已不在运行（含"其实没在跑"，目标状态已达成）；
    `1` = 本来就没有面板在运行（**无事可做，不是故障**）；
    `2` = 面板可能还在跑、没能结束（唯一需要人看一眼的那种）。
    """
    info = read_panel_pid(cfg)
    port = int(args.port) if getattr(args, "port", None) else int((info or {}).get("port") or 8787)
    # 第一优先：让面板自己优雅退出（写它的 HTTP 接口，不用知道 pid）。
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/stop", method="POST", data=b"",
            headers={"X-Houmai-Stop": "1", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3):
            clear_panel_pid(cfg)
            print(f"面板已停止（http://127.0.0.1:{port}/）。值守不受影响。")
            return 0
    except Exception:
        pass
    # 兜底：面板卡死时按 pid 文件结束进程。pid 文件是我们自己写的，只指向面板。
    if info and info.get("pid"):
        pid = int(info["pid"])
        r = subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
        if r.returncode == 0:
            clear_panel_pid(cfg)
            print(f"面板进程（PID {pid}）已结束。值守不受影响。")
            return 0
        # taskkill 非零**不等于**"进程已经不存在"——也可能是杀不掉（权限不足、
        # 进程被占用）。从前一律当成"已不存在"并报成功，等于把"没停成"说成
        # "已结束"，正是本项目最忌讳的静默失效。回读一次再决定怎么说。
        alive = pid_alive(pid)
        if alive is False:
            clear_panel_pid(cfg)
            print(f"PID {pid} 已不存在（面板其实没在运行），清掉过期 pid 文件。")
            return 0
        if alive is True:
            print(f"PID {pid} 仍在运行，未能结束（taskkill 退出码 {r.returncode}）。"
                  "面板可能还在跑；pid 文件保留，可重试或到任务管理器结束。")
            return 2
        print(f"PID {pid} 是否仍在运行无法确认（taskkill 退出码 {r.returncode}）。"
              "pid 文件保留，请人工确认面板是否已停。")
        return 2
    print("面板未在运行（HTTP 不通，也没有 pid 文件）。")
    return 1


def cmd_status(cfg: dict, state: State, args) -> int:
    now = time.time()
    print("houmai 状态")
    print(f"  版本            {VERSION}")
    print(f"  Codex 目录      {codex_home(cfg)}")
    cmd = resolve_codex(cfg)
    print(f"  Codex 入口      {' '.join(cmd) if cmd else '未找到'}")
    print(f"  提醒通道        {cfg['notify'].get('provider')}")
    print(f"  记录文件        {state.events_path}")
    print()
    pending = state.data["pending"]
    if not pending:
        print("  当前没有在册任务。")
    for key, task in pending.items():
        window = task.get("window") or {}
        record = "  │  ".join(x for x in [
            f"{task.get('status')}",
            f"轮次 {task.get('turn_id', '')[:13]}",
            f"模型 {(task.get('context') or {}).get('model')}",
            f"窗口 {window.get('binding')}（{window.get('window_minutes')} 分钟）",
        ] if x)
        print(f"  ● {key[:8]}  {record}")
        if task.get("status") in ("waiting", "retrying"):
            print(f"      下次续跑 {fmt_local(task.get('retry_at', 0))}（{fmt_delta(task.get('retry_at', 0) - now)}后）"
                  f"，已尝试 {task.get('attempts', 0)} 次")
        if task.get("status") == "unconfirmed":
            deadline = task.get("confirm_deadline_at") or 0
            print(f"      续跑已发出但尚未确认新轮次：复查截止 {fmt_local(deadline)}"
                  f"（{fmt_delta(deadline - now)}后转人工）")
        if task.get("status") == "notify_only":
            kind = (task.get("window") or {}).get("kind")
            reason = task.get("notify_reason")
            if kind in ("unknown", None):
                why = "额度信息缺失"
            elif reason == "beyond_max_wait":
                why = "长周期额度（重置超出等待上限）"
            elif reason == "switch_off":
                why = "长周期额度（自动续跑已关闭）"
            else:
                why = "长周期额度"
            print(f"      {why}，只提醒不续跑")
    print()
    # 先多读一些再按展示窗口裁剪，避免"窗口内的记录被 limit 挤掉"。
    limit = max(1, int(args.limit))
    slice_ = split_events(state.recent(max(limit * 4, 200)), cfg, now)
    events = slice_["events"][-limit:]
    if events:
        print(f"  最近 {len(events)} 条记录（展示窗口近 {slice_['history_days']:g} 天）：")
        for ev in events:
            extra = {k: v for k, v in ev.items() if k not in ("at", "kind")}
            print(f"    {fmt_local(ev.get('at', now))}  {ev.get('kind'):<11} {json.dumps(extra, ensure_ascii=False)[:120]}")
    else:
        print(f"  近 {slice_['history_days']:g} 天内没有记录。")
    stale = slice_["stale_summary"]
    notes = []
    if slice_["merged"]:
        notes.append(f"合并同类结论 {slice_['merged']} 条")
    if stale:
        oldest = stale.get("oldest_hours")
        tail = f"，最早 {fmt_delta(oldest * 3600)}前" if oldest is not None else ""
        notes.append(f"折叠 {stale['count']} 条历史中断跳过记录{tail}")
    if notes:
        print("  （" + "；".join(notes) + "）")
    return 0


def cmd_check(cfg: dict, state: State, args) -> int:
    ok = True
    print("houmai 自检")

    # 先看运行时是否独立：这一项不通过，后面再顺都是假象。
    rt = collect_runtime(cfg)
    mark = "·" if not rt["markers"] else ""
    print(f"  · 排除目录  {', '.join(rt['markers']) or '（无）'} {mark}".rstrip())
    if rt["python_external"]:
        print(f"  ✓ 解释器    {rt['python']}")
    else:
        print(f"  ✗ 解释器    {rt['python']}")
        print("              来自被排除目录，候脉不能靠被看护对象的运行时活着")
        ok = False

    cmd = resolve_codex(cfg)
    if not cmd:
        print("  ✗ 未找到 Codex CLI")
        return 2
    print(f"  · 入口      {' '.join(cmd)}")
    if rt["node"]:
        if rt["node_external"]:
            print(f"  ✓ Node      {rt['node']}")
        else:
            print(f"  ✗ Node      {rt['node']}")
            print("              来自被排除目录，请在 config.json 指定 codex.node")
            ok = False
    else:
        print("  · Node      由 codex 启动器自行解析，无法确认它用哪个 node")

    version_text = codex_version(cmd)
    print(f"  · 版本      {version_text or '未知'}")
    if not version_text:
        print("  ✗ CLI 调用失败")
        ok = False
    else:
        cur = parse_version(version_text.split()[-1])
        need = parse_version(cfg["codex"]["min_version"])
        if cur < need:
            print(f"  ✗ 版本过低，需要 ≥ {cfg['codex']['min_version']}（旧版无法接管客户端创建的任务）")
            ok = False
        else:
            print("  ✓ 版本满足要求")
    home = codex_home(cfg)
    sessions = os.path.join(home, "sessions")
    if os.path.isdir(sessions):
        files = session_files(cfg)
        print(f"  ✓ 会话目录  {sessions}（近 {cfg['watch']['lookback_days']} 天 {len(files)} 个文件）")
    else:
        print(f"  ✗ 会话目录不存在：{sessions}")
        ok = False
    if os.path.exists(os.path.join(home, "config.toml")):
        print("  ✓ 配置文件可读")
    else:
        print("  · 未发现 config.toml（非致命）")
    print(f"  · 状态目录  {workdir(cfg)}")
    print(f"  · 提醒通道  {cfg['notify'].get('provider')}")

    pv = proxy_view(cfg)
    if pv["from_config"]:
        print("  · 网络出口  " + ", ".join(f"{k}={v}" for k, v in sorted(pv["proxy"].items()))
              + "（netcheck 探测用；codex 不读 env 代理，走系统代理）")
    elif pv["has_proxy"]:
        print(f"  · 网络出口  会话级代理（{', '.join(sorted(pv['proxy']))}）—— 仅供参考；codex 走系统代理")
    else:
        print("  · 网络出口  未设置代理变量；codex 联网走系统代理，可用 netcheck 复核")

    print("  结论：" + ("可以值守" if ok else "存在阻塞项，需先修复"))
    return 0 if ok else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="houmai", description="侯脉 · Codex 额度恢复值守")
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument("--state", default=None, help="状态文件路径")
    sub = parser.add_subparsers(dest="command")

    p_run = sub.add_parser("run", help="执行一次值守（供计划任务调用）")
    p_run.add_argument("--dry-run", action="store_true", help="演练：只判定不发送")
    p_run.add_argument("--verbose", "-v", action="store_true")

    p_st = sub.add_parser("status", help="查看在册任务与最近记录")
    p_st.add_argument("--limit", default=15)

    p_lg = sub.add_parser("log", help="查看值守日志（计划任务后台跑的输出）")
    p_lg.add_argument("--lines", "-n", default=40, help="显示最近多少行，默认 40")
    p_lg.add_argument("--all", action="store_true", help="显示全部")

    sub.add_parser("check", help="自检运行环境与 CLI 版本")

    sub.add_parser("netcheck", help="连通性检查：续跑时连不连得上 Codex 后端")

    sub.add_parser("health", help="体检：判断值守是否正常运行")

    p_web = sub.add_parser("web", help="启动本地值守面板（浏览器查看）")
    p_web.add_argument("--port", default=8787, help="监听端口，默认 8787")
    p_web.add_argument("--host", default="127.0.0.1", help="监听地址，默认仅本机")
    p_web.add_argument("--no-open", action="store_true", help="不自动打开浏览器")

    p_stop = sub.add_parser("stop", help="停止值守面板（值守不受影响）")
    p_stop.add_argument("--port", default=None, help="面板端口，默认取 pid 文件记录或 8787")

    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    state_path = args.state or os.path.join(workdir(cfg), "state.json")
    state = State(state_path)

    # 只有值守本身需要落盘日志；查询类命令不污染日志。
    if args.command == "run":
        set_log_file(log_file(cfg))

    if args.command == "run":
        return cmd_run(cfg, state, args)
    if args.command == "check":
        return cmd_check(cfg, state, args)
    if args.command == "netcheck":
        return cmd_netcheck(cfg, state, args)
    if args.command == "health":
        return cmd_health(cfg, state, args)
    if args.command == "log":
        return cmd_log(cfg, state, args)
    if args.command == "web":
        return cmd_web(cfg, state, args)
    if args.command == "stop":
        return cmd_stop(cfg, state, args)
    return cmd_status(cfg, state, args)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
