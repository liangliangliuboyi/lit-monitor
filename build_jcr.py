#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
把 JCR 官方导出 + 分区表合并成本地查询表 jcr.csv

用法:
  python build_jcr.py "C:\\path\\2026JCR.xlsx" "C:\\path\\2026年JCR期刊分区信息.xlsx"

产物: jcr.csv  (issn|eissn|journal| quartile| jif| jif5| rank| categories| edition)
同时打印一份体检报告到 _jcr_report.txt，方便确认数据年份和覆盖度。
"""
from __future__ import annotations

import csv
import os
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import openpyxl

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "jcr.csv")
REPORT = os.path.join(BASE, "_jcr_report.txt")


def nissn(s):
    """ISSN 归一：只留数字和 X，大写。'N/A' / 空 -> ''"""
    s = (s or "").strip().upper()
    if s in ("N/A", "NA", "-", "NULL", "NONE"):
        return ""
    return re.sub(r"[^0-9X]", "", s)


def pick(row, keys):
    for k in keys:
        v = row.get(k)
        if v not in (None, ""):
            return v
    return ""


def read_sheet(path):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    it = ws.iter_rows(values_only=True)
    header = None
    for r in it:
        if r and any(c is not None for c in r):
            header = [("" if c is None else str(c).strip()) for c in r]
            break
    rows = []
    for r in it:
        if not r or all(c is None for c in r):
            continue
        rows.append(dict(zip(header, r)))
    wb.close()
    return header, rows


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if len(args) < 2:
        print("用法: python build_jcr.py <官方JCR.xlsx> <分区表.xlsx>")
        return 1

    rep = []

    # ---------- 官方 JCR：分类、排名、5 年 IF ----------
    h1, r1 = read_sheet(args[0])
    rep.append("官方表表头: %s" % h1)
    official = {}          # issn -> dict
    name_official = {}     # 期刊名归一 -> dict
    for row in r1:
        rec = {
            "journal": str(pick(row, ["Journal name", "期刊名称", "Journal"]) or ""),
            "abbr": str(pick(row, ["Abbreviated journal", "缩写"]) or ""),
            "issn": nissn(pick(row, ["ISSN"])),
            "eissn": nissn(pick(row, ["eISSN", "EISSN"])),
            "publisher": str(pick(row, ["Publisher"]) or ""),
            "categories": str(pick(row, ["Categories", "分类"]) or ""),
            "edition": str(pick(row, ["Editions", "Edition"]) or ""),
            "rank": str(pick(row, ["Rank", "排名"]) or ""),
            "jif": str(pick(row, ["2025 JIF", "2024 JIF", "JIF", "IF"]) or ""),
            "jif5": str(pick(row, ["5-year JIF", "5 Year JIF"]) or ""),
        }
        for k in ("issn", "eissn"):
            if rec[k]:
                official.setdefault(rec[k], rec)
        for nm in (rec["journal"], rec["abbr"]):
            if nm:
                name_official.setdefault(re.sub(r"[^a-z0-9]+", " ", nm.lower()).strip(), rec)

    # ---------- 分区表：Q1-Q4 ----------
    h2, r2 = read_sheet(args[1])
    rep.append("分区表表头: %s" % h2)
    quart = {}
    name_quart = {}
    qcount = {}
    for row in r2:
        q = str(pick(row, ["分区", "Quartile", "JCR Quartile", "Q"]) or "").strip().upper()
        q = re.sub(r"[^Q1-4]", "", q) or ""
        if q:
            qcount[q] = qcount.get(q, 0) + 1
        rec = {
            "journal": str(pick(row, ["期刊名称", "Journal name", "Journal"]) or ""),
            "issn": nissn(pick(row, ["ISSN"])),
            "eissn": nissn(pick(row, ["EISSN", "eISSN"])),
            "quartile": q,
            "jif": str(pick(row, ["IF", "2025 JIF", "影响因子"]) or ""),
            "jif5": str(pick(row, ["5 Year JIF", "5-year JIF"]) or ""),
            "q5": re.sub(r"[^Q1-4]", "", str(pick(row, ["5 Year JIF Quartile"]) or "").upper()),
        }
        for k in ("issn", "eissn"):
            if rec[k]:
                quart.setdefault(rec[k], rec)
        nm = re.sub(r"[^a-z0-9]+", " ", rec["journal"].lower()).strip()
        if nm:
            name_quart.setdefault(nm, rec)

    rep.append("分区取值分布: %s" % qcount)
    rep.append("官方表 %d 行，ISSN 键 %d 个；分区表 %d 行，ISSN 键 %d 个" %
               (len(r1), len(official), len(r2), len(quart)))

    # ---------- 合并 ----------
    keys = set(official) | set(quart)
    merged = {}
    for k in keys:
        o = official.get(k) or {}
        q = quart.get(k) or {}
        jname = o.get("journal") or q.get("journal") or ""
        if not jname:
            for kk in (o.get("issn"), o.get("eissn"), q.get("issn"), q.get("eissn")):
                pass
        cat = o.get("categories") or ""
        jif = o.get("jif") or q.get("jif") or ""
        jif5 = o.get("jif5") or q.get("jif5") or ""
        merged[k] = {
            "issn": o.get("issn") or q.get("issn") or "",
            "eissn": o.get("eissn") or q.get("eissn") or "",
            "journal": jname,
            "abbr": o.get("abbr") or "",
            "quartile": q.get("quartile") or "",
            "jif": jif,
            "jif5": jif5,
            "rank": o.get("rank") or "",
            "categories": cat,
            "edition": o.get("edition") or "",
            "publisher": o.get("publisher") or "",
        }
    rep.append("合并后 ISSN 键 %d 个" % len(merged))

    # 名称键（用于 ISSN 缺失时兜底）
    name_key = {}
    for v in merged.values():
        for nm in (v["journal"], v["abbr"]):
            n = re.sub(r"[^a-z0-9]+", " ", (nm or "").lower()).strip()
            if n:
                name_key.setdefault(n, v)

    with open(OUT, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["issn", "eissn", "journal", "abbr", "quartile", "jif", "jif5",
                    "rank", "categories", "edition", "publisher"])
        seen = set()
        for v in merged.values():
            sig = (v["journal"].lower(), v["issn"], v["eissn"])
            if sig in seen:
                continue
            seen.add(sig)
            w.writerow([v["issn"], v["eissn"], v["journal"], v["abbr"], v["quartile"],
                        v["jif"], v["jif5"], v["rank"], v["categories"], v["edition"], v["publisher"]])
    rep.append("写入 %s：%d 行" % (OUT, len(seen)))

    # ---------- 体检 ----------
    def probe(nm):
        n = re.sub(r"[^a-z0-9]+", " ", nm.lower()).strip()
        v = name_key.get(n)
        if not v:
            for k, vv in name_key.items():
                if k.startswith(n) and len(n) >= 8:
                    v = vv
                    break
        return v

    rep.append("")
    rep.append("抽查（确认年份与数值是否对得上）：")
    for nm in ["Lancet", "Nature", "Science", "New England Journal of Medicine",
               "JAMA", "BMJ", "PNAS", "Nature Communications", "Health Affairs",
               "Social Science & Medicine", "Health Policy", "Health Economics",
               "Value in Health", "Medical Care", "Milbank Quarterly",
               "Health Services Research", "Bulletin of the World Health Organization",
               "Journal of Health Economics", "Implementation Science",
               "International Journal for Equity in Health", "PLoS One",
               "Scientific Reports", "BMC Health Services Research",
               "Journal of Informetrics", "Scientometrics", "Research Policy",
               "Computers in Human Behavior", "Sustainability"]:
        v = probe(nm)
        if v:
            rep.append("  %-46s %-4s IF=%-8s rank=%-7s %s" %
                       (nm, v["quartile"] or "-", v["jif"] or "-", v["rank"] or "-",
                        (v["categories"] or "")[:60]))
        else:
            rep.append("  %-46s ** 未找到 **" % nm)

    open(REPORT, "w", encoding="utf-8").write("\n".join(rep))
    print("\n".join(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
