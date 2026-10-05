#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
全文下载 + 按周归档（2026-10 改版）

  目录结构（papers_dir 在 config.json 里配）:
  0博士文献/
    2026-W40 (09-27~10-03)/
      0导师组文献/   只有 PDF（导师组：贾伟平 / 蔡淳 / 刘月星 / 鲍萍萍 / 黄珏，无论期刊档次）
      1世界顶刊/     只有 PDF（S+：Nature/Science/Cell/NEJM/Lancet 级）
      2领域顶刊/     只有 PDF（S：领域顶刊）
      3一区top/      只有 PDF（A 且 IF≥阈值，默认 7）
      （下载范围 download_folders 只到 0-3；4一区/5二区/6其他 不自动下载，进待下载清单）
      _一键打开链接.html  全部符合要求文献（含打开本地 PDF 按钮）+ 交互筛选栏
      _题录总表.html      与一键打开同内容同序（浏览器阅读版）
      _题录总表.csv       同内容数据版，可导入 Zotero / Excel
      _待下载清单.html    没下到的，附全部可点链接（谷歌学术 / DOI / 原文）
      本周综述.html        按导师组 + 研究方法 + 研究内容分组的真正综述（APA 参考文献）

用法:
  python download.py            # 归档本周（库里最新一周）
  python download.py --dry-run  # 只试下载不写盘，看能拿到多少
  python download.py --week YYYY-MM-DD   # 指定周

设计原则：PDF 文件夹里【只放 PDF】，所有题录/清单都放在根目录的 _ 开头文件里，
这样你进某个分区文件夹看到的就是干净的 pdf 列表。清单与下载均为【增量】：
  - 数据库按 week 分区，download.run 默认只归档库里最新一周；
  - 本地已有 PDF 会自动跳过，重跑不会重复下载。

关于付费墙（重要）:
  自动下载只能拿开放获取（OA）/ 预印本（arXiv/bioRxiv/medRxiv）全文。出版社
  （Elsevier/Wiley/OUP/Springer）有反爬，直接请求会 403。要拿这些，两个办法：
    1) config.json 里填 download.ezproxy_prefix（交大图书馆的代理前缀），
       生成的链接在校园网/VPN 内点开即可下载
    2) 从登录了交大账号的浏览器导出 cookies.txt，填到 download.cookies_file
    3) 连交大 VPN（<YOUR_VPN_HOST>）后本机即校内 IP，出版社直接放行
  三个都不填也没关系 —— 所有文献的完整题录一定会落盘，你可以自己抽时间下。
