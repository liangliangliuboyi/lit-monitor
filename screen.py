#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
分层筛选 —— 把几百篇压到几十篇

读 profile.md 的 core / proxy / eco / exclude 四层关键词，
给库里本周的每一篇打 layer（core/proxy/eco/noise）和 1-10 的规则分。

用法:
  python screen.py            # 筛本周全部
  python screen.py 24         # 只导出前 24 篇到 data/focus.json 供精读
  python screen.py --all      # 重算库里所有条目（换关键词后需要）

设计原则（取自 frontier-tracker）:
  - 全量扫描，一条不漏地过一遍关键词，避免"标题没踩中词但其实相关"的漏检
  - 分层是透明的：每篇都能看到它命中了哪个词、属于哪一层
  - 不用大模型判断好坏，纯规则，可解释可调参
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jcr            # noqa: E402
import profile as P   # noqa: E402
import reportlib as RL  # noqa: E402  (llm_chat / _llm_cfg)

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
DB = os.path.join(DATA_DIR, "papers.db")
FOCUS = os.path.join(DATA_DIR, "focus.json")

# 百分制：分得开才排得出优先级。1-10 分制会让一堆顶刊同时顶到 10 分，失去区分度。
LAYER_BASE = {"core": 48, "proxy": 26, "eco": 8, "noise": 0}
TIER_BONUS = {"S+": 14, "S": 11, "A": 7, "W": 7, "B": 3, "C": 0, "D": -5, "E": -8, "P": 0, "": -10}
# 导师组（watch authors）文章加权，保证永远排在前面、且不被排除词误杀
AUTHOR_BONUS = 25


def layer_of(text):
    """返回 (layer, 命中的关键词列表)"""
    p = P.profile()
    t = (text or "").lower()
    core = [w for w in p["core"] if w in t]
    proxy = [w for w in p["proxy"] if w in t]
    eco = [w for w in p["eco"] if w in t]
    if core:
        return "core", (core + proxy)[:8]
    if proxy:
        return "proxy", proxy[:8]
    if eco:
        return "eco", eco[:8]
    return "noise", []


def score_of(layer, tier, hits, jif, cites, journal, excluded):
    """内容相关性优先于期刊光环：核心词命中数权重 >= 期刊等级权重"""
    s = LAYER_BASE.get(layer, 0)
    s += TIER_BONUS.get(tier or "", 0)
    s += min(len(hits), 6) * 6                         # 命中越多越贴题，0~36 分
    if jif and jif >= 20:
        s += 6
    elif jif and jif >= 10:
        s += 3
    if cites and cites >= 20:
        s += 3
    jn = (journal or "").lower()
    if any(m and m in jn for m in P.profile()["must_journals"]):
        s += 5
    if excluded:
        s -= 30
    return max(0, min(100, s))


def _llm_augment_one_batch(batch, cfg):
    """把一批 (idx, title, abstract) 送大模型判定 core/extension/method/frontier/irrelevant。
    返回 {idx: (tag, reason)}。失败返回空 dict（调用方跳过）。"""
    l = RL._llm_cfg(cfg)
    if not l:
        return {}
    lines = []
    for it in batch:
        lines.append("%d | %s | %s" % (it["idx"], it["title"][:200], (it["abstract"] or "")[:360]))
    prompt = (
        "你是一个生物医学文献分类器。下面是一周内新检索到的文献（序号 | 标题 | 摘要节选）。\n"
        "【任务一】判断每篇与「2 型糖尿病」研究的相关程度，从以下类别中选一个：\n"
        "- core：明确关于糖尿病 / 糖尿病并发症 / 糖尿病诊疗 / 血糖\n"
        "- extension：代谢相关的跨疾病延伸主题（代谢-肿瘤、代谢-肌少症、代谢手术、NAFLD、"
        "肥胖机制、妊娠代谢等），虽可能不提 diabetes 但与糖尿病研究高度互通\n"
        "- method：可迁移到糖尿病研究的方法学 / AI / 研究范式（临床大模型、EHR 深度学习、因果推断新范式等）\n"
        "- frontier：新颖、能启发研究思路的前沿方向\n"
        "- irrelevant：与糖尿病无关\n"
        "【任务二】判断每篇与【导师组研究方向】的贴合度，从 high / medium / low 中选一个：\n"
        "导师组方向为（贾伟平院士组）：CGM 与 TIR（持续葡萄糖监测 / 葡萄糖目标范围内时间）、"
        "腹型肥胖、糖尿病易感基因、AI 糖尿病管理（中国路径）、基层糖尿病防治管理。\n"
        "high = 直接命中上述某一具体方向；medium = 相邻 / 方法可迁移 / 人群相关；low = 基本不相关。\n\n"
        "严格只输出一个 JSON 数组，每个元素为 "
        "{\"idx\":int, \"tag\":str, \"reason\":str(中文20字内), \"adv\":str(high/medium/low)}，"
        "顺序与输入一致，不要解释、不要序号以外的文字：\n"
        + "\n".join(lines)
    )
    out = RL.llm_chat([{"role": "user", "content": prompt}], cfg, max_tokens=1600,
                      tag="screen_llm_augment", n_items=len(lines))
    if not out:
        return {}
    m = re.search(r"\[.*\]", out, re.S)
    if not m:
        return {}
    try:
        arr = json.loads(m.group(0))
    except Exception:
        return {}
    res = {}
    for o in arr:
        try:
            idx = int(o.get("idx"))
            tag = str(o.get("tag", "")).strip().lower()
            adv = str(o.get("adv", "")).strip().lower()
            if adv not in ("high", "medium", "low"):
                adv = ""
            res[idx] = (tag, (o.get("reason") or "")[:200], adv)
        except Exception:
            continue
    return res


