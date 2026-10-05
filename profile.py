#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
配置与画像加载

- config.json : 运行参数（数据源开关、路径、下载、LLM）
- profile.md  : 研究方向与关键词分层（人读人改）

对外:
  cfg()      -> 合并后的配置 dict
  profile()  -> {"core": [...], "proxy": [...], "eco": [...], "exclude": [...],
                 "must_journals": [...], "capacity": int}
  所有关键词统一小写、去空白。
  同时向后兼容旧 config.json 里的 topics（转成 core）。
"""
from __future__ import annotations

import json
import os
import re

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(BASE, "config.json")
PROFILE = os.path.join(BASE, "profile.md")

DEFAULTS = {
    "lookback_days": 7,
    "max_papers_per_run": 400,
    "sources": {"pubmed": True, "openalex": True, "biorxiv": True,
                "medrxiv": True, "arxiv": True, "rss": True},
    "output": {"dir": "reports", "html": True, "markdown": True},
    "paths": {"papers_dir": "", "weekly_prefix": True},
    "download": {"enabled": True, "only_tiers": ["S+", "S", "A", "W"],
                 "min_score": 6, "max_per_week": 60, "timeout": 45,
                 "unpaywall_email": "", "cookies_file": "", "ezproxy_prefix": ""},
    "llm": {"enabled": False, "base_url": "", "api_key": "",
            "model": "", "max_items": 24, "temperature": 0.2},
    "pubmed": {"api_key": ""},
    "researcher": {"name": "", "email": "", "mailto": ""},
}

_P = None
_C = None


def _merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def cfg():
    global _C
    if _C is not None:
        return _C
    data = {}
    if os.path.exists(CONFIG):
        try:
            with open(CONFIG, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print("[warn] config.json 解析失败，用默认值: %r" % e)
            data = {}
    _C = _merge(DEFAULTS, data)
    return _C


def _block(text, header):
    """抓取 '## header' 到下一个 '## ' 之间的正文"""
    m = re.search(r"^##\s+%s\s*$(.*?)(?=^##\s|\Z)" % re.escape(header),
                  text, re.M | re.I | re.S)
    if not m:
        return ""
    return m.group(1)


def _kwlist(block):
    """优先取 ```text ... ``` 代码块里的行；没有就取 - / * 列表项"""
    out = []
    codes = re.findall(r"```(?:text)?\s*(.*?)```", block, re.S)
    if codes:
        for c in codes:
            for line in c.splitlines():
                s = line.strip().lstrip("-*0123456789. ").strip()
                if s and not s.startswith("#"):
                    out.append(s.lower())
    else:
        for line in block.splitlines():
            s = line.strip()
            if s.startswith("-") or s.startswith("*"):
                s = s.lstrip("-* ").strip()
                if s:
                    out.append(s.lower())
    return [x for x in dict.fromkeys(out) if x]


def _authors(block):
    """解析 Watch authors 块。

    每行格式:  中文名 | 英文署名变体(逗号分隔) | 额外限定词(可留空) | 机构限定覆盖(可留空)
    第 4 个字段（可选）用于个别作者放宽/收紧机构限定：
      - 留空/省略   -> 沿用全局「作者机构限定」
      - 写 `none`   -> 不限制机构（靠署名变体 + 额外限定词去重名，适合常在外单位署名的作者）
      - 写自定义词  -> 仅用这些词（逗号分隔）做机构限定，不再用全局的
    保留原始大小写（PubMed 字段标签如 [Author] 最好不要小写）。
    """
    out = []
    codes = re.findall(r"```(?:text)?\s*(.*?)```", block, re.S)
    lines = []
    if codes:
        for c in codes:
            lines += c.splitlines()
    else:
        lines = block.splitlines()
    for line in lines:
        s = line.strip().lstrip("-*0123456789. ").strip()
        if not s or s.startswith("#"):
            continue
        parts = [x.strip() for x in s.split("|")]
        if len(parts) < 2:
            continue
        name = parts[0]
        variants = [x.strip() for x in parts[1].split(",") if x.strip()]
        if not name or not variants:
            continue
        extra = parts[2] if len(parts) > 2 else ""
        aff = None
        if len(parts) > 3:
            a = parts[3].lower()
            if a == "none":
                aff = []            # 不限制机构
            elif a:
                aff = [x.strip() for x in parts[3].split(",") if x.strip()]
            # 空字符串 -> 沿用全局（aff 保持 None）
        out.append({"name": name, "variants": variants, "extra": extra, "affils": aff})
    return out


def profile():
    global _P
    if _P is not None:
        return _P
    p = {"core": [], "proxy": [], "eco": [], "exclude": [],
         "extension": [], "method": [], "frontier": [],
         "must_journals": [], "capacity": 12, "identity": "",
         "watch_authors": [], "author_affils": []}
    if not os.path.exists(PROFILE):
        # 回退：旧 config.json 的 topics
        for tp in cfg().get("topics", []):
            p["core"] += [t.lower() for t in tp.get("terms", [])]
        p["exclude"] = [w.lower() for w in cfg().get("exclude_terms", [])]
        _P = p
        return p

    with open(PROFILE, encoding="utf-8") as f:
        text = f.read()

    p["core"] = _kwlist(_block(text, "Core keywords"))
    p["proxy"] = _kwlist(_block(text, "Proxy keywords"))
    p["eco"] = _kwlist(_block(text, "Eco keywords"))
    p["exclude"] = _kwlist(_block(text, "Exclude keywords"))
    # 2026-10 新增：用于拓宽抓取面的三组「相邻 / 方法 / 前沿」词（捞回后由 LLM 判相关性）
    p["extension"] = _kwlist(_block(text, "Extension keywords"))
    p["method"] = _kwlist(_block(text, "Method keywords"))
    p["frontier"] = _kwlist(_block(text, "Frontier keywords"))
    p["must_journals"] = [x for x in _kwlist(_block(text, "Must-track journals"))]

    p["watch_authors"] = _authors(_block(text, "Watch authors"))
    p["author_affils"] = [x for x in _kwlist(_block(text, "作者机构限定")) if x]

    m = re.search(r"每周精读容量[：:]\s*(\d+)", text)
    if m:
        p["capacity"] = int(m.group(1))
    m = re.search(r"^##\s+我是谁\s*$(.*?)(?=^##\s|\Z)", text, re.M | re.S)
    if m:
        p["identity"] = m.group(1).strip()[:1500]

    # 向后兼容：config.json 里还有 topics 就并进 core
    for tp in cfg().get("topics", []):
        for t in tp.get("terms", []):
            if t.lower() not in p["core"]:
                p["core"].append(t.lower())
    for w in cfg().get("exclude_terms", []):
        if w.lower() not in p["exclude"]:
            p["exclude"].append(w.lower())

    _P = p
    return p


def all_terms():
    """给关键词分层用的词表（core + proxy）"""
    p = profile()
    return list(dict.fromkeys(p["core"] + p["proxy"]))


def all_fetch_terms():
    """给抓取端用的全量检索词：core + proxy + extension + method + frontier。
    后三组用于拓宽抓取面（捞回糖尿病相邻 / 方法学 / 前沿文献），
    相关性由 screen.py 的 LLM 增强识别二次判定，不靠字面词直接收。"""
    p = profile()
    return list(dict.fromkeys(p["core"] + p["proxy"] + p["extension"]
                              + p["method"] + p["frontier"]))


def ai_terms():
    """AI 板块关键词（医学人工智能 / 通用 AI / 大模型 / 智能体）。
    aiboard 用来从 arXiv 的 cs.AI/CL/CV/LG/MA/stat.ML 结果里筛出 AI 相关文献。"""
    if not os.path.exists(PROFILE):
        return []
    with open(PROFILE, encoding="utf-8") as f:
        text = f.read()
    return _kwlist(_block(text, "AI 板块关键词"))


def ai_core_terms():
    """AI 核心方向关键词（与糖尿病侧「导师组方向」等价）。
    命中即视为「本方向核心」(core_hit)，报告里单独成组、优先翻译与写评语、进「本周必读」。
    与 ai_terms 不同：ai_terms 只用于「噪音过滤」（不命中就剔除），
    ai_core_terms 用于「重点加权」（命中即核心）。两者都会并入抓取词表。"""
    if not os.path.exists(PROFILE):
        return []
    with open(PROFILE, encoding="utf-8") as f:
        text = f.read()
    return _kwlist(_block(text, "AI 核心方向"))


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    p = profile()
    for k in ("core", "proxy", "eco", "exclude", "must_journals"):
        print("%-14s %3d 条  %s" % (k, len(p[k]), " / ".join(p[k][:6])))
    print("capacity =", p["capacity"])
    print("watch_authors =", ", ".join(
        "%s(%s)" % (a["name"], "/".join(a["variants"][:2])) for a in p["watch_authors"]) or "-")
    print("author_affils =", " / ".join(p["author_affils"]) or "-")