"""
from __future__ import annotations

import csv
import http.cookiejar
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jcr            # noqa: E402
import profile as P   # noqa: E402

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
DB = os.path.join(DATA_DIR, "papers.db")

# 下载归档：按 JCR 分区把 PDF 放进不同文件夹；文件夹内【只放 PDF】（纯净原则）
TIER_FOLDER = {
    "S+": "顶刊_S+S", "S": "顶刊_S+S",
    "A": "一区_A",
    "B": "二区_B",
    "W": "其他_CDEWP", "C": "其他_CDEWP", "D": "其他_CDEWP",
    "E": "其他_CDEWP", "P": "其他_CDEWP", "": "其他_CDEWP",
}

BROWSER = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_opener = None


def opener(cookies_file=""):
    """带 cookie 支持的全局 opener（用于机构登录态下载）"""
    global _opener
    if _opener is not None:
        return _opener
    cj = http.cookiejar.MozillaCookieJar()
    if cookies_file and os.path.exists(cookies_file):
        try:
            cj.load(cookies_file, ignore_discard=True, ignore_expires=True)
        except Exception as e:
            print("[warn] cookies 读取失败：%r" % e)
    _opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    return _opener


def fetch(url, timeout=45, cookies_file="", headers=None):
    """返回 (bytes, content_type)。非 PDF 返回 (b"", ct)"""
    import gzip as _gzip
    h = dict(BROWSER)
    h["Accept-Encoding"] = "gzip, deflate"   # 别要 br，标准库解不开
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    op = opener(cookies_file)
    with op.open(req, timeout=timeout) as r:
        data = r.read()
        ct = (r.headers.get("Content-Type") or "").lower()
        ce = (r.headers.get("Content-Encoding") or "").lower()
    if "gzip" in ce:
        try:
            data = _gzip.decompress(data)
        except Exception:
            pass
    return data, ct


def human_bytes(n):
    n = n or 0
    if n < 1024:
        return "%d B" % n
    if n < 1024 * 1024:
        return "%.0f KB" % (n / 1024.0)
    return "%.1f MB" % (n / 1024.0 / 1024.0)


def is_pdf(data, ct):
    if data[:5] == b"%PDF-":
        return True
    return "application/pdf" in ct and len(data) > 3000


def save(url, path, timeout=45, cookies_file="", referer=""):
    try:
        data, ct = fetch(url, timeout, cookies_file,
                         {"Referer": referer} if referer else None)
        if is_pdf(data, ct):
            with open(path, "wb") as f:
                f.write(data)
            return len(data)
    except Exception:
        pass
    return 0


# ------------------------------------------------------------ OA 解析
def unpaywall(doi, email):
    """返回 [(host_type, url)]，PDF 直链优先，落地页兜底"""
    if not doi or not email:
        return []
    try:
        u = "https://api.unpaywall.org/v2/%s?email=%s" % (urllib.parse.quote(doi), urllib.parse.quote(email))
        d = json.loads(fetch(u, 30)[0].decode("utf-8", "replace"))
        out = []
        for loc in (d.get("oa_locations") or []):
            pdf = loc.get("url_for_pdf")
            host = loc.get("host_type") or ""
            if pdf:
                out.append((host + ":pdf", pdf))
            land = loc.get("url")
            if land and land != pdf:
                out.append((host + ":landing", land))
        best = (d.get("best_oa_location") or {})
        if best.get("url_for_pdf"):
            out.insert(0, ("best:pdf", best["url_for_pdf"]))
        if best.get("url") and best["url"] != best.get("url_for_pdf"):
            out.insert(0, ("best:landing", best["url"]))
        seen, res = set(), []
        for h, x in out:
            if x and x not in seen:
                seen.add(x)
                res.append((h, x))
        return res
    except Exception:
        return []


PDF_META = re.compile(
    r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)', re.I)
PDF_META2 = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']citation_pdf_url["\']', re.I)
PDF_HREF = re.compile(r'href=["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']', re.I)


def _is_sjtu_ip(ip):
    """判断 IP 是否属于上海交大网段（含医学院 VPN 的 59.78.x 出口）"""
    if not ip:
        return False
    if ip.startswith("10."):           # 校内/VPN 内网地址
        return True
    for p in ("59.78.", "202.120.", "202.121.", "202.112.", "202.141.", "116.236."):
        if ip.startswith(p):
            return True
    return False


def detect_campus(timeout=10):
    """自动判断本机是否已连上交大 VPN / 在校园网内。

    两种判定都接受（取「或」）：
      1) 主 VPN 横幅：打开 https://net.sjtu.edu.cn/ 底部显示
         「您正在使用交大VPN，IP地址是:10.184.XX.XX」。
      2) 出口 IP 兜底：探测本机出口 IP 是否在交大网段内。
         ——医学院 VPN 走的是 59.78.x 出口，不会触发上面的横幅，
           但出版社在 IP 层面仍会把本机当校内放行，所以也要认。
    返回 True 表示本机已有校内 IP，出版社付费墙会直接放行。
    """
    # 1) 主 VPN 横幅
    try:
        data, _ = fetch("https://net.sjtu.edu.cn/", timeout, "")
        txt = data.decode("utf-8", "replace")
        if "您正在使用交大VPN" in txt and "IP地址是" in txt:
            m = re.search(r"IP地址是\s*[:：]\s*([0-9.]+)", txt)
            print("检测到交大 VPN 已连接（校内 IP %s）→ 开启付费墙下载"
                  % (m.group(1) if m else "10.x"))
            return True
    except Exception:
        pass
    # 2) 出口 IP 兜底（覆盖医学院 VPN 等走交大出口但不显示横幅的情况）
    try:
        ip, _ = fetch("https://ifconfig.me/ip", timeout, "")
        ip = ip.decode("utf-8", "replace").strip()
        if _is_sjtu_ip(ip):
            print("检测到本机出口 IP %s 属于交大网段 → 视为校园网/VPN，开启付费墙下载" % ip)
            return True
        print("本机出口 IP %s 不是交大网段" % ip)
    except Exception as e:
        print("[warn] 出口 IP 探测失败：%r" % e)
    return False


def landing_to_pdf(url, cookies="", timeout=20, referer=""):
    """落地页 -> PDF 直链。学术站点普遍有 citation_pdf_url 这个 Highwire meta 标签。"""
    try:
        data, ct = fetch(url, timeout, cookies, {"Referer": referer} if referer else None)
        if b"<html" not in data[:3000] and b"<HTML" not in data[:3000]:
            return ""
        txt = data.decode("utf-8", "replace")
        for pat in (PDF_META, PDF_META2):
            m = pat.search(txt)
            if m:
                return urllib.parse.urljoin(url, m.group(1).replace("&amp;", "&"))
        for m in PDF_HREF.finditer(txt):
            u = urllib.parse.urljoin(url, m.group(1).replace("&amp;", "&"))
            if u.lower().endswith(".pdf") or "/pdf" in u.lower():
                return u
    except Exception:
        pass
    return ""


# DOI 前缀 -> 需要先用首页暖一下会话 cookie 的出版社根域名
_PUB_WARM = [
    ("10.1016", "https://www.sciencedirect.com/"),       # Elsevier
    ("10.1002", "https://onlinelibrary.wiley.com/"),     # Wiley
    ("10.1007", "https://link.springer.com/"),           # Springer
    ("10.1093", "https://academic.oup.com/"),            # OUP
    ("10.2337", "https://academic.oup.com/"),            # ADA / Diabetes Care (OUP)
    ("10.1056", "https://www.nejm.org/"),                # NEJM
    ("10.1053", "https://www.sciencedirect.com/"),       # Clinics (Elsevier)
    ("10.1017", "https://www.cambridge.org/"),           # CUP
]


def warm_publisher(doi, cookies="", timeout=15):
    """撞付费墙前先访问出版社首页，让对方下发会话 cookie，
    否则 Elsevier/Wiley 等会对‘裸请求’直接 403 反爬。"""
    if not doi:
        return
    for prefix, root in _PUB_WARM:
        if doi.startswith(prefix):
            try:
                fetch(root, timeout, cookies)
            except Exception:
                pass
            return


def pmcid_of(pmid, doi):
    """PubMed -> PMCID"""
    if not (pmid or doi):
        return ""
    try:
        term = ("%s[pmid]" % pmid) if pmid else ("%s[doi]" % doi)
        u = ("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pmc&term=%s"
             "&retmode=json&retmax=1" % urllib.parse.quote(term))
        d = json.loads(fetch(u, 25)[0].decode("utf-8", "replace"))
        ids = d.get("esearchresult", {}).get("idlist") or []
        return ("PMC" + ids[0]) if ids else ""
    except Exception:
        return ""


def epmc_xml(pmcid, doi):
    """Europe PMC：有 OA 全文则返回可下载链接"""
    try:
        q = ('PMCID:%s' % pmcid) if pmcid else ('DOI:"%s"' % doi)
        u = ("https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=%s"
             "&format=json&resultType=core&pageSize=1" % urllib.parse.quote(q))
        d = json.loads(fetch(u, 25)[0].decode("utf-8", "replace"))
        res = (d.get("resultList", {}).get("result") or [])
        if not res:
            return "", ""
        r = res[0]
        pmcid = r.get("pmcid") or pmcid
        if (r.get("isOpenAccess") or "").upper() != "Y" or not pmcid:
            return pmcid, ""
        return pmcid, "https://www.ebi.ac.uk/europepmc/webservices/rest/%s/fullTextXML" % pmcid
    except Exception:
        return "", ""


def candidate_urls(it):
    """按成功率从高到低给出候选下载地址"""
    src = (it.get("source") or "")
    url = it.get("url") or ""
    doi = it.get("doi") or ""
    out = []
    if src == "arxiv" or "arxiv.org" in url:
        m = re.search(r"arxiv\.org/abs/([0-9.]+v?\d*)", url)
        if m:
            out.append(("arXiv", "https://arxiv.org/pdf/%s" % m.group(1)))
    if src in ("medrxiv", "biorxiv") and doi:
        host = "medrxiv" if src == "medrxiv" else "biorxiv"
        out.append((src, "https://www.%s.org/content/%s" % (host, doi)))
        out.append((src + "-pdf", "https://www.%s.org/content/%s.full.pdf" % (host, doi)))
        out.append((src + "-v1", "https://www.%s.org/content/%sv1.full.pdf" % (host, doi)))
    return out


# ------------------------------------------------------------ 文件名
BAD = r'[\\/:*?"<>|\r\n\t]'


def safe(s, n=80):
    s = re.sub(BAD, " ", s or "")
    s = re.sub(r"\s+", " ", s).strip().rstrip(".")
    return s[:n] or "untitled"


def first_author(authors):
    a = (authors or "").split(";")[0].strip()
    a = re.split(r"[,\s]", a)[0]
    return safe(a, 24) or "anon"


def fname(it, idx):
    yr = (it.get("pub_date") or "")[:4] or "n.d."
    return "%02d_%s_%s_%s" % (idx, first_author(it.get("authors")), yr, safe(it.get("title"), 60))


# ------------------------------------------------------------ PDF 内容校验
# 2026-10-04 事故：某条记录 DOI 被参考文献 DOI 覆盖 → 按错 DOI 下载 → 抓回别人的全文，
# 而报告里却显示「已下载」，点开是另一篇文章。以下函数在【落盘后立刻核对内容】，
# 从机制上杜绝"链接与本地 PDF 对不上"。
_SKIP_WORDS = set("""a an the of and or in on for with to by from at as is are was were be been
this that these those their its it we our us study studies using used between among during
through via based than into over under""".split())


def _title_tokens(s):
    s = re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower())
    return {w for w in s.split() if len(w) >= 4 and w not in _SKIP_WORDS}


def _tok_list(s):
    """与 _title_tokens 同一套过滤规则，但保留【顺序】（列表），用于连续短语核对。"""
    s = re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower())
    return [w for w in s.split() if len(w) >= 4 and w not in _SKIP_WORDS]


def _longest_run(a, b, cap=14, limit=900):
    """两个 token 序列的最长【连续】公共片段长度（token 级）。

    为什么要连续片段：2026-10-05 发现一篇 arXiv 综述（Securing Automated Insulin
    Delivery Systems…）被当成 BME 的 Toward Trustworthy Diabetes Technologies…
    挂了进来 —— 两篇都是"糖尿病设备安全"主题，词重合率有 56%（diabetes/safety/
    security/evaluation 这些通用词都撞上了），只按重合率判定就会放行。但"整条标题
    连续出现"是编不出来的：正篇文章首页一定有标题那一串词，冒名顶替的没有。
    """
    if not a or not b:
        return 0
    b = b[:limit]
    prev = [0] * (len(b) + 1)
    best = 0
    for x in a[:limit]:
        cur = [0] * (len(b) + 1)
        for j, y in enumerate(b, 1):
            if x == y:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
                    if best >= cap:
                        return best
        prev = cur
    return best


def pdf_head_text(path, nchars=4000):
    """抽 PDF 前两页文本，用来核对「下到的到底是哪篇」。"""
    try:
        import PyPDF2
        with open(path, "rb") as f:
            rd = PyPDF2.PdfReader(f)
            txt = ""
            for p in rd.pages[:2]:
                txt += p.extract_text() or ""
                if len(txt) >= nchars:
                    break
        return re.sub(r"\s+", " ", txt)[:nchars]
    except Exception:
        return ""


def pdf_matches(path, title, min_ratio=0.5):
    """PDF 首页是否真是这篇文章。返回 (是否相符, 词重合率)。

    判据（2026-10-05 收紧）＝两条都要过：
      ① 词重合率 ≥ min_ratio（0.5）；
      ② 标题实词序列能在首页文本里【连续】命中 ≥ 30% 的标题实词（至少 3 个）。
    单靠①会误放：一篇 arXiv 综述（Securing Automated Insulin Delivery Systems…）
    被当成 BME 的 Toward Trustworthy Diabetes Technologies… 挂在同一条记录上 ——
    两篇都是"糖尿病设备安全"主题，diabetes/safety/security/evaluation 这些通用词
    全撞上，重合率 56% 就放行了，报告里显示"已下载"却点开别人的全文。②专治这种：
    实测 68 个真 PDF 的最长连续片段都 ≥5，冒名那篇只有 2，分离得很干净。
    抽不出文本（扫描版/加密/图片型 PDF）时返回 (True, -1) 放行，
    宁可保留也不要误删正确文献。
    """
    tt = _title_tokens(title)
    if not tt:
        return True, -1.0
    head = pdf_head_text(path)
    if not head:
        return True, -1.0
    ratio = len(tt & _title_tokens(head)) / float(len(tt))
    tl = _tok_list(title)
    if len(tl) > 3:
        need = max(3, int(len(tl) * 0.3 + 0.999))
        ok = (ratio >= min_ratio) and (_longest_run(tl, _tok_list(head)) >= need)
    else:
        ok = ratio >= 0.99      # 短标题没有连续片段可依，退回词重合率
    return ok, ratio


def save_checked(url, path, it, timeout=45, cookies_file="", referer=""):
    """下载 → 立刻校验内容是否真是这篇文章；不符即删掉，当作没下到。
    返回 (字节数, 重合率)；字节数=0 表示没拿到或内容不符。"""
    n = save(url, path, timeout, cookies_file, referer=referer)
    if not n:
        return 0, 0.0
    ok, ratio = pdf_matches(path, it.get("title") or "")
    if not ok:
        try:
            os.remove(path)
        except Exception:
            pass
        print("[warn] 下到的 PDF 与题录不符（标题词重合仅 %.0f%%），已丢弃：%s"
              % (max(ratio, 0) * 100, (it.get("title") or "")[:52]))
        return 0, ratio
    return n, ratio


# ------------------------------------------------------------ 题录
def jline(it):
    """期刊等级 + JCR 分区 + IF，避免「JCR Q2 Q1」这种自相矛盾的显示"""
    tier = (it.get("tier") or "")
    label = jcr.TIER_LABEL.get(tier, "未分级")
    q = it.get("quartile") or ""
    jif = it.get("jif") or 0
    extra = ""
    if q and tier not in ("A", "B", "C", "D"):   # A/B/C/D 本身就是分区，别重复
        extra += " / " + q
    if jif:
        extra += " / IF %.1f" % jif
    return label + extra


def citation_md(it, ez=""):
    doi = it.get("doi") or ""
    pmid = it.get("pmid") or ""
    links = []
    if doi:
        links.append("- DOI：https://doi.org/%s" % doi)
    if pmid:
        links.append("- PubMed：https://pubmed.ncbi.nlm.nih.gov/%s/" % pmid)
    links.append("- 原文：%s" % (it.get("url") or ""))
    if ez and doi:
        links.append("- 交大图书馆（需校园网/VPN）：%shttps://doi.org/%s" % (ez, doi))
    if doi:
        links.append("- 谷歌学术：https://scholar.google.com/scholar?q=%s" % urllib.parse.quote(it.get("title") or ""))
    jinfo = jline(it)
    abs_ = re.sub(r"\s+", " ", (it.get("abstract") or ""))[:1200]
    return "\n".join([
        "### %s" % (it.get("title") or ""),
        "",
        "- **期刊**：%s（%s）" % (it.get("journal") or "-", jinfo),
        "- **作者**：%s" % (it.get("authors") or "-"),
        "- **发表**：%s ｜ 来源：%s ｜ 相关度：%s" % (it.get("pub_date") or "-", it.get("source") or "-",
                                                    {"core": "核心", "proxy": "方法/相邻", "eco": "背景"}.get(it.get("layer"), "-")),
        "",
        "**摘要**：%s" % (abs_ or "（无摘要）"),
        "",
        "**链接**",
    ] + links + [""])


# ------------------------------------------------------------ 主流程
def week_label(week_iso, days=None):
    """稳定的周目录名 = 该日期所属 ISO 周的【周一~周日】。

    旧实现是「该日往前推 days-1 天」，同一周内换一天跑（跨零点 / 隔天补跑）就会算出
    两个不同目录（09-27~10-03 与 09-28~10-04），已下载 PDF 的路径随之全部失效，
    报告里的「打开本地PDF」就指向不存在的文件。改为按 ISO 周取整，同周永远同名。
    days 参数仅为兼容旧调用签名保留。
    """
    d = datetime.strptime(week_iso, "%Y-%m-%d").date()
    monday = d - timedelta(days=d.isoweekday() - 1)
    sunday = monday + timedelta(days=6)
    iso = monday.isocalendar()
    return "%04d-W%02d (%s~%s)" % (iso[0], iso[1], monday.strftime("%m-%d"), sunday.strftime("%m-%d"))


def _ensure_db():
    """打开库并增量补齐老库缺的列（affiliations / llm_tag / llm_reason 等），避免 SELECT 时报错。"""
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    need = {"affiliations", "llm_tag", "llm_reason",
            "cn_abstract", "cn_authors", "cn_aff", "cn_keywords"}
    try:
        have = {r[1] for r in con.execute("PRAGMA table_info(papers)")}
        for col in need - have:
            con.execute("ALTER TABLE papers ADD COLUMN %s TEXT DEFAULT ''" % col)
    except Exception as e:
        print("[warn] 数据库列补齐失败：%r" % e)
    return con


def _relevant(r):
    """是否纳入文献库 / 下载：核心层、导师组、或大模型判定为延伸/方法/前沿。"""
    if r.get("author_hit"):
        return True
    if (r.get("layer") or "") == "core":
        return True
    tag = (r.get("llm_tag") or "").lower()
    return tag in ("extension", "method", "frontier")


def _save_cn(items):
    """把大模型翻译结果（中标题/中文摘要/作者/单位/关键词/评语）回写数据库。

    不回写的话，这些结果只存在于当次生成的 HTML 里：下周重跑会全部重新翻译一遍
    （白白再打上百次接口、再撞一次限流），而且中途被打断就等于白翻。写回库里之后，
    enrich() 只处理"还缺翻译"的条目，越跑越省。
    """
    if not items:
        return 0
    con = _ensure_db()
    cur = con.cursor()
    n = 0
    for it in items:
        key = it.get("key")
        if not key:
            continue
        vals = [(f, (it.get(f) or "").strip()) for f in
                ("cn_title", "cn_abstract", "cn_authors", "cn_aff", "cn_keywords", "note")]
        vals = [(f, v) for f, v in vals if v]
        if not vals:
            continue
        cur.execute("UPDATE papers SET %s WHERE key=?"
                    % ",".join("%s=?" % f for f, _ in vals),
                    [v for _, v in vals] + [key])
        n += 1
    con.commit()
    con.close()
    if n:
        print("中文翻译已回写数据库 %d 条（下次只翻新条目）" % n)
    return n


def _week_dir(root, week, days):
    """周目录路径。

    你已经把文献挪进 0博士文献/糖尿病文献 了，config 里 papers_dir 也跟着改了；
    但在你真正挪完之前，新 papers_dir 还不存在。这时若照新路径写报告，报告会和
    PDF 分家（PDF 还在旧位置）→ 满屏死链。所以：新根目录不存在、而旧位置（上一级
    目录）里能找到同名周文件夹时，先沿用旧位置，等你挪完了自然切过去。
    """
    folder = os.path.join(root, week_label(week, days))
    if not os.path.isdir(folder) and root:
        alt = os.path.join(os.path.dirname(root.rstrip("/\\")), week_label(week, days))
        if os.path.isdir(alt):
            print("[提示] 新 papers_dir 里还没有本周文件夹，本次沿用在旧位置：%s" % alt)
            return alt
    return folder


def _report_counts(n, n_carry, n_keep):
    """报告条目构成一行说明，方便一眼看出有没有『越跑越少』。"""
    msg = "报告条目 %d 条" % n
    extra = []
    if n_carry:
        extra.append("跨周延续 %d" % n_carry)
    if n_keep:
        extra.append("上一版沿用 %d" % n_keep)
    if extra:
        msg += "（其中 " + "、".join(extra) + "，其余为本周）"
    print(msg)


def _snapshot_init(con):
    """报告快照表：每出一版报告，就把这一版【进入报告的条目集合】记下来。

    下一版报告出的时候，把这个集合并回去 —— 于是「上一版里出现过的条目，下一版一定还在」，
    除非它被人工删库/判为不相关。这是给用户兜底用的：测试阶段你会反复让我重跑，
    不能出现「跑一次换一批，上一批凭空消失」的情况。
    """
    con.execute("""CREATE TABLE IF NOT EXISTS report_snapshot(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        label TEXT, week TEXT, ts TEXT, n INTEGER, keys TEXT)""")
    con.commit()


def _snapshot_load(con, label, within_days=0):
    """取上一版快照：(label, ts, key集合)。

    within_days：只认这么多天以内写的快照。目的 —— 把「同一周里反复重跑」和
    「正常进入下一周」区分开：前者必须把上一版全部并回来（用户要求：上次跑出来的
    文献要保留到这一版里），后者应该是一份新周报，不该把上周几千条全拖过来。
    同一周 label 的一律沿用（不看天数）；跨周才看 within_days；传 0 则无条件认上一版。
    """
    try:
        row = con.execute("SELECT label,ts,keys FROM report_snapshot "
                          "ORDER BY id DESC LIMIT 1").fetchone()
    except Exception:
        return ("", "", set())
    if not row:
        return ("", "", set())
    lb, ts = row[0] or "", row[1] or ""
    if lb != label:                     # 跨周：看时间间隔
        if within_days <= 0:
            return ("", "", set())
        try:
            d = datetime.fromisoformat(ts).date()
        except Exception:
            return ("", "", set())
        if (date.today() - d).days > within_days:
            return ("", "", set())      # 距上一版太久 → 已是新的一周，不沿用
    try:
        return (lb, ts, set(json.loads(row[2] or "[]")))
    except Exception:
        return (lb, ts, set())


def _snapshot_save(con, label, week, rows):
    """存当前这一版的报告条目集合；同 label 只留最近 8 版，避免表无限膨胀。"""
    try:
        _snapshot_init(con)
        keys = [r["key"] for r in rows if r.get("key")]
        con.execute("INSERT INTO report_snapshot(label,week,ts,n,keys) VALUES(?,?,?,?,?)",
                    (label, week, datetime.now().isoformat(timespec="seconds"),
                     len(keys), json.dumps(keys, ensure_ascii=False)))
        con.execute("""DELETE FROM report_snapshot WHERE id NOT IN
                       (SELECT id FROM report_snapshot ORDER BY id DESC LIMIT 40)""")
        con.commit()
    except Exception as e:
        print("[warn] 报告快照写入失败（不影响本次出报告）：%r" % e)


def _main_view(r):
    """条目是否应出现在「一键打开/题录总表」清单里。

    判据原本与「下载目标 targets」一致；多加一条：
        **本地已有 PDF 的一律进清单。**
    否则会出现「磁盘上有全文、页面里却找不到入口」的孤儿 PDF ——
    条目因为层标签变化（core→proxy）被相关性过滤掉，但它的全文早已经下载好了，
    对用户来说就是一份下过却找不到的文献（2026-10-05 核出 1 例：
    69_Nasrin_2026_Maternal and Fetal Outcomes of Pregnancy with Controlled...）。
    """
    if r.get("pdf_path") and os.path.exists(r["pdf_path"]):
        return True
    if r.get("_cat") in ("0", "1", "2"):
        return _relevant(r) or (r.get("layer") in ("core", "proxy", "eco"))
    return _relevant(r)


def _week_rows(con, week, cfg, cols, q1):
    """取本周报告的条目 =【本周全量】+【顶刊延续】+【上一版沿用】。

    三层保障，缺一不可（2026-10-05 重构）：

    1) 本周全量 week BETWEEN 周一 AND 周日
       库里 week 记的是「哪一天抓到」，不是「属于哪一周」。同一周内换个日子重跑就会得到
       新的 week，报告只取 week=最新 → 上一次抓到的整批被判为"旧批次"而消失。
       实例：10-03 抓到 3244 条，10-04 的报告里只剩它们中的 227 条（还仅限档位 0/1/2），
       整整 3017 条一夜蒸发，其中一区及以上 1046 条。用户肉眼看到的只是"少了 2 篇导师组"。
       现在按所属 ISO 周取区间，同周任何一次重跑都属于同一个周 —— 只做加法，不替换。

    2) 顶刊延续 carryover
       跨周用：上周的世界顶刊/领域顶刊，这周的检索窗口未必再撞见，丢掉太可惜。
       按 config.report.carryover_days / carryover_folders 控制，默认 14 天 + 档位 0/1/2。

    3) 上一版沿用 snapshot（同周）
       硬兜底：上一版报告里出现过的条目，这版一定还在（除非已不在库里）。
       哪怕将来筛选规则改动、评分重算、数据库清理，也不会把你看过的条目凭空抹掉。
       标记 _carry=2 → 报告里显示「沿用」徽章，可用筛选栏剔掉。

    _carry: 0/None=本周新增；1=顶刊延续；2=上一版沿用。
    """
    sel = "SELECT %s FROM papers " % ",".join(cols)
    lo, hi = jcr.week_range(week)
    base = [dict(r) for r in con.execute(
        sel + "WHERE week BETWEEN ? AND ? ORDER BY score DESC, cites DESC", (lo, hi))]
    for r in base:
        r["_cat"] = jcr.folder_code(r.get("tier") or "", r.get("jif"), bool(r.get("author_hit")), q1)
    rc = cfg.get("report") or {}

    have = {r["key"] for r in base}

    # ---- 2) 跨周顶刊延续 ----
    n_carry = 0
    if rc.get("carryover", True):
        days = int(rc.get("carryover_days", 14))
        keep = set(rc.get("carryover_folders") or ["0", "1", "2"])
        try:
            cut = (date.fromisoformat(lo) - timedelta(days=days)).isoformat()
        except Exception:
            cut = (date.today() - timedelta(days=days)).isoformat()
        prev = [dict(r) for r in con.execute(
            sel + "WHERE (week<? OR week>?) AND pub_date>=? ORDER BY score DESC, cites DESC",
            (lo, hi, cut))]
        for r in prev:
            if r["key"] in have:
                continue
            r["_cat"] = jcr.folder_code(r.get("tier") or "", r.get("jif"),
                                        bool(r.get("author_hit")), q1)
            if r["_cat"] not in keep or not _main_view(r):
                continue
            r["_carry"] = 1
            base.append(r)
            have.add(r["key"])
            n_carry += 1

    # ---- 3) 上一版沿用（快照）----
    n_keep = 0
    if rc.get("snapshot", True):
        label = week_label(week, None)
        window = int(rc.get("rerun_window_days", 4))
        _lb, _ts, old = _snapshot_load(con, label, window)
        missing = [k for k in old if k not in have]
        if missing:
            ph = ",".join("?" * len(missing))
            for r in con.execute(sel + "WHERE key IN (%s)" % ph, missing):
                r = dict(r)
                if r["key"] in have:
                    continue
                r["_cat"] = jcr.folder_code(r.get("tier") or "", r.get("jif"),
                                            bool(r.get("author_hit")), q1)
                r["_carry"] = 2
                base.append(r)
                have.add(r["key"])
                n_keep += 1
    return base


def fix_paths(cfg=None, verbose=True):
    """自愈 pdf_path：你手动挪动过文献文件夹后，把失效的链接重新对上。

    场景：把「0博士文献\\2026-W40 (09-28~10-04)」整个拖进「0博士文献\\糖尿病文献\\」、
    或者改过 papers_dir 之后，库里存的还是旧绝对路径 → 报告里「打开本地PDF」全线失效。

    做法：对所有指向不存在文件的 pdf_path，按【文件名】在 papers_dir 树（以及它的
    上一级目录，兼容"平移到子目录"）里搜同名文件，找到了就改写为新路径；找不到就
    清空（宁可显示"未下载"，也不要给一个打不开的死链）。

    返回 (修好条数, 清空条数)。
    """
    cfg = cfg or P.cfg()
    root = ((cfg.get("paths") or {}).get("papers_dir") or "").strip()
    if not root:
        return (0, 0)
    con = _ensure_db()
    cur = con.cursor()
    bad = [(r["key"], r["pdf_path"], r["title"] or "") for r in
           cur.execute("SELECT key, pdf_path, title FROM papers WHERE pdf_path<>''")]
    bad = [(k, p, t) for k, p, t in bad if p and not os.path.exists(p)]
    if not bad:
        con.close()
        if verbose:
            print("pdf_path 全部有效，无需修复")
        return (0, 0)
    # 建一次文件名索引，避免每篇都全盘遍历
    index = {}
    roots = [root, os.path.dirname(root.rstrip("/\\"))]
    seen = set()
    for base in roots:
        if not base or base in seen or not os.path.isdir(base):
            continue
        seen.add(base)
        for dirpath, _dn, files in os.walk(base):
            if os.path.basename(dirpath).startswith("."):
                continue
            for f in files:
                if f.lower().endswith(".pdf"):
                    index.setdefault(f, []).append(os.path.join(dirpath, f))
    fixed = cleared = 0
    for key, old, title in bad:
        cands = index.get(os.path.basename(old)) or []
        # 同名多个时，优先选目录名里含原周标签的那个
        pick = None
        if len(cands) == 1:
            pick = cands[0]
        elif cands:
            parent_old = os.path.basename(os.path.dirname(old))
            for c in cands:
                if parent_old and parent_old in c:
                    pick = c
                    break
            pick = pick or cands[0]
        # 认链之前先验内容：只按文件名重链，万一那个文件其实是别人的（同名/曾下错），
        # 就会把"打开本地PDF"指到别的文章上——比不显示按钮更糟。不符就不认。
        if pick:
            ok, ratio = pdf_matches(pick, title)
            if not ok:
                if verbose:
                    print("[warn] 同名文件内容与题录不符（重合 %.0f%%），不重链：%s"
                          % (max(ratio, 0) * 100, os.path.basename(pick)))
                pick = None
        if pick:
            cur.execute("UPDATE papers SET pdf_path=? WHERE key=?", (pick, key))
            fixed += 1
        else:
            cur.execute("UPDATE papers SET pdf_path='' WHERE key=?", (key,))
            cleared += 1
    con.commit()
    con.close()
    if verbose:
        print("pdf_path 自愈：重链 %d 条，清空 %d 条（文件确实不在新目录里）" % (fixed, cleared))
    return (fixed, cleared)


def run(dry_run=False, week=None, no_paywall=False):
    cfg = P.cfg()
    paths = cfg.get("paths") or {}
    root = (paths.get("papers_dir") or "").strip()
    dl = cfg.get("download") or {}
    if not root:
        print("config.json 的 paths.papers_dir 为空，跳过归档")
        return
    con = _ensure_db()
    if not week:
        # 用库里最新的一周，而不是「今天」—— 抓取日和归档日跨零点时会错位
        r = con.execute("SELECT max(week) FROM papers").fetchone()
        week = (r[0] if r and r[0] else date.today().isoformat())
    days = int(cfg.get("lookback_days", 7))
    fix_paths(cfg)          # 你挪过文件夹的话，先把旧 pdf_path 重新对上，别让报告出死链
    folder = _week_dir(root, week, days)
    print("归档目录：%s" % folder)
    if dry_run:
        os.makedirs(folder, exist_ok=True)

    cols = ["key", "doi", "title", "journal", "pub_date", "url", "abstract", "authors",
            "source", "tier", "quartile", "jif", "pmid", "layer", "score", "cites", "topics",
            "field", "pdf_path", "cn_title", "note", "affiliations", "author_hit", "author_name",
            "llm_tag", "cn_abstract", "cn_authors", "cn_aff", "cn_keywords"]
    only = set(dl.get("only_tiers") or [])
    min_score = int(dl.get("min_score", 0))
    cap = int(dl.get("max_per_week", 0))
    q1 = float(dl.get("q1_top_if", 7))
    rows = _week_rows(con, week, cfg, cols, q1)   # 本周全量 + 延续 + 沿用
    if rows:
        _snapshot_save(con, week_label(week, days), week, rows)   # 记住这一版，给下一版兜底
    con.close()
    n_carry = sum(1 for r in rows if r.get("_carry") == 1)
    n_keep = sum(1 for r in rows if r.get("_carry") == 2)
    _report_counts(len(rows), n_carry, n_keep)
    if not rows:
        print("本周库里没有条目")
        return

    # 2026-10 调整：下载范围扩到 0-4（新增一区），0/1/2（导师组/世界顶刊/领域顶刊）不受 tier 限制
    download_folders = set(dl.get("download_folders") or ["0", "1", "2", "3", "4"])
    library_folders = set(dl.get("library_folders") or ["0", "1", "2", "3", "4", "5", "6"])
    # 下载目标：
    #  - 分类必须在 download_folders(0-4)
    #  - cat 0/1/2（导师组 / 世界顶刊 / 领域顶刊）：不限 tier（二区甚至三区的领域顶刊、三区的导师组文献也下），
    #    只要不是纯噪音（core/proxy/eco 或导师组或 LLM 判定相关）即下载
    #  - cat 3/4（一区top / 一区）：由 folder_code 保证是 Q1，需满足相关性（core/导师组/LLM 延伸·方法·前沿）
    targets = []
    for r in rows:
        c = r["_cat"]
        if c not in download_folders:
            continue
        if c in ("0", "1", "2"):
            if (_relevant(r) or (r.get("layer") in ("core", "proxy", "eco"))):
                targets.append(r)
        else:
            if _relevant(r) and (r["score"] or 0) >= min_score:
                targets.append(r)
    if cap > 0:                       # 0 或负数 = 不限量
        targets = targets[:cap]
    print("本周 %d 篇；下载目标(分类 0-4)命中 %d 篇（其中 0/1/2 不限 tier）；清单范围(0-6)共 %d 篇"
          % (len(rows), len(targets),
             sum(1 for r in rows if r["_cat"] in library_folders and _relevant(r))))

    email = (dl.get("unpaywall_email") or "").strip()
    cookies = (dl.get("cookies_file") or "").strip()
    ez = (dl.get("ezproxy_prefix") or "").strip()
    timeout = int(dl.get("timeout", 45))
    # 交大校外访问走的是 VPN（<YOUR_VPN_HOST>），不是 EZproxy。
    # 连上 VPN 后本机就是校内 IP，出版社直接放行 —— 这时要把 on_campus 打开才会去撞付费墙。
    on_campus = bool(dl.get("on_campus", False))
    # 没手动指定时自动探测一次：连了交大 VPN 就自动开启（VPN 是交大校外访问的正道，非 EZproxy）
    if not on_campus and dl.get("auto_detect_vpn", True):
        on_campus = detect_campus()
    # 校正模式：跳过付费墙尝试（仅做 OA / 预印本 / EPMC XML），用于快速重整归档而不重复撞出版社
    if no_paywall:
        on_campus = False
        ez = ""
    # 撞出版社的间隔（秒）。图书馆/出版社都明令禁止批量连续下载，必须限速。
    paywall_delay = float(dl.get("paywall_delay", 1.5))
    if on_campus or ez:
        print("付费墙尝试：已开启（%s），请求间隔 %.1fs"
              % ("EZproxy 前缀" if ez else ("EZproxy+VPN" if on_campus and ez else "VPN/校园网 IP"), paywall_delay))
    else:
        print("付费墙尝试：关闭（未配 ezproxy_prefix 且 on_campus=false）→ 付费文献全部进待下载清单")

    got, fail = [], []
    if not dry_run:
        os.makedirs(folder, exist_ok=True)
        # 只建下载范围内的分类文件夹（0-3），避免空目录；清单/待下载走根目录 _ 文件
        for code in download_folders:
            name = jcr.CAT_NAME.get(code, "其他")
            os.makedirs(os.path.join(folder, "%s%s" % (code, name)), exist_ok=True)

    from concurrent.futures import ThreadPoolExecutor
    opener(cookies)                      # 先建好 opener，避免多线程里重复建

    def try_one(arg):
        idx, it = arg
        base = fname(it, idx)
        dest_dir = os.path.join(folder, "%s%s" % (it["_cat"], jcr.CAT_NAME.get(it["_cat"], "其他")))
        ok, how, nbytes, saved_path = False, "", 0, ""
        # 增量：本地已经有这份 PDF 就跳过，不重复拉（重新跑也不会重复下载）
        # 但必须先核对内容——历史 DOI 串号可能留下「名不对文」的旧文件，不能原样放行
        if it.get("pdf_path") and os.path.exists(it["pdf_path"]):
            okx, _rx = pdf_matches(it["pdf_path"], it.get("title") or "")
            if okx:
                return (it, "已存在(跳过)", 0, it["pdf_path"])
            try:
                os.remove(it["pdf_path"])
            except Exception:
                pass
            print("[warn] 已存在但与题录不符，删除重下（重合 %.0f%%）：%s"
                  % (max(_rx, 0) * 100, (it.get("title") or "")[:52]))
            it["_stale_pdf"] = it["pdf_path"]
            it["pdf_path"] = ""

        # 1) 预印本直链（100% 开放，先试）
        for name, u in candidate_urls(it):
            if dry_run:
                try:
                    data, ct = fetch(u, timeout, cookies)
                    if is_pdf(data, ct):
                        ok, how, nbytes = True, name, len(data)
                        break
                except Exception:
                    pass
            else:
                n, _r = save_checked(u, os.path.join(dest_dir, base + ".pdf"), it, timeout, cookies)
                if n:
                    ok, how, nbytes, saved_path = True, name, n, os.path.join(dest_dir, base + ".pdf")
                    break

        # 2) 先问 Unpaywall 是不是 OA。不是就一个字节都不去试 —— 出版社反爬会拖满超时。
        oa = []
        if not ok and it.get("doi"):
            oa = unpaywall(it["doi"], email)

        if not ok and oa:
            for host, u in oa[:6]:
                target = u
                n = 0
                if ":landing" in host or not (u.lower().endswith(".pdf") or "/pdf" in u.lower()):
                    # 落地页：先挖出真正的 PDF 地址，别拿 HTML 当 PDF 存下来
                    p = landing_to_pdf(u, cookies, min(timeout, 20))
                    if not p:
                        continue
                    target = p
                if dry_run:
                    try:
                        data, ct = fetch(target, timeout, cookies)
                        if is_pdf(data, ct):
                            ok, how, nbytes = True, "OA:" + host, len(data)
                            break
                    except Exception:
                        pass
                    continue
                n, _r = save_checked(target, os.path.join(dest_dir, base + ".pdf"), it, timeout, cookies,
                                     referer="https://doi.org/%s" % it["doi"])
                if n:
                    ok, how, nbytes, saved_path = True, "OA:" + host, n, os.path.join(dest_dir, base + ".pdf")
                    break

        # 2.5) 付费墙：需要「机构通道」才去撞出版社。两种机构通道：
        #        (a) EZproxy 前缀（ez）—— 链接在校园网/VPN 内点开即下
        #        (b) 交大 VPN 模式（on_campus）—— 连了 <YOUR_VPN_HOST> 后本机就是校内 IP，
        #            出版社直接认，不需要任何前缀；cookies 里的 jaccount 登录态此时才真正生效
        #      两者都没有时跳过：校外 IP + 只有 jaccount cookies 去撞 Elsevier/Wiley 必 403，
        #      而且对上千篇逐个试会触发出版社反爬 / 图书馆风控。
        if not ok and (ez or on_campus) and it.get("doi"):
            # 限速：图书馆与出版社均禁止批量连续抓取，每次撞出版社前先等一下
            if paywall_delay > 0:
                time.sleep(paywall_delay)
            # 先暖出版社会话 cookie，避开 403 反爬
            warm_publisher(it.get("doi"), cookies)
            tries = ([ez + "https://doi.org/%s" % it["doi"]] if ez else []) + \
                    ["https://doi.org/%s" % it["doi"]]
            for tu in tries:
                ref = "https://doi.org/" + it["doi"]
                p = landing_to_pdf(tu, cookies, min(timeout, 20), referer=ref)
                if not p:
                    continue
                if dry_run:
                    try:
                        data, ct = fetch(p, timeout, cookies, {"Referer": ref})
                        if is_pdf(data, ct):
                            ok, how, nbytes = True, "机构登录/EZproxy", len(data)
                            break
                    except Exception:
                        pass
                    continue
                n, _r = save_checked(p, os.path.join(dest_dir, base + ".pdf"), it, timeout, cookies, referer=ref)
                if n:
                    ok, how, nbytes, saved_path = True, "机构登录/EZproxy", n, os.path.join(dest_dir, base + ".pdf")
                    break

        # PDF 下不下来的文献直接进待下载清单（不再存 XML 兜底）。
        return (it, how, nbytes, saved_path) if ok else None

    workers = min(8, max(3, len(targets) // 5 or 3))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(try_one, enumerate(targets, 1)))
    got = [r for r in results if r]
    got_keys = {r[0]["key"] for r in got}
    fail = [r for r in targets if r["key"] not in got_keys]
    # 全部为真实 PDF（已移除 XML 兜底）
    pdf_got = got
    xml_got = []

    print("自动下载成功 PDF %d 篇；其余 %d 篇已生成完整题录（待下载清单）"
          % (len(pdf_got), len(fail)))
    if dry_run:
        for it, how, n, _p in got[:10]:
            print("  ✓ %-16s %s" % (how, (it["title"] or "")[:56]))
        return

    # ---- 统一富文本报告（reportlib）----
    import reportlib as RL
    pdf_map = {}
    for it, how, _n, sp in pdf_got:
        if sp:
            try:
                pdf_map[it["key"]] = os.path.relpath(sp, folder)
            except Exception:
                pdf_map[it["key"]] = sp
    library_folders = set(dl.get("library_folders") or ["0", "1", "2", "3", "4", "5", "6"])
    # 清单纳入见 _main_view：
    #  - cat 0/1/2（导师组/世界顶刊/领域顶刊）：相关(导师组/LLM 判定/layer∈{core,proxy,eco}) 进
    #  - cat 3-6：仅相关(core/导师组/LLM) 进
    #  - 本地已有 PDF 的一律进（下了的必须能在页面里找到入口）
    # 本轮刚下到的先把路径挂回 row，好让上面那条判据生效
    for r in rows:
        sp = pdf_map.get(r["key"])
        if sp and not (r.get("pdf_path") and os.path.exists(r["pdf_path"])):
            r["pdf_path"] = sp if os.path.isabs(sp) else os.path.join(folder, sp)
    library_items = [r for r in rows if r.get("_cat") in library_folders and _main_view(r)]
    # 顺手把库里已存在的 pdf_path 也并入 pdf_map（增量重跑时本地已有 PDF 但本轮没重下，仍能点开）
    for r in rows:
        fp = r.get("pdf_path") or ""
        if fp and os.path.exists(fp) and r["key"] not in pdf_map:
            try:
                pdf_map[r["key"]] = os.path.relpath(fp, folder)
            except Exception:
                pdf_map[r["key"]] = fp
    RL.write_library(folder, week_label(week, days), library_items, pdf_map, cfg,
                     save_cb=_save_cn)   # 边翻边存，中途断了也不白翻
    _save_cn(library_items)
    RL.write_pending(folder, week_label(week, days), fail, cfg, ez or "")
    RL.write_review(folder, week_label(week, days), library_items, cfg, week=week)

    # 回写 pdf_path（只写真正的 PDF）
    con = _ensure_db()
    for it, how, _n, sp in pdf_got:
        con.execute("UPDATE papers SET pdf_path=?, oa_url=? WHERE key=?",
                    (sp, how, it["key"]))
    # 清掉指向已不存在文件的 pdf_path（旧周目录改名 / 内容不符被删 / 手工挪走），
    # 否则报告里的「打开本地PDF」会指向死链——这正是 2026-10-04 事故的另一半成因。
    stale = [r["key"] for r in rows
             if (r.get("pdf_path") or "") and not os.path.exists(r["pdf_path"])
             and r["key"] not in pdf_map]
    for k in stale:
        con.execute("UPDATE papers SET pdf_path='' WHERE key=?", (k,))
    con.commit()
    con.close()
    if stale:
        print("[clean] 清理失效本地链接 %d 条（文件已不存在）" % len(stale))
    print("完成：%s" % folder)


def report_only(week=None, no_llm=False):
    """只按当前数据库重新生成报告/清单（不联网、不下载）。

    用途：清理过 pdf_path（删错下 PDF、清失效链接）之后，需要让报告里的
    「打开本地PDF」与磁盘真实文件重新对齐时，跑这个即可。
    加 --no-llm 可跳过中文翻译/综述（省时间、避开限流），纯报告对齐用它最快。
    """
    cfg = P.cfg()
    if no_llm:
        cfg.setdefault("llm", {})["enabled"] = False
    root = (cfg.get("paths") or {}).get("papers_dir", "").strip()
    dl = cfg.get("download") or {}
    if not root:
        print("config.json 的 paths.papers_dir 为空")
        return
    con = _ensure_db()
    if not week:
        r = con.execute("SELECT max(week) FROM papers").fetchone()
        week = (r[0] if r and r[0] else date.today().isoformat())
    days = int(cfg.get("lookback_days", 7))
    fix_paths(cfg)          # 挪过文件夹时先把 pdf_path 重新对上
    folder = _week_dir(root, week, days)
    os.makedirs(folder, exist_ok=True)
    cols = ["key", "doi", "title", "journal", "pub_date", "url", "abstract", "authors",
            "source", "tier", "quartile", "jif", "pmid", "layer", "score", "cites", "topics",
            "field", "pdf_path", "cn_title", "note", "affiliations", "author_hit", "author_name",
            "llm_tag", "cn_abstract", "cn_authors", "cn_aff", "cn_keywords"]
    q1 = float(dl.get("q1_top_if", 7))
    rows = _week_rows(con, week, cfg, cols, q1)   # 本周全量 + 延续 + 沿用
    if rows:
        _snapshot_save(con, week_label(week, days), week, rows)   # 记住这一版，给下一版兜底
    con.close()
    if not rows:
        print("本周库里没有条目")
        return
    n_carry = sum(1 for r in rows if r.get("_carry") == 1)
    n_keep = sum(1 for r in rows if r.get("_carry") == 2)
    _report_counts(len(rows), n_carry, n_keep)
    download_folders = set(dl.get("download_folders") or ["0", "1", "2", "3", "4"])
    library_folders = set(dl.get("library_folders") or ["0", "1", "2", "3", "4", "5", "6"])
    for r in rows:
        r["_cat"] = jcr.folder_code(r["tier"] or "", r.get("jif"), bool(r.get("author_hit")), q1)

    library_items = [r for r in rows if r.get("_cat") in library_folders and _main_view(r)]
    # 只认磁盘上真实存在、且内容与题录相符的 PDF
    import reportlib as RL
    pdf_map, dropped = {}, 0
    for r in rows:
        fp = r.get("pdf_path") or ""
        if not fp or not os.path.exists(fp):
            continue
        ok, _ = pdf_matches(fp, r.get("title") or "")
        if not ok:
            dropped += 1
            continue
        try:
            pdf_map[r["key"]] = os.path.relpath(fp, folder)
        except Exception:
            pdf_map[r["key"]] = fp
    fail = [r for r in rows if r.get("_cat") in download_folders and _main_view(r)
            and r["key"] not in pdf_map]
    ez = (dl.get("ezproxy_prefix") or "").strip()
    RL.write_library(folder, week_label(week, days), library_items, pdf_map, cfg,
                     save_cb=_save_cn)   # 边翻边存，中途断了也不白翻
    _save_cn(library_items)
    RL.write_pending(folder, week_label(week, days), fail, cfg, ez)
    RL.write_review(folder, week_label(week, days), library_items, cfg, week=week)
    print("报告已重建：%s" % folder)
    print("  清单 %d 篇 ｜ 本地PDF按钮 %d 个 ｜ 待下载 %d 篇 ｜ 内容不符已剔除 %d 个"
          % (len(library_items), len(pdf_map), len(fail), dropped))


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    a = sys.argv[1:]
    if "report" in a:
        report_only(no_llm=("--no-llm" in a))
    else:
        run(dry_run=("--dry-run" in a), no_paywall=("--no-paywall" in a))