def llm_augment(week=None, cfg=None, verbose=True):
    """对本周「非 core、非导师组、未判定过」的文献批量送大模型，写回 llm_tag / llm_reason。

    这是 2026-10 的「LLM 增强识别」：核心(core)词命中的文献与导师组文献已确定相关，无需重判；
    其余相邻 / 方法 / 前沿 / 背景文献交给大模型判定是否真与糖尿病相关，避免纯关键词漏掉
    「代谢-肿瘤」「肌少症」「医学 AI」等有价值的延伸文献。
    """
    cfg = cfg or P.cfg()
    if not RL._llm_cfg(cfg):
        if verbose:
            print("[llm_augment] 未配置大模型，跳过增强识别")
        return
    con = sqlite3.connect(DB)
    try:
        have = {r[1] for r in con.execute("PRAGMA table_info(papers)")}
    except Exception:
        have = set()
    if "llm_tag" not in have:
        con.close()
        print("[llm_augment] 库里还没有 llm_tag 字段，先跑 monitor.py fetch")
        return

    if week is None:
        week_filter = ""          # None = 全库（--all 重算时）
        params = ()
    else:
        # 按【所属 ISO 周】而不是 week=某一天 取：同周多次抓取会落在不同的 week 上，
        # 只筛 week=今天 会把先前批次漏掉，导致一批文献永远得不到打分。
        week_filter = " AND week BETWEEN ? AND ?"
        params = tuple(jcr.week_range(week))
    p = P.profile()
    excl = set(w.lower() for w in p["exclude"])

    rows = con.execute(
        "SELECT key,title,abstract,layer,author_hit FROM papers "
        "WHERE layer<>'core' AND (author_hit IS NULL OR author_hit=0) "
        "AND (llm_tag IS NULL OR llm_tag='')" + week_filter, params).fetchall()

    # 预筛：标题+摘要命中排除词的视为无关，不浪费大模型调用
    cands = []
    skip = 0
    for key, title, abstract, layer, ahit in rows:
        t = "%s %s" % (title or "", abstract or "")
        if any(w in t.lower() for w in excl):
            skip += 1
            continue
        cands.append({"key": key, "idx": len(cands) + 1,
                      "title": title or "", "abstract": abstract or ""})
    if verbose:
        print("[llm_augment] 候选 %d 篇（跳过排除词命中 %d 篇），开始分批送大模型判定" % (len(cands), skip))

    stats = {}
    B = 15
    for i in range(0, len(cands), B):
        batch = cands[i:i + B]
        # 用本地连续 idx 作为批次内序号；恢复 key 映射
        key_by_idx = {it["idx"]: it["key"] for it in batch}
        res = _llm_augment_one_batch(batch, cfg)
        for idx, (tag, reason, adv) in res.items():
            key = key_by_idx.get(idx)
            if not key:
                continue
            con.execute("UPDATE papers SET llm_tag=?, llm_reason=?, advisor_fit=? WHERE key=?",
                        (tag, reason, adv, key))
            stats[tag] = stats.get(tag, 0) + 1
        # 没返回结果的（限流/解析失败）本批跳过，不阻塞
        con.commit()
    con.commit()
    con.close()
    if verbose:
        print("[llm_augment] 判定结果：%s" % (stats or "无（可能全部命中排除词或限流）"))


