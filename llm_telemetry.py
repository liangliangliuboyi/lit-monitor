# -*- coding: utf-8 -*-
"""LLM 调用埋点（telemetry）—— 为论文的「调用预算—效用」分析采集数据。

设计原则：
  * 只追加（append-only），行缓冲实时落盘 → 长任务中途崩溃也不丢已记录的部分；
  * 任何异常都被吞掉，绝不影响主流程；
  * 不碰主数据库 schema（单独落到 论文材料/05_运行日志/llm_telemetry.csv）。

记录两类事件：
  kind="llm_call"  : 每次真实调用（含重试/429/冷却/耗时/字符数）
  kind="gate"      : 每次「门控」决策（多少条进了 LLM、多少条被规则挡下）
  kind="run"       : 一轮运行的汇总（周标签、条目数、调用次数）
"""
from __future__ import annotations
import os, csv, json, time, threading, datetime

_LOCK = threading.Lock()
_HEADER = ["ts", "kind", "tag", "n_items", "model", "max_tokens", "prompt_chars",
           "completion_chars", "duration_ms", "attempts", "n429", "cooldown_s",
           "ok", "extra"]
_COLS = {c: i for i, c in enumerate(_HEADER)}

# 日志目录：副本根/论文材料/05_运行日志
_HERE = os.path.dirname(os.path.abspath(__file__))
_LOG_DIR = os.path.join(_HERE, "论文材料", "05_运行日志")
_CSV_PATH = os.path.join(_LOG_DIR, "llm_telemetry.csv")
_enabled = True


def set_enabled(v):
    global _enabled
    _enabled = bool(v)


def csv_path():
    return _CSV_PATH


def trace(kind, **fields):
    """追加一条事件。永不抛异常。"""
    if not _enabled:
        return
    try:
        with _LOCK:
            os.makedirs(_LOG_DIR, exist_ok=True)
            new = not os.path.exists(_CSV_PATH) or os.path.getsize(_CSV_PATH) == 0
            row = {c: "" for c in _HEADER}
            row["ts"] = datetime.datetime.now().isoformat(timespec="seconds")
            row["kind"] = kind
            extra = {}
            for k, v in fields.items():
                if k == "extra" and isinstance(v, dict):
                    extra.update(v)
                elif k in _COLS:
                    row[k] = v
                else:
                    extra[k] = v
            if extra:
                # extra 里如已有则合并
                prev = row.get("extra") or ""
                if prev:
                    try:
                        extra = dict(json.loads(prev), **extra)
                    except Exception:
                        pass
                row["extra"] = json.dumps(extra, ensure_ascii=False)
            with open(_CSV_PATH, "a", encoding="utf-8-sig", newline="") as f:
                w = csv.DictWriter(f, fieldnames=_HEADER)
                if new:
                    w.writeheader()
                w.writerow(row)
    except Exception:
        pass


class CallTimer:
    """在 llm_chat 内使用的调用计时/计数上下文。"""

    def __init__(self, tag, n_items, model, max_tokens, prompt_chars):
        self.t0 = time.time()
        self.tag = tag
        self.n_items = n_items
        self.model = model
        self.max_tokens = max_tokens
        self.prompt_chars = prompt_chars
        self.attempts = 0
        self.n429 = 0
        self.cooldown_s = 0.0

    def done(self, ok, completion_chars=0, **extra):
        trace("llm_call", tag=self.tag, n_items=self.n_items, model=self.model,
              max_tokens=self.max_tokens, prompt_chars=self.prompt_chars,
              completion_chars=completion_chars,
              duration_ms=int((time.time() - self.t0) * 1000),
              attempts=self.attempts, n429=self.n429,
              cooldown_s=round(self.cooldown_s, 1), ok=1 if ok else 0, **extra)
