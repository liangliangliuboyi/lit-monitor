#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
文献监控流水线 —— 抓取 / 归一化 / 去重 / 入库 / 出报

用法:
  python monitor.py fetch    抓取新文献，入库，导出 pending.json 给智能体打分
  python monitor.py apply    读 scored.json，把相关性打分回填进库
  python monitor.py report   生成本周 Markdown + HTML 日报
  python monitor.py stats    查看库内概况

原则: 抓取只靠官方 API（保证 DOI 真实、不遗漏），相关性判断交给智能体。

可移植说明: 本文件只依赖标准库 + 同目录的 jcr.py / profile.py。
把整个目录拷到任何机器、任何 agent 平台，装好 Python 3.8+ 即可运行。
"""
from __future__ import annotations

import csv
import gzip
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jcr            # noqa: E402
import profile as P   # noqa: E402
import queryx         # noqa: E402  (检索前大模型扩词：RAG 式召回增强)

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(BASE, "config.json")
JOURNALS = os.path.join(BASE, "journals.csv")
DATA_DIR = os.path.join(BASE, "data")
DB = os.path.join(DATA_DIR, "papers.db")
PENDING = os.path.join(DATA_DIR, "pending.json")
SCORED = os.path.join(DATA_DIR, "scored.json")
LOGF = os.path.join(DATA_DIR, "monitor.log")

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) lit-monitor/0.2"}
LOG = []


def log(msg):
    line = "[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg)
    LOG.append(line)
    print(line)


def flush_log():
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(LOGF, "a", encoding="utf-8") as f:
        f.write("\n".join(LOG) + "\n")


# ---------------------------------------------------------------- HTTP
def _decode(data, enc):
    if enc == "gzip":
        try:
            data = gzip.decompress(data)
        except Exception:
            pass
    return data.decode("utf-8", "replace")


def http_get(url, timeout=45, retries=2):
    """带 gzip 的 GET。

    实测：PubMed efetch 200 篇全文摘要，不压缩要 180 秒（4.4 MB 明文 XML），
    开 gzip 只要 10 秒。这是整条流水线最大的一个提速点，别去掉。
    """
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=dict(UA, **{"Accept-Encoding": "gzip"}))
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return _decode(r.read(), (r.headers.get("Content-Encoding") or "").lower())
        except urllib.error.HTTPError as e:
            last = e
            # 429/503 是限流，退避要够久，否则重试也是白试（OpenAlex 尤其容易踩）
            if e.code in (429, 503):
                ra = e.headers.get("Retry-After") if e.headers else None
                try:
                    # ⚠️ 必须封顶：OpenAlex 有时返回 Retry-After: 600（秒），
                    # 不封顶就会整条流水线卡死 10 分钟。一个源卡住不能拖垮全局。
                    wait = max(5, min(45, int(ra)))
                except Exception:
                    wait = 5 * (i + 1) ** 2
                time.sleep(wait)
            else:
                time.sleep(2 + i * 2)
        except Exception as e:          # noqa
            last = e
            time.sleep(2 + i * 2)
    raise last


def http_json(url, **kw):
    return json.loads(http_get(url, **kw))


# ---------------------------------------------------------------- 配置
def load_config():
    with open(CONFIG, encoding="utf-8") as f:
        c = json.load(f)
    # topics 由 profile.md 生成：core + proxy + eco + extension + method + frontier 全量，
    # 保证抓取端（预印本 / arXiv / RSS）不漏掉糖尿病的相邻 / 方法 / 前沿领域。
    p = P.profile()
    if "topics" not in c:
        c["topics"] = [{"name": "core", "terms": p["core"]},
                       {"name": "proxy", "terms": p["proxy"]},
                       {"name": "eco", "terms": p["eco"]},
                       {"name": "extension", "terms": p["extension"]},
                       {"name": "method", "terms": p["method"]},
                       {"name": "frontier", "terms": p["frontier"]}]
    if not c.get("exclude_terms"):
        c["exclude_terms"] = p["exclude"]
    return c


def norm_name(s):
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return s


def tier_of(journal, issn="", preprint=False):
    """期刊分级：人工覆盖表 journals.csv > JCR 2026 分区 > 未收录。见 jcr.py"""
    return jcr.tier_of(journal, issn, preprint)


# ---------------------------------------------------------------- 工具
def norm_doi(doi):
    if not doi:
        return ""
    d = doi.strip().lower()
    d = re.sub(r"^https?://(dx\.)?doi\.org/", "", d)
    d = re.sub(r"^doi:\s*", "", d)
    return d.strip()


def norm_date(s):
    if not s:
        return ""
    s = str(s).strip()
    m = re.match(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        return "%04d-%02d-%02d" % tuple(int(x) for x in m.groups())
    m = re.match(r"(\d{4})\s*([A-Za-z]{3})\s*(\d{1,2})?", s)
    if m:
        try:
            return datetime.strptime("%s %s %s" % (m.group(3) or 1, m.group(2), m.group(1)), "%d %b %Y").strftime("%Y-%m-%d")
        except Exception:
            return "%s-01-01" % m.group(1)
    m = re.match(r"(\d{4})", s)
    if m:
        y = int(m.group(1))
        if 1900 <= y <= 2100:
            return "%04d-01-01" % y
    return ""


def clean(s, n=None):
    s = re.sub(r"\s+", " ", (s or "").replace("\n", " ")).strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    return s[:n] if n else s


BAD_SRC = ["zenodo", "doaj", "figshare", "research square", "researchsquare", "preprints.org",
           "protocols.io", "authorea", "open science framework", "semantic scholar", "pubmed",
           "repository", "repositorio", "university of", "universidad", "universite",
           "academy", "college", "school of", "institute of", "library", "archive"]


def bad_source(journal):
    j = (journal or "").lower()
    return any(b in j for b in BAD_SRC)


def hit_topics(text, topics=None):
    """粗筛：返回命中的 core/proxy 关键词（保留原样，日报里能看到是踩中哪个词）。
    topics 参数仅为兼容旧调用，实际取 profile.md 的词表。"""
    p = P.profile()
    t = (text or "").lower()
    return [w for w in (p["core"] + p["proxy"]) if w in t][:8]


def excluded(text, excludes=None):
    p = P.profile()
    t = (text or "").lower()
    return [w for w in (excludes if excludes is not None else p["exclude"]) if w in t]


PREPRINT_SRC = ("biorxiv", "medrxiv", "arxiv", "ssrn")


def mk(doi, title, journal, pub_date, url, abstract, authors, source, cites=0, issn="", pmid="", affiliations=""):
    key = norm_doi(doi) or ("%s:%s" % (source, re.sub(r"\W+", "", norm_name(title))[:60]))
    is_pre = source in PREPRINT_SRC
    tier, quart, jif, field, _note = jcr.enrich(journal, issn, is_pre)
    return {
        "key": key, "doi": norm_doi(doi), "title": clean(title),
        "journal": clean(journal), "pub_date": norm_date(pub_date),
        "url": url or ("https://doi.org/%s" % norm_doi(doi) if norm_doi(doi) else ""),
        "abstract": clean(abstract), "authors": clean(authors, 300),
        "affiliations": clean(affiliations, 400),
        "source": source, "cites": cites or 0,
        "tier": tier, "quartile": quart, "jif": jif, "field": field,
        "pmid": pmid or "", "issn": re.sub(r"[^0-9Xx]", "", (issn or "")).upper(),
    }


# ---------------------------------------------------------------- PubMed
def pubmed_terms(cfg=None, extra_terms=None):
    """检索词分组：每组 6 个词，逐组查 PubMed，避免单条超长 query 被截断。
    2026-10 起用 all_fetch_terms()（含 Extension/Method/Frontier），并可选并入
    queryx 的 LLM 扩词（RAG 式召回增强）。"""
    if cfg is None:
        cfg = load_config()
    terms = P.all_fetch_terms()
    if extra_terms is None and queryx.enabled(cfg):
        try:
            extra_terms = queryx.load_expanded_terms(cfg)
        except Exception as e:
            log("[warn] 扩词失败，用基础词表：%r" % e)
    if extra_terms:
        terms = terms + list(extra_terms)
    terms = list(dict.fromkeys(terms))
    return [terms[i:i + 6] for i in range(0, len(terms), 6)]


def openalex_terms(cfg=None, extra_terms=None):
    """OpenAlex 用 '|' 做 OR，一组 8 个词一次请求。
    2026-10 起用 all_fetch_terms()（含 Extension/Method/Frontier），并可选并入
    queryx 的 LLM 扩词（RAG 式召回增强）。"""
    if cfg is None:
        cfg = load_config()
    terms = P.all_fetch_terms()
    if extra_terms is None and queryx.enabled(cfg):
        try:
            extra_terms = queryx.load_expanded_terms(cfg)
        except Exception as e:
            log("[warn] 扩词失败，用基础词表：%r" % e)
    if extra_terms:
        terms = terms + list(extra_terms)
    terms = list(dict.fromkeys(terms))
    return [terms[i:i + 8] for i in range(0, len(terms), 8)]


def pubmed_records(xml, source="pubmed"):
    """解析 PubMed efetch 的 XML -> 记录列表（关键词检索与作者检索共用）"""
    root = ET.fromstring(xml)
    out = []
    for pa in root.findall(".//PubmedArticle"):
        # ↓ 必须限定路径取「本文自己」的 PMID / DOI。
        #   曾经用 .// 后代搜索：PubMed 的 <ReferenceList> 里每条参考文献也有
        #   <ArticleIdList><ArticleId IdType="doi">，于是本文 DOI 被循环里最后一条
        #   参考文献的 DOI 覆盖（实测 JAMA 那篇被写成 10.23838/pfm.2021.00135），
        #   后续按错 DOI 下载 → 抓到别人的全文。此坑勿再踩。
        pmid = pa.findtext("./MedlineCitation/PMID") or ""
        art = pa.find("./MedlineCitation/Article")
        if art is None:
            art = pa.find(".//Article")
        if art is None:
            continue
        title = "".join(art.findtext("ArticleTitle") or "")
        abs_txt = " ".join(x.text or "" for x in art.findall(".//Abstract/AbstractText"))
        jt = art.findtext(".//Journal/Title") or ""
        issn = art.findtext(".//Journal/ISSN") or ""
        doi = ""
        for aid in pa.findall("./PubmedData/ArticleIdList/ArticleId"):
            if aid.get("IdType") == "doi":
                doi = (aid.text or "").strip()
                break
        if not doi:      # 兜底：少数记录 DOI 只写在 ELocationID 上
            for el in art.findall("./ELocationID"):
                if el.get("EIdType") == "doi" and el.text:
                    doi = el.text.strip()
                    break
        # 交叉校验（2026-10-05 加）：PubmedData 的 DOI 与 Article/ELocationID 的 DOI 应当一致。
        # 不一致 = 解析很可能又踩到"参考文献 DOI 覆盖"（那次污染了 1400+ 条记录），
        # 必须留日志，不能静默放过去。以 PubmedData 为准。
        _eldoi = ""
        for el in art.findall("./ELocationID"):
            if el.get("EIdType") == "doi" and el.text:
                _eldoi = el.text.strip()
                break
        if doi and _eldoi and norm_doi(doi) != norm_doi(_eldoi):
            log("[warn] PMID %s 两处 DOI 不一致：PubmedData=%s / ELocationID=%s，以 PubmedData 为准"
                % (pmid, doi, _eldoi))
        pd = ""
        # 优先取 edat（PubMed 入库日），因为它才是 esearch 用 reldate+datetype=edat
        # 实际筛选的那个日期；ahead-of-print 文章的期刊发表日可能是旧日期或未来日期，
        # 直接拿它做“近 N 天”过滤会把本该收录的文章全部误杀。
        for tag in (".//PubMedPubDate[@PubStatus='entrez']", ".//ArticleDate", ".//Journal/JournalIssue/PubDate"):
            nd = pa.find(tag)
            if nd is not None:
                y, m, dd = nd.findtext("Year"), nd.findtext("Month"), nd.findtext("Day")
                if y:
                    mraw = (m or "").strip()
                    if mraw.isdigit():
                        mo = max(1, min(12, int(mraw)))
                    else:
                        mo = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6, "Jul": 7,
                              "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}.get(mraw[:3], 1)
                    pd = "%s-%02d-%02d" % (y, mo, int(dd or 1))
                    break
        aus = []
        author_affs_pairs = []
        for au in art.findall(".//AuthorList/Author"):
            nm = ("%s %s" % (au.findtext("ForeName") or "", au.findtext("LastName") or "")).strip()
            if nm:
                aus.append(nm)
            afs = []
            for af in au.findall(".//AffiliationInfo/Affiliation"):
                if af.text and af.text.strip():
                    afs.append(af.text.strip())
            if nm and afs:
                author_affs_pairs.append((nm, "; ".join(afs)))
        # 单位去重（逐作者 + 顶层 Affiliation 兜底）
        affs = []
        for _nm, a in author_affs_pairs:
            for piece in a.split("; "):
                if piece and piece not in affs:
                    affs.append(piece)
        for af in art.findall(".//Affiliation"):
            if af.text and af.text.strip() and af.text.strip() not in affs:
                affs.append(af.text.strip())
        rec = mk(doi, title, jt, pd, "https://pubmed.ncbi.nlm.nih.gov/%s/" % pmid,
                  abs_txt, "; ".join(x for x in aus if x), source, 0, issn, pmid,
                  affiliations="; ".join(affs[:3]))
        # 保留「逐作者→单位」对照，供 fetch_authors 做"命中作者本人是否真在六院/交大"核验
        rec["_author_affs"] = author_affs_pairs
        out.append(rec)
    return out


def pubmed_efetch(ids, ak, timeout=90):
    xml = http_get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pubmed&id=%s&retmode=xml%s"
                   % (",".join(ids), ak), timeout=timeout)
    return pubmed_records(xml)


def fetch_pubmed(cfg, since, until, extra_terms=None):
    """PubMed 检索。

    2026-10-05 修：原来每组 esearch 只取 retmax=200，而 sort=date 只返回**最新入库的 200 条**。
    实测 43 组检索词里有 17 组在 7 天窗口内的命中数超过 200（最多的一组 2404 条），
    也就是说每次跑都会静默丢掉八千多条本该看到的记录 —— 窗口里靠前入库的顶刊很可能正躺在被截掉的那部分里。
    现在按 retstart 翻页，每组最多取 pubmed.max_per_group 条（配置可调，0=不限）。

    注意：这里用 EDAT(入库日) 而不是发表日，因为 PubMed 的 reldate 本身就是按 EDAT 算的，
    两者口径必须一致，否则窗口边缘的文献会被反复漏掉。
    """
    api_key = (cfg.get("pubmed") or {}).get("api_key", "")
    ak = ("&api_key=%s" % api_key) if api_key else ""
    # 用绝对日期窗口（since~until，上周一~上周日）替代 reldate，确保只抓「目标周」文献，
    # 不跨周污染、不滚雪球。datetype=edat 与之前 reldate 口径一致（入库日窗口）。
    def _fmt(s):
        try:
            return str(s)[:10].replace("-", "/")
        except Exception:
            return str(s)
    m1, m2 = _fmt(since), _fmt(until)
    cap = int((cfg.get("pubmed") or {}).get("max_per_group", 400))
    step = 200
    gap = 0.4 if api_key else 0.5
    terms_all = pubmed_terms(cfg, extra_terms)
    out = []
    hit_total = lost_total = 0
    for gi, terms in enumerate(terms_all):
        q = " OR ".join('"%s"[Title/Abstract]' % t for t in terms)
        ids, hit, retstart = [], 0, 0
        while True:
            want = step if cap <= 0 else min(step, cap - len(ids))
            if want <= 0:
                break
            u = ("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed&term=%s"
                 "&retmode=json&retmax=%d&retstart=%d&sort=date&datetype=edat&mindate=%s&maxdate=%s%s"
                 % (urllib.parse.quote(q), want, retstart, m1, m2, ak))
            try:
                d = http_json(u)
            except Exception as e:
                log("  PubMed esearch 失败 [组%s 第%d页]: %s" % (gi, retstart // step + 1, e))
                break
            res = d.get("esearchresult", {})
            batch = res.get("idlist", []) or []
            try:
                hit = int(res.get("count", 0) or 0)
            except Exception:
                hit = 0
            ids += batch
            if len(batch) < want or (hit and retstart + len(batch) >= hit):
                break
            retstart += len(batch)
            time.sleep(gap)
        if not ids:
            continue
        got = []
        for i in range(0, len(ids), 200):
            time.sleep(gap)
            try:
                got += pubmed_efetch(ids[i:i + 200], ak)
            except Exception as e:
                log("  PubMed efetch 失败 [组%s 第%d批]: %s" % (gi, i // 200 + 1, e))
        out += got
        lost = max(0, hit - len(ids))
        hit_total += hit
        lost_total += lost
        log("  PubMed 组 %d/%d：窗口内命中 %d，取回 %d 篇%s"
            % (gi + 1, len(terms_all), hit, len(got),
               ("（受 max_per_group=%d 限制，仍漏 %d）" % (cap, lost) if lost else "")))
    log("PubMed 抓到 %d 条（%d 组检索词，窗口内命中合计 %d，受上限遗漏 %d）"
        % (len(out), len(terms_all), hit_total, lost_total))
    if lost_total:
        log("  ↳ 想更全就把 config.json 的 pubmed.max_per_group 调大（0=不限），代价是抓取时间变长。")
    return out


# ---------------------------------------------------------------- 导师组作者追踪
def author_query(a, affils):
    """把 profile.md 里的一位作者拼成 PubMed 检索式。

    affils 为该作者的机构限定词列表：
      - None       -> 沿用全局「作者机构限定」
      - [] (空列表) -> 不限制机构（affils 覆盖被显式设为 none）
      - 非空列表   -> 仅用这些词
    """
    vs = " OR ".join('"%s"[Author]' % v for v in a["variants"])
    q = "(%s)" % vs
    # 机构限定：a.get("affils") 为 None 时退回全局 affils；为空列表则不加机构限定
    use = affils if a.get("affils") is None else a["affils"]
    if use:
        q += " AND (%s)" % " OR ".join('"%s"[Affiliation]' % x for x in use)
    if a.get("extra"):
        q += " AND (%s)" % a["extra"]
    return q


def _split_name(n):
    """拆成 (lastname, forename)，小写、去标点。PubMed 常见 'Weiping Jia' / 'Jia W' 都能拆。"""
    n = re.sub(r"[^a-z0-9 ]", " ", (n or "").lower())
    parts = [p for p in n.split() if p]
    if not parts:
        return ("", "")
    if len(parts) == 1:
        return (parts[0], "")
    return (parts[-1], parts[0])


def _name_matches(rec_name, variants):
    """记录里的作者名是否匹配某个署名变体（姓相同 + 名首字母/前缀匹配）。

    署名变体按中文惯例「姓在前」（如 Huang J / Jia Weiping / Liu Yuexing），
    而 PubMed 记录里的作者名可能是「姓在前」也可能是「名在前」（如 Weiping Jia / Jun Huang）。
    这里两种顺序都试，只要『姓相同 + 名首字母一致』即视为同一人。
    """
    rtoks = [t for t in re.sub(r"[^a-z0-9 ]", " ", (rec_name or "").lower()).split() if t]
    if not rtoks:
        return False
    for v in variants:
        vtoks = [t for t in re.sub(r"[^a-z0-9 ]", " ", (v or "").lower()).split() if t]
        if not vtoks:
            continue
        v_sur, v_giv = vtoks[0], vtoks[-1]
        # 记录名两种可能的「(姓, 名)」拆分都试一遍
        cands = [(rtoks[0], rtoks[-1])]
        if len(rtoks) == 2:
            cands.append((rtoks[-1], rtoks[0]))
        for r_sur, r_giv in cands:
            if r_sur != v_sur:
                continue
            if not v_giv or not r_giv:
                return True
            if v_giv[0] == r_giv[0] or v_giv.startswith(r_giv) or r_giv.startswith(v_giv):
                return True
        # 单 token 变体（如只给姓）直接看是否出现在记录里
        if len(vtoks) == 1 and vtoks[0] in rtoks:
            return True
    return False


def _author_in_institution(rec, variants, affil_keywords):
    """核验「命中作者本人」的单位是否含可接受机构词（六院/交大/糖尿病所等）。

    PubMed 的 [Affiliation] 是"全篇任意作者"匹配，会跨作者泄漏，导致同名重名被误收。
    这里逐作者核对：只有命中的导师「本人」单位含可接受机构词才保留。
    返回:
      True  : 命中作者本人单位含可接受机构词 -> 保留
      False : 命中作者本人单位不含（在外单位署名）-> 重名，丢弃
      None  : 无法判定（结构化逐作者单位缺失）-> 退回整篇机构判定（query 已带机构限定）
    """
    aas = rec.get("_author_affs") or []
    if not aas:
        return None
    for nm, aff in aas:
        if not _name_matches(nm, variants):
            continue
        if not aff:
            continue
        if not affil_keywords:
            return True  # 该作者设了 none：信任署名变体 + 额外限定词
        low = aff.lower()
        if any(k in low for k in affil_keywords):
            return True
        return False  # 命中作者本人在外单位署名 -> 视为重名
    return None


def fetch_authors(cfg):
    """导师组文章全量收集：不看关键词，只按作者 + 机构 + 时间窗口"""
    api_key = (cfg.get("pubmed") or {}).get("api_key", "")
    ak = ("&api_key=%s" % api_key) if api_key else ""
    days = int(cfg.get("lookback_days", 7))
    p = P.profile()
    people = p.get("watch_authors") or []
    affils = p.get("author_affils") or []
    if not people:
        return []
    out, seen = [], set()
    for a in people:
        # 该作者若显式设了机构覆盖（含 none），author_query 内部会用它；否则用全局 affils
        q = author_query(a, affils)
        # 单位核验用的可接受机构词：以 author_query 实际采用的机构词为准
        use = affils if a.get("affils") is None else a["affils"]
        u = ("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed&term=%s"
             "&retmode=json&retmax=100&sort=date&reldate=%s&datetype=edat%s"
             % (urllib.parse.quote(q), days, ak))
        try:
            d = http_json(u)
            ids = d.get("esearchresult", {}).get("idlist", [])
        except Exception as e:
            log("作者检索失败 [%s]: %s" % (a["name"], e))
            continue
        if not ids:
            log("导师 %-6s 近 %d 天 0 篇" % (a["name"], days))
            time.sleep(0.4)
            continue
        time.sleep(0.4 if api_key else 0.5)
        try:
            recs = pubmed_efetch(ids, ak)
        except Exception as e:
            log("作者 efetch 失败 [%s]: %s" % (a["name"], e))
            continue
        kept = 0
        for r in recs:
            if r["key"] in seen:
                continue
            seen.add(r["key"])
            # 单位核验：命中的导师必须其「本人」单位含六院/交大，否则视为重名丢弃
            verdict = _author_in_institution(r, a["variants"], use)
            if verdict is False:
                log("  ⚠️ 导师 %s 命中但署名单位非六院/交大（疑似重名），跳过：%s"
                    % (a["name"], (r.get("title") or "")[:70]))
                continue
            r["author_hit"], r["author_name"] = 1, a["name"]
            out.append(r)
            kept += 1
        log("导师 %-6s 近 %d 天 %d 篇（单位核验后保留 %d）" % (a["name"], days, len(recs), kept))
        time.sleep(0.4 if api_key else 0.5)
    log("导师组合计 %d 篇（不受关键词约束，全部保留）" % len(out))
    return out


# ---------------------------------------------------------------- OpenAlex
_TOP_ISSN = None


def top_issns():
    """人工覆盖表里 S+/S/A/W 级期刊的 ISSN，用于 OpenAlex 全刊扫描"""
    global _TOP_ISSN
    if _TOP_ISSN is not None:
        return _TOP_ISSN
    res = []
    if os.path.exists(JOURNALS):
        with open(JOURNALS, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                t = (row.get("tier") or "").strip().upper()
                if t not in ("S+", "S", "A", "W"):
                    continue
                k = re.sub(r"[^0-9Xx]", "", (row.get("issn") or "")).upper()
                if k:
                    res.append(k)
    _TOP_ISSN = sorted(set(res))
    return _TOP_ISSN


def oa_abstract(inv):
    if not inv:
        return ""
    pos = {}
    for w, ps in inv.items():
        for p in ps:
            pos[p] = w
    return " ".join(pos[k] for k in sorted(pos))


def fetch_openalex(cfg, since, until, budget=240, extra_terms=None):
    """OpenAlex 抓取。

    budget: 本源的秒级时间预算。OpenAlex 限流（429）和偶发卡顿都会让请求拖很久，
    没有预算的话一个源就能把整条流水线拖死。超预算就带着已抓到的部分返回。
    """
    t0 = time.time()
    mailto = cfg.get("researcher", {}).get("mailto") or ""
    mail = ("&mailto=%s" % urllib.parse.quote(mailto)) if mailto else ""
    out = []
    seen = set()

    def add(w):
        src = (w.get("primary_location") or {}).get("source") or {}
        jn = src.get("display_name") or ""
        if src.get("type") not in (None, "journal"):     # 排除仓储、会议、图书
            return
        if bad_source(jn):                                # 排除 Zenodo / DOAJ 之类
            return
        pd = w.get("publication_date") or ""
        if pd and (pd < since or pd > until):             # 客户端再兜一次日期
            return
        key = w.get("doi") or w.get("id")
        if key in seen:
            return
        seen.add(key)
        aus = "; ".join(a.get("author", {}).get("display_name", "") for a in (w.get("authorships") or [])[:8])
        out.append(mk(w.get("doi"), w.get("display_name"), src.get("display_name"),
                      w.get("publication_date"), w.get("doi") or w.get("id"),
                      oa_abstract(w.get("abstract_inverted_index")), aus, "openalex",
                      w.get("cited_by_count") or 0, src.get("issn_l") or ""))

    # 1) 关键词检索（| = OR，一组词一次请求，把上百次往返压到二三十次）
    nq = 0
    for terms in openalex_terms(cfg, extra_terms):
        if time.time() - t0 > budget:
            log("OpenAlex 已达时间预算 %ds，提前结束（已完成 %d/%d 组）" % (budget, nq, len(openalex_terms(cfg, extra_terms))))
            return out
        t = "|".join(terms)
        f = "title_and_abstract.search:%s,from_publication_date:%s,to_publication_date:%s,type:article" % (t, since, until)
        u = ("https://api.openalex.org/works?filter=%s&per-page=100&sort=publication_date:desc%s"
             % (urllib.parse.quote(f, safe=":,-|"), mail))
        try:
            d = http_json(u, timeout=25, retries=1)
        except Exception as e:
            log("OpenAlex 关键词失败 [组%s]: %s" % (nq, e))
            continue
        nq += 1
        for w in d.get("results", []):
            add(w)
        time.sleep(1.0)          # OpenAlex 限流很敏感，0.3 秒会被 429
    log("OpenAlex 关键词检索 %d 组" % nq)

    # 2) 顶刊 ISSN 批量扫描（人工覆盖表里 S+/S/A/W 且带 ISSN 的）
    issns = top_issns()
    if issns:
        log("OpenAlex 顶刊扫描：%d 个 ISSN" % len(issns))
        chunk = 25
        for i in range(0, len(issns), chunk):
            if time.time() - t0 > budget:
                log("OpenAlex 顶刊扫描超出时间预算，跳过剩余批次")
                break
            part = "|".join(issns[i:i + chunk])
            f = "primary_location.source.issn:%s,from_publication_date:%s,to_publication_date:%s" % (part, since, until)
            u = ("https://api.openalex.org/works?filter=%s&per-page=100&sort=publication_date:desc%s"
                 % (urllib.parse.quote(f, safe=":,-|"), mail))
            try:
                d = http_json(u, timeout=25, retries=1)
            except Exception as e:
                log("OpenAlex 顶刊扫描失败: %s" % e)
                continue
            for w in d.get("results", []):
                add(w)
            time.sleep(1.0)

    log("OpenAlex 抓到 %d 条" % len(out))
    return out


# ---------------------------------------------------------------- 预印本
def fetch_preprint(kind, since, until, topics, cap=600, budget=180):
    t0 = time.time()
    host = {"biorxiv": "biorxiv", "medrxiv": "medrxiv"}[kind]
    out, cursor, got = [], 0, 0
    while got < cap:
        if time.time() - t0 > budget:
            log("%s 超出时间预算 %ds，取已抓到的 %d 条" % (kind, budget, got))
            break
        u = "https://api.%s.org/details/%s/%s/%s/%d" % (host, host, since, until, cursor)
        try:
            d = http_json(u, timeout=45, retries=1)
        except Exception as e:
            log("%s 抓取失败: %s" % (kind, e))
            break
        coll = d.get("collection") or []
        if not coll:
            break
        got += len(coll)
        for r in coll:
            text = "%s %s" % (r.get("title", ""), r.get("abstract", ""))
            if not hit_topics(text, topics):
                continue
            out.append(mk(r.get("doi"), r.get("title"), "%s (%s)" % (kind, r.get("category", "")),
                          r.get("date"), "https://www.%s.org/content/%s" % (host, r.get("doi") or ""),
                          r.get("abstract"), r.get("authors", ""), kind, 0, ""))
        cursor += len(coll)
        if len(coll) < 30:
            break
        time.sleep(0.5)
    log("%s 命中 %d 条（累计扫描 %d）" % (kind, len(out), got))
    return out


def fetch_arxiv(since, topics, cap=200):
    out = []
    for tp in topics:
        q = " OR ".join('all:"%s"' % t for t in tp.get("terms", [])[:4])
        u = ("https://export.arxiv.org/api/query?search_query=%s&start=0&max_results=60"
             "&sortBy=submittedDate&sortOrder=descending" % urllib.parse.quote(q))
        try:
            xml = http_get(u, timeout=60)
            ns = {"a": "http://www.w3.org/2005/Atom"}
            root = ET.fromstring(xml)
        except Exception as e:
            log("arXiv 失败: %s" % e)
            continue
        for e in root.findall("a:entry", ns):
            t = (e.findtext("a:title", "", ns) or "").strip()
            s = (e.findtext("a:summary", "", ns) or "").strip()
            pub = norm_date(e.findtext("a:published", "", ns))
            if pub and pub < since:
                continue
            if not hit_topics(t + " " + s, topics):
                continue
            link = e.findtext("a:id", "", ns)
            out.append(mk(link, t, "arXiv (%s)" % tp["name"], pub, link, s, "", "arxiv", 0, ""))
        time.sleep(1.0)
    log("arXiv 命中 %d 条" % len(out))
    return out


# ---------------------------------------------------------------- RSS
NS = {"a": "http://www.w3.org/2005/Atom", "dc": "http://purl.org/dc/elements/1.1/"}


RDFNS = "http://purl.org/rss/1.0/"
DCNS = "http://purl.org/dc/elements/1.1/"


def _t(el, name, default=""):
    """RSS 1.0 (RDF) / RSS 2.0 / DC 三种命名空间的字段取值"""
    for ns in ("", "{%s}" % RDFNS, "{%s}" % DCNS):
        v = el.findtext(ns + name)
        if v:
            return v
    return default


def rss_items(xml):
    try:
        root = ET.fromstring(xml)
    except Exception:
        return []
    rss1 = ".//{%s}item" % RDFNS
    items = root.findall(".//item") + root.findall(rss1) + root.findall(".//a:entry", NS)
    res = []
    for it in items:
        if it.tag.endswith("entry"):
            link = it.find("a:link", NS)
            res.append({
                "title": it.findtext("a:title", "", NS),
                "link": (link.get("href") if link is not None else "") or it.findtext("a:id", "", NS),
                "date": it.findtext("a:updated", "", NS) or it.findtext("a:published", "", NS),
                "desc": it.findtext("a:summary", "", NS) or it.findtext("a:content", "", NS) or "",
            })
        else:
            res.append({
                "title": _t(it, "title"),
                "link": _t(it, "link"),
                "date": _t(it, "pubDate") or _t(it, "date"),
                "desc": _t(it, "description") or _t(it, "abstract"),
            })
    return [r for r in res if r["title"]]


def fetch_rss(cfg, since, topics):
    out = []
    for f in cfg.get("rss_feeds", []):
        try:
            xml = http_get(f["url"], timeout=40)
        except Exception as e:
            log("RSS 失败 [%s]: %s" % (f.get("name"), e))
            continue
        n = 0
        for it in rss_items(xml):
            pd = norm_date(it["date"])
            if pd and pd < since:
                continue
            text = "%s %s" % (it["title"], re.sub(r"<[^>]+>", "", it["desc"]))
            if not hit_topics(text, topics):
                continue
            out.append(mk(it["link"], it["title"], f.get("name", "RSS"), pd, it["link"],
                          re.sub(r"<[^>]+>", "", it["desc"]), "", "rss", 0, ""))
            n += 1
        log("RSS [%s] 命中 %d 条" % (f.get("name"), n))
    return out


# ---------------------------------------------------------------- 入库
SCHEMA = """
CREATE TABLE IF NOT EXISTS papers(
  key TEXT PRIMARY KEY, doi TEXT, title TEXT, journal TEXT, pub_date TEXT,
  url TEXT, abstract TEXT, authors TEXT, source TEXT, cites INTEGER,
  tier TEXT, topics TEXT, first_seen TEXT, week TEXT,
  status TEXT DEFAULT 'new', score INTEGER DEFAULT 0, note TEXT DEFAULT '',
  quartile TEXT DEFAULT '', jif REAL DEFAULT 0, field TEXT DEFAULT '',
  pmid TEXT DEFAULT '', issn TEXT DEFAULT '', layer TEXT DEFAULT '',
  oa_url TEXT DEFAULT '', pdf_path TEXT DEFAULT '', cn_title TEXT DEFAULT '',
  llm_tag TEXT DEFAULT '', llm_reason TEXT DEFAULT '',
  advisor_fit TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_week ON papers(week);
CREATE INDEX IF NOT EXISTS idx_status ON papers(status);
"""

# 老库升级用的增量列
MIGRATIONS = [
    ("quartile", "TEXT DEFAULT ''"), ("jif", "REAL DEFAULT 0"), ("field", "TEXT DEFAULT ''"),
    ("pmid", "TEXT DEFAULT ''"), ("issn", "TEXT DEFAULT ''"), ("layer", "TEXT DEFAULT ''"),
    ("oa_url", "TEXT DEFAULT ''"), ("pdf_path", "TEXT DEFAULT ''"), ("cn_title", "TEXT DEFAULT ''"),
    ("author_hit", "INTEGER DEFAULT 0"), ("author_name", "TEXT DEFAULT ''"),
    ("affiliations", "TEXT DEFAULT ''"),
    ("llm_tag", "TEXT DEFAULT ''"), ("llm_reason", "TEXT DEFAULT ''"),
    ("advisor_fit", "TEXT DEFAULT ''"),
]


def open_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)
    have = {r[1] for r in con.execute("PRAGMA table_info(papers)")}
    for col, ddl in MIGRATIONS:
        if col not in have:
            try:
                con.execute("ALTER TABLE papers ADD COLUMN %s %s" % (col, ddl))
            except Exception:
                pass
    try:
        con.execute("CREATE INDEX IF NOT EXISTS idx_layer ON papers(layer)")
    except Exception:
        pass
    return con


# 入库列（与 SCHEMA 对齐；layer/oa_url/pdf_path/cn_title 走默认值，不显式写）。
# 单一来源：列名与取值都从这里派生，永远不可能错位。
PAPER_COLS = ["key", "doi", "title", "journal", "pub_date", "url", "abstract", "authors",
              "source", "cites", "tier", "topics", "first_seen", "week", "status", "score",
              "note", "quartile", "jif", "field", "pmid", "issn", "author_hit", "author_name",
              "affiliations", "llm_tag", "llm_reason", "advisor_fit"]


def _insert_vals(r, week, today):
    return [
        r["key"], r["doi"], r["title"], r["journal"], r["pub_date"], r["url"],
        r["abstract"], r["authors"], r["source"], r.get("cites", 0), r.get("tier", ""),
        r.get("topics", ""), today, week, "new", 0, "",
        r.get("quartile", ""), r.get("jif") or 0, r.get("field", ""),
        r.get("pmid", ""), r.get("issn", ""),
        r.get("author_hit", 0), r.get("author_name", ""), r.get("affiliations", ""),
        r.get("llm_tag", ""), r.get("llm_reason", ""), r.get("advisor_fit", ""),
    ]


def upsert(con, rows, week):
    new, upd = 0, 0
    today = date.today().isoformat()
    for r in rows:
        cur = con.execute("SELECT key FROM papers WHERE key=?", (r["key"],))
        if cur.fetchone():
            con.execute("UPDATE papers SET cites=MAX(cites,?), tier=COALESCE(NULLIF(tier,''),?),"
                        " quartile=COALESCE(NULLIF(quartile,''),?), jif=MAX(jif,?),"
                        " pmid=COALESCE(NULLIF(pmid,''),?),"
                        " author_hit=MAX(author_hit,?), author_name=COALESCE(NULLIF(author_name,''),?),"
                        " affiliations=COALESCE(NULLIF(affiliations,''),?),"
                        " llm_tag=COALESCE(NULLIF(llm_tag,''),?), llm_reason=COALESCE(NULLIF(llm_reason,''),?),"
                        " advisor_fit=COALESCE(NULLIF(advisor_fit,''),?)"
                        " WHERE key=?",
                        (r.get("cites", 0), r.get("tier", ""), r.get("quartile", ""),
                         r.get("jif") or 0, r.get("pmid", ""),
                         r.get("author_hit", 0), r.get("author_name", ""), r.get("affiliations", ""),
                         r.get("llm_tag", ""), r.get("llm_reason", ""), r.get("advisor_fit", ""),
                         r["key"]))
            upd += 1
        else:
            vals = _insert_vals(r, week, today)
            assert len(vals) == len(PAPER_COLS), (len(vals), len(PAPER_COLS))
            con.execute("INSERT INTO papers(%s) VALUES(%s)" % (
                ",".join(PAPER_COLS), ",".join("?" * len(PAPER_COLS))), vals)
            new += 1
    con.commit()
    return new, upd


def dedupe_pmid(con):
    """同一 PMID 出现多行时只保留一行。

    兜底清理 DOI 串号 bug（见 pubmed_records 注释）：历史上同一条 PubMed 记录
    可能因为 DOI 被参考文献覆盖而产生「同 pmid / 同标题 / 不同 key」的第二行。
    pmid 对一篇文章唯一，所以同 pmid 多行必为重复。
    保留优先级：first_seen 最早的那条（即首次正确入库的原始记录）。
    """
    groups = con.execute(
        "SELECT pmid, group_concat(key, char(31)) ks FROM papers "
        "WHERE pmid<>'' GROUP BY pmid HAVING count(*)>1").fetchall()
    removed = []
    for pmid, ks in groups:
        info = []
        for k in (ks or "").split(chr(31)):
            r = con.execute("SELECT first_seen FROM papers WHERE key=?", (k,)).fetchone()
            info.append((k, (r[0] if r else "") or "9999-99-99"))
        info.sort(key=lambda x: x[1])
        for k, _fs in info[1:]:
            con.execute("DELETE FROM papers WHERE key=?", (k,))
            removed.append(k)
    if removed:
        con.commit()
    return removed


# ---------------------------------------------------------------- 主流程
def _ingest(con, srows, since_s, until_s, topics, excl, week, name, proxy_requires_core=True,
            extra_terms=None, cfg=None):
    """归一化一批抓取结果并即时入库。

    日期硬过滤 + 仓储过滤 + 主题命中 + 排除词 + 去重。
    每个源的批内去重用本地 seen；跨源重复交给 upsert 的 UPDATE 合并，
    这样一个源卡死/跳过也不会影响已抓到的其他源（每段抓完即落盘）。
    """
    if not srows:
        return 0, 0
    kept, dropped_date, dropped_src = [], 0, 0
    band_lo = int(since_s[:4])
    band_hi = int(until_s[:4]) + 1           # 宽松 1 年，兜住 ahead-of-print 的未来印期
    for r in srows:
        # 日期过滤（宽松版）：只丢年份明显越界的，避免发表日≠入库日的近期文章被整批误杀。
        pd = r["pub_date"] or ""
        if pd:
            try:
                py = int(pd[:4])
                if py < band_lo or py > band_hi:
                    dropped_date += 1
                    continue
            except Exception:
                pass
        if r["source"] in ("openalex", "rss") and bad_source(r["journal"]):
            dropped_src += 1
            continue
        text = "%s %s" % (r["title"], r["abstract"])
        t = text.lower()
        if r.get("author_hit"):
            # 导师组：一律保留，但把命中的课题词记下来便于归类
            tp = hit_topics(text, topics)
            r["topics"] = ",".join(tp) if tp else ""
            kept.append(r)
            continue
        # 强相关约束（2026-10）：核心(糖尿病)词命中才保留；
        # 仅有方法/相邻(proxy)词命中时，默认要求同时命中糖尿病核心词，
        # 避免纯方法学或非糖尿病文献混入。可由 config.proxy_requires_core 关闭（恢复旧行为）。
        p = P.profile()
        core_hit = [w for w in p["core"] if w in t]
        proxy_hit = [w for w in p["proxy"] if w in t]
        if core_hit:
            tp = core_hit + proxy_hit
        elif proxy_hit:
            if proxy_requires_core:
                continue
            tp = proxy_hit
        else:
            # RAG 式召回：命中 LLM 扩词（无核心/方法词）也先保留，交给 llm_augment 判相关，
            # 相关（extension/method/frontier）才进库/下载；与现有「无词就丢」逻辑互补。
            extra_hit = [w for w in (extra_terms or []) if w in t]
            if extra_hit and cfg is not None and queryx.survive_without_core(cfg):
                ex = excluded(text, excl)
                if ex and not r["tier"]:
                    continue
                r["topics"] = ",".join(extra_hit[:8])
                if ex:
                    r["topics"] += " | 疑似排除:" + ",".join(ex[:2])
                kept.append(r)
                continue
            continue
        ex = excluded(text, excl)
        if ex and not r["tier"]:
            continue
        r["topics"] = ",".join(tp[:8])
        if ex:
            r["topics"] += " | 疑似排除:" + ",".join(ex[:2])
        kept.append(r)

    seen, uniq = set(), []
    for r in sorted(kept, key=lambda x: (jcr.TIER_ORDER.get(x["tier"], 9), -len(x.get("topics", "")))):
        k = r["key"]
        if k in seen:
            continue
        seen.add(k)
        uniq.append(r)

    new, upd = upsert(con, uniq, week)
    log("源 %s：原始 %d → 过滤 %d → 入库新增 %d、更新 %d（丢弃：超期 %d、仓储 %d）"
        % (name, len(srows), len(uniq), new, upd, dropped_date, dropped_src))
    return new, upd


def cmd_fetch(from_cache=False):
    cfg = load_config()
    days = int(cfg.get("lookback_days", 7))
    # 目标周 = 上一周（今天往前推 7 天的 ISO 周）。节奏：周一跑、抓「上一周」、当周读，
    # 即 10-12(周一)抓 10-05~10-11(W41)，用户在 10-12~10-18 这周读 W41；各 W 册独立、不累计。
    today = date.today()
    target_monday = jcr.week_bounds(today - timedelta(days=7))[0]
    until = target_monday + timedelta(days=6)   # 上周日
    since = target_monday
    week = target_monday.isoformat()            # 标签=上周一，报告据此归到正确的 W 册
    since_s, until_s = since.isoformat(), until.isoformat()
    topics = cfg["topics"]
    excl = cfg.get("exclude_terms", [])
    srcs = cfg.get("sources", {})

    # 检索前大模型扩词（RAG 式召回增强）：本次只调一次 LLM，缓存复用
    extra_terms = ([w.lower() for w in queryx.load_expanded_terms(cfg)] if queryx.enabled(cfg) else [])
    if extra_terms:
        log("LLM 扩词已启用：本次追加 %d 个检索词（RAG 式召回增强）" % len(extra_terms))

    con = open_db()          # 一上来就开库，每个源抓完立即落盘
    tot_new = tot_upd = 0

    plan = [
        ("pubmed",  srcs.get("pubmed"),   lambda: fetch_pubmed(cfg, since_s, until_s, extra_terms)),
        ("openalex", srcs.get("openalex"), lambda: fetch_openalex(cfg, since_s, until_s, 240, extra_terms)),
        ("biorxiv", srcs.get("biorxiv"),   lambda: fetch_preprint("biorxiv", since_s, until_s, topics)),
        ("medrxiv", srcs.get("medrxiv"),   lambda: fetch_preprint("medrxiv", since_s, until_s, topics)),
        ("arxiv",   srcs.get("arxiv"),     lambda: fetch_arxiv(since_s, topics)),
        ("rss",     srcs.get("rss"),       lambda: fetch_rss(cfg, since_s, topics)),
    ]
    for name, on, fn in plan:
        if not on:
            continue
        cache = os.path.join(DATA_DIR, "_cache_%s.json" % name)
        srows = None
        if from_cache and os.path.exists(cache):
            try:
                with open(cache, encoding="utf-8") as f:
                    srows = json.load(f)
                log("源 %s：从缓存恢复 %d 条" % (name, len(srows)))
            except Exception:
                srows = None
        if srows is None:
            try:
                srows = fn()
            except Exception as e:          # noqa
                log("源 %s 抓取异常，跳过：%s" % (name, e))
                continue
            # 缓存原始结果：万一后续源崩溃，不必重抓整个 PubMed
            try:
                with open(cache, "w", encoding="utf-8") as f:
                    json.dump(srows, f, ensure_ascii=False)
            except Exception:
                pass
        try:
            n, u = _ingest(con, srows, since_s, until_s, topics, excl, week, name,
                          cfg.get("proxy_requires_core", True), extra_terms=extra_terms, cfg=cfg)
        except Exception as e:          # noqa
            log("源 %s 入库异常，跳过：%s" % (name, e))
            continue
        tot_new += n
        tot_upd += u

    # 导师组文章：不看关键词，单独一条通道（同样即时落盘）
    n_author = 0
    if srcs.get("watch_authors", True):
        cache = os.path.join(DATA_DIR, "_cache_authors.json")
        arows = None
        if from_cache and os.path.exists(cache):
            try:
                with open(cache, encoding="utf-8") as f:
                    arows = json.load(f)
                log("导师组：从缓存恢复 %d 条" % len(arows))
            except Exception:
                arows = None
        if arows is None:
            try:
                arows = fetch_authors(cfg)
                try:
                    with open(cache, "w", encoding="utf-8") as f:
                        json.dump(arows, f, ensure_ascii=False)
                except Exception:
                    pass
            except Exception as e:          # noqa
                log("导师组抓取异常，跳过：%s" % e)
                arows = None
        if arows:
            try:
                n, u = _ingest(con, arows, since_s, until_s, topics, excl, week, "导师组",
                              extra_terms=extra_terms, cfg=cfg)
                tot_new += n
                tot_upd += u
                n_author = len(arows)
            except Exception as e:          # noqa
                log("导师组入库异常，跳过：%s" % e)

    log("本轮入库完成：新增 %d 条，更新 %d 条" % (tot_new, tot_upd))
    if n_author:
        log("其中导师组文章 %d 篇（已强制保留，不受关键词/排除词约束）" % n_author)

    # 兜底去重：同 PMID 多行只留最早的一条（防 DOI 串号残留）
    try:
        rm = dedupe_pmid(con)
        if rm:
            log("按 PMID 去重：清理重复行 %d 条" % len(rm))
    except Exception as e:          # noqa
        log("去重异常（跳过）：%s" % e)

    # 导出待打分
    cap = int(cfg.get("max_papers_per_run", 600))
    # 待打分导出按【所属 ISO 周】取，而不是 week=今天。
    # 同周多次补跑时，早先批次里还没打分（或本次重打分）的条目必须一起带上，
    # 否则它们会一直卡在 status='new'/score=0，永远排不进报告前排。
    _lo, _hi = jcr.week_range(week)
    pend = con.execute(
        "SELECT key,doi,title,journal,pub_date,url,abstract,authors,tier,topics,source,cites,quartile,jif,pmid,"
        "author_hit,author_name"
        " FROM papers WHERE week BETWEEN ? AND ? AND status='new' ORDER BY author_hit DESC, "
        "CASE tier WHEN 'S+' THEN 0 WHEN 'S' THEN 1 WHEN 'A' THEN 2 WHEN 'W' THEN 3"
        " WHEN 'B' THEN 4 WHEN 'C' THEN 5 WHEN 'D' THEN 6 ELSE 7 END, cites DESC LIMIT ?",
        (_lo, _hi, cap)).fetchall()
    cols = ["key", "doi", "title", "journal", "pub_date", "url", "abstract", "authors",
            "tier", "topics", "source", "cites", "quartile", "jif", "pmid",
            "author_hit", "author_name"]
    items = [dict(zip(cols, r)) for r in pend]
    for it in items:
        it["abstract"] = (it["abstract"] or "")[:1600]
    with open(PENDING, "w", encoding="utf-8") as f:
        json.dump({"week": week, "since": since.isoformat(), "until": until.isoformat(),
                   "count": len(items), "items": items}, f, ensure_ascii=False, indent=1)
    log("已导出 %d 条待打分 -> data/pending.json" % len(items))
    con.close()

    # 检索闭环（#1）：用本周实际入库的文献，让 LLM 反推下周该补搜的检索词，写回缓存供下周长用。
    # 一周一次调用，成本低；LLM 不可用时自动跳过，不影响主流程。
    try:
        import profile as P
        prof = P.profile()
        c2 = open_db()
        wk = c2.execute(
            "SELECT title,abstract,topics FROM papers WHERE week BETWEEN ? AND ?",
            (_lo, _hi)).fetchall()
        c2.close()
        papers = [{"title": r[0] or "", "abstract": r[1] or "", "topics": r[2] or ""} for r in wk]
        if papers:
            n = queryx.suggest_and_store(cfg, prof, papers, week)
            if n:
                log("检索闭环：基于本周 %d 篇生成 %d 个下周检索词（反馈环已写入缓存）" % (len(papers), n))
    except Exception as e:
        log("检索闭环生成失败（跳过，不阻断主流程）：%s" % e)


def cmd_apply():
    if not os.path.exists(SCORED):
        log("没有 data/scored.json，先打分再回填")
        return
    with open(SCORED, encoding="utf-8") as f:
        data = json.load(f)
    con = open_db()
    n = 0
    for it in data.get("items", []):
        # 评语为空时不要覆盖库里已有的评语：整批可能只有前 24 篇进模型，
        # 其余走规则兜底（已改为不产评语），直接写空会把上一轮的好评语抹掉。
        note = (it.get("note", "") or "")[:900]
        cn = (it.get("cn_title", "") or "")[:300]
        if note:
            con.execute("UPDATE papers SET score=?, note=?, cn_title=?, status=? WHERE key=?",
                        (int(it.get("score", 0)), note, cn, it.get("status", "new"), it["key"]))
        else:
            con.execute("UPDATE papers SET score=?, "
                        "cn_title=CASE WHEN ?<>'' THEN ? ELSE cn_title END, status=? WHERE key=?",
                        (int(it.get("score", 0)), cn, cn, it.get("status", "new"), it["key"]))
        n += 1
    con.commit()
    con.close()
    log("回填 %d 条打分" % n)


TIER_LABEL = jcr.TIER_LABEL


def cmd_report():
    cfg = load_config()
    con = open_db()
    # 周报取数要按【所属 ISO 周】而不是 week=今天：抓取日与出报日跨过周一、
    # 或同一周内补跑过，week 就不再是今天，按今天取会得到一份空的/少半截的周报。
    _r = con.execute("SELECT max(week) FROM papers").fetchone()
    week0 = (_r[0] if _r and _r[0] else date.today().isoformat())
    _lo, _hi = jcr.week_range(week0)
    rows = con.execute(
        "SELECT key,doi,title,journal,pub_date,url,abstract,authors,tier,topics,source,cites,status,score,note,"
        "quartile,jif,pmid,layer,field,author_hit,author_name"
        " FROM papers WHERE week BETWEEN ? AND ? ORDER BY author_hit DESC, score DESC, "
        "CASE tier WHEN 'S+' THEN 0 WHEN 'S' THEN 1 WHEN 'A' THEN 2 WHEN 'W' THEN 3"
        " WHEN 'B' THEN 4 WHEN 'C' THEN 5 WHEN 'D' THEN 6 ELSE 7 END, cites DESC",
        (_lo, _hi)).fetchall()
    cols = ["key", "doi", "title", "journal", "pub_date", "url", "abstract", "authors", "tier",
            "topics", "source", "cites", "status", "score", "note",
            "quartile", "jif", "pmid", "layer", "field", "author_hit", "author_name"]
    items = [dict(zip(cols, r)) for r in rows]
    con.close()
    week = week0   # 标题/文件名用库里最新的目标周，避免与今天错位
    outdir = os.path.join(BASE, cfg.get("output", {}).get("dir", "reports"))
    os.makedirs(outdir, exist_ok=True)

    # 按 profile.md 里写的「每周精读容量」切分，而不是拍一个分数阈值
    cap = P.profile().get("capacity") or 12
    # 导师组文章无条件进重点，且排在最前面
    author_items = [i for i in items if i.get("author_hit")]
    good = author_items[:]
    used = {i["key"] for i in good}
    good += [i for i in items if i["key"] not in used and (i["score"] or 0) >= 62][:cap]
    used = {i["key"] for i in good}
    if len(good) < min(6, len(items)):          # 一周没什么高分货时，至少补齐几篇
        good += [i for i in items if i["key"] not in used][:min(cap, len(items)) - len(good)]
    used = {i["key"] for i in good}
    maybe = [i for i in items if i["key"] not in used and (i["score"] or 0) >= 48][:cap]
    used |= {i["key"] for i in maybe}
    drop = [i for i in items if i["key"] not in used]
    drop_show = [i for i in drop if (i["layer"] or "") == "eco"][:8] + \
                [i for i in drop if (i["layer"] or "") != "eco"][:12]

    md = ["# 文献周报 %s" % week, "",
          "扫描范围：%s ~ %s（%d 天）｜ 共 %d 篇，其中重点 %d 篇、可看 %d 篇、其余 %d 篇" %
          ((date.today() - timedelta(days=int(cfg.get("lookback_days", 7)))).isoformat(), week,
           int(cfg.get("lookback_days", 7)), len(items), len(good), len(maybe), len(drop)), ""]
    for label, group in (("重点推荐", good), ("可以一看", maybe), ("已过滤（节选）", drop_show)):
        if not group:
            continue
        md += ["## %s（%d）" % (label, len(group)), ""]
        for i in group:
            au = " 🎓 **导师组 · %s** ｜" % i["author_name"] if i.get("author_hit") else ""
            md += ["### %s" % i["title"],
                   "- %s ｜ %s ｜ %s ｜ %s%s" % (i["journal"], i["pub_date"],
                                               TIER_LABEL.get(i["tier"], "其他"), i["source"], au),
                   "- 命中主题：%s" % (i["topics"] or "-"),
                   "- %s" % (i["note"] or (i["abstract"][:200] if i["abstract"] else "")),
                   "- [原文](%s)" % i["url"], ""]
    mdp = os.path.join(outdir, "%s.md" % week)
    with open(mdp, "w", encoding="utf-8") as f:
        f.write("\n".join(md))

    if cfg.get("output", {}).get("html", True):
        htmlp = os.path.join(outdir, "%s.html" % week)
        with open(htmlp, "w", encoding="utf-8") as f:
            f.write(render_html(week, cfg, good, maybe, drop_show, items))
        log("日报已生成：%s / %s" % (mdp, htmlp))
    else:
        log("日报已生成：%s" % mdp)


def esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def card(i):
    tier = i["tier"] or ""
    badge = jcr.TIER_COLOR.get(tier, "#888780")
    txt = jcr.TIER_LABEL.get(tier, "未分级")
    q = i.get("quartile") or ""
    jif = i.get("jif") or 0
    if q and jif:
        txt += " · %s · IF %.1f" % (q, jif)
    elif q:
        txt += " · %s" % q
    layer = i.get("layer") or ""
    lmark = {"core": "核心", "proxy": "方法/相邻", "eco": "背景"}.get(layer, "")
    note = i["note"] or (esc(i["abstract"])[:260] + "..." if i["abstract"] else "")
    # 导师组：金色徽章，一眼可见
    abadge = ('<span class="badge au">导师组 · %s</span>' % esc(i.get("author_name") or "")) \
        if i.get("author_hit") else ""
    return (
        '<div class="card" data-tier="%s" data-topic="%s" data-layer="%s" data-author="%s">'
        '<div class="row">%s<span class="badge" style="background:%s">%s</span>'
        '<span class="lay">%s</span><span class="src">%s</span><span class="score">%s</span></div>'
        '<a class="title" href="%s" target="_blank">%s</a>'
        '<div class="meta">%s ｜ %s ｜ %s</div>'
        '<div class="note">%s</div></div>'
    ) % (esc(tier), esc(i["topics"]), esc(layer), "1" if i.get("author_hit") else "0",
         abadge, badge, txt, lmark, esc(i["source"]),
         ("%d 分" % i["score"]) if i["score"] else "", esc(i["url"]), esc(i["title"]),
         esc(i["journal"]), esc(i["pub_date"]), esc(i["topics"] or "-"), esc(note))


def render_html(week, cfg, good, maybe, drop_show, items):
    topics = sorted({t.strip() for i in items for t in (i["topics"] or "").split(",") if t.strip() and "|" not in t})
    opts = "".join('<option value="%s">%s</option>' % (esc(t), esc(t)) for t in topics)
    used = sorted({(i["tier"] or "") for i in items}, key=lambda x: jcr.TIER_ORDER.get(x, 9))
    tier_opts = "".join('<option value="%s">%s</option>' % (esc(t), jcr.TIER_LABEL.get(t, "未分级"))
                        for t in used if t)
    body = '<div class="tip" style="background:#EAF3FB">本邮件为预筛选的强相关文献；完整可检索清单与交互筛选栏见下载目录的「0_一键打开链接.html」（用浏览器打开即可按分类/等级/关键词/下载状态实时筛选）。</div>'
    for label, group in (("重点推荐", good), ("可以一看", maybe), ("已过滤 · 节选", drop_show)):
        if not group:
            continue
        body += '<h2 class="gh">%s <span>%d 篇</span></h2><div class="grid">%s</div>' % (label, len(group), "".join(card(i) for i in group))
    return """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>文献周报 %s</title><style>
*{box-sizing:border-box}body{margin:0;padding:24px;background:#F7F6F3;color:#2C2C2A;
font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;line-height:1.6}
.wrap{max-width:940px;margin:0 auto}
h1{font-size:20px;font-weight:500;margin:0 0 4px}
.sub{font-size:13px;color:#5F5E5A;margin-bottom:16px}
.bar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:18px}
select,input{padding:6px 10px;border:0.5px solid #D3D1C7;border-radius:8px;background:#fff;font-size:13px}
.gh{font-size:15px;font-weight:500;margin:22px 0 10px}
.gh span{font-size:12px;color:#888780;font-weight:400}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
.card{background:#fff;border:0.5px solid #D3D1C7;border-radius:12px;padding:14px 16px}
.row{display:flex;align-items:center;gap:8px;margin-bottom:8px}
.badge{font-size:11px;color:#fff;padding:2px 8px;border-radius:6px}
.src{font-size:11px;color:#888780}
.lay{font-size:11px;color:#7A4E9E;background:#F3EDF9;padding:2px 7px;border-radius:6px}
.score{margin-left:auto;font-size:11px;color:#185FA5}
.badge.au{background:#B8860B}
.card[data-author="1"]{border-color:#B8860B;box-shadow:0 0 0 2px rgba(184,134,11,.14)}
.title{display:block;font-size:14px;font-weight:500;color:#0C447C;text-decoration:none;margin-bottom:6px}
.title:hover{text-decoration:underline}
.meta{font-size:12px;color:#5F5E5A;margin-bottom:6px}
.note{font-size:13px;color:#444441}
</style></head><body><div class="wrap">
<h1>文献周报 %s</h1>
<div class="sub">%s ~ %s ｜ 共 %d 篇：重点 %d、可看 %d、已过滤 %d</div>
<div class="bar">
<select id="ft"><option value="">全部期刊等级</option>%s</select>
<select id="fl"><option value="">全部相关度</option><option value="core">核心</option><option value="proxy">方法/相邻</option><option value="eco">背景</option></select>
<select id="fp"><option value="">全部命中词</option>%s</select>
<select id="fa"><option value="">全部作者</option><option value="1">只看导师组</option></select>
<input id="fq" placeholder="搜索标题 / 期刊关键词" style="flex:1;min-width:180px">
</div>%s
</div><script>
var cs=[].slice.call(document.querySelectorAll('.card'));
function apply(){var t=document.getElementById('ft').value,l=document.getElementById('fl').value,p=document.getElementById('fp').value,a=document.getElementById('fa').value,q=document.getElementById('fq').value.toLowerCase();
cs.forEach(function(c){var ok=(!t||c.dataset.tier===t)&&(!l||c.dataset.layer===l)&&(!p||(c.dataset.topic||'').indexOf(p)>=0)&&(!a||c.dataset.author===a)&&(!q||c.textContent.toLowerCase().indexOf(q)>=0);
c.style.display=ok?'':'none';});}
['ft','fl','fp','fa'].forEach(function(i){document.getElementById(i).onchange=apply;});
document.getElementById('fq').oninput=apply;
</script></body></html>""" % (
        week, week,
        (date.today() - timedelta(days=int(cfg.get("lookback_days", 7)))).isoformat(), week,
        len(items), len(good), len(maybe), len(drop_show), tier_opts, opts, body)


def cmd_retier():
    """改完 journals.csv / 换了新的 JCR 表之后，重算全部条目的期刊等级与分区"""
    con = open_db()
    rows = con.execute("SELECT key, journal, issn, source FROM papers").fetchall()
    for k, j, issn, src in rows:
        tier, quart, jif, field, _ = jcr.enrich(j or "", issn or "", (src or "") in PREPRINT_SRC)
        con.execute("UPDATE papers SET tier=?, quartile=?, jif=?, field=? WHERE key=?",
                    (tier, quart, jif or 0, field, k))
    con.commit()
    con.close()
    log("已重算 %d 条的期刊等级 / 分区 / IF" % len(rows))


def cmd_stats():
    con = open_db()
    tot = con.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    by = con.execute("SELECT week,COUNT(*) FROM papers GROUP BY week ORDER BY week DESC LIMIT 8").fetchall()
    st = con.execute("SELECT status,COUNT(*) FROM papers GROUP BY status").fetchall()
    con.close()
    log("库内共 %d 篇；最近批次：%s；状态：%s" % (tot, by, st))


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    cmd = sys.argv[1] if len(sys.argv) > 1 else "fetch"
    args = sys.argv[2:]
    fn = {"fetch": cmd_fetch, "apply": cmd_apply, "report": cmd_report, "stats": cmd_stats,
          "retier": cmd_retier}.get(cmd, cmd_fetch)
    if cmd == "fetch":
        fn("--from-cache" in args)
    else:
        fn()
    flush_log()
