#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
一键跑完整个流程

  python run.py                 抓取 → 分层 → 摘要 → 出报 → 下载归档 → 发邮件
  python run.py --no-download   不下载全文，只出报告
  python run.py --no-mail       不发邮件
  python run.py --days 30       把回看窗口改成 30 天（月刊模式）
  python run.py --rule          跳过 LLM，纯规则（没配 key 时自动降级的就是这个）
  python run.py --screen-only   只重跑分层和报告，不重新抓（改完 profile.md 用这个）

每一步都是独立的 .py，可以单独跑：
  monitor.py fetch / apply / report / stats / retier
  screen.py [N] [--all]
  summarize.py [--rule]
  download.py [--dry-run]
  mail.py [--dry-run]
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable


def step(title, args, optional=False):
    print("\n" + "=" * 62)
    print("▶ %s" % title)
    print("=" * 62)
    t0 = time.time()
    try:
        r = subprocess.run([PY] + args, cwd=BASE, timeout=3600)
        ok = r.returncode == 0
    except subprocess.TimeoutExpired:
        print("!! 超时（已跑满 60 分钟），跳过这一步")
        ok = False
    except Exception as e:
        print("!! 异常：%r" % e)
        ok = False
    print("── 耗时 %.1fs，%s" % (time.time() - t0, "完成" if ok else "失败"))
    if not ok and not optional:
        print("!! 关键步骤失败，流程中止")
        sys.exit(1)
    return ok


def set_days(days):
    p = os.path.join(BASE, "config.json")
    try:
        with open(p, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return
    cfg["lookback_days"] = int(days)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print("回看窗口已设为 %d 天" % int(days))


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    a = sys.argv[1:]
    if "--days" in a:
        i = a.index("--days")
        if i + 1 < len(a) and a[i + 1].isdigit():
            set_days(a[i + 1])
    if "--help" in a or "-h" in a:
        print(__doc__)
        return

    if "--screen-only" not in a:
        step("1/5 抓取（PubMed / OpenAlex / 预印本 / RSS）", ["monitor.py", "fetch"])
    step("2/5 分层筛选（core / proxy / eco / noise + 导师组）", ["screen.py", "40"])
    step("3/5 中文摘要与打分", ["summarize.py"] + (["--rule"] if "--rule" in a else []))
    step("4/5 回填打分并生成周报", ["monitor.py", "apply"])
    step("4/5 生成周报", ["monitor.py", "report"], optional=True)
    if "--no-download" not in a:
        step("5/6 下载全文并按周归档", ["download.py"], optional=True)
    if "--no-ai" not in a:
        step("AI 文献板块：抓取 / 报告 / 下载", ["aiboard.py"], optional=True)
    if "--no-mail" not in a:
        step("6/6 邮件推送周报（含 AI 板块）", ["mail.py"], optional=True)
    print("\n全部完成。周报在 reports/，文献在 config.json 里 papers_dir 指定的目录。")


if __name__ == "__main__":
    main()
