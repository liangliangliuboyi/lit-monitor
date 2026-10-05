#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
给筛选后的文献写中文一句话摘要 + 相关性打分

两种模式：
  1) 配了 llm.api_key  -> 调 OpenAI 兼容接口（DeepSeek / 通义 / 智谱 / OpenAI / 本地 Ollama 都行）
  2) 没配             -> 纯规则降级：截取摘要首句，照样能出报告，只是没有"人味"

产物 data/scored.json，随后由 monitor.py apply 回填进库。

用法:
  python summarize.py          # 处理 data/focus.json 里的条目
  python summarize.py --rule   # 强制走规则模式
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import profile as P  # noqa: E402

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
FOCUS = os.path.join(DATA_DIR, "focus.json")
SCORED = os.path.join(DATA_DIR, "scored.json")

PROMPT = """你是医学（糖尿病 / 代谢 / 公共卫生）方向的文献助理，服务于一位做糖尿病相关研究的直博生。
下面是一周内新发表的英文文献（JSON 数组）。对每一篇输出：
- score: 0-10 的整数，它与该生研究方向的综合相关度（涵盖糖尿病精准诊疗 / 预警筛查 / 发病机制 /
  防治工程管理，以及可迁移的方法学如因果推断、机器学习、文献计量；也欢迎医学 AI / 大模型 /
  临床预测、眼底 / 视网膜模型、糖尿病脑健康、主动健康管理、可穿戴设备等前沿方向）
- note: 一句中文（40-90 字），客观说明这篇文献「做了什么、怎么做的、有什么贡献或发现」，
  直接陈述研究本身（对象 / 数据 / 方法 / 结论）即可；不要评价它是否偏向卫生政策、经济评价或
  卫生服务体系，不要写"值得关注""很有价值"这类空话，不要编造摘要里没有的信息
- cn_title: 中文标题（可选，30 字内）

严格只输出 JSON 数组，每项形如 {"key":"...","score":9,"note":"...","cn_title":"..."}。
不要编造事实，不要输出 DOI 或标题以外的新信息。"""


def rule_note(it):
    """规则兜底（没配 llm.api_key 或这轮模型不可用时）。

    2026-10-05 改：不再拿"英文摘要前两句"充当中文评语。
    那条路径曾经造成报告里「评语 = 摘要」（卡片上半段英文摘要、下半段折叠的同一段摘要），
    而且它写进 note 后，出报告时的富文本层只判"note 为空才补写"，于是永久冒充评语。
    现在规则模式一律不写评语（评语由出报告时的大模型按档位补，未覆盖档位就是不显示），
    分数字段照常返回。
    """
    ab = re.sub(r"\s+", " ", (it.get("abstract") or "")).strip()
    if not ab:
        return "（该条目无摘要，建议点开原文判断）", it.get("score", 3), ""
    return "", it.get("score", 3), ""


def call_llm(items, cfg):
    l = cfg.get("llm") or {}
    key = (l.get("api_key") or "").strip()
    base = (l.get("base_url") or "https://api.deepseek.com/v1").rstrip("/")
    model = (l.get("model") or "deepseek-chat").strip()
    if not key:
        return None
    payload = [{
        "key": it["key"], "title": it["title"], "journal": it["journal"],
        "tier": it["tier"], "jif": it.get("jif"), "layer": it.get("layer"),
        "abstract": (it.get("abstract") or "")[:1200],
    } for it in items]
    # 走 reportlib.llm_chat（统一节流 / 429 退避 / 冷却重试），不要自己裸打接口：
    # 硅基流动是按分钟限频的突发限制，裸打遇到一次 429 就会整批静默退回规则模式，
    # 表现就是"中文评语和中文标题一直没生成"——之前正是这样。
    import reportlib as RL
    txt = RL.llm_chat(
        [{"role": "system", "content": PROMPT},
         {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        cfg, max_tokens=int(l.get("max_tokens", 4000)),
        tag="summarize", n_items=len(items))
    if not txt:
        print("[warn] LLM 调用失败，回退规则模式（本批 %d 篇）" % len(items))
        return None
    m = re.search(r"\[.*\]", txt, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def run(force_rule=False):
    if not os.path.exists(FOCUS):
        print("没有 data/focus.json，先跑 screen.py")
        return
    with open(FOCUS, encoding="utf-8") as f:
        data = json.load(f)
    items = data.get("items", [])
    cfg = P.cfg()
    l = cfg.get("llm") or {}
    cap = int(l.get("max_items", 24))
    todo = items[:cap]

    result = None
    if l.get("enabled") and not force_rule:
        result = call_llm(todo, cfg)

    out = []
    if result:
        idx = {str(x.get("key")): x for x in result if isinstance(x, dict)}
        for it in todo:
            r = idx.get(it["key"])
            if r:
                out.append({"key": it["key"], "score": int(r.get("score", it.get("score", 3))),
                            "note": (r.get("note") or "").strip(),
                            "cn_title": (r.get("cn_title") or "").strip(), "status": "new"})
            else:
                note, sc, _ = rule_note(it)
                out.append({"key": it["key"], "score": sc, "note": note, "cn_title": "", "status": "new"})
        print("LLM 已为 %d 篇生成中文摘要" % len([x for x in out if x["note"] and "无摘要" not in x["note"]]))
    else:
        for it in todo:
            note, sc, _ = rule_note(it)
            out.append({"key": it["key"], "score": sc, "note": note, "cn_title": "", "status": "new"})
        print("规则模式：%d 篇（未配置 llm.api_key，如需人味摘要请在 config.json 里配）" % len(out))

    with open(SCORED, "w", encoding="utf-8") as f:
        json.dump({"week": data.get("week"), "count": len(out), "items": out}, f, ensure_ascii=False, indent=1)
    print("-> data/scored.json")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    run(force_rule=("--rule" in sys.argv))
