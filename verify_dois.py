#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
DOI 自查 / 自修：拿 PMID 去 PubMed 反查这条文献【真正的 DOI】，和库里存的比对。

为什么需要它
------------
2026-10-04 事故：解析 PubMed XML 时用了后代搜索，`<ReferenceList>` 里每条参考文献
也有 `<ArticleIdList><ArticleId IdType="doi">`，循环里最后一条把本文 DOI 覆盖掉了
→ 库里那条记录挂着【别人参考文献的 DOI】→ 按错 DOI 下载 → 抓回别人的全文，
报告里却显示"已下载"。解析器当时已修（限定 `./PubmedData/ArticleIdList`），
但**修复之前已经入库的脏数据还在库里**。这个工具负责把它们找出来并纠正。

判定：库里 source=pubmed 且有 pmid 的记录，逐条与 PubMed 返回的 DOI 比对
（只比规范化后的字符串）。不一致 = 脏数据。

用法
----
  python verify_dois.py            # 只体检，打印不一致清单，不改库
  python verify_dois.py --fix      # 顺手修：改正 doi 与 key（key 就是 DOI），
                                   # 并把 report_snapshot 里引用的旧 key 一起替换
  python verify_dois.py --limit 200
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import monitor as M  # noqa: E402
import download as D  # noqa: E402


def _zh(s):
    """是否含中文（判断这条评语是不是"英文摘要冒充的"）。"""
    return bool(re.search(r"[\u4e00-\u9fff]", s or ""))