def run(week=None, top_n=None, all_rows=False, verbose=True):
    p = P.profile()
    con = sqlite3.connect(DB)
    try:
        have = {r[1] for r in con.execute("PRAGMA table_info(papers)")}
    except Exception:
        have = set()
    if "layer" not in have:
        con.close()
        print("库里还没有 layer 字段，先跑 monitor.py fetch")
        return []

    cols0 = "key,title,abstract,journal,tier,jif,cites,source,author_hit,author_name"
    if all_rows:
        rows = con.execute("SELECT %s FROM papers" % cols0).fetchall()
    else:
        week = week or date.today().isoformat()
        rows = con.execute(
            "SELECT %s FROM papers WHERE week BETWEEN ? AND ?" % cols0,
            tuple(jcr.week_range(week))).fetchall()

    stats = {"core": 0, "proxy": 0, "eco": 0, "noise": 0, "author": 0}
    scored = []
    for key, title, abstract, journal, tier, jif, cites, source, ahit, aname in rows:
        text = "%s %s" % (title or "", abstract or "")
        layer, hits = layer_of(text)
        ex = [w for w in p["exclude"] if w in text.lower()]
        if ahit:
            # 导师组：排除词不扣分（他们本来也做机制研究）、强制进必读层、加权重保证排最前
            sc = score_of(layer, tier, hits, jif or 0, cites or 0, journal, []) + AUTHOR_BONUS
            if layer in ("noise", "eco"):
                layer = "core"
            stats["author"] = stats.get("author", 0) + 1
        else:
            sc = score_of(layer, tier, hits, jif or 0, cites or 0, journal, ex)
        stats[layer] = stats.get(layer, 0) + 1
        con.execute("UPDATE papers SET layer=?, score=?, topics=? WHERE key=?",
                    (layer, sc, ",".join(hits)[:300], key))
        scored.append({"key": key, "layer": layer, "score": sc, "tier": tier or "",
                       "hits": hits, "journal": journal, "title": title,
                       "jif": jif or 0, "cites": cites or 0, "source": source,
                       "author_hit": ahit or 0, "author_name": aname or ""})
    con.commit()

    # LLM 增强识别：把非 core / 非导师组的相邻文献送大模型判定相关性（core/extension/method/frontier）
    try:
        llm_augment(week if not all_rows else None, P.cfg(),
                    verbose=verbose)
    except Exception as e:
        if verbose:
            print("[warn] LLM 增强识别失败，跳过（不影响关键词分层）：%r" % e)

    # 导师组文章（author_hit）无条件视为导师组高贴合度（它们本身就在做这些方向）
    try:
        con.execute("UPDATE papers SET advisor_fit='high' "
                    "WHERE author_hit=1 AND (advisor_fit IS NULL OR advisor_fit='')")
        con.commit()
    except Exception:
        pass

    scored.sort(key=lambda x: (-x["score"], jcr.TIER_ORDER.get(x["tier"], 9), -(x["cites"] or 0)))
    keep = scored[:top_n] if top_n else scored

    # 导出给智能体精读的清单
    cols = ["key", "doi", "title", "journal", "pub_date", "url", "abstract", "authors",
            "tier", "topics", "source", "cites", "quartile", "jif", "pmid", "layer", "score",
            "author_hit", "author_name"]
    items = []
    for k in keep:
        r = con.execute("SELECT %s FROM papers WHERE key=?" % ",".join(cols), (k["key"],)).fetchone()
        if not r:
            continue
        d = dict(zip(cols, r))
        d["abstract"] = (d["abstract"] or "")[:1800]
        items.append(d)
    con.close()

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(FOCUS, "w", encoding="utf-8") as f:
        json.dump({"week": week or date.today().isoformat(), "stats": stats,
                   "count": len(items), "items": items}, f, ensure_ascii=False, indent=1)

    if verbose:
        print("分层统计：核心 %d ｜ 方法/相邻 %d ｜ 背景 %d ｜ 噪音 %d ｜ 合计 %d"
              % (stats["core"], stats["proxy"], stats["eco"], stats["noise"], len(rows)))
        print("已导出前 %d 篇 -> data/focus.json" % len(items))
        for it in items[:12]:
            print("  %2d分 %-2s %-12s %s" % (it["score"], it["tier"], it["layer"], (it["title"] or "")[:58]))
    return items


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    args = [a for a in sys.argv[1:]]
    top = None
    for a in args:
        if a.isdigit():
            top = int(a)
    run(top_n=top, all_rows=("--all" in args))
