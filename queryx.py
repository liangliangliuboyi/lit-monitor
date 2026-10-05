#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
检索前「大模型扩词」(RAG 式召回增强) + 检索闭环（反馈环）

每次抓取前，让大模型基于研究画像，补充一批 PubMed / OpenAlex / arXiv 检索词，
并入检索式，从而召回画像里没写、但高度相关的相邻 / 方法 / 前沿文献。

支持两个板块（domain 参数）：
  - "diabetes"（默认）：糖尿病 / 公共卫生方向，画像取 profile.profile()
  - "ai"              ：医学人工智能 / 大模型方向，画像取 profile.ai_core_terms() + ai_terms()
两块用各自独立的缓存文件，互不干扰：
  diabetes -> data/llm_queries.json     ai -> data/llm_queries_ai.json

命中的文献若不含核心词，先保留并交给 screen.llm_augment（糖尿病）/ ai_llm_augment（AI）
判相关，相关才进库 / 下载（避免噪音）。结果缓存到 data/ 下，带 TTL，不每次都调 LLM。
"""
from __future__ import annotations

import json
import os
import re
import time

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
CACHE = os.path.join(DATA_DIR, "llm_queries.json")   # 兼容旧引用（糖尿病）

DEFAULTS = {
    "enabled": True,
    "max_terms": 30,
    "ttl_days": 30,
    "survive_without_core": True,
    "refresh": False,
}


def _is_ai(domain):
    return str(domain or "").lower() == "ai"


def _cache_path(domain="diabetes"):
    fn = "llm_queries_ai.json" if _is_ai(domain) else "llm_queries.json"
    return os.path.join(DATA_DIR, fn)


def _cfg(cfg, domain="diabetes"):
    c = dict(DEFAULTS)
    c.update((cfg or {}).get("query_expansion") or {})
    if _is_ai(domain):
        # AI 板块可用 ai_board.query_expansion 覆盖全局设置
        c.update(((cfg or {}).get("ai_board") or {}).get("query_expansion") or {})
    return c


def _profile_for(domain):
    """按板块取画像词表（用于「不要重复基础词」的去重基准，也用于生成 prompt）。"""
    import profile as P
    if _is_ai(domain):
        return {"core": P.ai_core_terms(), "extension": P.ai_terms()}
    return P.profile()


def enabled(cfg, domain="diabetes"):
    return bool(_cfg(cfg, domain).get("enabled"))


def survive_without_core(cfg, domain="diabetes"):
    return bool(_cfg(cfg, domain).get("survive_without_core", True))


def _read_cache(domain="diabetes"):
    try:
        with open(_cache_path(domain), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_cache(terms, domain="diabetes"):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(_cache_path(domain), "w", encoding="utf-8") as f:
        json.dump({"generated": time.strftime("%Y-%m-%d"), "terms": terms},
                  f, ensure_ascii=False, indent=1)


def _gen(cfg, profile, domain="diabetes"):
    """调大模型生成补充检索词；失败返回 None（调用方退回缓存/空）。"""
    try:
        import reportlib as RL
    except Exception:
        return None
    if not RL._llm_cfg(cfg):
        return None
    n = int(_cfg(cfg, domain).get("max_terms", 30))
    if _is_ai(domain):
        prompt = (
            "你是医学人工智能文献检索助手。下面是一位做医学人工智能 / 大模型·智能体·多模态方向的博士生的研究关键词。\n"
            "【核心方向】%s\n"
            "【AI 关键词】%s\n\n"
            "请补充最多 %d 个【额外】的英文检索短语（每个 2-5 个单词，适合放进标题/摘要字段检索），"
            "用于召回上述词表里没有、但与医学人工智能 / 大模型 / 智能体 / 多模态 / AI 医疗应用高度相关的新方向"
            "（例如新模型架构、训练/对齐范式、临床落地场景、可迁移的方法）。\n"
            "要求：\n"
            "1. 不要重复上面已列出的词；\n"
            "2. 不要脱离 AI / 医学AI（如单纯 'cancer'、'surgery' 单独出现）；\n"
            "3. 必须是英文短语，用小写。\n"
            "严格只输出一个 JSON 数组，元素为字符串，不要解释、不要序号："
        ) % (", ".join(profile.get("core", [])[:30]),
             ", ".join(profile.get("extension", [])[:40]),
             n)
    else:
        prompt = (
            "你是医学文献检索助手。下面是一位糖尿病 / 公共卫生方向博士生的研究画像。\n"
            "【身份】%s\n"
            "【核心关键词】%s\n"
            "【方法/相邻】%s\n"
            "【延伸领域】%s\n"
            "【方法范式】%s\n"
            "【前沿方向】%s\n\n"
            "请补充最多 %d 个【额外】的 PubMed / OpenAlex 英文检索短语（每个 2-5 个单词，"
            "适合放进标题/摘要字段检索），用于召回上述词表里没有、但又与糖尿病 / 代谢研究"
            "高度相关的文献（例如新出现的机制、交叉疾病、可迁移的方法、指南/政策方向）。\n"
            "要求：\n"
            "1. 不要重复上面已列出的词；\n"
            "2. 不要宽泛到脱离糖尿病/代谢（如单纯 'cancer'、'obesity' 单独出现）；\n"
            "3. 必须是英文短语，用小写。\n"
            "严格只输出一个 JSON 数组，元素为字符串，不要解释、不要序号："
        ) % (profile.get("identity", "")[:400] or "(未填写)",
             ", ".join(profile.get("core", [])[:40]),
             ", ".join(profile.get("proxy", [])[:30]),
             ", ".join(profile.get("extension", [])[:30]),
             ", ".join(profile.get("method", [])[:30]),
             ", ".join(profile.get("frontier", [])[:30]),
             n)
    out = RL.llm_chat([{"role": "user", "content": prompt}], cfg, max_tokens=1200,
                      tag="queryx_suggest_terms")
    if not out:
        return None
    m = re.search(r"\[.*\]", out, re.S)
    if not m:
        return None
    try:
        arr = json.loads(m.group(0))
    except Exception:
        return None
    terms = []
    for x in arr:
        if not isinstance(x, str):
            continue
        s = x.strip().lower()
        s = re.sub(r"\s+", " ", s).strip('"\'[] ')
        if s and 1 <= len(s.split()) <= 6:
            terms.append(s)
    return terms


def load_expanded_terms(cfg, profile=None, force=False, domain="diabetes"):
    """返回补充检索词列表（已去重、已截断、已剔除与基础词表重复项）。

    - 缓存命中且未过期：直接返回，不调 LLM；
    - 否则调大模型生成并写缓存；
    - LLM 不可用：退回（即使过期的）缓存，再不行返回空，绝不阻断主流程。
    domain="ai" 用独立缓存 + AI 画像词表。
    """
    c = _cfg(cfg, domain)
    if not c.get("enabled"):
        return []
    force = force or bool(c.get("refresh"))
    if profile is None:
        profile = _profile_for(domain)
    if _is_ai(domain):
        base = set(w.lower() for w in (profile.get("core", []) + profile.get("extension", [])))
    else:
        base = set(w.lower() for w in (
            profile.get("core", []) + profile.get("proxy", []) +
            profile.get("extension", []) + profile.get("method", []) +
            profile.get("frontier", [])))
    max_terms = int(c.get("max_terms", 30))
    ttl = int(c.get("ttl_days", 30))

    cache = _read_cache(domain)
    fb = (cache.get("feedback_terms") or []) if cache else []   # 上周检索闭环产出的反馈词
    fresh = False
    if cache and not force:
        try:
            g = time.strptime(cache.get("generated", "2000-01-01"), "%Y-%m-%d")
            fresh = (time.time() - time.mktime(g)) / 86400.0 < ttl
        except Exception:
            fresh = False
    if fresh and cache.get("terms"):
        return [t for t in (cache["terms"] + fb) if t not in base][:max_terms]

    gen = _gen(cfg, profile, domain)
    if gen is None:                       # LLM 不可用：尽量用缓存兜底（含反馈词）
        if cache and cache.get("terms"):
            return [t for t in (cache["terms"] + fb) if t not in base][:max_terms]
        return []
    terms = [t for t in dict.fromkeys(gen) if t not in base][:max_terms]
    _write_cache(terms, domain)
    return terms


def suggest_next_terms(cfg, profile, papers, max_terms=20, domain="diabetes"):
    """检索闭环（#1）：基于本周【实际入库】的文献，让 LLM 建议下周该补搜的检索短语。

    与 _gen 的区别：_gen 只凭静态画像扩词（开环）；这里用"本周真捡到了什么"反推"下周还该搜什么"，
    形成 本周捡到 → 下周该搜 的反馈环，提升召回、减少漏检。每周一次调用，成本低。
    失败返回空列表（调用方跳过，不阻断主流程）。
    """
    try:
        import reportlib as RL
    except Exception:
        return []
    if not RL._llm_cfg(cfg):
        return []
    sample = papers[:40] if len(papers) > 40 else papers
    if not sample:
        return []
    lines = []
    for i, it in enumerate(sample):
        lines.append("%d | %s | %s" % (i + 1, (it.get("title") or "")[:160],
                                        (it.get("abstract") or "")[:240]))
    if _is_ai(domain):
        prompt = (
            "你是医学人工智能文献检索助手。下面是本周实际检索并入库的 AI / 医学AI 文献（序号 | 标题 | 摘要节选）。\n"
            "请基于【这些文献实际覆盖的主题与缺口】，补充最多 %d 个【额外】的英文检索短语"
            "（2-5 个单词，适合放进标题 / 摘要字段检索），用于下周召回：\n"
            "① 与本周文献相邻但本次没抓到的方向；\n"
            "② 本周出现的某模型 / 方法 / 任务值得进一步深挖的词。\n"
            "要求：不要脱离 AI / 医学AI（如单纯 'cancer'）；必须是英文短语，小写；"
            "不要重复本周已有的明显主题。\n"
            "严格只输出一个 JSON 数组，元素为字符串，不要解释、不要序号：\n%s"
        ) % (max_terms, "\n".join(lines))
    else:
        prompt = (
            "你是医学文献检索助手。下面是本周实际检索并入库的糖尿病 / 代谢方向文献（序号 | 标题 | 摘要节选）。\n"
            "请基于【这些文献实际覆盖的主题与缺口】，补充最多 %d 个【额外】的 PubMed / OpenAlex 英文检索短语"
            "（2-5 个单词，适合放进标题 / 摘要字段检索），用于下周召回：\n"
            "① 与本周文献相邻但本次没抓到的方向；\n"
            "② 本周出现的某方法 / 人群 / 机制值得进一步深挖的词。\n"
            "要求：不要宽泛到脱离糖尿病 / 代谢（如单纯 'cancer'）；必须是英文短语，小写；"
            "不要重复本周已有的明显主题。\n"
            "严格只输出一个 JSON 数组，元素为字符串，不要解释、不要序号：\n%s"
        ) % (max_terms, "\n".join(lines))
    out = RL.llm_chat([{"role": "user", "content": prompt}], cfg, max_tokens=900,
                      tag="queryx_feedback", n_items=len(lines))
    if not out:
        return []
    m = re.search(r"\[.*\]", out, re.S)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except Exception:
        return []
    terms = []
    for x in arr:
        if not isinstance(x, str):
            continue
        s = re.sub(r"\s+", " ", x.strip().lower()).strip('"\'[] ')
        if s and 1 <= len(s.split()) <= 6:
            terms.append(s)
    return terms[:max_terms]


def store_feedback(cfg, terms, week, domain="diabetes"):
    """把本周生成的反馈检索词写回缓存（供下周 load_expanded_terms 合并使用）。"""
    cache = _read_cache(domain) or {}
    cache["terms"] = cache.get("terms", [])
    cache["feedback_terms"] = list(terms)
    cache["feedback_week"] = week
    cache["generated"] = cache.get("generated", time.strftime("%Y-%m-%d"))
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(_cache_path(domain), "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)


def suggest_and_store(cfg, profile, papers, week, max_terms=20, domain="diabetes"):
    """检索闭环一站式：生成下周检索词并落缓存。返回词数（0 表示未生成）。"""
    fb = suggest_next_terms(cfg, profile, papers, max_terms=max_terms, domain=domain)
    if fb:
        store_feedback(cfg, fb, week, domain=domain)
    return len(fb)