def _batches(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def scan(batch=100, limit=0, verbose=True):
    """返回 [(key, pmid, db_doi, real_doi, title)]"""
    cfg = M.load_config()
    api_key = (cfg.get("pubmed") or {}).get("api_key", "")
    ak = ("&api_key=%s" % api_key) if api_key else ""
    con = M.open_db()
    con.row_factory = __import__("sqlite3").Row
    rows = [dict(r) for r in con.execute(
        "SELECT key, doi, pmid, title FROM papers WHERE source='pubmed' AND pmid<>''")]
    con.close()
    if limit:
        rows = rows[:limit]
    if verbose:
        print("待核 PubMed 记录 %d 条（分批 %d 条向 NCBI 反查）" % (len(rows), batch))
    out = []
    for i, chunk in enumerate(_batches(rows, batch), 1):
        ids = [r["pmid"] for r in chunk]
        try:
            recs = M.pubmed_efetch(ids, ak)
        except Exception as e:
            print("[warn] 第 %d 批 efetch 失败：%r" % (i, e))
            continue
        real = {}
        for rec in recs:
            if rec.get("pmid"):
                real[str(rec["pmid"])] = (rec.get("doi") or "")
        for r in chunk:
            got = (real.get(str(r["pmid"])) or "").strip().lower()
            if not got:
                continue          # NCBI 没给 DOI（部分记录确实没有），不动
            if got != (r["doi"] or "").strip().lower():
                out.append((r["key"], r["pmid"], r["doi"], got, r["title"]))
        if verbose:
            print("  第 %d 批：%d 条，累计发现不一致 %d 条" % (i, len(chunk), len(out)))
    return out


_FILL = ["pmid", "issn", "abstract", "authors", "affiliations", "topics", "url",
         "cn_title", "cn_abstract", "cn_authors", "cn_aff", "cn_keywords", "note",
         "tier", "quartile", "field", "layer", "llm_tag", "llm_reason", "doi"]


def _merge_into(cur, keep_key, dup_key, verbose=True):
    """把重复行（dup）里有、而保留行（keep）缺的字段补过去，然后删掉重复行。

    用途：同一篇文献既被抓成 openalex 行（DOI 正确）、又被抓成 pubmed 行
    （DOI 被参考文献覆盖成错的），此时应以 openalex 行为准，但把 pubmed 行才有的
    PMID / 单位 / 摘要 等信息并过去，别把有用的东西一起删掉。
    """
    keep = dict(cur.execute("SELECT * FROM papers WHERE key=?", (keep_key,)).fetchone())
    dup = dict(cur.execute("SELECT * FROM papers WHERE key=?", (dup_key,)).fetchone())
    sets, vals = [], []
    for f in _FILL:
        if f not in dup:
            continue
        dv = (dup.get(f) or "")
        kv = (keep.get(f) or "")
        if str(dv).strip() and not str(kv).strip():
            sets.append("%s=?" % f)
            vals.append(dup[f])
    # 评语：保留中文的；重复行若更早，分数取大者
    if dup.get("note") and not _zh(keep.get("note")) and _zh(dup.get("note")):
        sets.append("note=?")
        vals.append(dup["note"])
    try:
        if float(dup.get("score") or 0) > float(keep.get("score") or 0):
            sets.append("score=?")
            vals.append(dup["score"])
    except Exception:
        pass
    # PDF：保留行没有、而重复行的文件内容确实对得上，则接管过来
    dp = dup.get("pdf_path") or ""
    if dp and not (keep.get("pdf_path") or "") and os.path.exists(dp):
        ok, _ = D.pdf_matches(dp, dup.get("title") or "")
        if ok:
            sets.append("pdf_path=?")
            vals.append(dp)
    if sets:
        cur.execute("UPDATE papers SET %s WHERE key=?" % ",".join(sets), vals + [keep_key])
    cur.execute("DELETE FROM papers WHERE key=?", (dup_key,))
    if verbose:
        print("  [并] 删除重复行 %s（PMID=%s 等信息已并入 %s）｜%s"
              % (dup_key, dup.get("pmid") or "-", keep_key, (keep.get("title") or "")[:44]))


def fix(items, verbose=True):
    """把 doi/key 改成 PubMed 的真 DOI，并顺手收拾由此产生的三类残留：

    ① key 已被占用 → 说明这篇文献另有正确来源（多半是 openalex 行），合并后删重复；
    ② oa_url 是按错 DOI 解析出来的 → 清掉，免得下次照错链接下载；
    ③ pdf_path 若内容对不上题录 → 清掉（文件留在磁盘不动）。
    """
    con = M.open_db()
    con.row_factory = __import__("sqlite3").Row
    cur = con.cursor()
    fixed = merged = cleared_pdf = 0
    for key, pmid, old_doi, real_doi, title in items:
        real_doi = (real_doi or "").strip()
        if not real_doi:
            continue
        occupied = cur.execute("SELECT key FROM papers WHERE key=?", (real_doi,)).fetchone()
        if occupied:
            _merge_into(cur, real_doi, key, verbose=verbose)
            for s in cur.execute("SELECT id, keys FROM report_snapshot"):
                if key in (s["keys"] or ""):
                    cur.execute("UPDATE report_snapshot SET keys=? WHERE id=?",
                                ((s["keys"] or "").replace(key, real_doi), s["id"]))
            merged += 1
            continue
        # 同一篇文献换了 key，快照里的引用也要跟着换，否则"沿用"逻辑会指向空
        for s in cur.execute("SELECT id, keys FROM report_snapshot"):
            if key in (s["keys"] or ""):
                cur.execute("UPDATE report_snapshot SET keys=? WHERE id=?",
                            ((s["keys"] or "").replace(key, real_doi), s["id"]))
        r = cur.execute("SELECT pdf_path, oa_url, title FROM papers WHERE key=?", (key,)).fetchone()
        pdf = (r["pdf_path"] or "") if r else ""
        oa = (r["oa_url"] or "") if r else ""
        # 旧 oa_url 是拿错 DOI 解析的，重来一遍比留着更靠谱
        if oa and oa.upper().startswith(("HTTP://", "HTTPS://")):
            oa = ""
        if pdf and os.path.exists(pdf):
            ok, _ = D.pdf_matches(pdf, title)
            if not ok:
                pdf = ""
                cleared_pdf += 1
        cur.execute("UPDATE papers SET key=?, doi=?, pdf_path=?, oa_url=? WHERE key=?",
                    (real_doi, real_doi, pdf, oa, key))
        fixed += 1
        if verbose and fixed % 100 == 0:
            print("  ...已修正 %d 条" % fixed)
    con.commit()
    con.close()
    return fixed, merged, cleared_pdf


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    a = sys.argv[1:]
    lim = 0
    for i, x in enumerate(a):
        if x == "--limit" and i + 1 < len(a):
            lim = int(a[i + 1])
    bad = scan(limit=lim)
    print("\n=== 结果 ===")
    if not bad:
        print("全部一致，没有发现被「参考文献 DOI」污染的记录。")
    else:
        print("发现 %d 条 DOI 与 PubMed 不符：" % len(bad))
        for key, pmid, db_doi, real_doi, title in bad:
            print("  pmid=%s 库内=%s 真值=%s ｜ %s" % (pmid, db_doi or "(空)", real_doi, title[:50]))
        if "--fix" in a:
            f, m, c = fix(bad)
            print("\n已修正 %d 条；合并重复 %d 条；清掉内容不符的 pdf_path %d 条。" % (f, m, c))
        else:
            print("\n（只体检未修改；要修就加 --fix）")
