#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
统一富文本报告生成（2026-10 改版）

所有"本周文献清单"类产物都走这里，保证内容 / 排序 / 配色完全一致（带数字前缀，按常用度排序）：
  - 0_一键打开链接.html : 全部符合要求的文献（含打开本地 PDF 按钮 + 下载状态筛选）
  - 1_本周综述.html     : 导师组 / 研究方法 / 研究内容 / 前沿专栏 分组综述 + APA 参考文献
  - 2_题录总表.html     : 与一键打开同内容同序（便于浏览器阅读）
  - 2_题录总表.csv      : 同内容的数据版，可导入 Zotero / Excel
  - 3_待下载清单.html   : 只放没下到的，附全部可点链接

设计：
  - 分类 0-6（导师组/世界顶刊/领域顶刊/一区top/一区/二区/其他），数字前缀保证文件管理器按序排列
  - 五彩配色（QQ 邮件风）；每张卡片左边框 = 分类色
  - 顶部检索栏：按分类 / 期刊等级 / 相关度 / 导师组 / 标签 / 下载状态 / 关键词实时筛选
  - 大模型（可选）：配置了 config.llm 就做中文翻译（标题/作者/单位/摘要/关键词）、中文评语与综述成文
"""

from __future__ import annotations

import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    import jcr
except Exception:  # 独立运行时
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import jcr

BASE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- LLM 调用埋点（论文用）
# 唯一入口 llm_chat 里落一条事件；门控/批次另记 gate 事件。
# 埋点不可用时静默忽略，绝不影响主流程。
try:
    import llm_telemetry as _tele
except Exception:
    _tele = None


def trace(kind, **kw):
    try:
        if _tele:
            _tele.trace(kind, **kw)
    except Exception:
        pass


def _pc(messages):
    """prompt 字符数（成本估算用）。"""
    try:
        return sum(len(m.get("content") or "") for m in (messages or []))
    except Exception:
        return 0


# 落盘文件名（数字前缀 = 常用度排序，文件管理器里按序排列）
F_LIB = "0_一键打开链接.html"      # 最常用：全量清单 + 筛选
F_REVIEW = "1_本周综述.html"       # 次常用：分组综述
F_TOTAL = "2_题录总表.html"        # 与一键打开同内容
F_CSV = "2_题录总表.csv"           # 数据版（Zotero/Excel）
F_PEND = "3_待下载清单.html"       # 未下到的
# 旧命名（2026-10 前），生成新文件时顺手清理，避免两套并存
LEGACY_NAMES = ("_一键打开链接.html", "_题录总表.html", "_题录总表.csv",
                "_待下载清单.html", "本周综述.html")


def _cleanup_legacy(folder):
    for n in LEGACY_NAMES:
        p = os.path.join(folder, n)
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass


# ---------------------------------------------------------------- 配置 / 工具
def load_cfg():
    p = os.path.join(BASE, "config.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {}


def esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# 评语必须是中文（见 need_note 的说明）。判断"含不含中文"只用这一段。
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def has_cjk(s):
    return bool(_CJK_RE.search(s or ""))


def need_note(it):
    """这条文献的评语要不要（重新）生成？

    2026-10-05 修的坑：早期 summarize.py 的规则兜底会把【英文摘要前两句】当评语写进
    note 字段，之后 enrich 只判"note 为空不再补"，于是英文摘要就永久冒充评语——
    报告里卡片上半部分显示英文摘要、下半部分又显示同一段英文摘要（"评语变成了摘要"）。
    现在把"非空但没有中文"的 note 也视为待重写；对外展示同理（见 _card）。
    以「（」开头的 note 是明确的占位说明（如"该条目无摘要"），保持不动。
    """
    n = (it.get("note") or "").strip()
    if not n:
        return True
    if n.startswith("（"):
        return False
    return not has_cjk(n)


def folder_of(it, q1_top_if=7.0):
    return jcr.folder_code(it.get("tier") or "", it.get("jif"), bool(it.get("author_hit")), q1_top_if)


def cat_label(code):
    return jcr.CAT_NAME.get(code) or jcr.AI_CAT_NAME.get(code, "其他")


def cat_color(code):
    return jcr.CAT_COLOR.get(code) or jcr.AI_CAT_COLOR.get(code, "#6B6A63")


# ---------------------------------------------------------------- AI 模式开关
# aiboard 在调用报告函数前 set_ai_mode(True)，则全程走「AI 板块」口径：
#   - 卡片里的「导师组」徽章 → 「本方向核心」徽章（core_hit）
#   - 贴合度徽章文案改为「本方向·高/中」
#   - enrich 的翻译/评语门控改为 core_hit 或 顶会/顶刊线索(conf_hint)
#   - write_library 按 A0/A1/A2 分组而非 0-6
#   - write_review 的导师组段→本方向核心段、分组用 AI 方法/内容组、趋势查 ai.db
# 默认 False，糖尿病侧零影响。
AI_MODE = False


def set_ai_mode(v):
    global AI_MODE
    AI_MODE = bool(v)


def ai_priority(it):
    """AI 模式下，是否给这条文献做中文翻译 + 评语。
    判定：本方向核心(core_hit) 或 点名了顶会/顶刊线索(conf_hint)。"""
    return bool(it.get("core_hit")) or bool(it.get("conf_hint"))


# ---------------------------------------------------------------- 大模型（可选）
def _llm_cfg(cfg):
    l = (cfg or {}).get("llm") or {}
    if not (l.get("enabled") and l.get("api_key")):
        return None
    return l


# ---------------------------------------------------------------- LLM 限流保护
# 硅基流动按【分钟】限频（RPM / TPM），而且 429 是滚动窗口的突发限制、不是 key 失效：
# 实测隔几分钟再打照样 200。批量任务一轮要打上百次调用，一旦触限，若每次都各自重试
# 3 遍（8/16/32s），整轮会拖到几十分钟甚至看起来"卡死"。
#
# 三道保险：
#   ① 调用间隔节流（默认 2s），避免瞬时突发；
#   ② 连续触限后【冷却】——睡一段较长时间再试，而不是立刻放弃；
#   ③ 冷却 N 次仍然全败才熔断，本轮剩余走规则兜底。
# 用户的机器周三凌晨 3 点就开着、允许跑完为止，所以这里刻意"更有耐心"：
# 宁可多等几分钟，也要把中文翻译跑完。
_LLM_STATE = {"fails": 0, "disabled": False, "last": 0.0, "trips": 0}
_LLM_MAX_FAILS = 3          # 连续 429 达此数 → 触发一次冷却
_LLM_MAX_TRIPS = 4          # 冷却这么多次仍失败 → 才真正熔断
_LLM_COOLDOWN = float(os.environ.get("LITMON_LLM_COOLDOWN", 75))  # 每次冷却秒数
_LLM_GAP = float(os.environ.get("LITMON_LLM_GAP", 2.0))  # 两次调用最小间隔（秒）


def llm_disabled():
    return _LLM_STATE["disabled"]


def reset_llm_state():
    _LLM_STATE.update({"fails": 0, "disabled": False, "last": 0.0, "trips": 0})


def llm_chat(messages, cfg, max_tokens=1200, tag="", n_items=0):
    """调用 OpenAI 兼容接口。带 429/限流重试（指数退避）与熔断，最终失败返回 None 走规则兜底。

    tag / n_items 仅用于埋点（论文「调用预算—效用」分析），不改变任何行为。
    """
    l = _llm_cfg(cfg)
    if not l:
        return None
    if _LLM_STATE["disabled"]:      # 本轮已熔断，直接走规则兜底，不再打接口
        trace("llm_call", tag=tag, n_items=n_items, model=(l.get("model") or ""),
              max_tokens=max_tokens, prompt_chars=_pc(messages), completion_chars=0,
              duration_ms=0, attempts=0, n429=0, cooldown_s=0, ok=0,
              extra={"skipped": "circuit_open"})
        return None
    # 节流：两次调用之间至少隔 _LLM_GAP 秒，避免瞬时突发触限
    gap = _LLM_GAP - (time.time() - _LLM_STATE["last"])
    if gap > 0:
        time.sleep(gap)
    _t0 = time.time()
    _attempts = 0
    _n429 = 0
    _cool = 0.0

    def _fin(ok, nchar=0, **ex):
        trace("llm_call", tag=tag, n_items=n_items, model=l.get("model", ""),
              max_tokens=max_tokens, prompt_chars=_pc(messages), completion_chars=nchar,
              duration_ms=int((time.time() - _t0) * 1000), attempts=_attempts,
              n429=_n429, cooldown_s=round(_cool, 1), ok=1 if ok else 0,
              extra=(ex or None))

    base = (l.get("base_url") or "https://api.deepseek.com/v1").rstrip("/")
    body = json.dumps({
        "model": l.get("model", "deepseek-chat"),
        "messages": messages,
        "temperature": float(l.get("temperature", 0.2)),
        "max_tokens": max_tokens,
    }, ensure_ascii=False).encode("utf-8")
    # 硅基流动等按分钟限频，偶发 429；重试 3 次，退避 8/16/32s
    last_err = None
    for attempt in range(3):
        _attempts += 1
        try:
            req = urllib.request.Request(
                base + "/chat/completions", data=body,
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + l["api_key"]})
            with urllib.request.urlopen(req, timeout=180) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
            _LLM_STATE["last"] = time.time()
            _LLM_STATE["fails"] = 0          # 成功一次就把连续失败计数清零
            _c = d["choices"][0]["message"]["content"]
            _fin(True, len(_c or ""))
            return _c
        except urllib.error.HTTPError as e:
            last_err = e
            _LLM_STATE["last"] = time.time()
            if e.code == 429:
                _n429 += 1
                _LLM_STATE["fails"] += 1
                if _LLM_STATE["fails"] >= _LLM_MAX_FAILS:
                    _LLM_STATE["trips"] += 1
                    if _LLM_STATE["trips"] >= _LLM_MAX_TRIPS:
                        _LLM_STATE["disabled"] = True
                        print("[LLM 熔断] 冷却 %d 次仍持续限流，本轮剩余改用规则模式"
                              "（不降质量，只是没有中文翻译/评语）" % _LLM_STATE["trips"])
                        _fin(False, 0, outcome="circuit_tripped")
                        return None
                    # 冷却：优先听服务端 Retry-After（硅基流动 429 会带这个头，指明还要等多久），
                    # 没有再退到默认冷却。本质还是"按服务器指示等、再重试"，不是立刻放弃整轮。
                    ra = 0
                    try:
                        ra = float(e.headers.get("Retry-After") or 0)
                    except Exception:
                        ra = 0
                    cool = ra if ra > 0 else _LLM_COOLDOWN
                    _cool += cool
                    print("[LLM 限流] 连续 %d 次 429，冷却 %.0fs 后继续（第 %d/%d 次冷却）"
                          % (_LLM_STATE["fails"], cool,
                             _LLM_STATE["trips"], _LLM_MAX_TRIPS))
                    time.sleep(cool)
                    _LLM_STATE["fails"] = 0     # 冷却完重新计数，给它一次机会
                    if attempt < 2:
                        continue
                    break
                if attempt < 2:
                    # 优先按服务端给的 Retry-After 等待，没有再走指数退避
                    try:
                        wait = float(e.headers.get("Retry-After") or 0)
                    except Exception:
                        wait = 0
                    time.sleep(wait if wait > 0 else 8 * (attempt + 1))
                    continue
            break
        except Exception as e:  # noqa
            last_err = e
            if attempt < 2:
                time.sleep(8 * (attempt + 1))
                continue
            break
    print("[warn] LLM 调用失败（已重试），回退规则模式：%r" % last_err)
    _fin(False, 0, outcome="failed", err=str(last_err)[:200])
    return None


def translate_titles(items, cfg, batch=25):
    """把缺失中文标题的文献批量翻译（需要配置 llm）。原地写入 it['cn_title']。"""
    l = _llm_cfg(cfg)
    if not l:
        return
    todo = [it for it in items if not it.get("cn_title")]
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        titles = [it.get("title", "") for it in chunk]
        prompt = ("把下面每篇英文论文标题翻译成中文（准确、学术）。严格只输出一个 JSON 数组，"
                  "数组元素为与输入顺序对应的中文标题字符串，不要解释、不要序号：\n"
                  + json.dumps(titles, ensure_ascii=False))
        out = llm_chat([{"role": "user", "content": prompt}], cfg, max_tokens=2500,
                       tag="translate_titles", n_items=len(chunk))
        if not out:
            continue
        m = re.search(r"\[.*\]", out, re.S)
        if not m:
            continue
        try:
            arr = json.loads(m.group(0))
        except Exception:
            continue
        for it, cn in zip(chunk, arr):
            if isinstance(cn, str) and cn.strip():
                it["cn_title"] = cn.strip()


def _json_array(txt):
    if not txt:
        return []
    m = re.search(r"\[.*\]", txt, re.S)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
        return arr if isinstance(arr, list) else []
    except Exception:
        return []


NOTE_PROMPT = (
    "你是糖尿病 / 代谢方向的医学文献助理。对下面每篇文献，用一句中文（50–90 字）说清楚"
    "它『做了什么、怎么做的、有什么贡献或发现』——直接陈述研究本身（对象/数据/方法/结论）。\n"
    "要求：不要评价它是否偏向卫生政策、经济评价、卫生服务；不要写'值得关注''很有价值'这类空话；"
    "不要编造摘要里没有的信息。\n"
    "严格只输出一个 JSON 数组，元素形如 {\"key\":\"...\",\"note\":\"...\"}，顺序与输入一致，不要解释：\n"
)

TRANS_PROMPT = (
    "请把下面每篇文献的字段翻译成**准确、学术的中文**：标题、作者名（音译或通用中文）、"
    "作者单位（机构名译成中文）、摘要、关键词。\n"
    "严格只输出一个 JSON 数组，每个元素形如 "
    "{\"key\":\"...\",\"cn_title\":\"...\",\"cn_authors\":\"...\",\"cn_aff\":\"...\","
    "\"cn_abstract\":\"...\",\"cn_keywords\":\"...\"}，顺序与输入一致，不要解释：\n"
)

# AI 板块用的评语 prompt（不套糖尿病框架，通用 AI 口径）
AI_NOTE_PROMPT = (
    "你是人工智能方向的医学文献助理。对下面每篇文献，用一句中文（50–90 字）说清楚"
    "它『做了什么、怎么做的、有什么贡献或发现』——直接陈述研究本身（对象/数据/方法/结论）。\n"
    "要求：不要评价它是否前沿；不要写'值得关注''很有价值'这类空话；"
    "不要编造摘要里没有的信息。\n"
    "严格只输出一个 JSON 数组，元素形如 {\"key\":\"...\",\"note\":\"...\"}，顺序与输入一致，不要解释：\n"
)


def enrich(items, cfg, note_batch=12, trans_batch=8, on_progress=None):
    """给 items 补齐①中文评语（做了什么/怎么做/贡献）②中文翻译（标题/作者/单位/摘要/关键词）。

    只处理缺失字段（已有则跳过，便于跨次运行复用）；原地写入 items。
    大模型不可用时静默返回，不影响出报。

    on_progress：每处理完一批就回调一次（用来把翻译结果立刻写回数据库）。
    一轮翻译动辄几十分钟，不边翻边存的话中途断电/关机 = 全部白翻。
    """
    if not _llm_cfg(cfg) or not items:
        return items

    # 一轮最多翻多少篇（按 score 从高到低取）。0 = 不限。
    # 起因：报告动辄 700+ 篇，全翻要打上百次调用、跑一两个小时，夜里跑不完就前功尽弃。
    # 限量后每轮稳定在十几分钟；翻译结果已回写数据库，下周只翻新条目，覆盖会自然长起来。
    cap = int((cfg.get("llm") or {}).get("max_translate", 0) or 0)
    # 只翻译指定档位（默认全翻）。2026-10-05 起：清单动辄近 2000 篇，全翻既费 token
    # 又没人看得完 —— 用户要求只翻「一区top 及以上」（0/1/2/3 档）。空列表 = 不限档位。
    foldf = set(str(x) for x in ((cfg.get("llm") or {}).get("translate_folders") or []))
    q1x = float((cfg.get("download") or {}).get("q1_top_if", 7))

    def _cat_of(it):
        return str(it.get("_cat") or folder_of(it, q1x) or "")

    _inflight = 0
    if AI_MODE:
        # AI 板块：翻译/评语门控 = 本方向核心(core_hit) 或 顶会/顶刊线索(conf_hint)
        def _in_scope(it):
            return ai_priority(it)
        _inflight = sum(1 for it in items if _in_scope(it))
        if _inflight:
            print("  [AI] 翻译范围：本方向核心 + 顶会/顶刊线索（共 %d/%d 篇）" % (_inflight, len(items)))
    else:
        def _in_scope(it):
            return (not foldf) or (_cat_of(it) in foldf)
        _inflight = sum(1 for it in items if _in_scope(it))
        if foldf:
            print("  翻译范围：限定 %s 档（共 %d/%d 篇）" % ("/".join(sorted(foldf)), _inflight, len(items)))
    trace("gate", tag="enrich_scope", n_items=_inflight,
          extra={"total": len(items), "ai_mode": AI_MODE, "folders": sorted(foldf),
                 "cap": cap, "skipped_by_gate": len(items) - _inflight})

    def _top(lst, scope_note):
        lst = [it for it in lst if _in_scope(it)]
        if cap and len(lst) > cap:
            lst = sorted(lst, key=lambda x: -(x.get("score") or 0))[:cap]
            print("  本轮限量%s：%d 篇（按相关度取前 %d）" % (scope_note, len(lst), cap))
        return lst

    # 1) 中文评语（只给限定档位，见上 translate_folders / AI 门控）
    note_prompt = AI_NOTE_PROMPT if AI_MODE else NOTE_PROMPT
    todo_note = [it for it in items
                 if need_note(it) and (it.get("title") or "").strip()]
    todo_note = _top(todo_note, "评语")
    trace("gate", tag="note_queue", n_items=len(todo_note),
          extra={"batch": note_batch, "cap": cap})
    for i in range(0, len(todo_note), note_batch):
        chunk = todo_note[i:i + note_batch]
        payload = [{"key": it["key"], "title": it.get("title", ""),
                    "abstract": (it.get("abstract") or "")[:900]} for it in chunk]
        out = llm_chat([{"role": "user", "content": note_prompt + json.dumps(payload, ensure_ascii=False)}],
                       cfg, max_tokens=1800,
                       tag=("ai_note" if AI_MODE else "note"), n_items=len(chunk))
        by = {str(x.get("key")): x for x in _json_array(out) if isinstance(x, dict)}
        for it in chunk:
            r = by.get(str(it["key"]))
            # 只认中文评语：模型偶尔回英文，那和下面折叠的摘要重复，不如不写
            if r and (r.get("note") or "").strip() and has_cjk(r["note"]):
                it["note"] = (r["note"] or "").strip()[:300]
        if on_progress:
            try:
                on_progress(chunk)
            except Exception:
                pass

    # 2) 中文翻译（标题/作者/单位/摘要/关键词）
    todo_cn = [it for it in items
               if not (it.get("cn_abstract") or "").strip()
               or not (it.get("cn_authors") or "").strip()
               or not (it.get("cn_title") or "").strip()]
    todo_cn = _top(todo_cn, "翻译")
    trace("gate", tag="translate_queue", n_items=len(todo_cn),
          extra={"batch": trans_batch, "cap": cap})
    for i in range(0, len(todo_cn), trans_batch):
        chunk = todo_cn[i:i + trans_batch]
        payload = [{"key": it["key"], "title": it.get("title", ""),
                    "authors": it.get("authors", ""), "aff": it.get("affiliations", ""),
                    "keywords": it.get("topics", ""),
                    "abstract": (it.get("abstract") or "")[:1100]} for it in chunk]
        out = llm_chat([{"role": "user", "content": TRANS_PROMPT + json.dumps(payload, ensure_ascii=False)}],
                       cfg, max_tokens=6000, tag="translate", n_items=len(chunk))
        by = {str(x.get("key")): x for x in _json_array(out) if isinstance(x, dict)}
        for it in chunk:
            r = by.get(str(it["key"])) or {}
            if (r.get("cn_title") or "").strip():
                it["cn_title"] = r["cn_title"].strip()[:300]
            if (r.get("cn_authors") or "").strip():
                it["cn_authors"] = r["cn_authors"].strip()[:400]
            if (r.get("cn_aff") or "").strip():
                it["cn_aff"] = r["cn_aff"].strip()[:600]
            if (r.get("cn_abstract") or "").strip():
                it["cn_abstract"] = r["cn_abstract"].strip()[:1800]
            if (r.get("cn_keywords") or "").strip():
                it["cn_keywords"] = r["cn_keywords"].strip()[:300]
        if on_progress:
            try:
                on_progress(chunk)
            except Exception:
                pass
    return items


# ---------------------------------------------------------------- 单卡片
def _authors_short(s):
    aus = [a.strip() for a in (s or "").split(";") if a.strip()]
    if not aus:
        return ""
    if len(aus) > 4:
        return "; ".join(aus[:4]) + " 等"
    return "; ".join(aus)


def card_html(it, pdf_rel=None, pending=False, ez=""):
    # 优先用调用方预先算好的分类（AI 板块用 A0/A1/A2；糖尿病用 0-6）；否则按期刊档位回退
    code = it.get("_cat") or folder_of(it)
    col = cat_color(code)
    tier = it.get("tier") or ""
    badge = jcr.TIER_COLOR.get(tier, "#888780")
    tlabel = jcr.TIER_LABEL.get(tier, "未分级")
    q = it.get("quartile") or ""
    jif = it.get("jif") or 0
    if q and jif:
        tlabel += " · %s · IF %.1f" % (q, jif)
    elif q:
        tlabel += " · %s" % q
    layer = it.get("layer") or ""
    if AI_MODE:
        # AI 板块没有 keyword 分层（layer），用大模型给的 llm_tag 作为「相关度」维度
        layer = (it.get("llm_tag") or "").lower()
        lmark = {"core": "核心", "extension": "延伸", "method": "方法", "frontier": "前沿"}.get(layer, "")
    else:
        lmark = {"core": "核心", "proxy": "方法/相邻", "eco": "背景"}.get(layer, "")
    # LLM 增强识别标签（核心/延伸/方法/前沿），优先级高于纯关键词层
    tag = (it.get("llm_tag") or "").lower()
    taglabel = {"extension": "延伸", "method": "方法", "frontier": "前沿"}.get(tag, "")
    tagbadge = ('<span class="badge" style="background:#1E88A8">%s</span>' % taglabel) if taglabel else ""
    # 顶会/顶刊线索徽章：有点名具体会议/期刊时显示名称（如「顶会 NEURIPS」「顶刊 Nature MI」）
    _venue = (it.get("venue") or "").strip()
    confbadge = ('<span class="badge" style="background:%s">%s</span>'
                 % (jcr.CONF_HINT_COLOR, esc(_venue or jcr.CONF_HINT_LABEL))) \
        if it.get("conf_hint") else ""
    if AI_MODE:
        au = ('<span class="badge au">核心 · %s</span>' % esc(it.get("core_hit") or "")) \
            if it.get("core_hit") else ""
    else:
        au = ('<span class="badge au">导师组 · %s</span>' % esc(it.get("author_name") or "")) \
            if it.get("author_hit") else ""
    # 下载状态徽章（已下载 / 未下载）
    dl = bool(pdf_rel)
    dlbadge = ('<span class="badge dlok">已下载</span>' if dl
               else '<span class="badge dlno">未下载</span>')
    # 「延续 / 沿用」徽章：不在本批、但为避免漏读而继续展示的条目
    #   延续(_carry=1)：跨周的顶刊，检索窗口里没再撞见 → 继续展示
    #   沿用(_carry=2)：上一版报告里出现过，本版照旧列出 → 保证「看过的不会消失」
    if it.get("_carry") == 2:
        carrybadge = ('<span class="badge" style="background:#7A6A52" title="上一版报告里已出现，'
                      '本版照旧列出，保证看过的条目不会消失">沿用</span>')
    elif it.get("_carry"):
        carrybadge = ('<span class="badge" style="background:#7A6A52" title="上一批已发现的顶刊，'
                      '本批未重复捕获，继续展示避免漏读">延续</span>')
    else:
        carrybadge = ""
    # 贴合度徽章（#2）：大模型对"与用户主攻方向贴合度"的判定，high/medium 才显示
    # （AI 模式 = 本方向贴合度；糖尿病 = 导师组方向贴合度）
    _fit = (it.get("advisor_fit") or "").lower()
    fitbadge = ""
    if _fit == "high":
        fitbadge = '<span class="badge" style="background:#1E8449" title="%s高度贴合">%s·高</span>' % (
            ("与用户主攻方向（医学AI / 大模型智能体）" if AI_MODE else "与导师组方向（CGM/TIR、腹型肥胖、AI糖尿病管理、基层防治等）"),
            ("本方向" if AI_MODE else "导师组方向"))
    elif _fit == "medium":
        fitbadge = '<span class="badge" style="background:#B9770E" title="%s相邻/方法可迁移">%s·中</span>' % (
            ("与用户主攻方向" if AI_MODE else "与导师组方向"),
            ("本方向" if AI_MODE else "导师组方向"))
    # 方法可迁移徽章（#5）：高价值但本方向非核心、却被 LLM 判为方法/前沿/延伸的文献，
    # 单独标出来提醒值得一看。
    #   糖尿病：高分区(4-6 档) ｜ AI：点名了顶会/顶刊线索(conf_hint) 但非本方向核心
    _cat = str(code)
    if AI_MODE:
        _mt = bool(it.get("conf_hint")) and not it.get("core_hit") and (tag in ("method", "frontier", "extension"))
    else:
        _mt = (_cat in ("4", "5", "6")) and (tag in ("method", "frontier", "extension"))
    mtbadge = ('<span class="badge" style="background:#16A085" title="%s方法可迁移/前沿，建议关注">方法可迁移</span>'
               % ("顶会/顶刊线索但非本方向核心，" if AI_MODE else "高分区但非本方向核心，")) if _mt else ""
    title = esc(it.get("title") or "")
    cn = esc(it.get("cn_title") or "")
    cn_html = ('<div class="cn">%s</div>' % cn) if cn else ""
    authors = esc(_authors_short(it.get("authors")))
    cn_au = esc(it.get("cn_authors") or "")
    au_html = ('<div class="au">作者：%s</div>' % authors) if authors else ""
    if cn_au:
        au_html += '<div class="au-cn">作者（中）：%s</div>' % cn_au
    affs = esc(it.get("affiliations") or "")
    aff_html = ('<div class="aff">单位：%s</div>' % affs) if affs else ""
    cn_aff = esc(it.get("cn_aff") or "")
    if cn_aff:
        aff_html += '<div class="aff-cn">单位（中）：%s</div>' % cn_aff
    # 评语只展示中文。英文的（早前规则兜底把摘要前两句写进了 note）不显示——
    # 那是"评语变成摘要"，而且下方折叠的「摘要」里本来就有同一段，纯属重复。
    _raw_note = (it.get("note") or "").strip()
    note = esc(_raw_note) if has_cjk(_raw_note) else ""
    note_html = ('<div class="note">%s</div>' % note) if note else ""
    # 导师组但单位非六院/交大：黄色提示（可能合作/访问署名，已按导师组保留）
    # （AI 板块没有"六院/交大"概念，此提示仅在糖尿病模式生效）
    au_warn = ""
    if (not AI_MODE) and it.get("author_hit") and it.get("affiliations"):
        low = it["affiliations"].lower()
        if not any(k in low for k in ("shanghai sixth", "sixth people",
                                      "jiao tong", "diabetes institute", "shanghai diabetes")):
            au_warn = ('<div class="au-warn">⚠️ 单位未识别为六院/交大署名，可能为合作或访问期间署名，'
                       '已按导师组保留，请核对</div>')
    # 链接
    url = it.get("url") or ("https://doi.org/%s" % it["doi"] if it.get("doi") else "#")
    doi = it.get("doi") or ""
    pmid = it.get("pmid") or ""
    links = ['<a class="lk" href="%s" target="_blank">原文</a>' % esc(url)]
    if doi:
        links.append('<a class="lk" href="https://doi.org/%s" target="_blank">DOI</a>' % esc(doi))
    if pmid:
        links.append('<a class="lk" href="https://pubmed.ncbi.nlm.nih.gov/%s/" target="_blank">PubMed</a>' % esc(pmid))
    if ez and doi:
        links.append('<a class="lk" href="%shttps://doi.org/%s" target="_blank">交大图书馆</a>' % (ez, esc(doi)))
    if pdf_rel:
        # 本地 PDF 相对路径：中文/空格需按 URL 规则编码，否则部分浏览器点不开
        href = urllib.parse.quote(str(pdf_rel).replace("\\", "/"), safe="/")
        links.append('<a class="lk pdf" href="%s" target="_blank">打开本地PDF</a>' % esc(href))
    if pending:
        links.append('<a class="lk dl" href="https://scholar.google.com/scholar?q=%s" target="_blank">谷歌学术</a>'
                     % urllib.parse.quote(it.get("title") or ""))
    # 摘要：中文在前、英文在后
    abs_en = esc(it.get("abstract") or "")
    abs_cn = esc(it.get("cn_abstract") or "")
    abs_inner = ""
    if abs_cn:
        abs_inner += '<div class="abs"><b>中文：</b>%s</div>' % abs_cn
    if abs_en:
        abs_inner += '<div class="abs en">%s</div>' % abs_en
    abs_html = ('<details><summary>摘要</summary>%s</details>' % abs_inner) if abs_inner else ""
    meta = "%s ｜ %s ｜ %s ｜ %s" % (
        esc(it.get("journal") or ""), esc(it.get("pub_date") or ""),
        esc((it.get("topics") or "").replace("|", " / ")), esc(it.get("source") or ""))
    cn_kw = esc(it.get("cn_keywords") or "")
    kw_html = ('<div class="kw">中文关键词：%s</div>' % cn_kw) if cn_kw else ""
    return (
        '<div class="card" data-cat="%s" data-tier="%s" data-layer="%s" data-author="%s" data-tag="%s" '
        'data-dl="%s" data-conf="%s" data-carry="%s" style="border-left:5px solid %s">'
        '<div class="row"><span class="cat" style="background:%s">%s</span>'
        '<span class="badge" style="background:%s">%s</span>'
        '%s%s%s%s%s%s%s<span class="lay">%s</span><span class="score">%s</span></div>'
        '<a class="title" href="%s" target="_blank">%s</a>%s'
        '<div class="meta">%s</div>'
        '%s%s%s'
        '%s%s%s'
        '<div class="links">%s</div>'
        '</div>'
    ) % (code, esc(tier), esc(layer), "1" if (it.get("core_hit") or it.get("author_hit")) else "0",
         esc(tag or layer), "1" if dl else "0",          "1" if it.get("conf_hint") else "0",
         str(int(it.get("_carry") or 0)),
         col, col, esc(cat_label(code)),
     badge, esc(tlabel), confbadge, au, tagbadge, dlbadge, carrybadge, fitbadge, mtbadge, esc(lmark),
     ("%d 分" % it["score"]) if it.get("score") else "",
         esc(url), title, cn_html,
         meta, au_html, aff_html, kw_html,
         note_html, abs_html, au_warn,
         "".join(links))


# ---------------------------------------------------------------- 页面外壳
CSS = """
*{box-sizing:border-box}
body{margin:0;padding:24px;background:#F7F6F3;color:#2C2C2A;
font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;line-height:1.6}
.wrap{max-width:1000px;margin:0 auto}
h1{font-size:20px;font-weight:600;margin:0 0 4px}
.sub{font-size:13px;color:#5F5E5A;margin-bottom:8px}
.bar{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0 18px;position:sticky;top:0;
background:#F7F6F3;padding:10px 0;z-index:5}
select,input{padding:7px 10px;border:0.5px solid #D3D1C7;border-radius:8px;background:#fff;font-size:13px}
input{flex:1;min-width:200px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px}
.card{background:#fff;border:0.5px solid #E2E0D6;border-radius:12px;padding:13px 15px}
.row{display:flex;align-items:center;gap:6px;margin-bottom:7px;flex-wrap:wrap}
.cat{font-size:11px;color:#fff;padding:2px 8px;border-radius:6px;font-weight:600}
.badge{font-size:11px;color:#fff;padding:2px 8px;border-radius:6px}
.lay{font-size:11px;color:#7A4E9E;background:#F3EDF9;padding:2px 7px;border-radius:6px}
.score{margin-left:auto;font-size:11px;color:#185FA5}
.badge.au{background:#B8860B}
.badge.dlok{background:#2E7D32}
.badge.dlno{background:#9A9A93}
.title{display:block;font-size:14.5px;font-weight:600;color:#0C447C;text-decoration:none;margin-bottom:3px}
.title:hover{text-decoration:underline}
.cn{font-size:13px;color:#1A1A1A;margin-bottom:4px}
.meta{font-size:12px;color:#5F5E5A;margin-bottom:4px}
.au{font-size:12px;color:#3A3A38}
.au-cn{font-size:11.5px;color:#6B6A63}
.aff{font-size:11.5px;color:#6B6A63;font-style:italic}
.aff-cn{font-size:11.5px;color:#6B6A63;margin-bottom:4px}
.kw{font-size:11.5px;color:#6B6A63;margin-bottom:4px}
.abs.en{color:#8A8A84;border-top:1px dashed #E2E0D6;margin-top:5px;padding-top:5px}
.note{font-size:13px;color:#444441;background:#FFF8E6;border-radius:6px;padding:6px 9px;margin:4px 0}
.au-warn{font-size:12px;color:#7a5b00;background:#FFF4CC;border:0.5px solid #F0D98C;border-radius:6px;padding:5px 9px;margin:4px 0}
.abs{font-size:12.5px;color:#444;max-height:240px;overflow:auto}
.abs summary{cursor:pointer;color:#185FA5;font-size:12px}
.links{margin-top:8px;display:flex;gap:10px;flex-wrap:wrap}
.lk{color:#0C447C;text-decoration:none;font-size:12.5px;border-bottom:1px solid #cdd;padding-bottom:1px}
.lk:hover{text-decoration:underline}
.lk.pdf{color:#fff;background:#2E7D32;padding:2px 9px;border-radius:6px;border:0}
.lk.dl{color:#fff;background:#B8860B;padding:2px 9px;border-radius:6px;border:0}
.gh{font-size:16px;font-weight:600;margin:26px 0 12px;padding-left:10px;border-left:5px solid #ccc}
.tip{background:#EAF3FB;padding:11px 14px;border-radius:8px;font-size:13px;margin-bottom:8px;color:#135}
.count{font-size:12px;color:#888780;font-weight:400}
.rev-h2{font-size:17px;font-weight:600;margin:28px 0 6px;color:#0C447C}
.rev-p{font-size:13.5px;color:#333;background:#fff;border:0.5px solid #E2E0D6;border-radius:10px;padding:12px 14px;margin:8px 0}
.rev-ref{font-size:12.5px;color:#333;margin:5px 0 5px 18px;text-indent:-18px;line-height:1.5}
"""


def page(title, sub, bar_html, body_html):
    return """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>%s</title><style>%s</style></head><body><div class="wrap">
<h1>%s</h1><div class="sub">%s</div>%s
%s
</div><script>
var cs=[].slice.call(document.querySelectorAll('.card'));
function apply(){var c=document.getElementById('fc').value,t=document.getElementById('ft').value,
l=document.getElementById('fl').value,a=document.getElementById('fa').value,
g=document.getElementById('ftag').value,d=document.getElementById('fldl').value,
k=document.getElementById('fconf').value,
y=document.getElementById('fcarry').value,
q=(document.getElementById('fq').value||'').toLowerCase();
cs.forEach(function(x){var ok=(!c||x.dataset.cat===c)&&(!t||x.dataset.tier===t)&&
(!l||x.dataset.layer===l)&&(!a||x.dataset.author===a)&&
(!g||x.dataset.tag===g)&&(!d||x.dataset.dl===d)&&
(!k||x.dataset.conf===k)&&(!y||x.dataset.carry===y)&&
(!q||x.textContent.toLowerCase().indexOf(q)>=0);x.style.display=ok?'':'none';});
var n=cs.filter(function(x){return x.style.display!=='none';}).length;
var el=document.getElementById('cnt');if(el)el.textContent='当前显示 '+n+' 篇';}
['fc','ft','fl','fa','ftag','fldl','fconf','fcarry'].forEach(function(i){document.getElementById(i).onchange=apply;});
document.getElementById('fq').oninput=apply;
apply();
</script></body></html>""" % (title, CSS, title, sub, bar_html, body_html)


def _bar(cats_present, tiers_present, with_author=True, ai=False):
    if ai:
        order = [c for c in jcr.AI_CAT_ORDER if c in cats_present]
    else:
        order = [c for c in jcr.CAT_ORDER if c in cats_present] + \
                [c for c in jcr.AI_CAT_NAME if c in cats_present]
    cat_opts = "".join('<option value="%s">%s</option>' % (c, cat_label(c)) for c in order)
    tier_opts = "".join('<option value="%s">%s</option>' % (esc(t), jcr.TIER_LABEL.get(t, t))
                        for t in sorted(tiers_present, key=lambda x: jcr.TIER_ORDER.get(x, 9)))
    if ai:
        fa = ('<select id="fa"><option value="">全部方向</option>'
              '<option value="1">只看本方向核心</option></select>') if with_author else ""
    else:
        fa = ('<select id="fa"><option value="">全部作者</option>'
              '<option value="1">只看导师组</option></select>') if with_author else ""
    ftag = ('<select id="ftag"><option value="">全部标签</option>'
            '<option value="core">核心</option><option value="extension">延伸</option>'
            '<option value="method">方法</option><option value="frontier">前沿</option></select>')
    fldl = ('<select id="fldl"><option value="">全部下载状态</option>'
            '<option value="1">已下载</option><option value="0">未下载</option></select>')
    fconf = ('<select id="fconf"><option value="">全部顶会线索</option>'
             '<option value="1">仅顶会线索</option><option value="0">非顶会线索</option></select>')
    if ai:
        flayer = ('<select id="fl"><option value="">全部标签</option>'
                  '<option value="core">核心</option><option value="extension">延伸</option>'
                  '<option value="method">方法</option><option value="frontier">前沿</option></select>')
    else:
        flayer = ('<select id="fl"><option value="">全部相关度</option>'
                  '<option value="core">核心</option><option value="proxy">方法/相邻</option>'
                  '<option value="eco">背景</option></select>')
    fcarry = ('<select id="fcarry"><option value="">本周+延续+沿用</option>'
              '<option value="0">只看本周新增</option>'
              '<option value="1">只看延续</option>'
              '<option value="2">只看沿用</option></select>')
    return ('<div class="bar"><select id="fc"><option value="">全部分类</option>%s</select>'
            '<select id="ft"><option value="">全部期刊等级</option>%s</select>'
            '%s%s%s%s%s%s<input id="fq" placeholder="搜索标题 / 期刊 / 作者 / 关键词"></div>'
            '<div class="tip" id="cnt"></div>') % (cat_opts, tier_opts, flayer, fa, ftag, fldl, fconf, fcarry)


# ---------------------------------------------------------------- 对外：库 / 待下载 / 综述
def write_library(folder, label, items, pdf_map, cfg, save_cb=None):
    q1 = float((cfg.get("download") or {}).get("q1_top_if", 7))
    items = [dict(it) for it in items]
    for it in items:
        # 糖尿病：按期刊档位归 0-6；AI 板块：按研究方向归 A0/A1/A2
        if AI_MODE:
            it["_cat"] = it.get("cat") or "A2"
        else:
            it["_cat"] = folder_of(it, q1)
    # 补全中文评语（做了什么/怎么做/贡献）+ 中文翻译（标题/作者/单位/摘要/关键词）
    # 只填缺失字段，已填的不动，跨次运行可复用；大模型不可用时静默跳过
    # save_cb：每翻完一批就回调写库，避免长任务中途中断前功尽弃
    enrich(items, cfg, on_progress=save_cb)
    items.sort(key=lambda x: (x["_cat"], -(x.get("score") or 0)))
    cats_present = {it["_cat"] for it in items}
    tiers_present = {it.get("tier") or "" for it in items}
    body = ""
    for c in (jcr.AI_CAT_ORDER if AI_MODE else jcr.CAT_ORDER):
        grp = [it for it in items if it["_cat"] == c]
        if not grp:
            continue
        body += '<h2 class="gh" style="border-color:%s">%s <span class="count">%d 篇</span></h2>' \
                '<div class="grid">%s</div>' % (cat_color(c), esc(cat_label(c)), len(grp),
                                                "".join(card_html(it, pdf_map.get(it["key"])) for it in grp))
    if AI_MODE:
        sub = "共 %d 篇 · 含 核心 / 延伸 / 方法 / 前沿（顶部可按标签/方向筛选）· 按 大模型智能体→医学AI→通用AI 排序" % len(items)
    else:
        sub = "共 %d 篇 · 含核心 / 延伸 / 方法 / 前沿（顶部可按标签筛选）· 按 导师组→世界顶刊→领域顶刊→一区top→一区→二区→其他 排序" % len(items)
    html = page("本周文献清单 · " + label, sub,
                _bar(cats_present, tiers_present, with_author=AI_MODE, ai=AI_MODE), body)
    p1 = os.path.join(folder, F_LIB)
    p2 = os.path.join(folder, F_TOTAL)
    with open(p1, "w", encoding="utf-8") as f:
        f.write(html)
    with open(p2, "w", encoding="utf-8") as f:
        f.write(html)
    # CSV 数据版（可导入 Zotero / Excel）
    cpath = os.path.join(folder, F_CSV)
    with open(cpath, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["序号", "分类", "标题", "中文标题", "中文作者", "中文单位", "中文关键词",
                    "中文摘要", "作者", "作者单位", "期刊", "期刊标签",
                    "发表时间", "影响因子", "JCR分区", "DOI", "PMID", "链接", "相关度", "分值",
                    "是否已下载", "本地PDF"])
        for i, it in enumerate(items, 1):
            w.writerow([i, cat_label(it["_cat"]), it.get("title", ""), it.get("cn_title", ""),
                        it.get("cn_authors", ""), it.get("cn_aff", ""), it.get("cn_keywords", ""),
                        it.get("cn_abstract", ""),
                        it.get("authors", ""), it.get("affiliations", ""), it.get("journal", ""),
                        jcr.TIER_LABEL.get(it.get("tier") or "", ""),
                        it.get("pub_date", ""), it.get("jif") or "", it.get("quartile") or "",
                        it.get("doi", ""), it.get("pmid", ""), it.get("url", ""),
                        ({"core": "核心", "extension": "延伸", "method": "方法", "frontier": "前沿"}.get(it.get("llm_tag") or "", "")
                         if AI_MODE else
                         {"core": "核心", "proxy": "方法/相邻", "eco": "背景"}.get(it.get("layer") or "", "")),
                        it.get("score") or "",
                        "是" if pdf_map.get(it["key"]) else "否",
                        pdf_map.get(it["key"]) or ""])
    _cleanup_legacy(folder)
    print("文献清单：%s （%d 篇，题录总表 html+csv 同步生成）" % (p1, len(items)))
    return p1


def write_pending(folder, label, items, cfg, ez=""):
    q1 = float((cfg.get("download") or {}).get("q1_top_if", 7))
    items = [dict(it) for it in items]
    for it in items:
        it["_cat"] = (it.get("cat") or "A2") if AI_MODE else folder_of(it, q1)
    enrich(items, cfg)
    items.sort(key=lambda x: (x["_cat"], -(x.get("score") or 0)))
    cats_present = {it["_cat"] for it in items}
    tiers_present = {it.get("tier") or "" for it in items}
    body = ""
    for c in (jcr.AI_CAT_ORDER if AI_MODE else jcr.CAT_ORDER):
        grp = [it for it in items if it["_cat"] == c]
        if not grp:
            continue
        body += '<h2 class="gh" style="border-color:%s">%s <span class="count">%d 篇</span></h2>' \
                '<div class="grid">%s</div>' % (cat_color(c), esc(cat_label(c)), len(grp),
                                                "".join(card_html(it, pending=True, ez=ez) for it in grp))
    sub = "共 %d 篇未能自动下载 · 点卡片里的链接（联网/VPN/机构账号）即可获取全文" % len(items)
    html = page("待下载清单 · " + label, sub,
                _bar(cats_present, tiers_present, with_author=AI_MODE, ai=AI_MODE), body)
    p = os.path.join(folder, F_PEND)
    with open(p, "w", encoding="utf-8") as f:
        f.write(html)
    _cleanup_legacy(folder)
    print("待下载清单：%s （%d 篇）" % (p, len(items)))
    return p


# ---------------------------------------------------------------- 本周综述（分组 + APA）
def _apa(it):
    aus = [a.strip() for a in (it.get("authors") or "").split(";") if a.strip()]
    if aus:
        au = ", ".join(aus[:3]) + ("，等" if len(aus) > 3 else "")
    else:
        au = "Anonymous"
    year = (it.get("pub_date") or "")[:4] or "n.d."
    title = it.get("title") or ""
    j = it.get("journal") or ""
    doi = it.get("doi") or ""
    s = "%s (%s). %s. <i>%s</i>." % (esc(au), year, esc(title), esc(j))
    if doi:
        s += ' <a href="https://doi.org/%s" target="_blank">https://doi.org/%s</a>' % (esc(doi), esc(doi))
    return s


METHOD_GROUPS = [
    ("因果推断 / 卫生政策评估", ["difference-in-differences", "did", "instrumental variable",
                            "propensity score", "双重机器学习", "double machine learning",
                            "回归断点", "面板数据", "panel data", "quasi-experimental",
                            "health policy", "health policy evaluation", "卫生政策"]),
    ("机器学习 / AI / 大模型", ["machine learning", "deep learning", "artificial intelligence",
                          "large language model", "llm", "natural language processing",
                          "multimodal", "预测模型", "risk prediction", "nomogram"]),
    ("队列与遗传流行病学", ["cohort", "队列", "mendelian randomization", "孟德尔随机",
                        "genome-wide", "polygenic", "genetic risk"]),
    ("文献计量 / 科学学", ["bibliometric", "scientometric", "citation analysis", "science mapping",
                       "文献计量"]),
    ("卫生经济 / 成本效果 / 实施科学", ["cost-effectiveness", "economic evaluation", "health economics",
                                  "implementation science", "真实世界", "real-world", "quality of care"]),
    ("数字健康 / 可穿戴 / 远程医疗", ["digital health", "mobile health", "telemedicine", "wearable",
                                 "可穿戴", "smartphone", "chatbot", "remote monitoring"]),
]
CONTENT_GROUPS = [
    ("CGM / 血糖监测 / TIR", ["continuous glucose monitoring", "time in range", "tir",
                          "glucose variability", "cgm", "血糖监测"]),
    ("糖尿病并发症", ["retinopathy", "nephropathy", "neuropathy", "diabetic foot",
                  "cardiovascular complication", "microvascular", "macrovascular",
                  "糖尿病并发症", "肾病", "视网膜"]),
    ("基层慢病管理 / 初级卫生保健", ["primary care", "community-based", "grassroots",
                                "primary health care", "integrated care", "基层", "初级卫生保健",
                                "national diabetes prevention"]),
    ("肥胖 / 肌少症 / 营养", ["obesity", "overweight", "sarcopenia", "metabolic syndrome",
                          "体重管理", "营养", "abdominal obesity"]),
    ("精准 / 预测 / 风险模型", ["precision diabetes", "precision medicine", "risk score",
                            "risk prediction", "prediction model", "预警", "筛查", "screening"]),
    ("预防 /  remission / 管理", ["diabetes prevention", "diabetes remission", "diabetes management",
                              "diabetes care", "diabetes treatment", "预防", "缓解"]),
]


def _group(items, kw):
    out = []
    for it in items:
        txt = ("%s %s %s" % (it.get("title", ""), it.get("abstract", ""), it.get("topics", ""))).lower()
        if any(k in txt for k in kw):
            out.append(it)
    return out


def _review_section(title, grp, cfg, topn=8):
    if not grp:
        return ""
    grp = sorted(grp, key=lambda x: -(x.get("score") or 0))[:topn]
    summary, review = "", ""
    l = _llm_cfg(cfg)
    if l:
        bulk = "\n".join("%d. %s — %s" % (i + 1, it.get("title", ""),
                                          (it.get("note") or it.get("abstract") or "")[:280])
                         for i, it in enumerate(grp))
        dom = "医学人工智能 / 大模型·智能体·多模态" if AI_MODE else "内分泌 / 糖尿病"
        prompt = ("你是医学文献综述助手。下面是一周%s方向、按「%s」归类的 %d 篇新文献（标题与摘要节选）。\n"
                  "请只输出一个 JSON 对象，含两个字段：\n"
                  "  \"summary\"：2-3 句中文，概括这组文献共同关注的研究问题 / 方法 / 趋势（提炼共性，不要逐篇罗列），120 字内；\n"
                  "  \"review\"：一段 4-6 句的中文「文献综述」式文字，按主题 / 方法 / 结论串联这几篇，"
                  "体现它们如何共同推进该方向的认识，250 字内。\n"
                  "严格只输出 JSON，不要解释：\n%s") % (dom, title, len(grp), bulk)
        out = llm_chat([{"role": "user", "content": prompt}], cfg, max_tokens=800,
                       tag=("ai_review_section" if AI_MODE else "review_section"),
                       n_items=len(grp))
        if out:
            m = re.search(r"\{.*\}", out, re.S)
            if m:
                try:
                    obj = json.loads(m.group(0))
                    summary = (obj.get("summary") or "").strip()
                    review = (obj.get("review") or "").strip()
                except Exception:
                    pass
    if not summary:
        summary = "本周该方向共 %d 篇新文献，重点见下方参考文献（按相关度排序）。" % len(grp)
    if not review:
        review = summary
    refs = "".join('<div class="rev-ref">%s</div>' % _apa(it) for it in grp)
    return ('<h2 class="rev-h2">%s <span class="count">（%d 篇）</span></h2>'
            '<div class="rev-p"><b>概括：</b>%s</div>'
            '<div class="rev-p"><b>文献综述：</b>%s</div>%s'
            ) % (esc(title), len(grp), esc(summary), esc(review), refs)


def _must_read(items, cfg, topn=5):
    """本周必读 TopN（#4）：按相关度取前若干篇，让大模型精选最值得本周精读的 N 篇 + 一句中文理由。
    糖尿病：一区top 及以上（0-3 档）；AI 板块：本方向核心(core_hit) 或 顶会/顶刊线索(conf_hint)。
    无 LLM 或候选过少则返回空。"""
    l = _llm_cfg(cfg)
    if not l:
        return ""
    if AI_MODE:
        cands = [it for it in items if ai_priority(it)]
    else:
        cands = [it for it in items if str(it.get("_cat") or folder_of(it)) in ("0", "1", "2", "3")]
    cands = sorted(cands, key=lambda x: -(x.get("score") or 0))[:25]
    if len(cands) < 3:
        return ""
    bulk = "\n".join("%d. %s — %s，相关度%d分 — %s" % (
        i + 1, it.get("title", ""), it.get("journal", ""), it.get("score") or 0,
        (it.get("note") or it.get("abstract") or "")[:200]) for i, it in enumerate(cands))
    if AI_MODE:
        who = "人工智能方向（医学AI / 大模型智能体）博士生导师的文献助理"
        scope = "本方向核心 / 顶会顶刊线索"
    else:
        who = "糖尿病方向博士生导师的文献助理"
        scope = "一区top 及以上（含导师组/顶刊）"
    prompt = ("你是%s。下面是本周%s的新文献"
              "（序号 | 标题 | 期刊 | 相关度 | 摘要节选）。\n"
              "请挑出【最值得本周精读】的 %d 篇，输出一个 JSON 数组（顺序即推荐优先级），每个元素为 "
              "{\"key\":对应序号(int), \"why\":一句中文理由(30-50字，说清为什么值得读：方法/数据/结论/"
              "与用户主攻方向的关联)}。\n严格只输出 JSON，不要解释：\n%s") % (who, scope, topn, bulk)
    out = llm_chat([{"role": "user", "content": prompt}], cfg, max_tokens=900,
                   tag=("ai_must_read" if AI_MODE else "must_read"), n_items=len(cands))
    if not out:
        return ""
    m = re.search(r"\[.*\]", out, re.S)
    if not m:
        return ""
    try:
        arr = json.loads(m.group(0))
    except Exception:
        return ""
    by_idx = {i + 1: it for i, it in enumerate(cands)}
    picks = []
    for o in arr:
        try:
            it = by_idx.get(int(o.get("key")))
            if not it:
                continue
            why = (o.get("why") or "").strip()
            if not has_cjk(why):       # 只接受中文理由，英文/空不渲染
                why = ""
            picks.append((it, why))
        except Exception:
            continue
    picks = picks[:topn]
    if not picks:
        return ""
    rows = ""
    for it, why in picks:
        rows += '<div class="rev-ref"><b>%s</b><br>%s%s</div>' % (
            esc(it.get("title", "")), _apa(it),
            ('<br><span style="color:#C0392B">📌 %s</span>' % esc(why)) if why else "")
    return ('<h2 class="rev-h2">本周必读 Top %d</h2>'
            '<div class="rev-p">由大模型从一区top 及以上中精选，建议优先精读。</div>%s'
            ) % (len(picks), rows)


def _trend_and_linkage(items, cfg, week=None):
    """#6 趋势预警 + 跨文献关联（均规则驱动，不额外调 LLM）。

    - 趋势：本周 vs 上周 的 llm_tag 分布，某类明显增多则预警（提示下周重点跟）。
    - 关联：本周文献里被 >=2 篇共用的数据集/队列，列出可交叉对比。
    返回 (trend_html, linkage_html)，调用方自行拼接。
    """
    trend_html, linkage_html = "", ""

    # 跨文献关联：共享数据集 / 队列（AI 板块用 AI 基准数据集，糖尿病用队列数据集）
    DATASETS = jcr.AI_DATASETS if AI_MODE else [
        ("NHANES", "nhanes"),
        ("UK Biobank", "uk biobank"),
        ("CHARLS", "china health and retirement"),
        ("FinnGen", "finngen"),
        ("UKPDS", "ukpds"),
        ("ACCORD", "accord"),
        ("Look AHEAD", "look ahead"),
        ("DIAMOND", "diamond cohort"),
    ]
    dmap = {}
    for it in items:
        txt = ("%s %s" % (it.get("title", ""), it.get("abstract", ""))).lower()
        for name, kw in DATASETS:
            if kw in txt:
                dmap.setdefault(name, []).append(it)
    groups = [(n, its) for n, its in dmap.items() if len(its) >= 2]
    if groups:
        rows = ""
        for n, its in groups:
            refs = "".join('<div class="rev-ref">%s</div>' % _apa(x) for x in its[:6])
            rows += '<div class="rev-b"><b>%s</b>（%d 篇共用）：%s</div>' % (esc(n), len(its), refs)
        linkage_html = ('<h2 class="rev-h2">跨文献关联 · 共享数据集</h2>'
                        '<div class="rev-p">以下数据集 / 队列被多篇本周文献共用，可交叉对比结论、互相印证。</div>%s' % rows)

    # 趋势预警：本周 vs 上周
    if week:
        try:
            import sqlite3
            # 本周 tag 分布直接从本批 items 计算（准、且不受"按天取数丢批"影响）
            cur = {}
            for it in items:
                t = (it.get("llm_tag") or "").lower()
                cur[t] = cur.get(t, 0) + 1
            if AI_MODE:
                # AI 板块：查 ai.db，上周 = 库里比本周标签更早的最新一周
                con = sqlite3.connect(os.path.join(BASE, "ai.db"))
                try:
                    pw = con.execute("SELECT max(week) FROM papers WHERE week < ?", (week,)).fetchone()[0]
                    prev = {}
                    if pw:
                        for t, c in con.execute(
                            "SELECT COALESCE(llm_tag,'') tag, count(*) c FROM papers "
                            "WHERE week=? GROUP BY tag", (pw,)):
                            prev[t] = c
                finally:
                    con.close()
            else:
                # 糖尿病：查 papers.db，按【所属 ISO 周】区间对比上周
                from datetime import date as _date, timedelta as _td
                _lo, _hi = jcr.week_range(week)
                pm = jcr.week_bounds(_date.fromisoformat(week) - _td(days=7))[0].isoformat()
                plo, phi = jcr.week_range(pm)
                con = sqlite3.connect(os.path.join(BASE, "data", "papers.db"))
                try:
                    def _tag_counts(lo, hi):
                        d = {}
                        for t, c in con.execute(
                            "SELECT COALESCE(llm_tag,'') tag, count(*) c FROM papers "
                            "WHERE week BETWEEN ? AND ? GROUP BY tag", (lo, hi)):
                            d[t] = c
                        return d
                    prev = _tag_counts(plo, phi)
                finally:
                    con.close()
            alerts = []
            for tag in ("method", "frontier", "extension", "core"):
                c = cur.get(tag, 0)
                p = prev.get(tag, 0)
                if c >= p + 5 or (p == 0 and c >= 4):
                    arrow = "明显增多" if c >= p + 10 else "增多"
                    alerts.append("%s 类 %d 篇（上周 %d，%s）" % (tag, c, p, arrow))
            if alerts:
                trend_html = ('<div style="background:#FFF4E5;border-left:4px solid #E67E22;'
                              'padding:10px 14px;margin:12px 0;border-radius:6px">'
                              '<b>📈 趋势预警：</b>%s。建议下周重点关注这些方向的延续与新进展。</div>'
                              % "；".join(alerts))
        except Exception:
            pass
    return trend_html, linkage_html


def write_review(folder, label, items, cfg, week=None):
    items = [dict(it) for it in items]
    enrich(items, cfg)
    trend_html, linkage_html = _trend_and_linkage(items, cfg, week)   # #6 趋势预警 + 跨文献关联
    # 导师组 / 本方向核心 优先
    if AI_MODE:
        author_items = [it for it in items if it.get("core_hit")]
        sec_head = "本方向核心文献"
        sec_prose = "以下为本周「本方向核心」(core_hit) 文献 —— 用户点名要重点跟的 AI 前沿方向，详见参考文献。"
    else:
        author_items = [it for it in items if it.get("author_hit")]
        sec_head = "导师组文献"
        sec_prose = "导师组（贾伟平院士 / 蔡淳 / 刘月星 / 鲍萍萍 / 黄珏）本周新发 %d 篇，详见参考文献。" % len(author_items)
    body = trend_html + _must_read(items, cfg)   # #6 趋势预警置顶；#4 本周必读 Top5 紧随
    author_items = sorted(author_items, key=lambda x: -(x.get("score") or 0))[:12]
    if author_items:
        refs = "".join('<div class="rev-ref">%s</div>' % _apa(it) for it in author_items)
        body += '<h2 class="rev-h2">%s <span class="count">（%d 篇）</span></h2>' \
                '<div class="rev-p">%s</div>%s' % (sec_head, len(author_items), sec_prose, refs)
    # 研究方法 / 内容分组（AI 板块用 AI 方法/内容组）
    m_groups = jcr.AI_METHOD_GROUPS if AI_MODE else METHOD_GROUPS
    c_groups = jcr.AI_CONTENT_GROUPS if AI_MODE else CONTENT_GROUPS
    body += '<h2 class="rev-h2">按研究方法</h2>'
    for title, kw in m_groups:
        body += _review_section(title, _group(items, kw), cfg)
    # 研究内容
    body += '<h2 class="rev-h2">按研究内容</h2>'
    for title, kw in c_groups:
        body += _review_section(title, _group(items, kw), cfg)
    # 最相关萃取（糖尿病：去掉导师组；AI：去掉本方向核心）
    top = sorted(items, key=lambda x: -(x.get("score") or 0))[:15]
    if AI_MODE:
        top = [it for it in top if not it.get("core_hit")][:15]
    else:
        top = [it for it in top if not it.get("author_hit")][:15]
    if top:
        refs = "".join('<div class="rev-ref">%s</div>' % _apa(it) for it in top)
        head = "本方向高相关萃取 Top %d" if AI_MODE else "最相关萃取 Top %d"
        body += '<h2 class="rev-h2">%s</h2><div class="rev-p">以下为本周相关度最高的文献，' \
                '建议优先精读。</div>%s' % (head % len(top), refs)
    # 前沿专栏：大模型判为 frontier 的文献，单列展示并附 idea 备注
    frontier_items = [it for it in items if (it.get("llm_tag") or "").lower() == "frontier"]
    if frontier_items:
        frontier_items = sorted(frontier_items, key=lambda x: -(x.get("score") or 0))
        body += '<h2 class="rev-h2">前沿专栏 · 方向 / 内容 / 方法 <span class="count">（%d 篇）</span></h2>' \
                '<div class="rev-p">以下文献由大模型识别为「前沿 / 能启发研究 idea」的方向，建议作为选题与方法学参考。</div>' \
                % len(frontier_items)
        for it in frontier_items:
            reason = (it.get("llm_reason") or "").strip()
            idea = ('<br><span style="color:#1E88A8">💡 %s</span>' % esc(reason)) if reason else ""
            body += '<div class="rev-ref"><b>%s</b><br>%s%s</div>' % (esc(it.get("title", "")), _apa(it), idea)
    # 方法可迁移（#5）：高价值但本方向非核心、LLM 判为方法/前沿/延伸的文献，单列提醒。
    #   糖尿病：高分区(4-6 档)；AI：点名了顶会/顶刊线索(conf_hint) 但非本方向核心
    if AI_MODE:
        mt_items = [it for it in items
                    if it.get("conf_hint") and not it.get("core_hit")
                    and (it.get("llm_tag") or "").lower() in ("method", "frontier", "extension")]
        mt_head = "方法可迁移 · 顶会/顶刊值得关注"
        mt_prose = "以下文献点名了顶会/顶刊，虽非本方向核心题，但方法学 / 前沿方向可迁移，建议作为方法学与选题参考。"
    else:
        mt_items = [it for it in items
                    if str(it.get("_cat") or folder_of(it)) in ("4", "5", "6")
                    and (it.get("llm_tag") or "").lower() in ("method", "frontier", "extension")]
        mt_head = "方法可迁移 · 高分区值得关注"
        mt_prose = "以下文献刊于高分区（一区/二区/其他顶刊），虽非糖尿病本方向核心题，但方法学 / 前沿方向可迁移到糖尿病研究，建议作为方法学与选题参考。"
    if mt_items:
        mt_items = sorted(mt_items, key=lambda x: -(x.get("score") or 0))
        body += '<h2 class="rev-h2">%s <span class="count">（%d 篇）</span></h2>' \
                '<div class="rev-p">%s</div>' % (mt_head, len(mt_items), mt_prose)
        for it in mt_items:
            reason = (it.get("llm_reason") or "").strip()
            idea = ('<br><span style="color:#16A085">🔧 %s</span>' % esc(reason)) if reason else ""
            body += '<div class="rev-ref"><b>%s</b><br>%s%s</div>' % (esc(it.get("title", "")), _apa(it), idea)
    body += linkage_html   # #6 跨文献关联置底
    if AI_MODE:
        sub = "趋势预警 + 本周必读 + 本方向核心 + 研究方法 + 研究内容 + 前沿/方法可迁移 分组综述 · 共覆盖 %d 篇" % len(items)
    else:
        sub = "趋势预警 + 本周必读 + 导师组 + 研究方法 + 研究内容 + 前沿/方法可迁移 分组综述 · 共覆盖 %d 篇" % len(items)
    bar = '<div class="tip">本页为按主题归类的综述，点击下方参考文献链接可跳转原文；完整字段与筛选见「0_一键打开链接.html」。</div>'
    html = page("本周综述 · " + label, sub, bar, body)
    p = os.path.join(folder, F_REVIEW)
    with open(p, "w", encoding="utf-8") as f:
        f.write(html)
    _cleanup_legacy(folder)
    print("本周综述：%s" % p)
    return p


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print("reportlib 就绪。分类：", jcr.CAT_NAME)
