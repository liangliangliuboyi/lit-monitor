#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
JCR 期刊分级查询

数据来源: jcr.csv（由 build_jcr.py 从官方 JCR 导出 + 分区表合并而来）
分级优先级: 人工覆盖表 journals.csv  >  JCR 分区  >  未收录

分级代码（从高到低）:
  S+  世界顶刊   Nature/Science/Cell/NEJM/Lancet/JAMA/BMJ/PNAS 及 IF>=25
  S   领域顶刊   JCR Q1 且 IF>=10，或人工指定（卫生政策/卫管顶刊 IF 普遍偏低，需人工提级）
  A   一区       JCR Q1 且 IF<10
  B   二区       JCR Q2
  C   三区       JCR Q3
  D   四区       JCR Q4
  E   ESCI/未收录 无 JCR 分区（含大量新刊、ESCI）
  W   领域必读小刊 分区不高但本方向绕不开（人工指定）
  P   预印本     medRxiv/bioRxiv/arXiv/SSRN
"""
from __future__ import annotations

import csv
import json
import os
import re
import threading
from datetime import date, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
JCR_CSV = os.path.join(BASE, "jcr.csv")
JOURNALS_CSV = os.path.join(BASE, "journals.csv")

# 分级排序权重（越小越靠前）
TIER_ORDER = {"S+": 0, "S": 1, "A": 2, "W": 3, "B": 4, "C": 5, "D": 6, "E": 7, "P": 8, "": 9}
TIER_LABEL = {
    "S+": "世界顶刊", "S": "领域顶刊", "A": "JCR Q1", "W": "领域必读",
    "B": "JCR Q2", "C": "JCR Q3", "D": "JCR Q4", "E": "ESCI/未收录", "P": "预印本", "": "未分级",
}
TIER_COLOR = {
    "S+": "#8E1B1B", "S": "#A32D2D", "A": "#185FA5", "W": "#7A4E9E",
    "B": "#3B6D11", "C": "#6B6A63", "D": "#8A8780", "E": "#9A9892", "P": "#B08968", "": "#888780",
}

# ---------------------------------------------------------------- 归档文件夹体系（2026-10 改版）
# 数字前缀 0-6 保证文件管理器按序排列；导师组优先（无论期刊档次都进 0）。
#   0 导师组文献 | 1 世界顶刊(S+) | 2 领域顶刊(S) | 3 一区top(A 且 IF≥阈值)
#   4 一区(A) | 5 二区(B) | 6 其他(C/D/E/P/W/未分级)
CAT_ORDER = ["0", "1", "2", "3", "4", "5", "6"]
CATS = [
    ("0", "导师组文献", "#B8860B"),
    ("1", "世界顶刊",   "#8E1B1B"),
    ("2", "领域顶刊",   "#A32D2D"),
    ("3", "一区top",    "#185FA5"),
    ("4", "一区",       "#0E7C86"),
    ("5", "二区",       "#3B6D11"),
    ("6", "其他",       "#6B6A63"),
]
CAT_NAME = {c: n for c, n, _ in CATS}
CAT_COLOR = {c: col for c, _, col in CATS}

# ---------------------------------------------------------------- AI 板块分类（与糖尿病 0-6 体系并行，专供 aiboard）
# 数字前缀 A0/A1/A2 保证文件管理器按序排列；与糖尿病 0-6 互不干扰。
# 注：A0/A1/A2 按「研究方向」分（精准、贴合用户学习/发文需求）；
#     「顶会 CCF-A 线索」作为独立徽章 conf_hint + 筛选，不强行按会议录用归类（arXiv 元数据无法可靠识别录用）。
AI_CATS = [
    ("A0", "大模型·智能体·多模态", "#185FA5"),
    ("A1", "医学人工智能",         "#A32D2D"),
    ("A2", "通用 AI / 其他",       "#5B7C2E"),
]
AI_CAT_NAME = {c: n for c, n, _ in AI_CATS}
AI_CAT_COLOR = {c: col for c, _, col in AI_CATS}
# AI 板块分组顺序（供 reportlib 在 AI_MODE 下按研究方向出报告）
AI_CAT_ORDER = ["A0", "A1", "A2"]

# ---------------------------------------------------------------- AI 板块综述分组（对标糖尿病 CONTENT_GROUPS / METHOD_GROUPS）
# 仅 AI_MODE 下使用；关键词命中即归组，与糖尿病侧完全同构。
AI_METHOD_GROUPS = [
    ("大模型训练 / 对齐 / 推理", ["large language model", "llm", "training", "fine-tuning",
                            "instruction tuning", "rlhf", "dpo", "alignment", "reasoning",
                            "chain-of-thought", "mixture of experts", "moe", "long context",
                            "prompt", "diffusion transformer"]),
    ("智能体 / 工具调用 / 规划", ["agent", "multi-agent", "tool use", "function calling",
                            "agentic", "workflow", "planning"]),
    ("多模态 / 视觉语言 / 生成", ["multimodal", "vision language", "vlm", "image", "video",
                             "diffusion", "text-to-image", "speech", "audio"]),
    ("检索 / RAG / 知识", ["retrieval augmented", "rag", "knowledge graph", "embedding", "vector"]),
    ("图神经网络 / 序列 / 强化", ["graph neural network", "gnn", "time series", "forecasting",
                             "reinforcement learning"]),
    ("医学影像 / 计算病理", ["medical imaging", "segmentation", "detection", "radiology",
                        "pathology", "whole slide"]),
    ("联邦 / 隐私 / 可信", ["federated learning", "privacy", "differential privacy",
                       "explainable", "interpretable", "robustness"]),
]
AI_CONTENT_GROUPS = [
    ("医学AI大模型 / 临床NLP", ["clinical large language model", "medical large language model",
                           "clinical nlp", "ehr", "electronic health record", "icd"]),
    ("眼底 / 视网膜AI", ["retinal", "fundus", "diabetic retinopathy", "ophthalmic", "eye"]),
    ("医学影像诊断", ["radiology", "mri", "ct", "medical image", "chest", "pathology",
                   "tumor", "cancer detection"]),
    ("慢病 / 糖尿病AI", ["diabetes", "glucose", "cgm", "chronic disease", "cardiovascular", "obesity"]),
    ("主动健康 / 可穿戴 / 数字疗法", ["wearable", "smartwatch", "digital health", "mhealth",
                                "telemedicine", "remote monitoring", "exercise", "physical activity"]),
    ("药物发现 / 组学", ["drug discovery", "protein", "alphafold", "omics", "genomics",
                     "single cell", "biomarker"]),
    ("临床决策 / 诊疗辅助", ["clinical decision support", "decision support", "diagnosis",
                       "prognosis", "triaging", "risk prediction"]),
]

# 跨文献关联：AI 领域常用基准数据集 / 队列（对标糖尿病 DATASETS）
AI_DATASETS = [
    ("ImageNet", "imagenet"),
    ("MIMIC", "mimic"),
    ("CheXpert", "chexpert"),
    ("ADNI", "adni"),
    ("NIH ChestX-ray", "chestx-ray"),
    ("Kinetics", "kinetics"),
    ("COCO", "ms coco"),
    ("GLUE", "glue benchmark"),
    ("MedQA", "medqa"),
    ("PubMed", "pubmed"),
    ("UCF101", "ucf101"),
]

# 「顶会/顶刊线索」徽章（正文点名了某顶会或顶刊，仅代表"与该顶会/顶刊相关"，
#  不代表已被录用 / 已在该刊发表 —— arXiv 元数据无法可靠识别录用状态）
CONF_HINT_LABEL = "顶会/顶刊线索"
CONF_HINT_COLOR = "#B8860B"


def folder_code(tier="", jif=None, author_hit=False, q1_top_if=7.0):
    """返回归档文件夹的数字前缀 0-6（导师组优先）。"""
    if author_hit:
        return "0"
    if tier == "S+":
        return "1"
    if tier == "S":
        return "2"
    if tier == "A":
        try:
            j = float(jif or 0)
        except Exception:
            j = 0
        return "3" if j >= q1_top_if else "4"
    if tier == "B":
        return "5"
    return "6"


def folder_name(tier="", jif=None, author_hit=False, q1_top_if=7.0):
    return CAT_NAME[folder_code(tier, jif, author_hit, q1_top_if)]

_lock = threading.Lock()
_cache = None

QT = {"Q2": "B", "Q3": "C", "Q4": "D"}


def nissn(s):
    s = (s or "").strip().upper()
    if s in ("N/A", "NA", "-", "NULL", "NONE"):
        return ""
    return re.sub(r"[^0-9X]", "", s)


def nname(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _fnum(s):
    try:
        return float(str(s).replace(",", "").strip())
    except Exception:
        return None


def _load():
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        by_issn, by_name, man_name, man_issn = {}, {}, {}, {}

        # ---- JCR 数据 ----
        if os.path.exists(JCR_CSV):
            with open(JCR_CSV, encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    rec = {
                        "journal": row.get("journal") or "",
                        "abbr": row.get("abbr") or "",
                        "quartile": (row.get("quartile") or "").strip().upper(),
                        "jif": _fnum(row.get("jif")),
                        "jif5": _fnum(row.get("jif5")),
                        "rank": row.get("rank") or "",
                        "categories": row.get("categories") or "",
                        "edition": row.get("edition") or "",
                        "publisher": row.get("publisher") or "",
                        "issn": nissn(row.get("issn")),
                        "eissn": nissn(row.get("eissn")),
                    }
                    for k in (rec["issn"], rec["eissn"]):
                        if k and k not in by_issn:
                            by_issn[k] = rec
                    for nm in (rec["journal"], rec["abbr"]):
                        n = nname(nm)
                        if n and n not in by_name:
                            by_name[n] = rec

        # ---- 人工覆盖表 ----
        if os.path.exists(JOURNALS_CSV):
            with open(JOURNALS_CSV, encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    j = (row.get("journal") or "").strip()
                    if not j:
                        continue
                    tier = (row.get("tier") or "").strip().upper()
                    rec = {"tier": tier, "field": (row.get("field") or "").strip(),
                           "note": (row.get("note") or "").strip(), "journal": j}
                    man_name[nname(j)] = rec
                    short = re.sub(r"^(the|journal of the)\s+", "", nname(j))
                    man_name.setdefault(short, rec)
                    k = nissn(row.get("issn"))
                    if k:
                        man_issn[k] = rec

        _cache = (by_issn, by_name, man_name, man_issn)
        return _cache


def jcr_info(journal="", issn=""):
    """返回 JCR 记录 dict 或 None"""
    by_issn, by_name, _, _ = _load()
    k = nissn(issn)
    if k and k in by_issn:
        return by_issn[k]
    n = nname(journal)
    if n and n in by_name:
        return by_name[n]
    if n:
        for key, v in by_name.items():            # 前缀匹配，基准词要够长
            if len(key) >= 12 and n.startswith(key):
                return v
    return None


def manual_info(journal="", issn=""):
    _, _, man_name, man_issn = _load()
    k = nissn(issn)
    if k and k in man_issn:
        return man_issn[k]
    n = nname(journal)
    if not n:
        return None
    if n in man_name:
        return man_name[n]
    for key, v in man_name.items():
        if len(key) >= 10 and n.startswith(key):
            return v
    return None


def tier_of(journal="", issn="", preprint=False):
    """综合分级。人工覆盖 > JCR 分区 > 未收录"""
    if preprint:
        return "P"
    m = manual_info(journal, issn)
    if m and m.get("tier"):
        return m["tier"]
    j = jcr_info(journal, issn)
    if not j:
        return "P" if preprint else ""
    q, jif = j.get("quartile") or "", j.get("jif")
    if q == "Q1":
        if jif is not None and jif >= 25:
            return "S+"
        if jif is not None and jif >= 10:
            return "S"
        return "A"
    if q in QT:
        return QT[q]                      # Q2 -> B, Q3 -> C, Q4 -> D
    ed = (j.get("edition") or "").upper()
    if ed:
        return "E"
    return ""


def enrich(journal="", issn="", preprint=False):
    """返回 (tier, quartile, jif, field, note)"""
    m = manual_info(journal, issn)
    j = jcr_info(journal, issn)
    tier = tier_of(journal, issn, preprint)
    return (tier,
            (j or {}).get("quartile") or "",
            (j or {}).get("jif"),
            (m or {}).get("field") or "",
            (m or {}).get("note") or "")


def week_bounds(d=None):
    """任意日期 -> 它所属 ISO 周的 (周一, 周日) date 对象。

    用途：库里 papers.week 记的是「哪一天抓到」，同一周内换天重跑会得到不同的 week，
    报告按 week=最新 取数就会把上一次抓到的几百上千条整批丢掉（2026-10-04 事故：
    10-03 那批 3017 条在一夜之间全部从报告里消失，用户肉眼能发现的只是少了 2 篇导师组文献）。
    统一折算到「所属 ISO 周的周一~周日」区间后，同周任何一次重跑都落在同一个区间里，
    只做加法不做替换 —— 这才是「本周」应有的语义。
    """
    d = d or date.today()
    if isinstance(d, str):
        try:
            d = date.fromisoformat(d[:10])
        except Exception:
            # 传进来的可能不是日期（比如周标签 "2026-W40 (...)"），退回今天，
            # 别让整个流水线因为解析失败崩在这里。
            d = date.today()
    monday = d - timedelta(days=d.isoweekday() - 1)
    return (monday, monday + timedelta(days=6))


def week_range(d=None):
    """week_bounds 的字符串版：('YYYY-MM-DD','YYYY-MM-DD')，可直接喂 SQL BETWEEN。"""
    a, b = week_bounds(d)
    return (a.isoformat(), b.isoformat())


def stats():
    by_issn, by_name, man_name, man_issn = _load()
    return {"jcr_issn": len(by_issn), "jcr_name": len(by_name),
            "manual": len(man_name), "manual_issn": len(man_issn)}


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print("JCR 索引：%s" % stats())
    for nm, issn in [("The Lancet", "0140-6736"), ("Health Affairs", "0278-2715"),
                     ("BMC Health Services Research", "1472-6963"),
                     ("Sustainability", ""), ("Journal of Informetrics", ""),
                     ("Medical Care", "0025-7079"), ("", "2041-1723")]:
        t, q, jif, f, note = enrich(nm, issn)
        print("%-32s -> %-3s %-3s IF=%-7s %s" % (nm or issn, t, q, jif, f))
